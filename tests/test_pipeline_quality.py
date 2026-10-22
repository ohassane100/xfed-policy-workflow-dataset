"""Deterministic regressions for evidence preservation and parallel review plumbing.

These tests use simulated model answers; they do not measure model accuracy.
"""

from copy import deepcopy
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Barrier, Event, Thread
from tempfile import TemporaryDirectory
import json
import shutil
import unittest

from xfed.common import map_chunks, write_json
from xfed.enrichment import ContextIndex, enrich
from xfed.llm import OllamaPool
from xfed.policy_generation import (complete_fragment, generate, generate_answer, generate_item,
                                    response_schema, summarize_error, validate_fragment)
from jsonschema import Draft202012Validator, ValidationError
from xfed.policy_review import conflicts, review
from xfed.relevance import classify, classify_batch
from xfed.ui import CachedClient, Pipeline


ROOT = Path(__file__).resolve().parents[1]


def chunk(identifier='c1', text='Provider must delete the supplied data within 12 hours after notice.', **extra):
    return dict(chunk_id=identifier, text=text, source=dict(pages=[1], block_ids=['b1']), **extra)


def fragment(item, kind='obligation'):
    return complete_fragment(dict(odrlPolicy={kind: [dict(uid='r1', target='supplied data',
        assigner='Provider', assignee='Provider', action='delete')]}, traceability=[dict(
        ruleUid='r1', ruleType=kind, sourceClauses=[dict(chunk_id=item['chunk_id'], relationship='primary')])]),
        'contract', item)


class Client:
    model, model_digest, num_ctx, num_predict = 'simulated', 'digest', 32768, 4096
    relevance_num_predict, relevance_think, auto_relevance_think = 512, 'auto', False
    relevance_batch_size, relevance_compact, relevance_num_ctx = 1, False, 0

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def generate(self, prompt, value, output_schema, **options):
        self.calls.append((deepcopy(value), options))
        result = self.answer(value) if callable(self.answer) else self.answer
        return deepcopy(result)


