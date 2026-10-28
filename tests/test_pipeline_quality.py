"""Deterministic regressions for evidence preservation and parallel review plumbing.

These tests use simulated model answers; they do not measure model accuracy.
"""

from copy import deepcopy
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Barrier, Event, Thread
from tempfile import TemporaryDirectory
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.error import URLError
import json
import shutil
import unittest

from xfed.common import map_chunks, write_json
from xfed.enrichment import ContextIndex, enrich
from xfed.llm import OllamaClient, OllamaConnectionError, OllamaPool
from xfed.llm.service import OllamaServicePool, nvidia_gpus
from xfed.policy_generation import (clause_decoder_schema, compile_plan, complete_fragment, generate, generate_answer, generate_item,
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
        reason='Simulated review.', missing_rules=[] if value['candidate_rules'] else [dict(requirement='Deletion duty omitted.', primary_quote=value['clause']['text'])])


def plan(item, kind='obligation', **changes):
    rule = dict(kind=kind, actor='Provider', action='delete', asset='supplied data',
                conditions=[dict(leftOperand='elapsedTime', operator='lteq', rightOperand='PT12H'),
                            dict(leftOperand='event', operator='eq', rightOperand='notice')],
                evidence=dict(primary=item['chunk_id'], supporting=[]), duties=[], remedies=[])
    rule.update(changes)
    return dict(rules=[rule], unrepresented=[], reason='Simulated source-backed rule.')


def no_rule_review(value):
    return dict(relevance='NOT_RELEVANT', rule_reviews=[], coverage='NO_RULES_NEEDED',
                reason='Simulated no-rule evidence check.', missing_rules=[])


