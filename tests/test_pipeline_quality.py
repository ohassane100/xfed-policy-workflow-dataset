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
from xfed.llm import OllamaPool
from xfed.llm.service import OllamaServicePool, nvidia_gpus
from xfed.policy_generation import (compile_plan, complete_fragment, generate, generate_answer, generate_item,
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


def plan(item, kind='obligation', **changes):
    rule = dict(kind=kind, actor='Provider', action='delete', asset='supplied data',
                conditions=[dict(leftOperand='elapsedTime', operator='lteq', rightOperand='PT12H'),
                            dict(leftOperand='event', operator='eq', rightOperand='notice')],
                evidence=[dict(chunk_id=item['chunk_id'], quote=item['text'])], duties=[], remedies=[])
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
        self.assertEqual(map_chunks(items, operation, Pool()), ['0', '1', '2'])

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
                response.update(coverage='MISSING_RULES', missing_rules=['Provider must delete within 12 hours after notice.'])
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
            return dict(reviewed(value), coverage='MISSING_RULES', missing_rules=['Missing acknowledgement duty.'])
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
            return dict(reviewed(value), coverage='MISSING_RULES', missing_rules=['Acknowledgement duty.'])
        client = PlanClient(answer, check)
        client.last_request = dict(thinking_chars=10, answer_chars=0)
        output, entry, failed = generate_item(self.item, client, 'prompt', 'contract', response_schema(ROOT), ROOT)
        self.assertFalse(failed)
        self.assertIsNotNone(output)
        self.assertEqual(entry['outcome'], 'partial_needs_review')
        self.assertIn('token limit', entry['repair_error'])

    def test_reviewed_missing_evidence_is_an_abstention_not_a_transport_failure(self):
        answer = dict(rules=[], unrepresented=[dict(evidence=[dict(chunk_id='c1', quote=self.item['text'])],
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

    def test_rule_mismatches_explain_missing_and_unexpected_rules_without_softening_scores(self):
        from xfed.evaluation import policy_counts
        data = dict(inputs={'i': dict(contract_id='contract', clause=chunk(), policy_context=[], context=[])},
                    contexts={}, value_aliases={}, contract_aliases={}, action_aliases={})
        gold = dict(kind='obligation', action='delete', target='supplied data', assignee='Provider')
        case = dict(input_id='i', gold=dict(rules=[gold]))
        counts = policy_counts(case, dict(policy=fragment(self.item, 'permission')), data, ROOT)
        self.assertEqual((counts['tp'], counts['fp'], counts['fn']), (0, 1, 1))
        self.assertEqual(counts['rule_differences']['missing'][0]['kind'], 'obligation')
        self.assertEqual(counts['rule_differences']['unexpected'][0]['kind'], 'permission')

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
        for split, count, policy_count in [('dev', 300, 60), ('test', 76, 76)]:
            report = run_evaluation(data, Runner(answer), ROOT, split=split)
            self.assertEqual(len(report['predictions']['classification']), count)
            self.assertEqual(len(report['predictions']['policy']), policy_count)
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
        self.assertEqual(summary['counts']['policy']['total'], 76)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline = Pipeline(root, 'http://127.0.0.1:11434')
            with self.assertRaisesRegex(ValueError, 'test set only'):
                pipeline.start_evaluation(dict(split='dev'))
            report = dict(run_id='simulated-test-run', split='test', status='complete',
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

    def test_contract05_is_the_complete_independent_source_for_both_tasks(self):
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
        for task in ('classification', 'policy'):
            cases = [case for case in data[task] if case['split'] == 'test']
            self.assertEqual({case['input_id'] for case in cases}, set(inputs))
            self.assertTrue(all(case['gold']['review_status'] == 'draft' for case in cases))
        known = {chunk['chunk_id']: chunk for chunk in segmented['chunks']}
        for value in inputs.values():
            for field in ('context', 'policy_context'):
                for identifier in value[field]:
                    context = data['contexts'][identifier]
                    original = known[context['chunk_id']]
                    self.assertIn(context['text'], original['text'])
                    self.assertEqual(context['source'], original['source'])

    def test_full_document_reference_rejects_omissions_and_source_edits(self):
        from unittest.mock import patch
        from xfed.evaluation import load_reference
        original = load_reference(ROOT)
        for task in ('classification', 'policy'):
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

    def test_contract05_policy_gold_roundtrip_and_all_abstain_baseline(self):
        from xfed.evaluation import digest, input_for, load_reference, policy_counts, score_report
        data = load_reference(ROOT)
        cases = [case for case in data['policy'] if case['split'] == 'test']
        predictions = {}
        for case in cases:
            item = input_for(data, case, True)
            fragment = None
            for option in [case['gold']['rules'], *case['gold'].get('alternatives', [])]:
                policy, traces = {}, []
                for position, gold in enumerate(option):
                    rule = deepcopy(gold)
                    kind = rule.pop('kind')
                    rule['uid'] = f'r{position}'
                    if kind in ('permission', 'prohibition'):
                        rule['assigner'] = 'Equinor and the former Volve license partners'
                    policy.setdefault(kind, []).append(rule)
                    traces.append(dict(ruleUid=rule['uid'], ruleType=kind,
                        sourceClauses=[dict(chunk_id=item['chunk_id'], relationship='primary')]))
                candidate = complete_fragment(dict(odrlPolicy=policy, traceability=traces), 'contract_05', item) if option else None
                self.assertTrue(policy_counts(case, dict(policy=candidate), data, ROOT)['exact'])
                if option == case['gold']['rules']:
                    fragment = candidate
            predictions[case['case_id']] = dict(policy=fragment, input_sha256=digest(item))
        report = dict(reference_version=data['version'], reference_sha256=digest(data), split='test', task='policy',
            selected_cases=dict(policy=list(predictions)), predictions=dict(policy=predictions), execution={})
        scored = score_report(data, report, ROOT)
        complete = scored['metrics']['policy']['real/full_document']
        self.assertEqual(complete['n'], 76)
        self.assertEqual(complete['canonical_rules']['f1'], 1)
        self.assertEqual(complete['exact_case_accuracy'], 1)
        self.assertEqual(scored['reference_status'], 'provisional')
        for prediction in predictions.values():
            prediction['policy'] = None
        scored = score_report(data, report, ROOT)
        self.assertEqual(scored['metrics']['policy']['real/full_document']['canonical_rules']['recall'], 0)
        self.assertEqual(scored['metrics']['policy']['real/operative']['exact_case_accuracy'], 0)
        with self.assertRaisesRegex(ValueError, 'Independent review'):
            score_report(data, report, ROOT, require_reviewed=True)


if __name__ == '__main__':
    unittest.main()