def reviewed(value):
    return dict(relevance='RELEVANT', rule_reviews=[dict(rule_uid=row['rule']['uid'],
        rule_type=row['rule_type'], verdict='ENTAILED', reason='Simulated supported rule.')
        for row in value['candidate_rules']], coverage='COMPLETE' if value['candidate_rules'] else 'MISSING_RULES',
        reason='Simulated review.', missing_rules=[] if value['candidate_rules'] else ['Deletion duty omitted.'])


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.item = dict(chunk(), context=[])
        self.classified = dict(contract_id='contract', chunks=[dict(chunk(), classification=dict(label='RELEVANT'))])
        self.enriched = dict(contract_id='contract', items=[self.item])

    def test_parent_restrictions_include_referenced_provision(self):
        parent = chunk('c1', '1. Research use is subject to clause 8.', clause_id='1')
        child = chunk('c2', '(a) test reproducibility;', clause_id='1(a)', parent_clause_id='1')
        restriction = chunk('c3', '8. Do not publish identifiable data.', clause_id='8')
        context, missing = ContextIndex([parent, child, restriction]).context_for(child)
        self.assertIn('c3', [row['chunk_id'] for row in context])
        self.assertFalse(missing)

    def test_transitive_definitions_are_resolved_without_order_dependence(self):
        data = [chunk('c1', 'Provider may use Material.'),
                chunk('c2', 'Material means SensitiveDataset.', clause_id='2'),
                chunk('c3', 'SensitiveDataset means the supplied measurements.', clause_id='3')]
        context, _ = ContextIndex(data).context_for(data[0])
        self.assertEqual({row['chunk_id'] for row in context}, {'c2', 'c3'})

    def test_missing_policy_context_does_not_erase_relevance(self):
        self.classified['chunks'][0]['text'] += ' Subject to clause 88.'
        updated, enriched = enrich(self.classified, ROOT)
        self.assertEqual(updated['chunks'][0]['classification']['label'], 'RELEVANT')
        self.assertTrue(enriched['items'][0]['unresolved_references'])

    def test_uncertain_classification_gets_new_references_once(self):
        document = dict(contract_id='contract', chunks=[chunk('c1', '1. As allowed in clause 8.', clause_id='1'),
                        chunk('c2', '8. Provider may analyse supplied measurements.', clause_id='8')])
        def answer(value):
            if value['clause']['chunk_id'] == 'c1':
                return dict(label='RELEVANT' if value['context'] else 'NEEDS_CONTEXT')
            return dict(label='RELEVANT')
        client = Client(answer)
        output = classify(document, client, ROOT)
        self.assertEqual(output['chunks'][0]['classification']['label'], 'RELEVANT')
        self.assertEqual(len(client.calls), 3)

    def test_no_blind_reclassification_without_new_evidence(self):
        client = Client(dict(label='NEEDS_CONTEXT'))
        result = classify(dict(contract_id='contract', chunks=[chunk()]), client, ROOT)
        self.assertEqual(result['chunks'][0]['classification']['label'], 'NEEDS_CONTEXT')
        self.assertEqual(len(client.calls), 1)

    def test_classifier_duplicate_ids_rejected(self):
        item = dict(clause=chunk(), context=[], unresolved_references=[])
        with self.assertRaisesRegex(ValueError, 'unique'):
            classify_batch([item, item], Client(dict(label='RELEVANT')), ROOT)

    def test_policy_and_review_forbid_truncation(self):
        client = Client(dict(policy=None, reason='Simulated abstention.'))
        generate_answer(client, 'prompt', 'contract', self.item, response_schema(ROOT))
        self.assertIs(client.calls[0][1]['truncate'], False)
        reviewer = Client(reviewed)
        review(None, self.enriched, self.classified, reviewer, ROOT)
        self.assertIs(reviewer.calls[0][1]['truncate'], False)

    def test_unresolved_references_reach_generator(self):
        self.item['unresolved_references'] = ['88: provision not found']
        client = Client(dict(policy=None, reason='Unknown condition.'))
        generate_answer(client, 'prompt', 'contract', self.item, response_schema(ROOT))
        self.assertEqual(client.calls[0][0]['clause']['unresolved_references'], self.item['unresolved_references'])

    def test_invented_identifiers_dates_and_operators_rejected(self):
        for field, value in [('target', '#invented'), ('target', 'urn:asset:invented'),
                             ('target', 'https://example.invalid/invented'), ('constraint', [dict(leftOperand='dateTime', operator='eq', rightOperand='2023-11-01')]),
                             ('constraint', [dict(leftOperand='purpose', operator='and', rightOperand='research')])]:
            with self.subTest(field=field, value=value):
                policy = fragment(self.item)
                policy['odrlPolicy']['obligation'][0][field] = value
                with self.assertRaises(ValueError):
                    validate_fragment(policy, 'contract', self.item, ROOT)

    def test_empty_optional_metadata_does_not_discard_supported_rules(self):
        policy = fragment(self.item)
        policy.pop('contract_id')
        for field in ('@context', '@type', 'uid', 'profile'):
            policy['odrlPolicy'].pop(field)
        for trace in policy['traceability']:
            for source in trace['sourceClauses']:
                source.pop('text')
        policy['osdu'] = {}
        answer = dict(policy=policy, reason='Deletion obligation.')
        output_schema = response_schema(ROOT)
        Draft202012Validator(output_schema).validate(answer)
        output, entry, failed = generate_item(self.item, Client(answer), 'prompt', 'contract', output_schema, ROOT)
        self.assertFalse(failed)
        self.assertNotIn('osdu', output)
        self.assertEqual(entry['generated'], True)

    def test_schema_correction_names_the_actual_nested_error(self):
        value = {'policy': {'actor': 3}}
        validator = Draft202012Validator(dict(properties=dict(policy=dict(anyOf=[dict(type='object',
            required=['action']), dict(type='null')]))))
        with self.assertRaises(ValidationError) as caught:
            validator.validate(value)
        self.assertIn('action', summarize_error(caught.exception))
        self.assertNotIn("'actor': 3", summarize_error(caught.exception))

    def test_duplicate_primary_and_changed_quote_rejected(self):
        for duplicate in (True, False):
            policy = fragment(self.item)
            sources = policy['traceability'][0]['sourceClauses']
            if duplicate:
                sources.append(deepcopy(sources[0]))
            else:
                sources[0]['text'] = 'Invented source text.'
            with self.assertRaises(ValueError):
                validate_fragment(policy, 'contract', self.item, ROOT)

    def test_reasoning_only_exhaustion_is_not_repeated(self):
        client = Client(None)
        client.last_request = dict(thinking_chars=100, answer_chars=0)
        def exhausted(*args, **kwargs):
            client.calls.append(1)
            raise ValueError('Ollama output token limit reached at 4096 tokens')
        client.generate = exhausted
        with self.assertRaisesRegex(ValueError, 'Reasoning exhausted'):
            generate_answer(client, 'prompt', 'contract', self.item, response_schema(ROOT))
        self.assertEqual(len(client.calls), 1)

    def test_abstention_counts_do_not_imply_good_policy(self):
        inputs = dict(contract_id='contract', items=[dict(chunk(f'c{i}'), context=[]) for i in range(5)])
        policy, report = generate(inputs, Client(dict(policy=None, reason='Simulated abstention.')), ROOT)
        self.assertIsNone(policy)
        self.assertEqual(report['summary']['abstained_needs_review'], 5)
        self.assertEqual(report['models']['simulated']['generated'], 0)
        self.assertTrue(report['warnings'])

    def test_review_catches_abstentions_and_classifier_negatives(self):
        self.classified['chunks'][0]['classification']['label'] = 'NOT_RELEVANT'
        result = review(None, dict(contract_id='contract', items=[]), self.classified, Client(reviewed), ROOT)
        self.assertEqual(result['status'], 'changes_requested')
        self.assertEqual(result['clause_reviews'][0]['coverage'], 'MISSING_RULES')
        self.assertTrue(result['clause_reviews'][0]['classification_disagreement'])
        self.assertIs(result['requires_human_approval'], True)

    def test_review_entailed_is_never_approval_and_tracks_same_model(self):
        result = review(fragment(self.item), self.enriched, self.classified, Client(reviewed), ROOT,
                        generation_report=dict(clauses=[dict(chunk_id='c1', model='simulated', model_digest='digest')]))
        self.assertEqual(result['status'], 'reviewed_no_issues_found')
        self.assertTrue(result['requires_human_approval'])
        self.assertTrue(result['clause_reviews'][0]['same_model_as_generator'])

    def test_missing_review_rows_cannot_pass(self):
        def answer(value):
            response = reviewed(value)
            response['rule_reviews'] = []
            return response
        result = review(fragment(self.item), self.enriched, self.classified, Client(answer), ROOT)
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['rule_reviews'][0]['verdict'], 'AMBIGUOUS')

    def test_internally_inconsistent_review_cannot_pass(self):
        def answer(value):
            response = reviewed(value)
            response['coverage'] = 'NO_RULES_NEEDED'
            return response
        result = review(fragment(self.item), self.enriched, self.classified, Client(answer), ROOT)
        self.assertEqual(result['status'], 'incomplete')

    def test_unknown_evidence_remains_incomplete(self):
        self.item['unresolved_references'] = ['88: missing']
        result = review(fragment(self.item), self.enriched, self.classified, Client(reviewed), ROOT)
        self.assertEqual(result['status'], 'incomplete')

    def test_changed_source_and_context_cannot_pass_review(self):
        self.item['context'] = [dict(chunk('absent', 'Forged condition'), relationship='reference')]
        with self.assertRaisesRegex(ValueError, 'supporting evidence changed'):
            review(None, self.enriched, self.classified, Client(reviewed), ROOT)

    def test_literal_definition_excerpt_is_valid_evidence(self):
        definition = chunk('c2', 'Definitions. Data means supplied measurements.')
        self.classified['chunks'].append(dict(definition, classification=dict(label='RELEVANT')))
        self.item['context'] = [dict(definition, text='Data means supplied measurements.', relationship='definition')]
        policy = fragment(self.item)
        policy['traceability'][0]['sourceClauses'].append(dict(chunk_id='c2', relationship='context', text='Data means supplied measurements.'))
        result = review(policy, self.enriched, self.classified, Client(reviewed), ROOT)
        self.assertFalse(result['evidence_errors'])

    def test_multiple_definition_excerpts_keep_literal_citations(self):
        self.item['context'] = [dict(chunk('c2', text), relationship='definition') for text in
                                ('Data means supplied measurements.', 'Recipient means Provider.')]
        generated = fragment(self.item)
        generated['traceability'][0]['sourceClauses'].append(dict(chunk_id='c2', relationship='context'))
        completed = complete_fragment(generated, 'contract', self.item)
        validate_fragment(completed, 'contract', self.item, ROOT)
        self.assertEqual([row['text'] for row in completed['traceability'][0]['sourceClauses'][1:]],
                         [row['text'] for row in self.item['context']])

    def test_existing_policy_with_invented_fields_cannot_clear_review(self):
        policy = fragment(self.item)
        policy['odrlPolicy']['obligation'][0]['target'] = '#invented'
        policy['osdu'] = dict(legal=dict(legaltags=['invented-tag'], otherRelevantDataCountries=[]))
        result = review(policy, self.enriched, self.classified, Client(reviewed), ROOT)
        self.assertEqual(result['status'], 'incomplete')
        self.assertGreaterEqual(len(result['evidence_errors']), 2)

    def test_rejected_cached_answer_is_evicted(self):
        with TemporaryDirectory() as directory:
            raw = Client(dict(policy=None, reason='Simulated abstention.'))
            cached = CachedClient(raw, Path(directory) / 'cache.json', Event(), True)
            for _ in range(2):
                cached.generate('prompt', {}, {})
            self.assertEqual(len(raw.calls), 1)
            cached.discard_last_response()
            cached.generate('prompt', {}, {})
            self.assertEqual(len(raw.calls), 2)

    def test_exact_conflicting_modalities_reported(self):
        policy = fragment(self.item, 'permission')
        policy['odrlPolicy']['prohibition'] = [dict(policy['odrlPolicy']['permission'][0], uid='r2')]
        self.assertTrue(conflicts(policy))

    def test_parallel_operations_overlap_and_output_order_is_stable(self):
        class Pool:
            workers = 3
            @contextmanager
            def for_chunk(self, identifier):
                yield self
        barrier = Barrier(3)
        def operation(item, worker):
            barrier.wait(timeout=5)
            return item['chunk_id']
        items = [dict(chunk_id=str(i)) for i in range(3)]
        self.assertEqual(map_chunks(items, operation, Pool()), ['0', '1', '2'])

    def test_real_http_clients_overlap_in_classification_generation_and_review(self):
        barrier, requests = Barrier(3), []
        names = ['simulated-a', 'simulated-b', 'simulated-c']
        class Handler(BaseHTTPRequestHandler):
            def send(self, value):
                body = json.dumps(value).encode() + b'\n'
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def do_GET(self):
                self.send(dict(models=[dict(name=name, digest=name, details=dict(family='qwen35')) for name in names]))
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path == '/api/show':
                    self.send(dict(details=dict(family='qwen35'), thinking=dict(values=[True, False])))
                    return
                requests.append(payload)
                barrier.wait(timeout=5)  # Fails if requests are serialized by the client.
                value = json.loads(payload['messages'][1]['content'])
                if 'candidate_rules' in value:
                    answer = reviewed(value)
                elif 'contract_id' in value:
                    identifier = value['clause']['chunk_id']
                    answer = dict(policy=dict(odrlPolicy=dict(obligation=[dict(uid='r1', target='data '+identifier,
                        assigner='Provider', assignee='Provider', action='delete')]), traceability=[dict(ruleUid='r1',
                        ruleType='obligation', sourceClauses=[dict(chunk_id=identifier, relationship='primary')])]),
                        reason='Simulated deletion duty.')
                else:
                    answer = dict(label='RELEVANT')
                self.send(dict(message=dict(content=json.dumps(answer)), done=True, done_reason='stop',
                               eval_count=25, prompt_eval_count=200))
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            pool = OllamaPool([f'http://127.0.0.1:{server.server_port}'], names[0], worker_models=names,
                              worker_gpus=[0, 1, 2], num_ctx=32768, num_predict=4096, timeout=10)
            pool.preflight()
            source = dict(contract_id='contract', chunks=[chunk(f'c{i}', f'Provider must delete data c{i}.') for i in range(3)])
            classified = classify(source, pool, ROOT)
            _, enriched = enrich(classified, ROOT)
            policy, report = generate(enriched, pool, ROOT)
            result = review(policy, enriched, classified, pool, ROOT, generation_report=report)
            self.assertEqual([row['chunk_id'] for row in result['clause_reviews']], ['c0', 'c1', 'c2'])
            self.assertEqual(result['status'], 'reviewed_no_issues_found')
            self.assertEqual(report['summary']['generated'], 3)
            self.assertEqual(len(requests), 9)
            for offset in (0, 3, 6):
                self.assertEqual({p['model'] for p in requests[offset:offset+3]}, set(names))
                self.assertEqual({p['options']['main_gpu'] for p in requests[offset:offset+3]}, {0, 1, 2})
            for payload in requests:
                self.assertIs(payload['think'], False)
                self.assertIs(payload['truncate'], False)
                self.assertIs(payload['shift'], False)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_disabled_review_is_unavailable_and_stale_output_is_invalidated(self):
        from xfed.ui import STAGES
        self.assertNotIn('3', STAGES)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'contracts/contract/input').mkdir(parents=True)
            (root / 'contracts/contract/input/contract.pdf').touch()
            pipeline = Pipeline(root, 'http://127.0.0.1:11434')
            with self.assertRaises(ValueError):
                pipeline.start(dict(contract='contract', stage='3'))
            stale = pipeline.output('contract') / '3 policy_review/policy_review.json'
            write_json(stale, dict(status='reviewed_no_issues_found'))
            pipeline.invalidate('contract', '2')
            self.assertFalse(stale.exists())
            self.assertNotIn('3', pipeline.state('contract')['stages'])

    def test_heading_bypass_does_not_discard_substantive_merged_terms(self):
        heading = dict(clause=chunk(text='7. Confidentiality', heading='7. Confidentiality'), context=[], unresolved_references=[])
        client = Client(dict(label='RELEVANT'))
        self.assertEqual(classify_batch([heading], client, ROOT)[0]['label'], 'NOT_RELEVANT')
        self.assertEqual(len(client.calls), 0)
        heading['clause']['text'] += ' Provider must keep data confidential.'
        self.assertEqual(classify_batch([heading], client, ROOT)[0]['label'], 'RELEVANT')
        self.assertEqual(len(client.calls), 1)

    def test_asset_cannot_be_a_party_and_citation_cannot_be_a_condition(self):
        for field, value in [('assignee', 'Data'), ('target', 'any')]:
            policy = fragment(self.item)
            policy['odrlPolicy']['obligation'][0][field] = value
            with self.assertRaises(ValueError):
                validate_fragment(policy, 'contract', self.item, ROOT)
        policy = fragment(self.item)
        policy['odrlPolicy']['obligation'][0]['constraint'] = [dict(leftOperand='clause_id', operator='eq', rightOperand='1.1')]
        with self.assertRaises(ValueError):
            validate_fragment(policy, 'contract', self.item, ROOT)

    def test_duplicate_rules_with_different_ids_are_rejected(self):
        policy = fragment(self.item)
        duplicate = deepcopy(policy['odrlPolicy']['obligation'][0])
        duplicate['uid'] = 'r2'
        policy['odrlPolicy']['obligation'].append(duplicate)
        trace = deepcopy(policy['traceability'][0])
        trace['ruleUid'] = 'r2'
        policy['traceability'].append(trace)
        with self.assertRaisesRegex(ValueError, 'Duplicate obligation'):
            validate_fragment(policy, 'contract', self.item, ROOT)

    def test_real_benchmark_manifest_counts_and_missing_predictions(self):
        from xfed.evaluation import load_reference, run_evaluation, score_report
        data = load_reference(ROOT)
        self.assertEqual(len(data['classification']), 300)
        self.assertEqual(len(data['policy']), 60)
        for task in ('classification', 'policy'):
            self.assertTrue(all(c['source_type'] == 'real' and 'split' not in c for c in data[task]))
        report = dict(reference_version=data['version'], task='classification', source='real',
                      selected_cases=dict(classification=[c['case_id'] for c in data['classification']]),
                      predictions={}, execution={})
        scored = score_report(data, report, ROOT)
        self.assertEqual(scored['metrics']['classification']['real/coverage']['errors'], 300)
        self.assertEqual(scored['metrics']['classification']['real/coverage']['accuracy'], 0)
        report['selected_cases']['classification'].pop()
        with self.assertRaises(ValueError):
            score_report(data, report, ROOT)


if __name__ == '__main__':
    unittest.main()
