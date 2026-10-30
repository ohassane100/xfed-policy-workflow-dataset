"""Guided document/reference integrity and evaluator regressions, not model accuracy."""

from copy import deepcopy
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.request import Request, urlopen

from xfed import authoring
from xfed.policy import readable_rules, to_odrl, to_compact
from xfed.common import write_json
from xfed.extraction import extract_document
from xfed.segmentation import segment
from xfed.enrichment import ContextIndex
from xfed.ui import Pipeline, STAGES


ROOT = Path(__file__).resolve().parents[1]


class GuidedTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(ROOT / 'schemas', self.root / 'schemas')
        self.created = authoring.create(self.root, authoring.DEFAULTS)
        self.base = self.root / 'contracts' / self.created['contract']
        self.document = extract_document(self.base / 'input/contract.pdf', self.created['contract'], self.root)
        self.chunks = segment(self.document, self.root)['chunks']
        self.reference = authoring.load(self.base)[1]

    def reference_policy(self):
        chunks = {c.get('clause_id'): c for c in self.chunks}
        rules, sources = [], {}
        for expected in self.reference['rules']:
            chunk = chunks[expected['source']]
            rule = dict(deepcopy(expected), source=chunk['chunk_id'])
            rules.append(rule)
            sources[chunk['chunk_id']] = dict(chunk_id=chunk['chunk_id'], clause_id=chunk['clause_id'],
                                            text=chunk['text'], **chunk['source'])
        return dict(version='2.0', contract_id=self.created['contract'], id='simulated-policy', type='Set',
                    rules=rules, sources=sources, document=dict(sha256=self.reference['pdf_sha256']))

    def test_pdf_segmentation_preserves_every_selected_term_and_definition(self):
        self.assertEqual(self.created['expected_rules'], 7)
        clauses = {c.get('clause_id'): ' '.join(c['text'].split()) for c in self.chunks}
        for expected in self.reference['clauses']:
            self.assertEqual(clauses[expected['clause_id']], expected['clause_id'] + ' ' + expected['text'])
        self.assertIn('within 30 days', clauses['5.1'])
        self.assertIn('within 72 hours', clauses['5.2'])

    def test_reference_comparison_and_prohibition_examples_accept_the_authored_meaning(self):
        result = authoring.evaluate(self.base, self.reference_policy(), self.chunks, self.root)
        self.assertTrue(result['complete_policy'])
        self.assertEqual(result['f1'], 1)
        self.assertEqual(len(result['behavior_checks']), 9)
        self.assertTrue(all(c['passed'] for c in result['behavior_checks']))

    def test_source_bound_aliases_do_not_turn_missing_exceptions_into_matches(self):
        policy = self.reference_policy()
        for rule in policy['rules']:
            rule['actor'] = authoring.DEFAULTS['recipient']
            if rule['asset'] == 'Data': rule['asset'] = authoring.DEFAULTS['data']
        self.assertEqual(authoring.evaluate(self.base, policy, self.chunks, self.root)['f1'], 1)
        broadened = deepcopy(policy)
        next(r for r in broadened['rules'] if r['action'] == 'read')['action'] = 'access'
        self.assertEqual(authoring.evaluate(self.base, broadened, self.chunks, self.root)['matched_rules'], 6)
        sharing = next(r for r in policy['rules'] if r['action'] == 'share')
        sharing['conditions'] = [c for c in sharing['conditions'] if c['field'] != 'approval']
        result = authoring.evaluate(self.base, policy, self.chunks, self.root)
        self.assertEqual(result['matched_rules'], 6)
        self.assertFalse(next(c for c in result['behavior_checks'] if c['scenario'] == 'Third party with consent')['passed'])

    def test_source_identity_comes_from_original_chunks_and_pdf_not_optional_metadata(self):
        policy = self.reference_policy()
        for source in policy['sources'].values(): source.pop('clause_id')
        self.assertTrue(authoring.evaluate(self.base, policy, self.chunks, self.root)['complete_policy'])
        policy['document']['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'source PDF'):
            authoring.evaluate(self.base, policy, self.chunks, self.root)

    def test_deadline_direction_values_and_extra_rules_remain_errors(self):
        for mutation in ('deadline', 'operator', 'actor', 'duplicate'):
            policy = self.reference_policy()
            notification = next(r for r in policy['rules'] if r['action'] == 'notify')
            if mutation == 'deadline': notification['conditions'][1]['value'] = 'PT73H'
            elif mutation == 'operator': notification['conditions'][1]['op'] = 'gteq'
            elif mutation == 'actor': notification['actor'] = 'Provider'
            else: policy['rules'].append(dict(notification, id='duplicate'))
            result = authoring.evaluate(self.base, policy, self.chunks, self.root)
            self.assertLess(result['f1'], 1, mutation)
            self.assertFalse(result['complete_policy'], mutation)
            if mutation == 'actor': self.assertEqual(result['diagnostics']['wrong_parties'], 1)
            if mutation == 'duplicate': self.assertEqual(result['diagnostics']['extra_rules'], 1)

    def test_missing_policy_and_unselected_consent_are_not_false_passes(self):
        result = authoring.evaluate(self.base, None, self.chunks, self.root)
        self.assertEqual(result['recall'], 0)
        self.assertFalse(result['complete_policy'])
        self.assertFalse(any(c['passed'] for c in result['behavior_checks']))
        content = authoring.draft(dict(authoring.DEFAULTS, consent_exception=False))
        sharing = next(r for r in content['rules'] if r['action'] == 'share')
        self.assertFalse(any(c['field'] == 'approval' for c in sharing['conditions']))
        self.assertNotIn('consent', next(c['text'] for c in content['clauses'] if c['clause_id'] == '3.3'))

    def test_pdf_and_authoring_changes_invalidate_the_reference(self):
        manifest = json.loads((self.base / 'authoring.json').read_text(encoding='utf-8'))
        manifest['terms']['delete_days'] = 4
        write_json(self.base / 'authoring.json', manifest)
        with self.assertRaisesRegex(ValueError, 'changed'): authoring.load(self.base)

    def test_changed_pdf_or_reference_cannot_be_scored_as_the_original(self):
        path = self.base / 'input/contract.pdf'
        before = path.read_bytes()
        path.write_bytes(before + b'\nchanged')
        with self.assertRaisesRegex(ValueError, 'changed'): authoring.load(self.base)
        path.write_bytes(before)
        reference = deepcopy(self.reference)
        reference['rules'][0]['kind'] = 'prohibition'
        write_json(self.base / 'reference.json', reference)
        with self.assertRaisesRegex(ValueError, 'changed'): authoring.load(self.base)

    def test_each_checkbox_maps_to_its_own_explicit_clause_and_effect(self):
        features = dict(read=('3.1', 'permission', 'read'), purpose_limit=('3.2', 'prohibition', 'use'),
            sharing_limit=('3.3', 'prohibition', 'share'), confidentiality=('4.1', 'obligation', 'keepConfidential'),
            secure_storage=('4.2', 'obligation', 'store'), deletion=('5.1', 'obligation', 'delete'),
            notification=('5.2', 'obligation', 'notify'))
        for flag, (clause, kind, action) in features.items():
            terms = dict(authoring.DEFAULTS, **{key: key == flag for key in features}, delete_days=7, notify_hours=24)
            content = authoring.draft(terms)
            self.assertEqual(len(content['rules']), 1, flag)
            only = content['rules'][0]
            self.assertEqual((only['source'], only['kind'], only['action']), (clause, kind, action))
            text = next(c['text'] for c in content['clauses'] if c['clause_id'] == clause)
            self.assertIn('may' if kind == 'permission' else 'must not' if kind == 'prohibition' else 'must', text)
            if flag == 'deletion':
                self.assertIn('within 7 days after termination', text)
                self.assertIn(dict(field='elapsedTime', op='lteq', value='P7D'), only['conditions'])
            if flag == 'notification':
                self.assertIn('within 24 hours after becoming aware', text)
                self.assertIn(dict(field='elapsedTime', op='lteq', value='PT24H'), only['conditions'])

    def test_invalid_inputs_are_rejected_and_new_revisions_do_not_overwrite(self):
        for change in (dict(provider=authoring.DEFAULTS['recipient']), dict(delete_days=0),
                       dict(notify_hours=True), dict(purpose='line one\nline two'), dict(extra='unknown'),
                       dict(provider='Acme. Recipient may share Data')):
            with self.assertRaises(ValueError): authoring.validate_terms(dict(authoring.DEFAULTS, **change))
        before = (self.base / 'input/contract.pdf').read_bytes()
        revision = authoring.create(self.root, dict(authoring.DEFAULTS, delete_days=7))
        self.assertNotEqual(revision['contract'], self.created['contract'])
        self.assertEqual((self.base / 'input/contract.pdf').read_bytes(), before)

    def test_advanced_terms_preserve_exceptions_geography_and_encryption(self):
        terms = dict(authoring.DEFAULTS, encryption=True, storage_limit=True, legal_disclosure=True,
                     legal_retention=True, delete_days=7, notify_hours=24)
        self.created = authoring.create(self.root, terms)
        self.base = self.root / 'contracts' / self.created['contract']
        self.document = extract_document(self.base / 'input/contract.pdf', self.created['contract'], self.root)
        self.chunks = segment(self.document, self.root)['chunks']
        self.reference = authoring.load(self.base)[1]
        policy = self.reference_policy()
        result = authoring.evaluate(self.base, policy, self.chunks, self.root)
        self.assertEqual(result['expected_rules'], 9)
        self.assertTrue(result['complete_policy'])
        self.assertEqual(len(result['behavior_checks']), 17)
        self.assertTrue(all(c['passed'] for c in result['behavior_checks']))
        clauses = {c.get('clause_id'): c['text'] for c in self.chunks}
        self.assertIn('unless disclosure is required by law', clauses['3.3'])
        self.assertIn('unless retention is required by law', clauses['5.1'])
        self.assertIn('Norway or Sweden', clauses['4.4'])
        for clause, mutation in (('3.3', 'drop_exception'), ('5.1', 'drop_exception'),
                                 ('4.4', 'reverse_countries'), ('4.3', 'drop_algorithm')):
            changed = deepcopy(policy)
            rule = next(r for r in changed['rules'] if r['source'] == next(c['chunk_id'] for c in self.chunks if c.get('clause_id') == clause))
            if mutation == 'drop_exception': rule['conditions'] = [c for c in rule['conditions'] if c['field'] != 'compliance']
            elif mutation == 'reverse_countries': rule['conditions'][0]['op'] = 'isAnyOf'
            else: rule.pop('conditions')
            compared = authoring.evaluate(self.base, changed, self.chunks, self.root)
            self.assertEqual(compared['matched_rules'], 8, clause)
            self.assertTrue(any(not c['passed'] for c in compared['behavior_checks'] if c['clause_id'] == clause), clause)
            if mutation == 'drop_exception': self.assertGreater(compared['diagnostics']['failed_exception_checks'], 0)

    def test_readable_view_preserves_the_actual_json_boolean_logic_and_source(self):
        policy = self.reference_policy()
        rule = next(r for r in policy['rules'] if r['action'] == 'share')
        rule['conditions'] = [dict(any=rule['conditions'])]
        row = next(r for r in readable_rules(policy) if r['id'] == rule['id'])
        self.assertIn('must not share Data', row['summary'])
        self.assertIn('At least one of', row['conditions'][0])
        self.assertEqual(row['rule'], rule)
        self.assertEqual(row['source'], policy['sources'][rule['source']])
        self.assertEqual(row['source']['clause_id'], '3.3')
        rule['conditions'] = [dict(exactly_one=rule['conditions'][0]['any'])]
        row = next(r for r in readable_rules(policy) if r['id'] == rule['id'])
        self.assertIn('Exactly one of', row['conditions'][0])
        deletion = next(r for r in readable_rules(policy) if r['rule']['action'] == 'delete')
        self.assertIn('30 days', deletion['conditions'][0])

    def test_research_template_covers_multiple_resources_roles_scopes_and_publication_clocks(self):
        terms = dict(authoring.DEFAULTS, **{k: True for k in authoring.FLAGS})
        self.created = authoring.create(self.root, terms)
        self.base = self.root / 'contracts' / self.created['contract']
        self.document = extract_document(self.base / 'input/contract.pdf', self.created['contract'], self.root)
        self.chunks = segment(self.document, self.root)['chunks']
        self.reference = authoring.load(self.base)[1]
        policy = self.reference_policy()
        result = authoring.evaluate(self.base, policy, self.chunks, self.root)
        self.assertEqual(result['expected_rules'], 20)
        self.assertTrue(result['complete_policy'])
        self.assertGreaterEqual(len(result['behavior_checks']), 45)
        self.assertTrue(all(c['passed'] for c in result['behavior_checks']), [c for c in result['behavior_checks'] if not c['passed']])
        reversed_policy = dict(policy, rules=list(reversed(policy['rules'])))
        ordered = to_compact(to_odrl(reversed_policy, self.root), self.chunks, self.root)
        self.assertEqual([r['source'] for r in ordered['rules']], [r['source'] for r in policy['rules']])
        self.assertEqual({r['id']:r for r in ordered['rules']}, {r['id']:r for r in policy['rules']})
        self.assertEqual([r['id'] for r in readable_rules(reversed_policy)], [r['id'] for r in policy['rules']])
        by_clause = {c.get('clause_id'): c for c in self.chunks}
        self.assertIn('clause 7.1', by_clause['7.2']['text'])
        index = ContextIndex(self.chunks)
        for primary, required in (('6.1', '1.10'), ('9.3', '1.14')):
            context, _ = index.context_for(by_clause[primary])
            definition = next(c for c in context if c.get('clause_id') == required)
            self.assertEqual(definition['relationship'], 'definition')
            self.assertIn(definition['text'], by_clause[required]['text'])
        for clause, field, mutation in (('6.1', 'actor', 'Recipient'), ('6.1', 'scope', 'drop'), ('7.2', 'scope', 'drop'),
                                       ('8.1', 'dateTime', 'drop'), ('8.2', 'approval', 'drop'),
                                       ('9.2', 'postponementDuration', 'drop'), ('9.4', 'approval', 'drop')):
            changed = deepcopy(policy)
            rule = next(r for r in changed['rules'] if r['source'] == by_clause[clause]['chunk_id'])
            if field == 'actor': rule['actor'] = mutation
            else: rule['conditions'] = [c for c in rule['conditions'] if c['field'] != field]
            compared = authoring.evaluate(self.base, changed, self.chunks, self.root)
            self.assertFalse(compared['complete_policy'], clause)
            self.assertTrue(any(not p['passed'] for p in compared['behavior_checks'] if p['clause_id'] == clause), clause)
        rule = next(r for r in policy['rules'] if r['source'] == by_clause['7.2']['chunk_id'])
        rule['context'] = [by_clause['7.1']['chunk_id']]
        row = next(r for r in readable_rules(policy) if r['id'] == rule['id'])
        self.assertEqual(row['supporting_sources'][0]['text'], by_clause['7.1']['text'])

    def test_project_date_inputs_and_older_dct_forms(self):
        old = {k: v for k, v in authoring.DEFAULTS.items() if k not in authoring.PROJECT_DEFAULTS}
        self.assertEqual(authoring.validate_terms(old), authoring.DEFAULTS)
        for change in (dict(project_start='2026-02-30'), dict(project_end='2020-01-01'),
                       dict(publication_notice_days=0), dict(publication_delay_days=True),
                       dict(records_access=True, records=authoring.DEFAULTS['data'])):
            with self.assertRaises(ValueError): authoring.validate_terms(dict(authoring.DEFAULTS, **change))
        from xfed.common import constraint_truth
        comparisons = [dict(leftOperand='dateTime', operator='gteq', rightOperand='2026-11-01'),
                       dict(leftOperand='dateTime', operator='lteq', rightOperand='2027-10-31')]
        self.assertTrue(constraint_truth(comparisons, dict(dateTime='2026-11-01')))
        self.assertFalse(constraint_truth(comparisons, dict(dateTime='2027-11-01')))
        self.assertIsNone(constraint_truth(comparisons, dict(dateTime='not a date')))

    def test_legacy_forms_and_frozen_references_keep_their_meaning(self):
        legacy = {k: v for k, v in authoring.DEFAULTS.items() if k not in (*authoring.ADVANCED, *authoring.PROJECT_DEFAULTS)}
        self.assertEqual(authoring.validate_terms(legacy), authoring.DEFAULTS)
        manifest = json.loads((self.base / 'authoring.json').read_text())
        reference = deepcopy(self.reference)
        reference['template_version'] = 'guided-2'
        manifest.update(template_version='guided-2', terms=legacy, terms_sha256=authoring.digest(legacy),
                        reference_sha256=authoring.digest(reference))
        write_json(self.base / 'authoring.json', manifest)
        write_json(self.base / 'reference.json', reference)
        self.assertTrue(authoring.evaluate(self.base, self.reference_policy(), self.chunks, self.root)['complete_policy'])
        self.assertEqual(authoring.load(self.base)[0]['terms'], legacy)
        self.assertEqual(Pipeline(self.root, 'http://127.0.0.1:11435').guided_result(self.created['contract'])['clauses'], reference['clauses'])

    def test_advanced_country_values_are_bounded_and_inactive_exceptions_add_no_rules(self):
        for invalid in ([], ['Norway', 'Norway'], ['Unknown'], 'Norway', [True], list(authoring.STORAGE_COUNTRIES)):
            with self.assertRaises(ValueError): authoring.validate_terms(dict(authoring.DEFAULTS, storage_countries=invalid))
        terms = dict(authoring.DEFAULTS, sharing_limit=False, deletion=False, legal_disclosure=True, legal_retention=True)
        draft = authoring.draft(terms)
        self.assertFalse(any('required by law' in c['text'] for c in draft['clauses']))
        self.assertEqual(len(draft['rules']), 5)

    def test_full_pipeline_calls_do_not_receive_authoring_or_reference_payloads(self):
        policy = self.reference_policy()
        pipeline = Pipeline(self.root, 'http://127.0.0.1:11435')
        pipeline.update(status='running', contract=self.created['contract'])
        client = SimpleNamespace(model='simulated', preflight=lambda: None, reset_assignments=lambda: None, metadata=lambda: {})
        expected_ids = {r['source'] for r in self.reference['rules']}
        def classify(document, *args, **kwargs):
            result = deepcopy(document)
            for chunk in result['chunks']:
                chunk['classification'] = dict(label='RELEVANT' if chunk.get('clause_id') in expected_ids else 'NOT_RELEVANT')
            return result
        def generated(document, *args, **kwargs):
            self.assertEqual(set(document), {'contract_id', 'items'})
            self.assertTrue(all('terms' not in item and 'expected' not in item for item in document['items']))
            return policy, dict(status='draft')
        with patch('xfed.ui.classify', side_effect=classify), patch('xfed.ui.generate', side_effect=generated):
            pipeline.run(self.created['contract'], list(STAGES), client, False)
        self.assertEqual(pipeline.snapshot()['status'], 'complete', pipeline.snapshot()['message'])
        for stage in STAGES: self.assertTrue(pipeline.path(self.created['contract'], stage).is_file())
        report = json.loads((self.base / 'output/guided_evaluation.json').read_text(encoding='utf-8'))
        self.assertEqual(report['matched_rules'], 7)
        self.assertEqual(report['routed_operative_clauses'], 7)
        preview = pipeline.guided_result(self.created['contract'])['policy_preview']
        self.assertEqual(len(preview), 7)
        for row in preview:
            self.assertEqual(row['source']['text'], next(c['text'] for c in self.chunks if c['chunk_id'] == row['source']['chunk_id']))
            self.assertTrue(row['source']['pages'])
        changed = deepcopy(policy)
        changed['rules'].pop()
        write_json(pipeline.path(self.created['contract'], '2'), changed)
        with self.assertRaisesRegex(ValueError, 'changed after evaluation'):
            pipeline.guided_result(self.created['contract'])
        pipeline.invalidate(self.created['contract'], '1.1')
        self.assertFalse((self.base / 'output/guided_evaluation.json').exists())

    def test_guided_routes_preview_create_and_reopen_without_model_calls(self):
        from xfed.ui import handler
        pipeline = Pipeline(self.root, 'http://127.0.0.1:11435')
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler(pipeline))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(lambda: (server.shutdown(), thread.join()))
        url = 'http://127.0.0.1:' + str(server.server_port)
        with patch.object(pipeline, 'make_client') as factory:
            for route in ('preview', 'create'):
                request = Request(url + '/api/authoring/' + route, data=json.dumps(authoring.DEFAULTS).encode(),
                                  headers={'Content-Type': 'application/json'})
                with urlopen(request) as response: value = json.load(response)
                self.assertEqual(value['expected_rules'], 7)
            with urlopen(url + '/api/authoring?contract=' + value['contract']) as response: saved = json.load(response)
            self.assertEqual(saved['terms'], authoring.DEFAULTS)
            self.assertIsNone(saved['evaluation'])
            factory.assert_not_called()

    def test_cli_accepts_generated_contract_identifiers_and_starts_at_pdf_extraction(self):
        from xfed.__main__ import main
        main([self.created['contract'], '--root', str(self.root), '--through', '1.2'])
        self.assertTrue((self.base / 'output/1 preprocessing/1.1 extraction/document_blocks.json').is_file())
        self.assertTrue((self.base / 'output/1 preprocessing/1.2 segmentation/contract_chunks.json').is_file())
        self.assertFalse((self.base / 'output/2 policy_generation/policy.json').exists())

    def test_atomic_output_survives_a_transient_windows_lock_and_preserves_old_data_on_failure(self):
        target = self.root / 'saved.json'
        write_json(target, dict(old=True))
        replace = Path.replace
        locked = PermissionError('File is in use')
        locked.winerror = 32
        attempts = []
        def transient(source, destination):
            attempts.append(source)
            if len(attempts) == 1: raise locked
            return replace(source, destination)
        with patch('xfed.common.Path.replace', autospec=True, side_effect=transient), patch('xfed.common.sleep'):
            write_json(target, dict(new=True))
        self.assertEqual(json.loads(target.read_text()), dict(new=True))
        with patch('xfed.common.Path.replace', side_effect=locked), patch('xfed.common.sleep'):
            with self.assertRaises(PermissionError): write_json(target, dict(incomplete=True))
        self.assertEqual(json.loads(target.read_text()), dict(new=True))
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['contracts', 'saved.json', 'schemas'])


if __name__ == '__main__': unittest.main()
