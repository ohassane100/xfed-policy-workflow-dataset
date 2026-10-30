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
from xfed.common import write_json
from xfed.extraction import extract_document
from xfed.segmentation import segment
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
