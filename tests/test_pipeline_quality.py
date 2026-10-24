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
from xfed.llm.service import OllamaServicePool
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
            self.assertEqual(len(requests), 9)
            for offset in (0, 3, 6):
                self.assertEqual({p['model'] for p in requests[offset:offset+3]}, set(names))
                self.assertTrue(all('main_gpu' not in p['options'] for p in requests[offset:offset+3]))
            self.assertEqual(len(pool.clients), 3)
            self.assertEqual(pool.metadata()['completed'], [3, 3, 3])
            self.assertTrue(all(worker['performance']['responses'] == 3 for worker in pool.metadata()['models']))
            for payload in requests:
                self.assertIs(payload['think'], False)
                self.assertIs(payload['truncate'], False)
                self.assertIs(payload['shift'], False)
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
                     patch.object(OllamaServicePool, '_environment', return_value={'OLLAMA_MODELS': '/existing/models'}), \
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
                            self.assertIn('CUDA_VISIBLE_DEVICES=' + str(gpus[index]), command)
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

    def test_split_manifest_counts_and_missing_predictions(self):
        from xfed.evaluation import digest, load_reference, score_report
        data = load_reference(ROOT)
        for split, size in [('dev', 100), ('test', 200)]:
            selected = [c['case_id'] for c in data['classification'] if c['split'] == split]
            self.assertEqual(len(selected), size)
            report = dict(reference_version=data['version'], reference_sha256=digest(data),
                          split=split, task='classification', source='real',
                          selected_cases=dict(classification=selected), predictions={}, execution={})
            scored = score_report(data, report, ROOT)
            self.assertEqual(scored['metrics']['classification']['real/coverage']['errors'], size)
            self.assertEqual(scored['metrics']['classification']['real/coverage']['accuracy'], 0)
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
            return dict(policy=None, reason='Simulated abstention.') if 'contract_id' in value else dict(label='RELEVANT')
        data = load_reference(ROOT)
        for split, count in [('dev', 100), ('test', 200)]:
            report = run_evaluation(data, Runner(answer), ROOT, split=split)
            self.assertEqual(len(report['predictions']['classification']), count)
            self.assertEqual(len(report['predictions']['policy']), count // 5)
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
        self.assertEqual(summary['counts']['classification']['total'], 200)
        self.assertEqual(summary['counts']['policy']['total'], 40)
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


if __name__ == '__main__':
    unittest.main()
