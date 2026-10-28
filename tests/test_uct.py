"""UCT integration regressions; simulated answers test plumbing, not model accuracy."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from xfed import node_review, uct_evaluation
from xfed.common import validate, write_json
from xfed.policy_generation import complete_fragment
from xfed.uct import (UCTWorkspace, assemble, check_contract, clause_inputs, extract_clause,
                      fingerprint, human_policy)
from xfed.ui import Pipeline, handler


ROOT = Path(__file__).resolve().parents[1]


class UCTTests(unittest.TestCase):
    def setUp(self):
        self.document = json.loads((ROOT / 'config/uct_template.json').read_text(encoding='utf-8'))['contract']

    def workspace(self, temporary):
        workspace = UCTWorkspace(ROOT)
        workspace.path = Path(temporary) / 'state.json'
        workspace.data = dict(document=deepcopy(self.document), revision=1, artifacts={}, generation=None,
                              nodes=[], events=[], evaluation=None)
        return workspace

    def fragment(self, identifier='UCT-5.1'):
        item = next(row for row in clause_inputs(self.document) if row['chunk_id'] == identifier)
        return complete_fragment(dict(odrlPolicy=dict(**{'@type': 'Set'}, permission=[dict(uid='urn:xfed:uct:' + identifier + ':r1', action='read',
            target='Project Dataset', assignee='Harbor Research')]), traceability=[dict(ruleUid='urn:xfed:uct:' + identifier + ':r1',
            ruleType='permission', sourceClauses=[dict(chunk_id=identifier, relationship='primary')])]), 'uct_demo', item)

    def test_template_dependencies_and_closure(self):
        check_contract(self.document)
        item = next(row for row in clause_inputs(self.document) if row['chunk_id'] == 'UCT-5.1')
        self.assertEqual({row['chunk_id'] for row in item['context']}, {'UCT-3.1', 'UCT-4.1'})
        self.assertEqual(item['text'], self.document['clauses'][4]['text'])
        self.document['clauses'][0]['depends_on'] = ['UCT-5.1']
        self.document['clauses'][2]['depends_on'] = ['UCT-1.1']
        with self.assertRaisesRegex(ValueError, 'cycles'):
            check_contract(self.document)

    def test_missing_explicit_reference_and_duplicate_ids_rejected(self):
        self.document['clauses'][0]['text'] = 'See UCT-5.1 for access.'
        with self.assertRaisesRegex(ValueError, 'depends_on'):
            check_contract(self.document)
        self.document['clauses'][0]['depends_on'] = ['UCT-5.1']
        check_contract(self.document)
        self.document['clauses'][1]['id'] = 'UCT-1.1'
        with self.assertRaises(ValueError):
            check_contract(self.document)

    def test_changed_definition_invalidates_transitive_dependents(self):
        client = SimpleNamespace(cancel=Event())
        answer = dict(classification=dict(label='NOT_RELEVANT'), policy=None, failed=False)
        with TemporaryDirectory() as temporary, patch('xfed.uct.extract_clause', return_value=answer) as extract:
            workspace = self.workspace(temporary)
            with patch('xfed.uct.engine_identity', return_value={'version': 1}):
                first = workspace.run(client)
                self.assertEqual(len(first['regenerated']), 19)
                second = workspace.run(client)
                self.assertEqual(len(second['reused']), 19)
                self.assertEqual(extract.call_count, 19)
                document = deepcopy(workspace.data['document'])
                document['clauses'][2]['text'] += ' Additional definition scope.'
                workspace.save(document, 1)
                self.assertIsNone(workspace.data['generation'])
                third = workspace.run(client)
                self.assertIn('UCT-5.1', third['regenerated'])
                self.assertIn('UCT-8.1', third['regenerated'])
                self.assertIn('UCT-12.1', third['reused'])
            with patch('xfed.uct.engine_identity', return_value={'version': 2}):
                self.assertEqual(len(workspace.run(client)['regenerated']), 19)

    def test_failed_clause_retried_deleted_rule_removed_other_rule_identical(self):
        client = SimpleNamespace(cancel=Event())
        def operation(item, client, root, contract):
            return dict(classification=dict(label='RELEVANT'), policy=self.fragment() if item['chunk_id'] == 'UCT-5.1' else None,
                        failed=item['chunk_id'] == 'UCT-15.1')
        with TemporaryDirectory() as temporary, patch('xfed.uct.extract_clause', side_effect=operation), patch('xfed.uct.engine_identity', return_value={}):
            workspace = self.workspace(temporary)
            first = workspace.run(client)
            validate(first['policy'], 5, ROOT)
            second = workspace.run(client)
            self.assertEqual(second['regenerated'], ['UCT-15.1'])
            self.assertEqual(first['policy'], second['policy'])
            document = deepcopy(workspace.data['document'])
            document['clauses'] = [row for row in document['clauses'] if row['id'] != 'UCT-5.1']
            workspace.save(document, 1)
            third = workspace.run(client)
            self.assertEqual(third['deleted'], ['UCT-5.1'])
            self.assertIsNone(third['policy'])

    def test_router_exclusion_is_not_sent_to_generator(self):
        item = clause_inputs(self.document)[0]
        with patch('xfed.uct.classify_batch', return_value=[dict(label='NOT_RELEVANT')]), patch('xfed.uct.generate') as generate:
            self.assertIsNone(extract_clause(item, None, ROOT)['policy'])
            generate.assert_not_called()
        with patch('xfed.uct.classify_batch', return_value=[dict(label='NEEDS_CONTEXT')]), patch('xfed.uct.generate', return_value=(None, {'clauses': []})) as generate:
            extract_clause(item, None, ROOT)
            generate.assert_called_once()

    def test_node_proposal_is_advisory_until_approval_then_invalidates_votes(self):
        with TemporaryDirectory() as temporary:
            workspace = self.workspace(temporary)
            policy = self.fragment()
            workspace.data['generation'] = dict(policy=policy, policy_hash=fingerprint(policy))
            text = workspace.data['document']['clauses'][8]['text'].replace('24 hours', '12 hours')
            decision = dict(decision='MODIFY', reason='Shorter incident deadline requested.', amendments=[dict(
                clause_id='UCT-7.2', replacement_text=text)])
            node_review.submit(workspace, 'security', decision, 1, fingerprint(policy))
            self.assertIn('24 hours', workspace.data['document']['clauses'][8]['text'])
            self.assertIn('Amendment requested', node_review.consensus(workspace.snapshot()))
            node_review.approve(workspace, 'security', 1, fingerprint(policy))
            self.assertIn('12 hours', workspace.data['document']['clauses'][8]['text'])
            self.assertEqual(workspace.data['revision'], 2)
            self.assertIsNone(workspace.data['generation'])
            self.assertEqual(workspace.data['nodes'], [])
            with self.assertRaisesRegex(ValueError, 'Stale'):
                node_review.submit(workspace, 'security', decision, 1, fingerprint(policy))

    def test_reject_error_and_simulated_reviews_never_become_acceptance(self):
        with TemporaryDirectory() as temporary:
            workspace = self.workspace(temporary)
            policy = self.fragment()
            workspace.data['generation'] = dict(policy=policy, policy_hash=fingerprint(policy))
            node_review.run_reviews(workspace, 'simulated', lambda _: self.fail('No simulated model calls'))
            self.assertEqual({row['decision'] for row in workspace.data['nodes']}, {'ACCEPT', 'MODIFY', 'REJECT'})
            self.assertIn('Blocked', node_review.consensus(workspace.snapshot()))
            workspace.data['nodes'][2]['decision'] = 'ERROR'
            workspace.data['nodes'][1]['decision'] = 'ACCEPT'
            self.assertNotIn('All demo nodes accepted', node_review.consensus(workspace.snapshot()))

    def test_same_policy_hash_different_revision_and_busy_edits_rejected(self):
        with TemporaryDirectory() as temporary:
            workspace = self.workspace(temporary)
            policy = self.fragment()
            workspace.data['generation'] = dict(policy=policy, policy_hash=fingerprint(policy))
            with self.assertRaisesRegex(ValueError, 'Stale'):
                node_review.submit(workspace, 'research', dict(decision='ACCEPT', reason='ok', amendments=[]), 0, fingerprint(policy))
            workspace.running = True
            with self.assertRaises(ValueError):
                workspace.save(workspace.data['document'], 1)

    def test_node_amendments_can_declare_new_dependencies_without_hiding_them(self):
        text = self.document['clauses'][12]['text'] + ' See UCT-14.1.'
        decision = dict(decision='MODIFY', reason='Connect retention to the term.', amendments=[dict(
            clause_id='UCT-10.1', replacement_text=text, depends_on=['UCT-3.1', 'UCT-4.1', 'UCT-14.1'])])
        amended = node_review.check_decision(decision, self.document)
        self.assertIn('UCT-14.1', amended['clauses'][12]['depends_on'])
        decision['amendments'][0]['depends_on'] = ['UCT-3.1', 'UCT-4.1']
        with self.assertRaisesRegex(ValueError, 'depends_on'):
            node_review.check_decision(decision, self.document)

    def test_benchmark_gold_frozen_separate_and_failures_count_as_omissions(self):
        reference, cases = uct_evaluation.load_cases(ROOT)
        self.assertEqual(len(cases), 57)
        self.assertEqual({row['split'] for row in cases}, {'dev', 'test'})
        case = next(row for row in cases if row['id'] == 'uct_dev_a/UCT-5.1')
        missed = uct_evaluation.score(case, dict(classification=dict(label='NOT_RELEVANT'), policy=None), reference)
        self.assertFalse(missed['relevance_correct'])
        self.assertEqual(missed['fn'], 1)
        self.assertFalse(missed['exact'])
        control = next(row for row in cases if row['id'] == 'uct_dev_a/UCT-12.1')
        failed = uct_evaluation.score(control, dict(failed=True), reference)
        self.assertFalse(failed['exact'])
        self.assertEqual(uct_evaluation.summary([], 6)['completed'], 0)

    def test_benchmark_catches_extra_conditions_wrong_actor_and_wrong_deadline(self):
        reference, cases = uct_evaluation.load_cases(ROOT)
        case = next(row for row in cases if row['id'] == 'uct_dev_a/UCT-7.2')
        gold = deepcopy(case['gold']['rules'][0]); gold.pop('kind'); gold['uid'] = 'r1'
        policy = dict(odrlPolicy=dict(obligation=[gold]))
        result = dict(classification=dict(label='RELEVANT'), policy=policy)
        self.assertTrue(uct_evaluation.score(case, result, reference)['exact'])
        for field, value in [('assignee', 'Someone else'), ('constraint', []), ('target', 'Other Data')]:
            bad = deepcopy(result);bad['policy']['odrlPolicy']['obligation'][0][field] = value
            scores = uct_evaluation.score(case, bad, reference)
            self.assertFalse(scores['exact']);self.assertEqual(scores['fn'], 1);self.assertEqual(scores['fp'], 1)

    def test_relevance_definitions_are_distinct_from_operative_rule_truth(self):
        reference, cases = uct_evaluation.load_cases(ROOT)
        for identifier in ('UCT-3.1', 'UCT-4.1', 'UCT-8.1'):
            case = next(row for row in cases if row['id'] == 'uct_dev_a/' + identifier)
            self.assertEqual(case['gold']['relevance'], 'RELEVANT')
            self.assertEqual(case['gold']['rules'], [])
        self.assertEqual(reference['revision_history'][0]['phase'], 'post_development_prediction_exposure')

    def test_partial_checkpoint_survives_cancel_without_mixed_policy_publication(self):
        client = SimpleNamespace(cancel=Event())
        def operation(*args):
            client.cancel.set()
            return dict(classification=dict(label='NOT_RELEVANT'), policy=None, failed=False)
        with TemporaryDirectory() as temporary, patch('xfed.uct.extract_clause', side_effect=operation):
            workspace = self.workspace(temporary)
            with self.assertRaises(InterruptedError):
                workspace.run(client)
            self.assertEqual(list(workspace.data['artifacts']), ['UCT-1.1'])
            self.assertIsNone(workspace.data['generation'])
            self.assertTrue(workspace.path.exists())

    def test_three_llm_node_roles_use_structured_calls_without_services(self):
        class LocalClient:
            cancel, num_ctx, num_predict = Event(), 32768, 4096
            def preflight(self):
                pass
            def generate(self, prompt, value, schema, **options):
                self.payload = value
                self.options = options
                return dict(decision='ACCEPT', reason='Simulated LLM approval.', amendments=[])
            def close(self):
                pass
        with TemporaryDirectory() as temporary:
            workspace = self.workspace(temporary)
            policy = self.fragment()
            workspace.data['generation'] = dict(policy=policy, policy_hash=fingerprint(policy), clauses={})
            clients = []
            def factory(node):
                client = LocalClient();clients.append(client);return client
            node_review.run_reviews(workspace, 'ollama', factory)
            self.assertEqual(len(clients), 3)
            self.assertEqual(clients[0].payload['policy'], policy)
            self.assertEqual(len({client.payload['mandate'] for client in clients}), 3)
            self.assertTrue(all(client.options['truncate'] is False for client in clients))
            self.assertIn('All demo nodes accepted', node_review.consensus(workspace.snapshot()))

    def test_http_uct_routes_and_stale_save(self):
        with TemporaryDirectory() as temporary:
            pipeline = Pipeline(ROOT, 'http://127.0.0.1:11435')
            pipeline._uct = self.workspace(temporary)
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler(pipeline))
            thread = Thread(target=server.serve_forever, daemon=True);thread.start()
            base = 'http://127.0.0.1:' + str(server.server_port)
            try:
                with urlopen(base + '/api/uct') as response:
                    state = json.load(response)
                self.assertEqual(state['revision'], 1)
                document = state['document'];document['title'] = 'Edited native agreement'
                request = Request(base + '/api/uct/save', data=json.dumps(dict(document=document, revision=1)).encode(),
                                  headers={'Content-Type': 'application/json'})
                with urlopen(request) as response:
                    self.assertEqual(json.load(response)['revision'], 2)
                from urllib.error import HTTPError
                with self.assertRaises(HTTPError) as error:
                    urlopen(request)
                self.assertEqual(error.exception.code, 400)
            finally:
                server.shutdown();thread.join();server.server_close()


if __name__ == '__main__':
    unittest.main()