class PlanClient(Client):
    def __init__(self, answer, check=None):
        super().__init__(answer)
        self.check = check or (lambda value: reviewed(value) if value['candidate_rules'] else no_rule_review(value))

    def generate(self, prompt, value, output_schema, **options):
        if 'candidate_rules' in value:
            self.calls.append((deepcopy(value), options))
            return deepcopy(self.check(value))
        return super().generate(prompt, value, output_schema, **options)


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

    def test_external_articles_cannot_resolve_to_same_numbered_contract_clauses(self):
        for text in ('Processor shall comply with Article 6 GDPR.',
                     'Processor shall comply with GDPR Article 6.',
                     'Processor shall comply with Article 6 of the Data Act.'):
            source = [chunk('c1', text), chunk('c2', '6. Recipient may publish Data.', clause_id='6')]
            context, missing = ContextIndex(source).context_for(source[0])
            self.assertNotIn('c2', [row['chunk_id'] for row in context])
            self.assertTrue(any('external legal reference' in value for value in missing))

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
        completed = complete_fragment(policy, 'contract', self.item)
        validate_fragment(completed, 'contract', self.item, ROOT)
        self.assertNotIn('osdu', completed)
        answer = plan(self.item)
        output_schema = response_schema(ROOT)
        Draft202012Validator(output_schema).validate(answer)
        output, entry, failed = generate_item(self.item, PlanClient(answer), 'prompt', 'contract', output_schema, ROOT)
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
        policy, report = generate(inputs, PlanClient(dict(rules=[], unrepresented=[], reason='Simulated abstention.')), ROOT)
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
        received = []
        self.assertEqual(map_chunks(items, operation, Pool(), completed=lambda item, value:
                                    received.append((item['chunk_id'], value))), ['0', '1', '2'])
        self.assertEqual(sorted(received), [('0', '0'), ('1', '1'), ('2', '2')])

    def test_real_http_clients_overlap_in_classification_generation_and_review(self):
        barrier, requests = Barrier(3), []
        names = ['simulated-model']
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
                    answer = plan(value['clause'], asset='data '+identifier, conditions=[])
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
            pool = OllamaPool([f'http://127.0.0.1:{server.server_port}'], names[0], workers_per_endpoint=3,
                              num_ctx=32768, num_predict=4096, timeout=10)
            pool.preflight()
            source = dict(contract_id='contract', chunks=[chunk(f'c{i}', f'Provider must delete data c{i}.') for i in range(3)])
            classified = classify(source, pool, ROOT)
            _, enriched = enrich(classified, ROOT)
            policy, report = generate(enriched, pool, ROOT)
            result = review(policy, enriched, classified, pool, ROOT, generation_report=report)
            self.assertEqual([row['chunk_id'] for row in result['clause_reviews']], ['c0', 'c1', 'c2'])
            self.assertEqual(result['status'], 'reviewed_no_issues_found')
            self.assertEqual(report['summary']['generated'], 3)
            self.assertEqual(len(requests), 12)
            for offset in (0, 3, 6, 9):
                self.assertEqual({p['model'] for p in requests[offset:offset+3]}, set(names))
                self.assertTrue(all('main_gpu' not in p['options'] for p in requests[offset:offset+3]))
            self.assertEqual(len(pool.clients), 3)
            self.assertEqual(pool.metadata()['completed'], [3, 3, 3])
            self.assertTrue(all(worker['performance']['responses'] == 4 for worker in pool.metadata()['models']))
            for payload in requests:
                self.assertIs(payload['think'], False)
                self.assertIs(payload['truncate'], False)
                self.assertIs(payload['shift'], False)
            with TemporaryDirectory() as directory:
                cache = Path(directory) / 'responses.json'
                cached = CachedClient(pool, cache, Event(), False)
                for worker in pool.clients:
                    worker.verify_placement = MagicMock(side_effect=RuntimeError('GPU placement mismatch'))
                with self.assertRaisesRegex(RuntimeError, 'GPU placement mismatch'):
                    classify(source, cached, ROOT)
                self.assertFalse(cache.exists())  # Wrongly placed responses must not become reusable answers.
                self.assertTrue(all(worker.verify_placement.called for worker in pool.clients))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_schema_grammar_rejection_recovers_with_three_workers_and_validates_json(self):
        barrier, requests = Barrier(3), []
        grammar_error = dict(error=dict(code=400,
            message='Failed to initialize samplers: failed to parse grammar', type='invalid_request_error'))

        class Handler(BaseHTTPRequestHandler):
            def send(self, value, status=200):
                body = json.dumps(value).encode() + b'\n'
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self.send(dict(models=[dict(name='simulated-model', digest='digest', details=dict(family='qwen35'))]))

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path == '/api/show':
                    self.send(dict(details=dict(family='qwen35'), thinking=dict(values=[True, False])))
                    return
                requests.append(payload)
                value = json.loads(payload['messages'][1]['content'])
                forced = value.get('force_error')
                if forced == 'grammar':
                    self.send(grammar_error, 400)
                    return
                if forced:
                    self.send(dict(error='llama-server process has terminated' if forced == 500 else 'invalid request'), forced)
                    return
                if isinstance(payload['format'], dict):
                    barrier.wait(timeout=5)
                    self.send(grammar_error, 400)
                    return
                answer = ({} if value.get('invalid_answer') else reviewed(value)
                          if 'candidate_rules' in value else plan(value['clause']))
                self.send(dict(message=dict(content=json.dumps(answer)), done=True, done_reason='stop',
                               eval_count=25, prompt_eval_count=200))

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            pool = OllamaPool([base], 'simulated-model', workers_per_endpoint=3,
                              num_ctx=32768, num_predict=4096, timeout=10)
            pool.preflight()
            items = [dict(chunk(f'c{i}'), context=[]) for i in range(3)]
            for _ in range(2):
                policy, report = generate(dict(contract_id='contract', items=items), pool, ROOT)
                self.assertEqual(report['summary']['generated'], 3)
                self.assertEqual(len(policy['odrlPolicy']['obligation']), 3)
            structured = [value for value in requests if isinstance(value['format'], dict)]
            recovered = [value for value in requests if value['format'] == 'json']
            self.assertEqual(len(structured), 3)  # One rejected schema per worker, not per clause.
            self.assertEqual(len(recovered), 12)  # Both extraction and evidence review run twice.
            for payload in recovered:
                prompt = payload['messages'][0]['content']
                marker = 'Return exactly one JSON object conforming to this JSON schema:\n'
                self.assertIn(marker, prompt)
                supplied_schema = json.loads(prompt.split(marker, 1)[1])
                self.assertEqual(supplied_schema['type'], 'object')
                self.assertFalse(supplied_schema['additionalProperties'])
                if 'candidate_rules' not in json.loads(payload['messages'][1]['content']):
                    supplied_clause = json.loads(payload['messages'][1]['content'])['clause']
                    expected_schema = clause_decoder_schema(response_schema(ROOT), supplied_clause)
                    self.assertEqual(supplied_schema, expected_schema)
                self.assertIs(payload['truncate'], False)
                self.assertIs(payload['shift'], False)
            for worker in pool.metadata()['models']:
                self.assertEqual(worker['response_format'], 'json')
                self.assertEqual(worker['schema_grammar_fallbacks'], 1)
                self.assertIn('failed to parse grammar', worker['schema_grammar_error'])
            with self.assertRaises(ValidationError):
                pool.clients[0].generate('prompt', dict(invalid_answer=True), response_schema(ROOT))
            for status in (400, 500):
                client = OllamaClient(base, 'simulated-model', timeout=10)
                previous = len(requests)
                with self.assertRaisesRegex(RuntimeError, f'Ollama HTTP {status}'):
                    client.generate('prompt', dict(force_error=status), response_schema(ROOT))
                self.assertEqual(len(requests), previous + 1)
                self.assertEqual(client.response_format, 'schema')
                self.assertEqual(client.schema_grammar_fallbacks, 0)
            client = OllamaClient(base, 'simulated-model', timeout=10)
            previous = len(requests)
            with self.assertRaisesRegex(RuntimeError, 'failed to parse grammar'):
                client.generate('prompt', dict(force_error='grammar'), response_schema(ROOT))
            self.assertEqual(len(requests), previous + 2)  # Stop if JSON mode also fails.
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_managed_workers_isolate_gpus_and_release_owned_servers(self):
        def preflight(client):
            client.model_digest = 'same-digest'
        for gpus, servers, parallel in [(None, 1, '3'), ([0, 2, 4], 3, '1')]:
            with self.subTest(gpus=gpus):
                processes = [MagicMock() for _ in range(servers)]
                for process in processes:
                    process.poll.return_value = None
                pending = iter(enumerate(processes))
                def start(command, **kwargs):
                    index, process = next(pending)
                    kwargs['stdout'].write(f'XFED_OLLAMA_PID={10000 + index}\n'.encode())
                    kwargs['stdout'].flush()
                    return process
                with patch('xfed.llm.service.os', SimpleNamespace(name='posix')), \
                     patch.object(OllamaServicePool, '_environment', return_value={'OLLAMA_MODELS': '/existing/models',
                                  'OLLAMA_VULKAN': '1', 'OLLAMA_LLM_LIBRARY': 'vulkan'}), \
                     patch('xfed.llm.service.nvidia_gpus', return_value=[dict(index=i, uuid=f'GPU-physical-{i}') for i in range(5)]), \
                     patch('xfed.llm.service.subprocess.Popen', side_effect=start) as launch, \
                     patch('xfed.llm.service.subprocess.run', return_value=SimpleNamespace(returncode=0)) as stop, \
                     patch('xfed.llm.service.urlopen', side_effect=lambda *a, **k: BytesIO(b'{}')), \
                     patch('xfed.llm.OllamaClient.preflight', preflight):
                    pool = OllamaServicePool(['http://localhost:11434'], 'same-model', worker_count=3, worker_gpus=gpus)
                    metadata = pool.preflight()
                    self.assertEqual(metadata['workers'], 3)
                    self.assertEqual(metadata['model_digest'], 'same-digest')
                    self.assertEqual([c.model for c in pool.clients], ['same-model'] * 3)
                    self.assertEqual(metadata['worker_gpus'], gpus or [None] * 3)
                    for index, call in enumerate(launch.call_args_list):
                        command = call.args[0]
                        self.assertEqual(command[:9], ['sudo', '-n', '-H', '-u', 'ollama', '--', 'setsid', '--wait', 'sh'])
                        self.assertIn('OLLAMA_MODELS=/existing/models', command)
                        self.assertIn('OLLAMA_NUM_PARALLEL=' + parallel, command)
                        self.assertEqual(command[-2:], ['/local/ollama/bin/ollama', 'serve'])
                        if gpus:
                            self.assertIn('CUDA_VISIBLE_DEVICES=GPU-physical-' + str(gpus[index]), command)
                            self.assertIn('CUDA_DEVICE_ORDER=PCI_BUS_ID', command)
                            self.assertIn('OLLAMA_VULKAN=0', command)
                            self.assertIn('GGML_VK_VISIBLE_DEVICES=-1', command)
                            self.assertNotIn('OLLAMA_LLM_LIBRARY=vulkan', command)
                            self.assertEqual(pool.clients[index].gpu_uuid, f'GPU-physical-{gpus[index]}')
                        else:
                            self.assertFalse(any(arg.startswith('CUDA_VISIBLE_DEVICES=') for arg in command))
                    self.assertTrue(all(c.main_gpu is None for c in pool.clients))
                    pool.close()
                    pool.close()
                    self.assertEqual(stop.call_count, servers)
                    self.assertEqual({call.args[0][-1] for call in stop.call_args_list},
                                     {'-' + str(10000 + i) for i in range(servers)})
                    for process in processes:
                        process.terminate.assert_not_called()
                        process.wait.assert_called_once()

    def test_failed_worker_startup_cleans_up_earlier_workers(self):
        processes = [MagicMock(), MagicMock()]
        processes[0].poll.return_value = None
        processes[1].poll.return_value = 1
        pending = iter(processes)
        def start(command, **kwargs):
            kwargs['stdout'].write(b'XFED_OLLAMA_PID=10000\n')
            kwargs['stdout'].flush()
            return next(pending)
        with patch('xfed.llm.service.os', SimpleNamespace(name='posix')), \
             patch.object(OllamaServicePool, '_environment', return_value={}), \
             patch('xfed.llm.service.nvidia_gpus', return_value=[dict(index=i, uuid=f'GPU-{i}') for i in range(2)]), \
             patch('xfed.llm.service.subprocess.Popen', side_effect=start), \
             patch('xfed.llm.service.subprocess.run', return_value=SimpleNamespace(returncode=0)) as stop, \
             patch('xfed.llm.service.urlopen', side_effect=lambda *a, **k: BytesIO(b'{}')), \
             patch('xfed.llm.OllamaClient.preflight'):
            pool = OllamaServicePool(['http://localhost:11434'], 'same-model', worker_count=2, worker_gpus=[0, 1])
            with self.assertRaisesRegex(RuntimeError, 'Could not start'):
                pool.preflight()
            self.assertEqual(stop.call_count, 1)
            processes[0].wait.assert_called_once()
            self.assertEqual(pool._servers, [])

    def test_managed_gpu_validation_and_startup_cancellation(self):
        for gpus in ([0, 0], [0, None], [0], [True, 1]):
            with self.subTest(gpus=gpus), self.assertRaises(ValueError):
                OllamaServicePool(['http://localhost:11434'], 'same-model', worker_count=2, worker_gpus=gpus)
        cancel = Event()
        cancel.set()
        with patch('xfed.llm.service.subprocess.Popen') as launch:
            pool = OllamaServicePool(['http://localhost:11434'], 'same-model', worker_count=2, cancel=cancel)
            with self.assertRaises(InterruptedError):
                pool.preflight()
            launch.assert_not_called()

    def test_cancel_during_worker_startup_stops_the_started_process_group(self):
        cancel, process = Event(), MagicMock()
        process.poll.return_value = None
        def start(command, **kwargs):
            kwargs['stdout'].write(b'XFED_OLLAMA_PID=10000\n')
            kwargs['stdout'].flush()
            return process
        def waiting(*args, **kwargs):
            cancel.set()
            raise URLError('Not ready yet')
        with patch('xfed.llm.service.os', SimpleNamespace(name='posix')), \
             patch.object(OllamaServicePool, '_environment', return_value={}), \
             patch('xfed.llm.service.nvidia_gpus', return_value=[dict(index=i, uuid=f'GPU-{i}') for i in range(2)]), \
             patch('xfed.llm.service.subprocess.Popen', side_effect=start) as launch, \
             patch('xfed.llm.service.subprocess.run', return_value=SimpleNamespace(returncode=0)) as stop, \
             patch('xfed.llm.service.urlopen', side_effect=waiting), \
             patch('xfed.llm.OllamaClient.preflight'):
            pool = OllamaServicePool(['http://localhost:11434'], 'same-model', worker_count=2,
                                     worker_gpus=[0, 1], cancel=cancel)
            with self.assertRaises(InterruptedError):
                pool.preflight()
            launch.assert_called_once()
            self.assertEqual(stop.call_args.args[0][-3:], ['-TERM', '--', '-10000'])
            self.assertEqual(pool._servers, [])

    def test_workers_discover_service_model_store_and_allow_an_explicit_override(self):
        result = SimpleNamespace(returncode=0, stdout='"OLLAMA_MODELS=/existing/model store" "CUDA_VISIBLE_DEVICES=1,3"')
        with patch.dict('sys.modules', pwd=SimpleNamespace()), \
             patch('xfed.llm.service.subprocess.run', return_value=result) as read:
            pool = OllamaServicePool(['http://localhost:11434'], 'same-model', worker_count=2)
            environment = pool._environment()
            self.assertEqual(environment['OLLAMA_MODELS'], '/existing/model store')
            self.assertEqual(environment['CUDA_VISIBLE_DEVICES'], '1,3')
            self.assertEqual(read.call_args.args[0][0], 'systemctl')  # No root sudo is needed.
            pool = OllamaServicePool(['http://localhost:11434'], 'same-model', worker_count=2,
                                     service=dict(models_dir='/override/models'))
            self.assertEqual(pool._environment()['OLLAMA_MODELS'], '/override/models')

    def test_gpu_inventory_uses_physical_indices_and_rejects_missing_devices(self):
        with patch('xfed.llm.service.subprocess.run', return_value=SimpleNamespace(stdout='2, GPU-b\n0, GPU-a\n')):
            self.assertEqual(nvidia_gpus(), [dict(index=0, uuid='GPU-a'), dict(index=2, uuid='GPU-b')])
        for output in ('', '0, GPU-a\n1, GPU-a\n', '0, unknown\n'):
            with self.subTest(output=output), patch('xfed.llm.service.subprocess.run', return_value=SimpleNamespace(stdout=output)):
                with self.assertRaisesRegex(RuntimeError, 'Cannot discover'):
                    nvidia_gpus()
        with patch('xfed.llm.service.os', SimpleNamespace(name='posix')), \
             patch.object(OllamaServicePool, '_environment', return_value={}), \
             patch('xfed.llm.service.nvidia_gpus', return_value=[dict(index=0, uuid='GPU-a')]), \
             patch('xfed.llm.service.subprocess.Popen') as launch:
            pool = OllamaServicePool(['http://localhost:11434'], 'same-model', worker_count=2, worker_gpus=[0, 1])
            with self.assertRaisesRegex(ValueError, 'At most 1 workers'):
                pool._start()
            launch.assert_not_called()

    def test_gpu_verification_checks_worker_descendants_and_rejects_wrong_gpu_or_cpu(self):
        with TemporaryDirectory() as directory:
            log = Path(directory) / 'worker.log'
            log.write_text('XFED_OLLAMA_PID=10000\n', encoding='utf-8')
            pool = OllamaServicePool(['http://localhost:11434'], 'same-model', worker_count=2, worker_gpus=[0, 2])
            process = MagicMock()
            process.poll.return_value = None
            pool._servers = [(process, log)]
            pool.gpu_devices = [dict(index=0, uuid='GPU-a'), dict(index=2, uuid='GPU-b')]
            client = pool.clients[0]
            client.gpu_uuid = 'GPU-a'
            client.model_digest = 'same-digest'
            # Runner is a grandchild; an unrelated model on GPU-b is deliberately ignored.
            parents = SimpleNamespace(stdout='10000 10\n10001 10000\n10002 10001\n20000 20\n')
            for applications, accepted in [('10002, GPU-a\n20000, GPU-b\n', True),
                                           ('10002, GPU-b\n', False),
                                           ('10002, GPU-a\n10002, GPU-b\n', False),
                                           ('20000, GPU-a\n', False)]:
                residency = dict(models=[dict(name='same-model', digest='same-digest', size_vram=1024)])
                with self.subTest(applications=applications), patch('xfed.llm.service.subprocess.run',
                        side_effect=[parents, SimpleNamespace(stdout=applications)]), \
                     patch('xfed.llm.service.urlopen', side_effect=lambda *a, **k: BytesIO(json.dumps(residency).encode())):
                    if accepted:
                        pool._verify_gpu(client, 0)
                        self.assertEqual(client.placement_status, 'verified')
                        self.assertEqual(client.observed_gpus, [0])
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'GPU placement verification failed'):
                            pool._verify_gpu(client, 0)
                        self.assertEqual(client.placement_status, 'failed')
                        self.assertTrue(client.placement_error)
            for residency in (dict(models=[]), dict(models=[dict(name='same-model', digest='same-digest', size_vram=0)])):
                with self.subTest(residency=residency), \
                     patch('xfed.llm.service.subprocess.run', side_effect=[parents, SimpleNamespace(stdout='10000, GPU-a\n')]), \
                     patch('xfed.llm.service.urlopen', side_effect=lambda *a, **k: BytesIO(json.dumps(residency).encode())):
                    with self.assertRaisesRegex(RuntimeError, 'does not report the selected model resident'):
                        pool._verify_gpu(client, 0)
            pool._servers = []  # These are simulated PIDs, never signal them.

    def test_dashboard_single_installed_model_can_have_parallel_workers(self):
        pipeline = Pipeline(ROOT, 'http://localhost:11434')
        with patch.object(pipeline, 'models', return_value=[dict(name='only-model')]), \
             patch.object(OllamaServicePool, 'preflight') as preflight:
            pool = pipeline.make_client(dict(model='only-model', parallel=True, worker_count=3))
            self.assertEqual(pool.workers, 3)
            self.assertEqual([c.model for c in pool.clients], ['only-model'] * 3)
            preflight.assert_not_called()  # Startup belongs to the cancellable background job.
            pipeline.active_client = pool
            with patch.object(pool, 'close') as close, patch.object(pool, 'unload') as unload:
                pipeline.finish('complete', 'Finished')
                close.assert_called_once()
                unload.assert_not_called()
                self.assertEqual(pipeline.snapshot()['status'], 'complete')

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

    def test_reset_removes_all_generated_json_for_only_the_selected_contract(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('contract', 'other'):
                source = root / 'contracts' / name / 'input/contract.pdf'
                source.parent.mkdir(parents=True)
                source.write_bytes(b'source PDF')
            pipeline = Pipeline(root, 'http://localhost:11434')
            generated = [pipeline.path('contract', stage) for stage in pipeline.state('contract')['stages']]
            generated += [pipeline.output('contract') / name for name in (
                '.runs.json', '.llm_cache.json', 'policy.json',
                '2 policy_generation/generation_report.json', '3 policy_review/policy_review.json')]
            for path in generated:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('broken JSON', encoding='utf-8')  # Reset also recovers unreadable records.
            preserved = [root / 'schemas/schema.json', root / 'evaluation/reference.json',
                         root / 'evaluation/runs/report.json', pipeline.output('other') / 'policy.json',
                         pipeline.output('contract') / '.gitkeep']
            for path in preserved:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('keep', encoding='utf-8')
            pipeline.job.update(status='complete', contract='contract', stage='2')
            pipeline.benchmark_report = dict(contract='contract')
            pipeline.evaluation_report = dict(run_id='saved-evaluation')
            state = pipeline.reset(dict(contract='contract'))
            self.assertFalse(any(path.exists() for path in generated))
            self.assertTrue(all(path.read_text(encoding='utf-8') == 'keep' for path in preserved))
            self.assertEqual((root / 'contracts/contract/input/contract.pdf').read_bytes(), b'source PDF')
            self.assertTrue(all(not stage['ready'] and stage['run'] is None for stage in state['stages'].values()))
            self.assertIsNone(state['report'])
            self.assertEqual(state['job']['status'], 'idle')
            self.assertIsNone(pipeline.benchmark_report)
            self.assertEqual(pipeline.evaluation_report, dict(run_id='saved-evaluation'))
            pipeline.reset(dict(contract='contract'))  # An already empty contract can be reset again.

    def test_reset_rejects_active_jobs_unknown_contracts_and_other_output_paths(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'contracts/contract/input/contract.pdf'
            source.parent.mkdir(parents=True)
            source.touch()
            pipeline = Pipeline(root, 'http://localhost:11434')
            saved = pipeline.output('contract') / 'policy.json'
            saved.parent.mkdir()
            saved.write_text('{}', encoding='utf-8')
            for status in ('running', 'cancelling'):
                pipeline.job.update(status=status, contract='other')
                with self.assertRaisesRegex(ValueError, 'current job'):
                    pipeline.reset(dict(contract='contract'))
                self.assertTrue(saved.exists())
            pipeline.job['status'] = 'idle'
            with self.assertRaisesRegex(ValueError, 'Unknown contract'):
                pipeline.reset(dict(contract='../contract'))
            with patch.object(pipeline, 'output', return_value=root / 'schemas'):
                with self.assertRaisesRegex(ValueError, 'output directory'):
                    pipeline.reset(dict(contract='contract'))
            self.assertTrue(saved.exists())

    def test_reset_endpoint_clears_records_and_returns_empty_stage_state(self):
        from urllib.request import Request, urlopen
        from xfed.ui import handler
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'contracts/contract/input/contract.pdf'
            source.parent.mkdir(parents=True)
            source.touch()
            pipeline = Pipeline(root, 'http://localhost:11434')
            saved = pipeline.output('contract') / '.llm_cache.json'
            write_json(saved, dict(cached='response'))
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler(pipeline))
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                request = Request(f'http://127.0.0.1:{server.server_port}/api/reset',
                                  data=json.dumps(dict(contract='contract')).encode(),
                                  headers={'Content-Type': 'application/json'})
                with urlopen(request, timeout=5) as response:
                    state = json.load(response)
                self.assertFalse(saved.exists())
                self.assertEqual(state['contract'], 'contract')
                self.assertTrue(all(not stage['ready'] for stage in state['stages'].values()))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

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

    def test_rule_plan_requires_literal_primary_evidence_and_preserves_conditions(self):
        answer = plan(self.item)
        Draft202012Validator(response_schema(ROOT)).validate(answer)
        output = compile_plan(answer, 'contract', self.item)
        validate_fragment(output, 'contract', self.item, ROOT)
        rule = output['odrlPolicy']['obligation'][0]
        self.assertEqual(rule['assignee'], 'Provider')
        self.assertEqual(rule['constraint'], answer['rules'][0]['conditions'])
        self.assertEqual(output['traceability'][0]['sourceClauses'][0]['text'], self.item['text'])
        for evidence in ([dict(chunk_id='c1', quote='Provider may keep the data.')],
                         [dict(chunk_id='context', quote='Definition of Data.')]):
            item = dict(self.item, context=[dict(chunk('context', 'Definition of Data.'), relationship='definition')])
            answer['rules'][0]['evidence'] = evidence
            with self.assertRaisesRegex(ValueError, 'evidence|primary'):
                compile_plan(answer, 'contract', item)

    def test_nested_duties_keep_their_actors_and_duplicates_are_rejected(self):
        item = dict(self.item, text='Recipient may use supplied data after Provider notifies Operator.')
        answer = plan(item, 'permission', actor='Recipient', action='use', conditions=[])
        duty = dict(actor='Provider', action='notify', asset='supplied data',
                    conditions=[dict(leftOperand='recipient', operator='eq', rightOperand='Operator')],
                    evidence=[dict(chunk_id='c1', quote=item['text'])])
        answer['rules'][0]['duties'] = [duty]
        output = compile_plan(answer, 'contract', item)
        validate_fragment(output, 'contract', item, ROOT)
        self.assertEqual(output['odrlPolicy']['permission'][0]['duty'][0]['assignee'], 'Provider')
        answer['rules'][0]['duties'].append(deepcopy(duty))
        with self.assertRaisesRegex(ValueError, 'Duplicate duty'):
            validate_fragment(compile_plan(answer, 'contract', item), 'contract', item, ROOT)

    def test_wrong_modality_is_repaired_using_fresh_evidence_feedback(self):
        attempts = []
        def answer(value):
            attempts.append(value)
            return plan(self.item, 'permission' if len(attempts) == 1 else 'obligation')
        def check(value):
            response = reviewed(value)
            if value['candidate_rules'][0]['rule_type'] == 'permission':
                response.update(coverage='MISSING_RULES', missing_rules=[dict(requirement='Provider must delete within 12 hours after notice.', primary_quote=value['clause']['text'])])
                response['rule_reviews'][0].update(verdict='CONTRADICTED', reason='Must delete is a duty, not a permission.')
            return response
        client = PlanClient(answer, check)
        output, entry, failed = generate_item(self.item, client, 'prompt', 'contract', response_schema(ROOT), ROOT)
        self.assertFalse(failed)
        self.assertIn('obligation', output['odrlPolicy'])
        self.assertEqual(len(client.calls), 4)
        self.assertEqual(entry['generation_attempts'], 2)
        self.assertIn('not a permission', entry['correction_reason'])
        self.assertTrue(entry['semantic_check']['same_model_as_generator'])
        self.assertIn('correction', client.calls[2][0])

    def test_semantically_wrong_rules_are_rejected_despite_valid_json(self):
        def check(value):
            response = reviewed(value)
            response['rule_reviews'][0].update(verdict='CONTRADICTED', reason='The source forbids this action.')
            return response
        output, entry, failed = generate_item(self.item, PlanClient(plan(self.item, 'permission'), check),
                                             'prompt', 'contract', response_schema(ROOT), ROOT)
        self.assertIsNone(output)
        self.assertTrue(failed)
        self.assertEqual(entry['semantic_check']['rule_reviews'][0]['verdict'], 'CONTRADICTED')

    def test_rejected_plans_and_checks_are_evicted_from_the_response_cache(self):
        def check(value):
            response = reviewed(value)
            response['rule_reviews'][0].update(verdict='CONTRADICTED', reason='Wrong actor.')
            return response
        with TemporaryDirectory() as directory:
            raw = PlanClient(plan(self.item), check)
            cached = CachedClient(raw, Path(directory) / 'responses.json', Event(), False)
            output, entry, failed = generate_item(self.item, cached, 'prompt', 'contract', response_schema(ROOT), ROOT)
            self.assertTrue(failed)
            self.assertIsNone(output)
            self.assertEqual(cached.cache, {})
            self.assertEqual(len(raw.calls), 4)

    def test_unknown_issuer_uses_a_set_and_explicit_issuer_can_form_an_agreement(self):
        answer = plan(self.item, 'permission')
        output = compile_plan(answer, 'contract', self.item)
        self.assertEqual(output['odrlPolicy']['@type'], 'Set')
        validate_fragment(output, 'contract', self.item, ROOT)
        answer['rules'][0]['issuer'] = 'Provider'
        output = compile_plan(answer, 'contract', self.item)
        self.assertEqual(output['odrlPolicy']['@type'], 'Agreement')
        validate_fragment(output, 'contract', self.item, ROOT)
        output['odrlPolicy']['permission'][0].pop('assigner')
        with self.assertRaises(ValidationError):
            validate_fragment(output, 'contract', self.item, ROOT)

    def test_rule_plan_keeps_optional_osdu_separate_and_drops_empty_metadata(self):
        answer = dict(plan(self.item), osdu={})
        Draft202012Validator(response_schema(ROOT)).validate(answer)
        self.assertNotIn('osdu', compile_plan(answer, 'contract', self.item))
        item = dict(self.item, text=self.item['text'] + ' LegalTag project-data; country NO.')
        answer = dict(plan(item), osdu=dict(legal=dict(legaltags=['project-data'], otherRelevantDataCountries=['NO'])))
        output = compile_plan(answer, 'contract', item)
        validate_fragment(output, 'contract', item, ROOT)
        self.assertEqual(output['osdu'], answer['osdu'])

    def test_unknown_reference_does_not_block_an_unaffected_standalone_rule(self):
        item = dict(self.item, unresolved_references=['88: external details unavailable'])
        output, entry, failed = generate_item(item, PlanClient(plan(item)), 'prompt',
                                             'contract', response_schema(ROOT), ROOT)
        self.assertFalse(failed)
        self.assertIsNotNone(output)
        self.assertEqual(entry['semantic_check']['unresolved_references'], item['unresolved_references'])

    def test_generation_rejects_forged_supporting_context_before_inference(self):
        item = dict(self.item, context=[dict(chunk('c1', 'Forged permission.'), relationship='definition')])
        client = PlanClient(plan(item))
        with self.assertRaisesRegex(ValueError, 'supporting evidence'):
            generate(dict(contract_id='contract', items=[item]), client, ROOT, classified=self.classified)
        self.assertEqual(client.calls, [])

    def test_false_abstention_gets_a_coverage_correction(self):
        attempts = []
        def answer(value):
            attempts.append(value)
            return dict(rules=[], unrepresented=[], reason='No rules.') if len(attempts) == 1 else plan(self.item)
        client = PlanClient(answer, reviewed)
        output, entry, failed = generate_item(self.item, client, 'prompt', 'contract', response_schema(ROOT), ROOT)
        self.assertFalse(failed)
        self.assertIsNotNone(output)
        self.assertEqual(entry['generation_attempts'], 2)
        self.assertIn('Deletion duty omitted', entry['correction_reason'])

    def test_partial_coverage_is_explicit_and_failed_checks_cannot_pass(self):
        def check(value):
            return dict(reviewed(value), coverage='MISSING_RULES', missing_rules=[dict(requirement='Missing acknowledgement duty.', primary_quote=value['clause']['text'])])
        output, entry, failed = generate_item(self.item, PlanClient(plan(self.item), check),
                                             'prompt', 'contract', response_schema(ROOT), ROOT)
        self.assertFalse(failed)
        self.assertIsNotNone(output)
        self.assertEqual(entry['outcome'], 'partial_needs_review')
        def missing_reviews(value):
            return dict(reviewed(value), rule_reviews=[])
        output, entry, failed = generate_item(self.item, PlanClient(plan(self.item), missing_reviews),
                                             'prompt', 'contract', response_schema(ROOT), ROOT)
        self.assertTrue(failed)
        self.assertIsNone(output)
        self.assertIn('error', entry['semantic_check'])

    def test_supported_partial_rules_survive_a_repair_token_failure(self):
        attempts = []
        def answer(value):
            attempts.append(value)
            if len(attempts) > 1:
                raise ValueError('Ollama output token limit reached')
            return plan(self.item)
        def check(value):
            return dict(reviewed(value), coverage='MISSING_RULES', missing_rules=[dict(requirement='Acknowledgement duty.', primary_quote=value['clause']['text'])])
        client = PlanClient(answer, check)
        client.last_request = dict(thinking_chars=10, answer_chars=0)
        output, entry, failed = generate_item(self.item, client, 'prompt', 'contract', response_schema(ROOT), ROOT)
        self.assertFalse(failed)
        self.assertIsNotNone(output)
        self.assertEqual(entry['outcome'], 'partial_needs_review')
        self.assertIn('token limit', entry['repair_error'])

    def test_reviewed_missing_evidence_is_an_abstention_not_a_transport_failure(self):
        answer = dict(rules=[], unrepresented=[dict(evidence=[dict(chunk_id='c1')],
                      reason='Responsible actor definition missing.')], reason='Cannot safely encode.')
        check = lambda value: dict(no_rule_review(value), relevance='RELEVANT', coverage='NEEDS_CONTEXT')
        output, entry, failed = generate_item(self.item, PlanClient(answer, check), 'prompt',
                                             'contract', response_schema(ROOT), ROOT)
        self.assertFalse(failed)
        self.assertIsNone(output)
        self.assertEqual(entry['outcome'], 'abstained_needs_review')

    def test_uncertain_clauses_reach_generation_and_exclusions_are_accounted_for(self):
        classified = dict(contract_id='contract', chunks=[
            dict(chunk('c1'), classification=dict(label='NEEDS_CONTEXT')),
            dict(chunk('c2', 'Invoice payment.'), classification=dict(label='NOT_RELEVANT', reason='Payment only.'))])
        _, enriched = enrich(classified, ROOT)
        self.assertEqual([item['chunk_id'] for item in enriched['items']], ['c1'])
        output, report = generate(enriched, PlanClient(plan(enriched['items'][0])), ROOT, classified=classified)
        self.assertIsNotNone(output)
        self.assertEqual(report['source_coverage'], dict(total_chunks=2, submitted_chunks=1,
            not_submitted=[dict(chunk_id='c2', label='NOT_RELEVANT', reason='Payment only.')]))

    def test_contract04_table_scope_and_blank_date_fields(self):
        from xfed.extraction import extract_document
        from xfed.segmentation import segment
        document = extract_document(ROOT / 'contracts/contract_04/input/contract.pdf', 'contract_04', ROOT)
        chunks = segment(document, ROOT)['chunks']
        index = ContextIndex(chunks, document['blocks'])
        restriction = next(row for row in chunks if row['text'].startswith('Project use only. No external licensing'))
        block = next(row for row in document['blocks'] if row['block_id'] in restriction['source']['block_ids'])
        self.assertIn('table_id', block)
        self.assertIn('table_row', block)
        context, _ = index.context_for(restriction)
        texts = '\n'.join(row['text'] for row in context)
        self.assertIn('B03 Operator geological', texts)
        self.assertIn('Operator; pre-existing well-interval', texts)
        self.assertIn('Holder and scope', texts)
        self.assertIn('They do not override the dataset restrictions', texts)
        self.assertNotIn('Supplier; pre-existing HW-710', texts)
        self.assertNotIn('date', index.definitions)


    def test_split_manifest_counts_and_missing_predictions(self):
        from xfed.evaluation import digest, load_reference, score_report
        data = load_reference(ROOT)
        for split, size, cohort in [('dev', 300, 'real/coverage'), ('test', 76, 'real/full_document')]:
            selected = [c['case_id'] for c in data['classification'] if c['split'] == split]
            self.assertEqual(len(selected), size)
            report = dict(reference_version=data['version'], reference_sha256=digest(data),
                          split=split, task='classification', source='real',
                          selected_cases=dict(classification=selected), predictions={}, execution={})
            scored = score_report(data, report, ROOT)
            self.assertEqual(scored['metrics']['classification'][cohort]['errors'], size)
            self.assertEqual(scored['metrics']['classification'][cohort]['accuracy'], 0)
            report['selected_cases']['classification'].pop()
            with self.assertRaises(ValueError):
                score_report(data, report, ROOT)

    def test_reference_hash_detects_changed_gold_without_version_change(self):
        from xfed.evaluation import digest, load_reference, score_report
        data = load_reference(ROOT)
        report = dict(reference_version=data['version'], reference_sha256=digest(data), split='dev')
        data['classification'][0]['gold']['label'] = 'NEEDS_CONTEXT'
        with self.assertRaisesRegex(ValueError, 'content or annotations changed'):
            score_report(data, report, ROOT)

    def test_cases_cannot_cross_their_contract_split(self):
        from unittest.mock import patch
        from xfed.evaluation import load_reference
        data = load_reference(ROOT)
        data['classification'][0]['split'] = 'test'
        with patch.object(Path, 'read_text', return_value=json.dumps(data)):
            with self.assertRaisesRegex(ValueError, 'source contract split'):
                load_reference(ROOT)

    def test_both_split_runners_and_changed_inference_detection(self):
        from unittest.mock import patch
        from xfed.evaluation import load_reference, run_evaluation
        class Runner(Client):
            num_ctx = 131072
            def preflight(self): pass
            def reset_assignments(self): pass
            def metadata(self): return dict(model=self.model)
        def answer(value):
            if 'candidate_rules' in value:
                return no_rule_review(value)
            return dict(rules=[], unrepresented=[], reason='Simulated abstention.') if 'contract_id' in value else dict(label='RELEVANT')
        data = load_reference(ROOT)
        for split, count in [('dev', 300), ('test', 76)]:
            report = run_evaluation(data, Runner(answer), ROOT, split=split)
            self.assertEqual(len(report['predictions']['classification']), count)
            self.assertEqual(report['status'], 'complete')
            self.assertTrue(report['run_id'])
            self.assertTrue(report['inference_sha256'])
            self.assertEqual(set(report['case_manifest']['classification']), set(report['selected_cases']['classification']))
            self.assertTrue(all(row['input']['clause']['chunk_id'] for row in report['case_manifest']['classification'].values()))
            self.assertTrue(report['test_exposure'] if split == 'test' else report['test_exposure'] is None)
        with patch('xfed.evaluation.inference_files', side_effect=[{}, {'changed': '1'}]):
            with self.assertRaisesRegex(ValueError, 'changed during evaluation'):
                run_evaluation(data, Runner(answer), ROOT, task='classification')

    def test_dashboard_test_only_and_reports_survive_restart(self):
        from unittest.mock import patch
        from xfed.evaluation import load_reference, reference_summary
        summary = reference_summary(load_reference(ROOT), split='test')
        self.assertEqual(summary['counts']['classification']['total'], 76)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline = Pipeline(root, 'http://127.0.0.1:11434')
            with self.assertRaisesRegex(ValueError, 'test set only'):
                pipeline.start_evaluation(dict(split='dev'))
            report = dict(run_id='simulated-test-run', split='test', status='complete', task='classification',
                          metrics={}, reference_status='provisional')
            with patch('xfed.ui.run_evaluation', return_value=report):
                pipeline.run_evaluation({}, object(), dict(split='test'))
            saved = root / 'evaluation/runs/simulated-test-run.json'
            self.assertTrue(saved.exists())
            restarted = Pipeline(root, 'http://127.0.0.1:11434')
            self.assertEqual(restarted.evaluation_report, report)
            restarted.mark_evaluation_stopped('cancelled', 'Simulated cancellation')
            restored = Pipeline(root, 'http://127.0.0.1:11434')
            self.assertEqual(restored.evaluation_report['status'], 'cancelled')
            self.assertEqual(restored.evaluation_report['error'], 'Simulated cancellation')

    def test_contract05_retains_all_classification_source_chunks(self):
        from xfed.evaluation import digest, load_reference, reference_summary
        from xfed.extraction import extract_document
        from xfed.segmentation import segment
        data = load_reference(ROOT)
        self.assertEqual(set(reference_summary(data)['sources']), {'contract_05'})
        self.assertEqual({key for key, source in data['sources'].items() if source['split'] == 'dev'},
                         {'contract_01', 'contract_02', 'contract_03'})
        segmented = segment(extract_document(ROOT / data['sources']['contract_05']['pdf'], 'contract_05', ROOT), ROOT)
        inputs = {key: value for key, value in data['inputs'].items() if value['contract_id'] == 'contract_05'}
        self.assertEqual([value['clause'] for value in inputs.values()], segmented['chunks'])
        self.assertEqual(digest(segmented), data['sources']['contract_05']['segmentation_content_sha256'])
        for task in ('classification',):
            cases = [case for case in data[task] if case['split'] == 'test']
            self.assertEqual({case['input_id'] for case in cases}, set(inputs))
            self.assertTrue(all(case['gold']['review_status'] == 'draft' for case in cases))
        known = {chunk['chunk_id']: chunk for chunk in segmented['chunks']}
        for value in inputs.values():
            for field in ('context',):
                for identifier in value[field]:
                    context = data['contexts'][identifier]
                    original = known[context['chunk_id']]
                    self.assertIn(context['text'], original['text'])
                    self.assertEqual(context['source'], original['source'])

    def test_full_document_reference_rejects_omissions_and_source_edits(self):
        from unittest.mock import patch
        from xfed.evaluation import load_reference
        original = load_reference(ROOT)
        for task in ('classification',):
            data = deepcopy(original)
            data[task].pop()  # Contract 05 follows the retained development cases.
            with patch.object(Path, 'read_text', return_value=json.dumps(data)):
                with self.assertRaisesRegex(ValueError, 'every full-document source chunk'):
                    load_reference(ROOT)
        data = deepcopy(original)
        data['inputs']['contract_05:c000017']['clause']['text'] += ' altered'
        with patch.object(Path, 'read_text', return_value=json.dumps(data)):
            with self.assertRaisesRegex(ValueError, 'frozen source chunk unchanged'):
                load_reference(ROOT)

    def test_absent_class_is_unmeasured_without_lowering_perfect_macro_scores(self):
        from xfed.evaluation import classification_metrics
        rows = [dict(gold=label, label=label) for label in ('RELEVANT', 'NOT_RELEVANT')]
        scores = classification_metrics(rows)
        self.assertEqual(scores['macro_f1'], 1)
        self.assertEqual(scores['macro_labels'], ['RELEVANT', 'NOT_RELEVANT'])
        self.assertIsNone(scores['per_class']['NEEDS_CONTEXT']['recall'])
        rows[0]['label'] = 'NEEDS_CONTEXT'
        scores = classification_metrics(rows)
        self.assertEqual(scores['macro_f1'], .5)
        self.assertEqual(scores['confusion_matrix']['RELEVANT']['NEEDS_CONTEXT'], 1)






    def test_blind_review_packets_preserve_evidence_and_require_completed_human_annotations(self):
        from xfed.evaluation import apply_reviews, digest, load_reference, review_packet
        data = load_reference(ROOT)
        before = digest(data)
        packet = review_packet(data, task='classification')
        self.assertEqual(len(packet['cases']), 76)
        self.assertTrue(all(entry['gold']['label'] is None for entry in packet['cases']))
        self.assertNotIn('predictions', packet)
        with self.assertRaisesRegex(ValueError, 'No completed reviews'):
            apply_reviews(data, packet)
        entry = packet['cases'][0]
        entry['gold'].update(label='NOT_RELEVANT', review_status='reviewed', reviewer='Test annotator',
                             rationale='A standalone title has no independent contractual meaning.')
        result = apply_reviews(data, packet)
        self.assertEqual(digest(data), before)
        self.assertEqual(digest(data['inputs']), digest(result['inputs']))
        reviewed_case = next(case for case in result['classification'] if case['case_id'] == entry['case_id'])
        self.assertEqual(reviewed_case['gold']['reviewer'], 'Test annotator')
        entry['gold']['requires_adjudication'] = True
        with self.assertRaisesRegex(ValueError, 'Resolve requires_adjudication'):
            apply_reviews(data, packet)
        entry['gold'].pop('requires_adjudication')
        entry['input']['clause']['text'] += ' invented'
        with self.assertRaisesRegex(ValueError, 'evidence changed'):
            apply_reviews(data, packet)



    def test_routing_metrics_distinguish_uncertainty_from_lost_relevant_clauses(self):
        from xfed.evaluation import classification_metrics
        rows = [dict(gold='RELEVANT', label='NEEDS_CONTEXT'),
                dict(gold='RELEVANT', label='NOT_RELEVANT'),
                dict(gold='RELEVANT', label='RELEVANT', error='Transport failure')]
        scores = classification_metrics(rows)
        self.assertEqual(scores['routing']['relevant_recall'], 1 / 3)
        self.assertEqual(scores['routing']['missed_relevant'], 2)
        self.assertEqual(scores['accuracy'], 0)

    def test_plan_schema_constrains_operators_and_source_labels_cannot_be_invented(self):
        answer = plan(self.item)
        answer['rules'][0]['conditions'][0]['operator'] = 'isPermittedByMandatoryLaw'
        with self.assertRaises(ValidationError):
            Draft202012Validator(response_schema(ROOT)).validate(answer)
        for field, value in [('actor', 'Unmentioned Controller'), ('asset', 'All Company Data'),
                             ('issuer', 'Unmentioned Licensor')]:
            answer = plan(self.item, **{field: value})
            with self.assertRaisesRegex(ValueError, 'literal label'):
                compile_plan(answer, 'contract', self.item)
        # A definition can establish a literal label without broadening scope.
        item = dict(self.item, context=[dict(chunk('c2', 'Recipient means Provider.'), relationship='definition')])
        answer = plan(item, actor='Recipient')
        answer['rules'][0]['evidence']['supporting'].append('c2')
        self.assertIsNotNone(compile_plan(answer, 'contract', item))

    def test_partial_rule_groups_keep_sound_rules_without_dropping_failed_prerequisites(self):
        item = dict(self.item, text='Provider must delete supplied data. Provider may use supplied data after notification.')
        answer = plan(item, conditions=[])
        permission = deepcopy(answer['rules'][0])
        permission.update(kind='permission', action='use', duties=[dict(
            actor='Provider', action='notify', asset='supplied data', conditions=[],
            evidence=[dict(chunk_id='c1')])])
        answer['rules'].append(permission)
        def check(value):
            result = reviewed(value)
            for row in result['rule_reviews']:
                if row['rule_type'] == 'duty':
                    row.update(verdict='UNSUPPORTED', reason='Notification recipient not established.')
            return result
        output, report = generate(dict(contract_id='contract', items=[item]), PlanClient(answer, check), ROOT)
        self.assertEqual(len(output['traceability']), 1)
        self.assertEqual(output['odrlPolicy']['obligation'][0]['action'], 'delete')
        self.assertFalse(output['odrlPolicy'].get('permission'))
        self.assertEqual(report['summary']['partial_needs_review'], 1)
        entry = report['clauses'][0]
        self.assertEqual(len(entry['withheld_rule_uids']), 2)
        self.assertTrue(all(':withheld:' in uid for uid in entry['withheld_rule_uids']))
        attempts = []
        def interrupted_repair(value):
            attempts.append(value)
            if len(attempts) > 1:
                raise ValueError('Ollama output token limit reached')
            return answer
        client = PlanClient(interrupted_repair, check)
        client.last_request = dict(thinking_chars=10, answer_chars=0)
        output, entry, failed = generate_item(item, client, 'prompt', 'contract', response_schema(ROOT), ROOT)
        self.assertFalse(failed)
        self.assertEqual(entry['outcome'], 'partial_needs_review')
        self.assertEqual(len(output['traceability']), 1)
        self.assertIn('repair_error', entry)

    def test_failed_plan_repair_withholds_context_only_groups_before_source_review(self):
        item = dict(self.item, context=[dict(chunk('c2', 'Provider may publish supplied data.'), relationship='other')])
        answer = plan(item)
        extra = plan(item, 'permission', action='publish', conditions=[], evidence=[dict(chunk_id='c2')])['rules'][0]
        answer['rules'].append(extra)
        with self.assertRaisesRegex(ValueError, 'primary'):
            compile_plan(answer, 'contract', item)
        output, entry, failed = generate_item(item, PlanClient(answer), 'prompt', 'contract', response_schema(ROOT), ROOT)
        self.assertFalse(failed)
        self.assertEqual(entry['generation_attempts'], 2)
        self.assertEqual(entry['outcome'], 'partial_needs_review')
        self.assertEqual(entry['withheld_plan_groups'][0]['plan_index'], 1)
        self.assertEqual(output['odrlPolicy']['obligation'][0]['action'], 'delete')
        self.assertNotIn('permission', output['odrlPolicy'])
        self.assertEqual(len(output['traceability']), 1)
        self.assertEqual(entry['semantic_check']['rule_reviews'][0]['verdict'], 'ENTAILED')
        validate_fragment(output, 'contract', item, ROOT)
        def contradicted(value):
            result = reviewed(value)
            for row in result['rule_reviews']:
                row.update(verdict='CONTRADICTED', reason='Simulated contradiction.')
            return result
        output, entry, failed = generate_item(item, PlanClient(answer, contradicted), 'prompt',
                                             'contract', response_schema(ROOT), ROOT)
        self.assertIsNone(output)
        self.assertTrue(failed)

    def test_invalid_nested_plan_evidence_withholds_its_whole_parent_group(self):
        item = dict(self.item, text=self.item['text'] + ' Provider may use supplied data after notification.',
                    context=[dict(chunk('c2', 'Provider must notify Customer of supplied data.'), relationship='other')])
        answer = plan(item)
        permission = plan(item, 'permission', action='use', conditions=[])['rules'][0]
        permission['duties'] = [dict(actor='Provider', action='notify', asset='supplied data',
            conditions=[dict(leftOperand='recipient', operator='eq', rightOperand='Customer')],
            evidence=[dict(chunk_id='c2')])]
        answer['rules'].append(permission)
        # A later valid group must not inherit traces or IDs from the failed duty.
        answer['rules'].append(plan(item, 'permission', action='use', conditions=[])['rules'][0])
        output, entry, failed = generate_item(item, PlanClient(answer), 'prompt', 'contract', response_schema(ROOT), ROOT)
        self.assertFalse(failed)
        self.assertEqual(entry['outcome'], 'partial_needs_review')
        self.assertEqual(entry['withheld_plan_groups'][0]['plan_index'], 1)
        self.assertEqual([row['ruleUid'] for row in output['traceability']], ['r1', 'r2'])
        self.assertNotIn('duty', output['odrlPolicy']['permission'][0])
        validate_fragment(output, 'contract', item, ROOT)

    def test_reviewer_retries_an_output_limit_without_dropping_evidence(self):
        from xfed.policy_review import review_item
        item = dict(self.item, candidate_rules=[dict(rule_type='obligation',
                    rule=fragment(self.item)['odrlPolicy']['obligation'][0], source_chunk_ids=['c1'])])
        seen = []
        def answer(value):
            seen.append(deepcopy(value))
            if len(seen) == 1:
                raise ValueError('Ollama output token limit reached at 4096 tokens')
            return reviewed(value)
        result = review_item(item, Client(answer), 'prompt', 'contract', ROOT)
        self.assertNotIn('error', result)
        self.assertEqual(result['coverage'], 'COMPLETE')
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], seen[1])
        def always_fails(value):
            raise ValueError('Ollama output token limit reached at 4096 tokens')
        client = Client(always_fails)
        result = review_item(item, client, 'prompt', 'contract', ROOT)
        self.assertEqual(len(client.calls), 2)
        self.assertIn('error', result)
        self.assertEqual(result['rule_reviews'][0]['verdict'], 'AMBIGUOUS')

    def test_reviewer_repairs_oversized_explanations_instead_of_truncating_them(self):
        from xfed.policy_review import review_item
        item = dict(self.item, candidate_rules=[dict(rule_type='obligation',
                    rule=fragment(self.item)['odrlPolicy']['obligation'][0], source_chunk_ids=['c1'])])
        attempts = []
        def answer(value):
            attempts.append(value)
            result = reviewed(value)
            if len(attempts) == 1:
                result['rule_reviews'][0]['reason'] = 'x' * 401
            return result
        result = review_item(item, Client(answer), 'prompt', 'contract', ROOT)
        self.assertNotIn('error', result)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(result['rule_reviews'][0]['reason'], 'Simulated supported rule.')

    def test_all_invalid_plan_groups_remain_a_failure_instead_of_an_abstention(self):
        answer = plan(self.item, evidence=[dict(chunk_id='missing')])
        output, entry, failed = generate_item(self.item, PlanClient(answer), 'prompt',
                                             'contract', response_schema(ROOT), ROOT)
        self.assertIsNone(output)
        self.assertTrue(failed)
        self.assertIn('No valid source-grounded', entry['reason'])
        self.assertEqual(len(entry['withheld_plan_groups']), 1)
        self.assertNotIn('semantic_check', entry)

    def test_summary_disclaimer_is_attached_on_its_own_page_without_spreading_to_operative_pages(self):
        disclaimer = chunk('c1', 'This summary is not a substitute for the license.')
        summary = chunk('c2', 'Recipient may share Data.')
        operative = dict(chunk('c3', 'Recipient may share Data.'), source=dict(pages=[2], block_ids=['b3']))
        index = ContextIndex([disclaimer, summary, operative])
        context, _ = index.context_for(summary)
        self.assertIn(disclaimer['text'], [value['text'] for value in context])
        context, _ = index.context_for(operative)
        self.assertNotIn(disclaimer['text'], [value['text'] for value in context])

    def test_numbered_topic_heading_does_not_inherit_its_child_rules(self):
        heading = chunk('c1', '8.1\nConfidentiality', heading='8.1', clause_id='8.1', child_clause_ids=['8.1.1'])
        child = chunk('c2', 'Recipient must protect Data.', clause_id='8.1.1', parent_clause_id='8.1')
        client = Client(dict(label='RELEVANT'))
        context, _ = ContextIndex([heading, child]).context_for(heading)
        self.assertNotIn(child['text'], [value['text'] for value in context])
        self.assertEqual(classify_batch([dict(clause=heading, context=context, unresolved_references=[])], client, ROOT)[0]['label'], 'NOT_RELEVANT')
        self.assertEqual(client.calls, [])
        title_without_heading = dict(heading)
        title_without_heading.pop('heading')
        self.assertEqual(classify_batch([dict(clause=title_without_heading, context=[], unresolved_references=[])], client, ROOT)[0]['label'], 'NOT_RELEVANT')
        for text in ('8.1\nRecipient must protect Data.', '8.1\nDelete Data', '8.1\nResearch use only'):
            substantive = dict(heading, text=text)
            self.assertEqual(classify_batch([dict(clause=substantive, context=[], unresolved_references=[])], client, ROOT)[0]['label'], 'RELEVANT')

    def test_source_id_evidence_and_pdf_whitespace_preserve_original_citations(self):
        item = dict(self.item, text='Provider must delete\nthe supplied data within 12 hours after notice.')
        answer = plan(item)
        answer['rules'][0]['evidence'] = [dict(chunk_id='c1')]
        Draft202012Validator(response_schema(ROOT)).validate(answer)
        output = compile_plan(answer, 'contract', item)
        self.assertEqual(output['traceability'][0]['sourceClauses'][0]['text'], item['text'])
        answer['rules'][0]['evidence'][0]['quote'] = item['text'].replace('\n', ' ')
        self.assertIsNotNone(compile_plan(answer, 'contract', item))
        answer['rules'][0]['evidence'][0]['quote'] = item['text'].replace('must', 'may')
        with self.assertRaisesRegex(ValueError, 'quote'):
            compile_plan(answer, 'contract', item)
        answer['rules'][0]['evidence'] = [dict(chunk_id='unknown')]
        with self.assertRaisesRegex(ValueError, 'evidence'):
            compile_plan(answer, 'contract', item)

    def test_prohibition_polarity_is_checked_even_if_model_review_would_accept_it(self):
        item = dict(self.item, text='Provider must not use supplied data for advertising.')
        answer = plan(item, 'prohibition', action='use', conditions=[dict(
            leftOperand='purpose', operator='isNoneOf', rightOperand='advertising')])
        with self.assertRaisesRegex(ValueError, 'reverses'):
            compile_plan(answer, 'contract', item)
        answer['rules'][0]['conditions'][0]['operator'] = 'eq'
        self.assertIsNotNone(compile_plan(answer, 'contract', item))
        item = dict(item, text='Provider must not use supplied data for purposes other than research.')
        answer = plan(item, 'prohibition', action='use', conditions=[dict(
            leftOperand='purpose', operator='isNoneOf', rightOperand='research')])
        self.assertIsNotNone(compile_plan(answer, 'contract', item))

    def test_eval_checkpoints_survive_cancellation_and_never_claim_full_results(self):
        from xfed.evaluation import load_reference, run_evaluation
        class Runner(Client):
            num_ctx = 131072
            def preflight(self): pass
            def reset_assignments(self): pass
            def metadata(self): return dict(model=self.model)
        snapshots, cancel = [], Event()
        def completed(report):
            snapshots.append(report)
            if report['progress']['classification']['completed'] >= 3:
                cancel.set()
        with self.assertRaises(InterruptedError):
            run_evaluation(load_reference(ROOT), Runner(dict(label='RELEVANT')), ROOT,
                           task='classification', cancel=cancel, completed=completed)
        snapshot = snapshots[-1]
        self.assertEqual(snapshot['status'], 'running')
        self.assertEqual(snapshot['progress']['classification'], dict(completed=3, total=76))
        self.assertEqual(len(snapshot['predictions']['classification']), 3)
        self.assertEqual(snapshot['live_metrics']['classification']['completed']['n'], 3)
        self.assertNotIn('metrics', snapshot)
        self.assertEqual(snapshots[0]['progress']['classification']['completed'], 0)

    def test_evaluation_stops_on_lost_connection_and_preserves_completed_predictions(self):
        from xfed.evaluation import load_reference, run_evaluation, score_report
        class Runner(Client):
            def preflight(self): pass
            def reset_assignments(self): pass
            def metadata(self): return dict(model=self.model)
        calls = []
        def answer(value):
            calls.append(value)
            if len(calls) == 2:
                raise OllamaConnectionError('Forwarded endpoint disconnected.')
            return dict(label='RELEVANT', reason='Simulated classification.')
        data, saved = load_reference(ROOT), []
        with self.assertRaisesRegex(OllamaConnectionError, 'disconnected'):
            run_evaluation(data, Runner(answer), ROOT, task='classification', completed=saved.append)
        report = saved[-1]
        self.assertEqual(len(calls), 2)
        self.assertEqual(report['status'], 'failed')
        self.assertTrue(report['infrastructure_error'])
        count = report['progress']['classification']['completed']
        self.assertGreater(count, 0)
        self.assertLess(count, 76)
        self.assertEqual(count, len(report['predictions']['classification']))
        self.assertEqual(report['live_metrics']['classification']['completed']['n'], count)
        self.assertNotIn('metrics', report)
        rescored = score_report(data, report, ROOT)
        self.assertEqual(rescored['metrics']['classification']['real/full_document']['errors'], 76 - count)
        class Unavailable(Runner):
            def preflight(self): raise OllamaConnectionError('Not connected.')
        saved.clear()
        with self.assertRaises(OllamaConnectionError):
            run_evaluation(data, Unavailable(dict(label='RELEVANT')), ROOT,
                           task='classification', completed=saved.append)
        self.assertTrue(saved[-1]['infrastructure_error'])
        self.assertEqual(saved[-1]['progress']['classification']['completed'], 0)

    def test_review_connection_failure_propagates_instead_of_becoming_a_model_verdict(self):
        from xfed.policy_review import review_item
        item = dict(self.item, candidate_rules=[dict(rule_type='obligation',
                    rule=fragment(self.item)['odrlPolicy']['obligation'][0], source_chunk_ids=['c1'])])
        def disconnected(value):
            raise OllamaConnectionError('Forwarded endpoint disconnected.')
        client = Client(disconnected)
        with self.assertRaises(OllamaConnectionError):
            review_item(item, client, 'prompt', 'contract', ROOT)
        self.assertEqual(len(client.calls), 1)

    def test_notification_recipient_and_qualitative_timing_are_not_lost(self):
        item = dict(self.item, text='Provider must notify Customer of incidents without undue delay.')
        answer = plan(item, action='notify', asset='incidents', conditions=[])
        with self.assertRaisesRegex(ValueError, 'recipient'):
            compile_plan(answer, 'contract', item)
        answer['rules'][0]['conditions'] = [dict(leftOperand='recipient', operator='eq', rightOperand='Customer'),
            dict(leftOperand='elapsedTime', operator='lteq', rightOperand='undue delay')]
        with self.assertRaisesRegex(ValueError, 'qualitative'):
            compile_plan(answer, 'contract', item)
        answer['rules'][0]['conditions'][1] = dict(leftOperand='timing', operator='eq', rightOperand='without undue delay')
        self.assertIsNotNone(compile_plan(answer, 'contract', item))

    def test_duration_syntax_rejects_invalid_component_order_and_missing_time_separator(self):
        for value in ['P1H', 'P1DT', 'PT', 'P1D2D', 'PT1.5H', 'P1W', 'P1YH']:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'duration'):
                compile_plan(plan(self.item, conditions=[dict(leftOperand='elapsedTime', operator='lteq', rightOperand=value)]),
                             'contract', self.item)
        for value in ['P3Y', 'P1Y2M3DT4H5M6.5S', 'PT0S', 'P30D', 'PT72H']:
            self.assertIsNotNone(compile_plan(plan(self.item, conditions=[dict(
                leftOperand='elapsedTime', operator='lteq', rightOperand=value)]), 'contract', self.item))

    def test_clause_decoder_does_not_invent_an_issuer_but_legacy_explicit_metadata_still_compiles(self):
        shape = response_schema(ROOT)
        self.assertNotIn('issuer', shape['properties']['rules']['items']['properties'])
        output, entry, failed = generate_item(self.item, PlanClient(plan(self.item)), 'prompt',
                                             'contract', shape, ROOT)
        self.assertFalse(failed)
        self.assertEqual(output['odrlPolicy']['@type'], 'Set')
        self.assertNotIn('assigner', output['odrlPolicy']['obligation'][0])
        explicit = plan(self.item, issuer='Provider')
        self.assertEqual(compile_plan(explicit, 'contract', self.item)['odrlPolicy']['@type'], 'Agreement')

    def test_access_permission_keeps_security_prerequisite_in_its_own_rule(self):
        item = dict(self.item, text='Provider may grant Recipient access to Registry. '
                    'Such access depends on Recipient meeting all security requirements.')
        answer = plan(item, 'permission', action='provideAccess', asset='Registry', conditions=[])
        with self.assertRaisesRegex(ValueError, 'security prerequisite'):
            compile_plan(answer, 'contract', item)
        answer['rules'].append(plan(item, 'obligation', actor='Recipient', action='meet',
                                  asset='security requirements', conditions=[])['rules'][0])
        with self.assertRaisesRegex(ValueError, 'security prerequisite'):
            compile_plan(answer, 'contract', item)
        answer['rules'][0]['conditions'] = [dict(leftOperand='security', operator='eq',
            rightOperand='Recipient meeting all security requirements')]
        prerequisite = answer['rules'].pop()
        with self.assertRaisesRegex(ValueError, 'independent security obligation'):
            compile_plan(dict(answer, rules=[*answer['rules'], prerequisite]), 'contract', item)
        prerequisite.pop('kind')
        prerequisite.pop('duties')
        prerequisite.pop('remedies')
        answer['rules'][0]['duties'] = [prerequisite]
        validate_fragment(compile_plan(answer, 'contract', item), 'contract', item, ROOT)

    def test_access_scope_does_not_drop_necessity_while_preserving_purpose(self):
        item = dict(self.item, text='Provider may read Data necessary for research.')
        answer = plan(item, 'permission', action='read', asset='Data', conditions=[
            dict(leftOperand='purpose', operator='eq', rightOperand='research')])
        with self.assertRaisesRegex(ValueError, 'necessity restriction'):
            compile_plan(answer, 'contract', item)
        answer['rules'][0]['conditions'].append(dict(leftOperand='scope', operator='eq',
            rightOperand='necessary for research'))
        validate_fragment(compile_plan(answer, 'contract', item), 'contract', item, ROOT)

    def test_notification_shorter_deadline_exception_cannot_be_omitted(self):
        item = dict(self.item, text='Provider must notify Customer of incidents within 72 hours of discovery, '
                    'unless mandatory rules require notification within a shorter period.')
        answer = plan(item, action='notify', asset='incidents', conditions=[
            dict(leftOperand='recipient', operator='eq', rightOperand='Customer'),
            dict(leftOperand='elapsedTime', operator='lteq', rightOperand='PT72H'),
            dict(leftOperand='event', operator='eq', rightOperand='discovery')])
        with self.assertRaisesRegex(ValueError, 'shorter-period exception'):
            compile_plan(answer, 'contract', item)
        answer['rules'][0]['conditions'].append(dict(leftOperand='timing', operator='eq',
                                                   rightOperand='subject to any mandatory shorter deadline'))
        validate_fragment(compile_plan(answer, 'contract', item), 'contract', item, ROOT)
        answer['rules'][0]['conditions'][-1] = dict(leftOperand='exception', operator='eq',
            rightOperand='mandatory rules require a shorter period')
        with self.assertRaisesRegex(ValueError, 'AND condition'):
            compile_plan(answer, 'contract', item)

    def test_source_or_authorisation_cannot_be_a_conjunctive_exception(self):
        item = dict(self.item, text='Provider may use supplied data for research or as agreed by Customer.')
        answer = plan(item, 'permission', action='use', conditions=[
            dict(leftOperand='purpose', operator='eq', rightOperand='research'),
            dict(leftOperand='exception', operator='eq', rightOperand='agreed by Customer')])
        with self.assertRaisesRegex(ValueError, 'AND exception'):
            compile_plan(answer, 'contract', item)
        # Distinct branches retain the source's choice instead of conjoining it.
        alternative = deepcopy(answer['rules'][0])
        alternative['conditions'] = [dict(leftOperand='approval', operator='eq', rightOperand='agreed by Customer')]
        answer['rules'][0]['conditions'] = [dict(leftOperand='purpose', operator='eq', rightOperand='research')]
        answer['rules'].append(alternative)
        self.assertEqual(len(compile_plan(answer, 'contract', item)['odrlPolicy']['permission']), 2)

    def test_example_rule_is_withheld_only_when_the_same_general_rule_already_covers_it(self):
        item = dict(self.item, text='Provider may implement protection measures, such as a software update, '
                    'if they do not negatively affect Customer.')
        answer = plan(item, 'permission', action='implement', asset='protection measures', conditions=[
            dict(leftOperand='impact', operator='neq', rightOperand='negative effect on Customer')])
        example = deepcopy(answer['rules'][0])
        example['asset'] = 'software update'
        answer['rules'].append(example)
        with self.assertRaisesRegex(ValueError, 'Illustrative example'):
            compile_plan(answer, 'contract', item)
        output, entry, failed = generate_item(item, PlanClient(answer), 'prompt', 'contract', response_schema(ROOT), ROOT)
        self.assertFalse(failed)
        self.assertEqual(entry['outcome'], 'partial_needs_review')
        self.assertEqual(len(output['odrlPolicy']['permission']), 1)
        self.assertEqual(entry['withheld_plan_groups'][0]['plan_index'], 1)
        validate_fragment(output, 'contract', item, ROOT)
        # Independently qualified examples and other actions are distinct rules.
        example['conditions'] = [dict(leftOperand='purpose', operator='eq', rightOperand='security')]
        self.assertEqual(len(compile_plan(answer, 'contract', item)['odrlPolicy']['permission']), 2)
        example['conditions'] = deepcopy(answer['rules'][0]['conditions'])
        example['action'] = 'modify'
        self.assertEqual(len(compile_plan(answer, 'contract', item)['odrlPolicy']['permission']), 2)


    def test_free_access_and_plan_compliance_cannot_be_dropped_or_made_alternatives(self):
        item = dict(self.item, text='Provider may access supplied data at no charge for research in accordance with the Work Plan.')
        answer = plan(item, 'permission', action='read', conditions=[dict(leftOperand='purpose', operator='eq', rightOperand='research')])
        with self.assertRaisesRegex(ValueError, 'no-charge'):
            compile_plan(answer, 'contract', item)
        answer['rules'][0]['conditions'].append(dict(leftOperand='payAmount', operator='eq', rightOperand=0))
        answer['rules'][0]['conditions'].append(dict(leftOperand='compliance', operator='eq', rightOperand='in accordance with the Work Plan'))
        validate_fragment(compile_plan(answer, 'contract', item), 'contract', item, ROOT)
        answer['rules'][0]['conditions'][0] = dict(leftOperand='purpose', operator='isAnyOf',
            rightOperand=['research', 'in accordance with the Work Plan'])
        with self.assertRaisesRegex(ValueError, 'alternative purpose'):
            compile_plan(answer, 'contract', item)

    def test_symbolic_shorter_deadline_preserves_duty_and_requires_binding(self):
        item = dict(self.item, text='Provider must notify Customer of incidents within 72 hours of discovery, '
                    'unless mandatory rules require notification within a shorter period.')
        shorter = dict(leftOperand='shorterPeriodRequired', operator='eq', rightOperand=True)
        answer = plan(item, action='notify', asset='incidents', conditions=[
            dict(leftOperand='recipient', operator='eq', rightOperand='Customer'),
            dict(leftOperand='elapsedTime', operator='lteq', rightOperand='PT72H'),
            dict(leftOperand='event', operator='eq', rightOperand='discovery'),
            {'or': [dict(shorter, rightOperand=False), {'and': [shorter,
                dict(leftOperand='elapsedTime', operator='lteq', rightOperandReference='shorter')]}]}])
        answer['parameters'] = [dict(key='shorter', label='shorter period', dataType='duration',
                                     evidence=[dict(chunk_id=item['chunk_id'])])]
        Draft202012Validator(response_schema(ROOT)).validate(answer)
        fragment = compile_plan(answer, 'contract', item)
        validate_fragment(fragment, 'contract', item, ROOT)
        binding = fragment['parameterBindings'][0]
        self.assertEqual(binding['status'], 'unresolved')
        wrong_type = deepcopy(fragment)
        wrong_type['parameterBindings'][0]['dataType'] = 'boolean'
        with self.assertRaisesRegex(ValueError, 'must bind a duration'):
            validate_fragment(wrong_type, 'contract', item, ROOT)
        self.assertNotIn('value', binding)
        self.assertEqual(binding['sourceClauses'][0]['text'], item['text'])
        output, entry, failed = generate_item(item, PlanClient(answer), 'prompt', 'contract', response_schema(ROOT), ROOT)
        self.assertFalse(failed)
        self.assertEqual(entry['outcome'], 'partial_needs_review')
        self.assertIn('do not enforce', entry['reason'])
        tampered = deepcopy(fragment)
        tampered.pop('parameterBindings')
        with self.assertRaisesRegex(ValueError, 'unresolved binding'):
            validate_fragment(tampered, 'contract', item, ROOT)
        answer['rules'][0]['conditions'][-1]['or'][1]['and'][1]['rightOperand'] = 'PT24H'
        with self.assertRaises(ValidationError):
            Draft202012Validator(response_schema(ROOT)).validate(answer)



    def test_every_logical_access_branch_retains_its_required_qualification(self):
        item = dict(self.item, text='Provider may read supplied data at no charge for research.')
        price = dict(leftOperand='payAmount', operator='eq', rightOperand=0)
        purpose = dict(leftOperand='purpose', operator='eq', rightOperand='research')
        answer = plan(item, 'permission', action='read', conditions=[{'or': [price, purpose]}])
        with self.assertRaisesRegex(ValueError, 'no-charge'):
            compile_plan(answer, 'contract', item)
        answer['rules'][0]['conditions'] = [price, {'or': [purpose,
            dict(leftOperand='approval', operator='eq', rightOperand='agreed')]}]
        validate_fragment(compile_plan(answer, 'contract', item), 'contract', item, ROOT)
        from xfed.common import constraint_branches
        with self.assertRaisesRegex(ValueError, 'Too many logical branches'):
            constraint_branches([{'or': [dict(purpose, rightOperand='a'), dict(purpose, rightOperand='b')]}] * 8)

    def test_heading_generation_does_not_extract_independent_child_rules(self):
        item = dict(self.item, heading='8.1', text='8.1\nConfidentiality', child_clause_ids=['8.1.1'],
                    context=[dict(chunk('c2', 'Provider must delete supplied data.'), relationship='reference')])
        client = PlanClient(plan(self.item))
        output, entry, failed = generate_item(item, client, 'prompt', 'contract', response_schema(ROOT), ROOT)
        self.assertIsNone(output)
        self.assertFalse(failed)
        self.assertEqual(entry['deterministic_no_rule'], 'standalone_heading')
        self.assertEqual(client.calls, [])

    def test_actor_citation_resolves_supplied_parent_but_never_invents_a_label(self):
        item = dict(self.item, text='(a) delete supplied data within 12 hours of notice;',
                    parent_clause_id='2', context=[dict(chunk('parent', '2. Provider must:', clause_id='2'), relationship='parent')])
        fragment = compile_plan(plan(item), 'contract', item)
        validate_fragment(fragment, 'contract', item, ROOT)
        self.assertEqual({source['chunk_id'] for source in fragment['traceability'][0]['sourceClauses']},
                         {item['chunk_id'], 'parent'})
        with self.assertRaisesRegex(ValueError, 'literal label'):
            compile_plan(plan(item, actor='Unknown party'), 'contract', item)

    def test_literal_labels_allow_typographic_apostrophes_but_not_changed_connectors(self):
        item = dict(self.item, text='Provider must maintain Company\u2019s Systems or Networks records.')
        answer = plan(item, action='maintain', asset="Company's Systems or Networks records", conditions=[])
        validate_fragment(compile_plan(answer, 'contract', item), 'contract', item, ROOT)
        answer['rules'][0]['asset'] = "Company's Systems and Networks records"
        with self.assertRaisesRegex(ValueError, 'literal label'):
            compile_plan(answer, 'contract', item)

    def test_primary_evidence_follows_context_and_repair_feedback_in_model_request(self):
        item = dict(self.item, context=[dict(chunk('parent', 'A supporting definition.'), relationship='definition')])
        client = PlanClient(plan(item))
        generate_answer(client, 'prompt', 'contract', item, response_schema(ROOT), feedback=dict(previous_plan='untrusted'))
        value = client.calls[0][0]
        self.assertLess(list(value).index('correction'), list(value).index('clause'))
        self.assertLess(list(value['clause']).index('context'), list(value['clause']).index('text'))
        self.assertEqual(value['clause']['text'], item['text'])
        self.assertEqual(value['clause']['context'][0]['text'], item['context'][0]['text'])

    def test_passive_request_duty_uses_an_explicit_unresolved_party_not_an_invented_name(self):
        item = dict(self.item, text='All requests for access to Project Results must be submitted in writing.')
        answer = plan(item, actor='requester', actor_role='requester', action='submit', asset='requests for access to Project Results',
                      conditions=[dict(leftOperand='form', operator='eq', rightOperand='in writing')])
        Draft202012Validator(response_schema(ROOT)).validate(answer)
        fragment = compile_plan(answer, 'contract', item)
        validate_fragment(fragment, 'contract', item, ROOT)
        binding = fragment['partyBindings'][0]
        self.assertEqual(binding['role'], 'requester')
        self.assertEqual(fragment['odrlPolicy']['obligation'][0]['assignee'], binding['uid'])
        output, entry, failed = generate_item(item, PlanClient(answer), 'prompt', 'contract', response_schema(ROOT), ROOT)
        self.assertFalse(failed)
        self.assertEqual(entry['outcome'], 'partial_needs_review')
        answer['rules'][0]['actor'] = 'Invented legal party'
        with self.assertRaisesRegex(ValueError, 'unresolved actor role'):
            compile_plan(answer, 'contract', item)


    def test_notifications_preserve_recipient_subject_trigger_and_quantitative_bound(self):
        item = dict(self.item, text='Provider must notify Customer of Incidents within 72 hours of becoming aware of the Incident.')
        answer = plan(item, action='notify', asset='Incidents', conditions=[
            dict(leftOperand='recipient', operator='eq', rightOperand='Customer'),
            dict(leftOperand='elapsedTime', operator='lteq', rightOperand='PT72H'),
            dict(leftOperand='event', operator='eq', rightOperand='becoming aware of the Incident')])
        validate_fragment(compile_plan(answer, 'contract', item), 'contract', item, ROOT)
        for field, replacement, message in [('recipient', 'Provider', 'explicit source recipient'),
                ('event', 'Customer', 'timing trigger'), ('elapsedTime', 'PT720H', 'longer')]:
            changed = deepcopy(answer)
            next(c for c in changed['rules'][0]['conditions'] if c['leftOperand'] == field)['rightOperand'] = replacement
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, message):
                compile_plan(changed, 'contract', item)
        answer['rules'][0]['asset'] = 'Customer'
        with self.assertRaisesRegex(ValueError, 'explicit subject'):
            compile_plan(answer, 'contract', item)

    def test_clause_grammar_requires_explicit_primary_and_known_supporting_source_ids(self):
        item = dict(self.item, context=[dict(chunk('parent', 'Provider means the supplier.'), relationship='definition')])
        shape = clause_decoder_schema(response_schema(ROOT), item)
        Draft202012Validator(shape).validate(plan(item))
        self.assertNotIn('enum', shape['properties']['rules']['items']['properties']['actor'])
        self.assertNotIn('enum', shape['properties']['rules']['items']['properties']['asset'])
        answer = plan(item)
        answer['rules'][0]['evidence'] = dict(primary='parent', supporting=[])
        with self.assertRaises(ValidationError):
            Draft202012Validator(shape).validate(answer)
        answer['rules'][0]['evidence'] = dict(primary=item['chunk_id'], supporting=['unknown'])
        with self.assertRaises(ValidationError):
            Draft202012Validator(shape).validate(answer)
        self.assertLessEqual(shape['properties']['rules']['maxItems'], 12)

    def test_access_right_grammar_does_not_turn_shall_have_access_into_a_provider_duty(self):
        item = dict(self.item, text='For Term, the Parties shall have access at no charge to supplied data.')
        shape = clause_decoder_schema(response_schema(ROOT), item)
        valid = plan(item, 'permission', actor='Parties', action='read', conditions=[
            dict(leftOperand='payAmount', operator='eq', rightOperand=0)])
        Draft202012Validator(shape).validate(valid)
        valid['rules'][0]['kind'] = 'obligation'
        with self.assertRaises(ValidationError):
            Draft202012Validator(shape).validate(valid)
        for text in ['No Parties shall have access to supplied data.',
                     'Parties shall have access to supplied data and must pay a fee.',
                     'Company shall have access to Records. Provider shall have access to supplied data.',
                     'Parties shall have access to supplied data and can publish results.',
                     'Parties shall have access to no supplied data.']:
            mixed = clause_decoder_schema(response_schema(ROOT), dict(item, text=text))
            self.assertNotIn('const', mixed['properties']['rules']['items']['properties']['kind'])

    def test_coverage_review_cannot_import_a_missing_rule_from_parent_context(self):
        from xfed.policy_review import review_item
        item = dict(self.item, candidate_rules=[], context=[dict(chunk('parent', 'Customer must acknowledge receipt.'), relationship='parent')])
        worker = Client(dict(relevance='RELEVANT', rule_reviews=[], coverage='MISSING_RULES', reason='Missing acknowledgement',
            missing_rules=[dict(requirement='Customer must acknowledge receipt.', primary_quote='Customer must acknowledge receipt.')]))
        checked = review_item(item, worker, 'prompt', 'contract', ROOT, require_resolved=False)
        self.assertIn('primary_quote', checked['error'])
        self.assertEqual(checked['coverage'], 'NEEDS_CONTEXT')

    def test_primary_action_grounding_blocks_parent_notices_and_request_approval_substitution(self):
        item = dict(self.item, text='The objecting Party can request a publication delay of 90 days.',
                    context=[dict(chunk('parent', 'Provider must notify Customer of publication plans.'), relationship='parent')])
        answer = plan(item, actor='Provider', action='notify', asset='publication plans', conditions=[],
                      evidence=[dict(chunk_id=item['chunk_id']), dict(chunk_id='parent')])
        with self.assertRaisesRegex(ValueError, 'operative PRIMARY'):
            compile_plan(answer, 'contract', item)
        answer = plan(item, 'permission', actor='objecting Party', action='obtainApproval', asset='publication delay', conditions=[])
        with self.assertRaisesRegex(ValueError, 'obtaining approval'):
            compile_plan(answer, 'contract', item)
        answer['rules'][0]['action'] = 'requestDelay'
        validate_fragment(compile_plan(answer, 'contract', item), 'contract', item, ROOT)
        fragment = dict(self.item, text='(a) publication plans;',
                        context=[dict(chunk('parent', 'Provider must notify Customer of:'), relationship='parent')])
        inherited = plan(fragment, actor='Provider', action='notify', asset='publication plans', conditions=[],
                         evidence=[dict(chunk_id=fragment['chunk_id']), dict(chunk_id='parent')])
        validate_fragment(compile_plan(inherited, 'contract', fragment), 'contract', fragment, ROOT)

    def test_format_requirements_joined_by_and_cannot_be_changed_to_alternatives(self):
        item = dict(self.item, text='Provider must supply supplied data in comprehensive, structured and machine-readable format.')
        answer = plan(item, action='supply', conditions=[dict(leftOperand='format', operator='isAnyOf',
            rightOperand=['comprehensive', 'structured', 'machine-readable'])])
        with self.assertRaisesRegex(ValueError, 'cumulative'):
            compile_plan(answer, 'contract', item)
        answer['rules'][0]['conditions'][0]['operator'] = 'isAllOf'
        validate_fragment(compile_plan(answer, 'contract', item), 'contract', item, ROOT)
        alternative = dict(item, text=item['text'].replace(' and ', ' or '))
        answer['rules'][0]['conditions'][0]['operator'] = 'isAnyOf'
        validate_fragment(compile_plan(answer, 'contract', alternative), 'contract', alternative, ROOT)

    def test_explicit_information_delivery_is_relevant_without_borrowing_context_duties(self):
        source = chunk('c1', 'Contractor shall report events to Company and shall provide information on possible causes.')
        client = Client(dict(label='NOT_RELEVANT'))
        result = classify_batch([dict(clause=source, context=[], unresolved_references=[])], client, ROOT)
        self.assertEqual(result[0]['label'], 'RELEVANT')
        self.assertEqual(client.calls, [])
        primary = chunk('c2', 'Definitions')
        result = classify_batch([dict(clause=primary, context=[dict(source, relationship='parent')], unresolved_references=[])], client, ROOT)
        self.assertEqual(result[0]['label'], 'NOT_RELEVANT')
        from xfed.common import explicit_information_duty
        self.assertFalse(explicit_information_duty(chunk(text='Illustrative example: Contractor shall provide information.')))

    def test_explanatory_faq_is_withheld_for_review_but_explicit_obligations_still_run(self):
        text = 'About access:\nGeneral guidance.\nHow to request access?\nSee https://example.test/guidance.'
        item = dict(self.item, text=text)
        client = PlanClient(plan(self.item))
        policy, entry, failed = generate_item(item, client, 'prompt', 'contract', response_schema(ROOT), ROOT)
        self.assertIsNone(policy)
        self.assertFalse(failed)
        self.assertEqual(entry['deterministic_no_rule'], 'explanatory_faq_requires_review')
        self.assertEqual(client.calls, [])
        operative = dict(self.item, text=text + '\nProvider must delete supplied data.')
        client = PlanClient(plan(operative, conditions=[]))
        policy, entry, failed = generate_item(operative, client, 'prompt', 'contract', response_schema(ROOT), ROOT)
        self.assertIsNotNone(policy)
        self.assertNotIn('deterministic_no_rule', entry)

    def test_sampling_options_are_bounded_and_cannot_override_resource_settings(self):
        options = dict(temperature=0.7, top_p=0.8, top_k=20, min_p=0.0, presence_penalty=1.5,
                       repeat_penalty=1.0, seed=42)
        client = OllamaClient('http://127.0.0.1:11435', 'simulated', sampling=options)
        self.assertEqual(client.metadata()['sampling'], options)
        changed = client.metadata()
        changed['sampling']['temperature'] = 1.5
        self.assertEqual(client.sampling['temperature'], 0.7)
        for invalid in [False, {'num_ctx': 100000}, {'temperature': float('nan')}, {'seed': True},
                        {'top_k': -1}, {'presence_penalty': 3}]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                OllamaClient('http://127.0.0.1:11435', 'simulated', sampling=invalid)

    def test_cache_does_not_reuse_answers_after_sampling_changes(self):
        class SamplingClient(Client):
            sampling = dict(temperature=0)
        client = SamplingClient(dict(answer=True))
        with TemporaryDirectory() as directory:
            cached = CachedClient(client, Path(directory)/'cache.json', Event(), False)
            for _ in range(2):
                cached.generate('prompt', dict(value=1), dict(type='object'))
            self.assertEqual(len(client.calls), 1)
            client.sampling = dict(temperature=0.7, seed=42)
            cached.generate('prompt', dict(value=1), dict(type='object'))
            self.assertEqual(len(client.calls), 2)

    def test_unused_osdu_does_not_break_the_model_decoder_but_explicit_metadata_is_supported(self):
        captured = []
        class Capture(Client):
            def generate(self, prompt, value, shape, **options):
                captured.append(deepcopy(shape))
                return super().generate(prompt, value, shape, **options)
        generate_answer(Capture(plan(self.item)), 'prompt', 'contract', self.item, response_schema(ROOT))
        self.assertNotIn('osdu', captured[-1]['properties'])
        item = dict(self.item, text=self.item['text'] + ' OSDU LegalTag project-data.')
        generate_answer(Capture(plan(item)), 'prompt', 'contract', item, response_schema(ROOT))
        self.assertIn('osdu', captured[-1]['properties'])

    def test_isolated_checkbox_option_remains_uncertain_without_an_operative_parent(self):
        client = Client(dict(label='RELEVANT'))
        item = dict(clause=chunk(text='\u2610 Remote access through a gateway'), context=[], unresolved_references=[])
        self.assertEqual(classify_batch([item], client, ROOT)[0]['label'], 'NEEDS_CONTEXT')
        self.assertEqual(client.calls, [])
        item['context'] = [dict(chunk('parent', 'Recipient may choose one access method:'), relationship='parent')]
        self.assertEqual(classify_batch([item], client, ROOT)[0]['label'], 'RELEVANT')
        item['context'] = []
        item['clause']['text'] = '\u2610 Recipient may use Data for research.'
        self.assertEqual(classify_batch([item], client, ROOT)[0]['label'], 'RELEVANT')


    def test_retired_evaluation_requests_are_rejected_before_model_access(self):
        pipeline = Pipeline(ROOT, 'http://127.0.0.1:11435')
        with patch.object(pipeline, 'make_client') as factory:
            with self.assertRaisesRegex(ValueError, 'classification'):
                pipeline.start_evaluation(dict(task='policy'))
            with self.assertRaisesRegex(ValueError, 'classification'):
                pipeline.start_evaluation(dict(task='both'))
            factory.assert_not_called()


    def test_classification_scores_unknown_labels_as_failed_cases(self):
        from xfed.evaluation import classification_metrics
        result = classification_metrics([dict(gold='RELEVANT', label='invalid')])
        self.assertEqual(result['errors'], 1)
        self.assertEqual(result['accuracy'], 0)

    def test_removed_drafting_and_node_review_routes_cannot_start_jobs(self):
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        from xfed.ui import handler
        pipeline = Pipeline(ROOT, 'http://127.0.0.1:11435')
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler(pipeline))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(pipeline, 'make_client') as factory:
                for route in ('/api/uct', '/api/uct/run', '/api/uct/decision', '/api/uct/approve', '/api/uct/save'):
                    request = Request('http://127.0.0.1:' + str(server.server_port) + route,
                                      data=b'{}', headers={'Content-Type': 'application/json'})
                    with self.assertRaises(HTTPError) as error:
                        urlopen(request)
                    self.assertEqual(error.exception.code, 404)
                factory.assert_not_called()
                self.assertEqual(pipeline.snapshot()['status'], 'idle')
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

if __name__ == '__main__':
    unittest.main()
