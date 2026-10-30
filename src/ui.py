"""Private, loopback-only pipeline dashboard; run with python -m xfed.ui."""

import argparse
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from time import perf_counter
from urllib.parse import urlsplit, parse_qs
from urllib.request import urlopen

from xfed.common import validate, write_json, write_output
from xfed.extraction import extract_document
from xfed.segmentation import segment
from xfed.relevance import classify
from xfed.enrichment import enrich
from xfed.llm import OllamaPool, load_client
from xfed.llm.service import OllamaServicePool, nvidia_gpus
from xfed.policy_generation import generate
from xfed.policy import to_odrl
# The separate review stage is disabled; generation uses its clause evidence check.
from xfed.benchmark import DEFAULT_VARIANTS, VARIANTS, compare_settings
from xfed.evaluation import load_reference, reference_summary, run_evaluation
from xfed import authoring


STAGES = {
    '1.1': ('1 preprocessing/1.1 extraction/document_blocks.json', 1, 'blocks'),
    '1.2': ('1 preprocessing/1.2 segmentation/contract_chunks.json', 2, 'chunks'),
    '1.3': ('1 preprocessing/1.3 relevance/classified_chunks.json', 3, 'chunks'),
    '1.4': ('1 preprocessing/1.4 enrichment/enriched_relevance.json', 4, 'items'),
    '2': ('2 policy_generation/policy.json', 5, 'rules'),
    # '3': ('3 policy_review/policy_review.json', 6, 'clause_reviews'),
}
PREFERRED_MODELS = ['qwen3.5:122b-a10b', 'qwen3.5:27b', 'qwen3.5:35b', 'qwen3.5:9b', 'gemma3:27b', 'llama3.1:70b']


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


class Cancelled(Exception):
    pass


class CachedClient:
    def __init__(self, client, path, cancel, reuse, cache=None, lock=None):
        self.client, self.path, self.cancel = client, path, cancel
        self.settings = {key: getattr(client, key, None) for key in
                         ('model', 'num_ctx', 'num_predict', 'think', 'model_digest', 'sampling')}
        # Cache identity follows the model digest, not endpoint/GPU placement.
        if not self.settings['model_digest']:
            self.settings['url'] = getattr(client, 'url', None)
        self.cache = cache if cache is not None else (read_json(path, {}) if reuse else {})
        self.lock = lock if lock is not None else threading.Lock()

    def __getattr__(self, name):
        return getattr(self.client, name)

    @contextmanager
    def for_chunk(self, chunk_id):
        if self.cancel.is_set():
            raise Cancelled()
        if hasattr(self.client, 'for_chunk'):
            with self.client.for_chunk(chunk_id) as worker:
                yield CachedClient(worker, self.path, self.cancel, False, self.cache, self.lock)
        else:
            yield self

    def generate(self, prompt, value, schema, num_predict=None, think=None, num_ctx=None, num_batch=None,
                 truncate=None, compact_json=False):
        if self.cancel.is_set():
            raise Cancelled()
        output_limit = self.settings['num_predict'] if num_predict is None else num_predict
        effective_think = self.settings['think'] if think is None else think
        if effective_think == 'auto':
            effective_think = getattr(self.client, 'auto_think', None)
        settings = dict(self.settings, num_predict=output_limit, think=effective_think,
                        sampling=deepcopy(getattr(self.client, 'sampling', None)))
        if num_ctx is not None:
            settings['num_ctx'] = num_ctx
        for key, value_setting in (('num_batch', num_batch), ('truncate', truncate)):
            if value_setting is not None:
                settings[key] = value_setting
        if compact_json:
            settings['compact_json'] = True
        key = hashlib.sha256(json.dumps([settings, prompt, value, schema],
                                       sort_keys=True).encode()).hexdigest()
        self.last_cache_key = key
        with self.lock:
            cached = key in self.cache
            result = deepcopy(self.cache[key]) if cached else None
        if not cached:
            options = dict(num_predict=output_limit)
            if effective_think is not None:
                options['think'] = effective_think
            options.update({key: setting for key, setting in
                            (('num_ctx', num_ctx), ('num_batch', num_batch), ('truncate', truncate))
                            if setting is not None})
            if compact_json:
                options['compact_json'] = True
            result = self.client.generate(prompt, value, schema, **options)
            with self.lock:
                self.cache[key] = deepcopy(result)
                write_json(self.path, self.cache)
        if self.cancel.is_set():
            raise Cancelled()
        return result

    def discard_last_response(self):
        """A downstream evidence/semantic validation failure must not become a reusable answer."""
        self.discard_response(getattr(self, 'last_cache_key', None))

    def discard_response(self, key):
        with self.lock:
            if self.cache.pop(key, None) is not None:
                write_json(self.path, self.cache)


class Pipeline:
    def __init__(self, root, ollama_url, config=None):
        self.root = Path(root).resolve()
        self.active_client = None
        self.ollama_url = ollama_url.rstrip('/')
        self.worker_config = load_client(config) if config else None
        self.service_settings = getattr(self.worker_config, 'service', None)
        self.defaults = dict(model=None, timeout=900, num_ctx=32768, num_predict=12288, think='auto',
                             relevance_num_predict=4096, relevance_think='auto', relevance_batch_size=1,
                             relevance_compact=False, relevance_num_ctx=0, relevance_num_batch=None, keep_alive='30m')
        if self.worker_config:
            self.ollama_url = (self.worker_config.source_urls[0] if self.service_settings else
                               self.worker_config.clients[0].url.removesuffix('/api/chat'))
            self.defaults.update({key: getattr(self.worker_config, key) for key in self.defaults})
        # The shared service supplies the model list; parallel runs own private workers.
        OllamaPool([self.ollama_url], 'configuration-check')
        self.lock, self.cancel = threading.Lock(), threading.Event()
        self.job = dict(status='idle', stage=None, completed=0, total=0, message='Ready', log=[])
        self.benchmark_report = None
        saved = sorted((self.root / 'evaluation/runs').glob('*.json'), key=lambda path: path.stat().st_mtime, reverse=True)
        self.evaluation_report = next((value for path in saved
                                       if isinstance(value := read_json(path), dict) and value.get('split') == 'test'
                                       and value.get('task') == 'classification'), None)

    def contracts(self):
        return sorted(p.name for p in (self.root / 'contracts').iterdir()
                      if p.is_dir() and (p / 'input/contract.pdf').is_file())

    def base(self, contract):
        if contract not in self.contracts():
            raise ValueError('Unknown contract.')
        return self.root / 'contracts' / contract

    def path(self, contract, stage):
        if stage not in STAGES:
            raise ValueError('Unknown stage.')
        return self.output(contract) / STAGES[stage][0]

    def output(self, contract):
        return self.base(contract) / 'output'

    def snapshot(self):
        with self.lock:
            job, client = deepcopy(self.job), self.active_client
        if client:
            metadata = client.metadata()
            job['workers'] = [dict(model=member['model'], url=member['url'], main_gpu=member['main_gpu'],
                                   gpu=member.get('gpu'),
                                   gpu_uuid=member.get('gpu_uuid'),
                                   placement_status=member.get('placement_status'),
                                   observed_gpus=member.get('observed_gpus'),
                                   placement_error=member.get('placement_error'),
                                   completed=metadata['completed'][index],
                                   assigned=metadata['assigned'][index], failed=metadata['failed'][index],
                                   active_chunks=metadata['active_chunks'][index],
                                   active_request=member['active_request'], last_request=member['last_request'],
                                   performance=member['performance'])
                              for index, member in enumerate(metadata['models'])]
        return job

    def update(self, **values):
        with self.lock:
            self.job.update(values)

    def progress(self, completed, total, message):
        if self.cancel.is_set():
            raise Cancelled()
        self.update(completed=completed, total=total, message=message)

    def models(self):
        with urlopen(self.ollama_url + '/api/tags', timeout=10) as response:
            return json.load(response)['models']

    def state(self, contract):
        meta = read_json(self.output(contract) / '.runs.json', {})
        stages = {}
        locations = {}
        for stage, (_, version, records) in STAGES.items():
            value = read_json(self.path(contract, stage))
            if stage == '1.2' and isinstance(value, dict):
                locations = {c['chunk_id']: c['source'] for c in value.get('chunks', [])}
            error = None
            if value is not None:
                try:
                    validate(value, version, self.root)
                except Exception as exc:
                    error = str(exc).splitlines()[0]
            stages[stage] = dict(ready=value is not None and error is None,
                                 count=len(value.get(records, value.get('traceability', []))) if isinstance(value, dict) else 0,
                                 error=error, run=meta.get(stage))
        classified = read_json(self.path(contract, '1.3'), {})
        labels = dict.fromkeys(['RELEVANT', 'NOT_RELEVANT', 'NEEDS_CONTEXT'], 0)
        for chunk in classified.get('chunks', []):
            label = chunk.get('classification', {}).get('label')
            if label in labels:
                labels[label] += 1
        report = self.output(contract) / '2 policy_generation/generation_report.json'
        return dict(contract=contract, stages=stages, labels=labels, source_locations=locations,
                    report=read_json(report), review=None, job=self.snapshot(),
                    guided_evaluation=read_json(self.output(contract) / 'guided_evaluation.json'))

    def create_guided(self, terms):
        with self.lock:
            if self.job['status'] in ('running', 'cancelling'):
                raise ValueError('Wait for the current job before creating a contract.')
            return authoring.create(self.root, terms)

    def guided_result(self, contract):
        base = self.base(contract)
        manifest, reference = authoring.load(base)
        evaluation = read_json(self.output(contract) / 'guided_evaluation.json')
        if evaluation and (evaluation.get('pdf_sha256') != manifest['pdf_sha256']
                           or evaluation.get('reference_sha256') != manifest['reference_sha256']):
            raise ValueError('Saved evaluation belongs to another contract revision. Run the pipeline again.')
        return dict(contract=contract, terms=manifest['terms'], title=reference['title'],
                    expected_rules=len(reference['rules']),
                    evaluation=evaluation)

    def make_client(self, request):
        available = {m['name']: m for m in self.models()}
        model = request.get('model')
        if not isinstance(model, str) or model not in available:
            raise ValueError('Choose an installed primary model.')
        parallel = request.get('parallel', False)
        if type(parallel) is not bool:
            raise ValueError('parallel must be true or false.')
        if request.get('worker_models') is not None:
            raise ValueError('Select one model and a worker_count; different worker models are no longer supported in the UI.')
        worker_count = request.get('worker_count', 2 if parallel else 1)
        worker_gpus = request.get('worker_gpus')
        if parallel:
            if type(worker_count) is not int or not 2 <= worker_count <= 32:
                raise ValueError('Choose 2 to 32 workers for the selected model.')
        else:
            if type(worker_count) is not int or worker_count != 1 or worker_gpus is not None:
                raise ValueError('Enable parallelization to configure workers and GPUs.')
        private_workers = parallel and (self.service_settings is not None or worker_gpus is not None)
        if not private_workers and worker_count > 3:
            raise ValueError('Choose at most 3 concurrent requests for the shared model endpoint.')
        pool_type = OllamaServicePool if private_workers else OllamaPool
        execution = dict(worker_count=worker_count, worker_gpus=worker_gpus,
                         service=self.service_settings) if private_workers else dict(workers_per_endpoint=worker_count)
        client = pool_type([self.ollama_url], model, **execution,
                            cancel=self.cancel,
                            timeout=request.get('timeout', self.defaults['timeout']),
                            num_ctx=request.get('num_ctx', self.defaults['num_ctx']),
                            num_predict=request.get('num_predict', self.defaults['num_predict']),
                            think=request.get('think', self.defaults['think']),
                            relevance_num_predict=request.get('relevance_num_predict', self.defaults['relevance_num_predict']),
                            relevance_think=request.get('relevance_think', self.defaults['relevance_think']),
                            relevance_batch_size=request.get('relevance_batch_size', self.defaults['relevance_batch_size']),
                            relevance_compact=request.get('relevance_compact', self.defaults['relevance_compact']),
                            relevance_num_ctx=request.get('relevance_num_ctx', self.defaults['relevance_num_ctx']),
                            relevance_num_batch=request.get('relevance_num_batch', self.defaults['relevance_num_batch']),
                            keep_alive=self.defaults['keep_alive'])
        return client

    def start(self, request):
        contract, stage = request.get('contract'), request.get('stage')
        self.path(contract, stage)
        mode = request.get('mode', 'step')
        if mode not in ('step', 'through'):
            raise ValueError('Unknown run mode.')
        selected = list(STAGES)[:list(STAGES).index(stage) + 1] if mode == 'through' else [stage]
        if mode == 'step' and stage != '1.1':
            previous = list(STAGES)[list(STAGES).index(stage) - 1]
            state = self.state(contract)
            if not state['stages'][previous]['ready']:
                raise ValueError(f'Run stage {previous} first.')
        client = None
        if any(s in ('1.3', '2') for s in selected):
            client = self.make_client(request)
        with self.lock:
            if self.job['status'] in ('running', 'cancelling'):
                raise ValueError('A job is already running. Wait or cancel it first.')
            self.cancel.clear()
            self.active_client = client
            self.job = dict(status='running', contract=contract, stage=selected[0], completed=0,
                            total=0, message='Preparing pipeline inputs', log=[])
        fresh_run = mode == 'through' and stage == '2'
        reuse = request.get('reuse', True) and not fresh_run
        threading.Thread(target=self.run, args=(contract, selected, client, reuse, fresh_run),
                         daemon=True).start()
        return self.snapshot()

    def overview(self):
        contracts = []
        for contract in self.contracts():
            state = self.state(contract)
            report = state['report']
            report_summary = None
            if report is not None:
                report_summary = dict(status=report['status'],
                                      skipped=sum(not c['generated'] for c in report['clauses']),
                                      partial=report.get('summary', {}).get('partial_needs_review', 0),
                                      unresolved=report.get('unresolved_chunks', 0))
                if report.get('warnings'):
                    report_summary['warnings'] = len(report['warnings'])
            contracts.append(dict(contract=contract, stages=state['stages'], labels=state['labels'],
                                  source_bytes=(self.base(contract) / 'input/contract.pdf').stat().st_size,
                                  report=report_summary))
        return dict(contracts=contracts, job=self.snapshot())

    def start_benchmark(self, request):
        contract, model = request.get('contract'), request.get('model')
        document = read_json(self.path(contract, '1.2'))
        if document is None:
            raise ValueError('Run step 1.2 before comparing classification settings.')
        validate(document, 2, self.root)
        if model not in {entry['name'] for entry in self.models()}:
            raise ValueError('Choose one installed model for the comparison.')
        limit = request.get('limit', 24)
        if type(limit) is not int or limit < 0:
            raise ValueError('Benchmark limit must be a nonnegative integer; 0 means all clauses.')
        variants = request.get('variants', list(DEFAULT_VARIANTS))
        if not isinstance(variants, list) or not variants or any(name not in VARIANTS for name in variants):
            raise ValueError('Choose supported benchmark variants.')
        options = dict(num_ctx=request.get('num_ctx', self.defaults['num_ctx']),
                       timeout=request.get('timeout', self.defaults['timeout']),
                       think=request.get('think', self.defaults['think']))
        OllamaPool([self.ollama_url], model, num_predict=128, **options)
        options['variants'] = variants
        options['blocks'] = read_json(self.path(contract, '1.1'), {}).get('blocks', [])
        with self.lock:
            if self.job['status'] in ('running', 'cancelling'):
                raise ValueError('A job is already running. Wait or cancel it first.')
            self.cancel.clear()
            self.active_client = None
            self.benchmark_report = None
            self.job = dict(status='running', contract=contract, stage='1.3', mode='benchmark',
                            completed=0, total=0, message='Comparing settings on a sample; saved outputs are preserved', log=[])
        threading.Thread(target=self.run_benchmark, args=(document, model, limit, options), daemon=True).start()
        return self.snapshot()

    def run_benchmark(self, document, model, limit, options):
        def client_ready(client):
            with self.lock:
                self.active_client = client
        def completed(result, report):
            if result['status'] == 'complete':
                message = (f"{result['name']}: {result['duration_seconds']:.2f}s, "
                           f"{result.get('label_agreement', 0):.1%} labels match baseline")
            else:
                message = f"{result['name']}: {result['error']}"
            with self.lock:
                self.job['log'].append(message)
                self.benchmark_report = deepcopy(report)
        try:
            report = compare_settings(document, self.ollama_url, model, self.root, limit=limit,
                                      progress=self.progress, completed=completed, cancel=self.cancel,
                                      client_ready=client_ready, **options)
            failed = any(result['status'] == 'failed' for result in report['runs'])
            self.finish('failed' if failed else 'complete',
                        'Comparison finished; inspect timings and changed labels before choosing settings')
        except (Cancelled, InterruptedError):
            self.finish('cancelled', 'Comparison cancelled; completed measurements are available.')
        except Exception as error:
            self.finish('failed', str(error))

    def benchmark_result(self):
        with self.lock:
            return deepcopy(self.benchmark_report)

    def start_evaluation(self, request):
        if request.get('split', 'test') != 'test':
            raise ValueError('The UI evaluates the test set only. Use the local CLI for development.')
        if request.get('task', 'classification') != 'classification' or 'policy_mode' in request:
            raise ValueError('Only task 1.3 classification evaluation is supported.')
        options = dict(task='classification', split='test')
        data = load_reference(self.root)
        client = self.make_client(request)
        with self.lock:
            if self.job['status'] in ('running', 'cancelling'):
                raise ValueError('A job is already running. Wait or cancel it first.')
            self.cancel.clear()
            self.active_client = client
            self.evaluation_report = None
            self.job = dict(status='running', contract='reference set', stage='1.3', mode='evaluation',
                            completed=0, total=0, message='Evaluating frozen references; pipeline outputs are preserved', log=[])
        threading.Thread(target=self.run_evaluation, args=(data, client, options), daemon=True).start()
        return self.snapshot()

    def run_evaluation(self, data, client, options):
        def progress(done, total, message):
            self.update(stage='1.3')
            self.progress(done, total, message)

        def completed(report):
            write_json(self.root / 'evaluation/runs' / (report['run_id'] + '.json'), report)
            with self.lock:
                previous_tasks = set((self.evaluation_report or {}).get('execution', {}))
                self.evaluation_report = deepcopy(report)
                new_tasks = set(report['execution']) - previous_tasks
                if new_tasks:
                    self.job['log'].append('Saved ' + ', '.join(sorted(new_tasks)) + ' evaluation predictions')
        try:
            report = run_evaluation(data, client, self.root, progress=progress,
                                    cancel=self.cancel, completed=completed, **options)
            write_json(self.root / 'evaluation/runs' / (report['run_id'] + '.json'), report)
            with self.lock:
                self.evaluation_report = report
            errors = sum(value.get('errors', value.get('failures', 0)) for task in report['metrics'].values() for value in task.values() if not value.get('aggregate'))
            self.finish('complete', f"Evaluation complete; {errors} failed cases; references: {report['reference_status']}")
        except (Cancelled, InterruptedError):
            self.mark_evaluation_stopped('cancelled', 'Evaluation cancelled; completed task predictions are available.')
        except Exception as error:
            self.mark_evaluation_stopped('failed', str(error))

    def mark_evaluation_stopped(self, status, message):
        with self.lock:
            report = deepcopy(self.evaluation_report)
        if report:
            report.update(status=status, error=message, finished_at=datetime.now(timezone.utc).isoformat())
            write_json(self.root / 'evaluation/runs' / (report['run_id'] + '.json'), report)
            with self.lock:
                self.evaluation_report = report
        self.finish(status, message)

    def evaluation_result(self):
        with self.lock:
            report = deepcopy(self.evaluation_report)
        return dict(reference=reference_summary(load_reference(self.root), split='test'), report=report)

    def invalidate(self, contract, stage):
        (self.output(contract) / 'guided_evaluation.json').unlink(missing_ok=True)
        stages = list(STAGES)
        for downstream in stages[stages.index(stage):]:
            self.path(contract, downstream).unlink(missing_ok=True)
        if stages.index(stage) <= stages.index('2'):
            (self.output(contract) / '2 policy_generation/generation_report.json').unlink(missing_ok=True)
        (self.output(contract) / '3 policy_review/policy_review.json').unlink(missing_ok=True)
        metadata = self.output(contract) / '.runs.json'
        runs = read_json(metadata, {})
        runs.pop('3', None)
        for downstream in stages[stages.index(stage):]:
            runs.pop(downstream, None)
        if runs:
            write_json(metadata, runs)
        else:
            metadata.unlink(missing_ok=True)

    def reset_generated(self, contract):
        output = self.output(contract)
        expected = self.root / 'contracts' / contract / 'output'
        if output.resolve() != expected:
            raise ValueError('Reset requires the contract output directory to be inside this repository.')
        paths = [path for path in output.rglob('*.json') if path.is_file() or path.is_symlink()]
        if any(not path.resolve().is_relative_to(expected) for path in paths):
            raise ValueError('Reset cannot remove JSON through a link outside the contract output directory.')
        for path in paths:
            path.unlink()

    def reset(self, request):
        contract = request.get('contract')
        self.base(contract)
        with self.lock:
            if self.job['status'] in ('running', 'cancelling'):
                raise ValueError('Wait for the current job to stop before resetting records.')
            self.reset_generated(contract)
            if self.benchmark_report and self.benchmark_report.get('contract') == contract:
                self.benchmark_report = None
            if self.job.get('contract') == contract:
                self.active_client = None
                self.job = dict(status='idle', stage=None, completed=0, total=0,
                                message='Ready', log=[])
        return self.state(contract)

    def run(self, contract, selected, raw_client, reuse, fresh_run=False):
        base = self.base(contract)
        try:
            if raw_client:
                self.update(message='Preparing model workers')
                raw_client.preflight()
            if fresh_run:
                self.reset_generated(contract)
            client = CachedClient(raw_client, self.output(contract) / '.llm_cache.json', self.cancel, reuse) if raw_client else None
            for stage in selected:
                stage_started = perf_counter()
                if raw_client and stage in ('1.3', '2'):
                    raw_client.reset_assignments()
                self.progress(0, 0, 'Starting stage ' + stage)
                self.update(stage=stage)
                # Keep the required upstream input in memory before invalidation.
                previous = list(STAGES)[list(STAGES).index(stage) - 1] if stage != '1.1' else None
                source = read_json(self.path(contract, previous)) if previous else None
                if previous:
                    validate(source, STAGES[previous][1], self.root)
                self.invalidate(contract, stage)
                if stage == '1.1':
                    result = extract_document(base / 'input/contract.pdf', contract, self.root)
                elif stage == '1.2':
                    result = segment(source, self.root)
                elif stage == '1.3':
                    blocks = read_json(self.path(contract, '1.1'), {}).get('blocks', [])
                    result = classify(source, client, self.root, self.progress, blocks=blocks)
                elif stage == '1.4':
                    blocks = read_json(self.path(contract, '1.1'), {}).get('blocks', [])
                    updated, result = enrich(source, self.root, self.progress, blocks=blocks)
                    self.progress(len(updated['chunks']), len(updated['chunks']), 'Saving enrichment')
                    write_output(self.path(contract, '1.3'), updated, 3, self.root)
                elif stage == '2':
                    result, report = generate(source, client, self.root, self.progress,
                                             classified=read_json(self.path(contract, '1.3')))
                    report['unresolved_chunks'] = self.state(contract)['labels']['NEEDS_CONTEXT']
                    self.progress(len(source['items']), len(source['items']), 'Saving policy')
                    write_json(self.output(contract) / '2 policy_generation/generation_report.json', report)
                else:
                    raise ValueError('Automated policy review is disabled.')
                self.progress(self.snapshot()['completed'], self.snapshot()['total'], 'Saving stage ' + stage)
                if result is not None:
                    write_output(self.path(contract, stage), result, STAGES[stage][1], self.root)
                if stage == '2' and (base / 'authoring.json').is_file():
                    chunks = read_json(self.path(contract, '1.3'))['chunks']
                    evaluation = authoring.evaluate(base, result, chunks, self.root)
                    write_json(self.output(contract) / 'guided_evaluation.json', evaluation)
                meta_path = self.output(contract) / '.runs.json'
                meta = read_json(meta_path, {})
                meta[stage] = dict(time=datetime.now(timezone.utc).isoformat(),
                                   duration_seconds=round(perf_counter() - stage_started, 3),
                                   model=raw_client.model if stage in ('1.3', '2') else None,
                                   options=raw_client.metadata() if raw_client and stage in ('1.3', '2') else None,
                                   status=report['status'] if stage == '2' else 'complete')
                write_json(meta_path, meta)
                with self.lock:
                    self.job['log'].append(f'{stage}: ' + meta[stage]['status']
                                           + f" ({meta[stage]['duration_seconds']:.3f}s)")
                if stage == '2' and report['status'] == 'generation_rejected':
                    self.finish('failed', 'No policy generated. Inspect the rejected clauses in the generation report.')
                    return
            self.finish('complete', 'Finished')
        except (Cancelled, InterruptedError):
            self.finish('cancelled', 'Cancelled. Completed model requests are saved for retry.')
        except Exception as exc:
            self.finish('failed', str(exc))

    def unload_models(self, client):
        result = client.unload()
        messages = []
        if result['unloaded']:
            messages.append('Unloaded: ' + ', '.join(result['unloaded']) + '.')
        for error in result['errors']:
            messages.append(f"Unload failed for {error['model']}: {error['error']}")
        with self.lock:
            self.job['unload'] = result
            self.job['log'].extend(messages)
        return result, ' '.join(messages)

    def finish(self, status, message):
        with self.lock:
            cancelled = self.cancel.is_set() or status == 'cancelled'
            client = self.active_client
            self.job.update(status='cancelling' if cancelled else 'running', message='Cleaning up model workers')
        detail = ''
        if client:
            try:
                if cancelled:
                    client.cancel_requests()
                if isinstance(client, OllamaServicePool):
                    client.close()
                elif cancelled:
                    _, detail = self.unload_models(client)
            except Exception as error:
                detail = 'Could not finish worker cleanup: ' + str(error)
                if not cancelled:
                    status = 'failed'
                with self.lock:
                    self.job['log'].append(detail)
        with self.lock:
            cancelled = cancelled or self.cancel.is_set()
            if cancelled:
                message = message if status == 'cancelled' else 'Cancelled. Completed requests are saved.'
            self.job.update(status='cancelled' if cancelled else status, message=(message + ' ' + detail).strip())

    def start_unload(self, request):
        names = request.get('models')
        available = {model['name'] for model in self.models()}
        if (not isinstance(names, list) or not 1 <= len(names) <= 32 or
                any(not isinstance(name, str) or name not in available for name in names) or
                len(set(names)) != len(names)):
            raise ValueError('Choose 1 to 32 distinct installed models to unload.')
        client = OllamaPool([self.ollama_url], names[0], worker_models=names if len(names) > 1 else None)
        with self.lock:
            if self.job['status'] in ('running', 'cancelling'):
                raise ValueError('Wait for the current job to stop before unloading models.')
            self.cancel.clear()
            self.active_client = client
            self.job = dict(status='running', mode='unload', contract=None, stage=None,
                            completed=0, total=len(names), message='Unloading selected models', log=[])
        threading.Thread(target=self.run_unload, args=(client,), daemon=True).start()
        return self.snapshot()

    def run_unload(self, client):
        try:
            result, message = self.unload_models(client)
            self.update(status='failed' if result['errors'] else 'complete',
                        completed=len(result['unloaded']), message=message)
        except Exception as error:
            self.update(status='failed', message='Unloading failed: ' + str(error))

    def stop(self):
        client = None
        with self.lock:
            if self.job['status'] == 'running' and self.job.get('mode') != 'unload':
                self.cancel.set()
                client = self.active_client
                self.job.update(status='cancelling', message='Stopping requests and unloading run models')
        if client:
            client.cancel_requests()
        return self.snapshot()


def handler(pipeline):
    class Handler(BaseHTTPRequestHandler):
        def send(self, value, status=200, content_type='application/json'):
            body = value if isinstance(value, bytes) else json.dumps(value).encode('utf-8')
            try:
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'self'")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def allowed(self):
            host = urlsplit('http://' + self.headers.get('Host', '')).hostname
            origin = self.headers.get('Origin')
            return host in ('localhost', '127.0.0.1', '[::1]', '::1') and (
                not origin or origin == 'http://' + self.headers.get('Host'))

        def do_GET(self):
            if not self.allowed():
                self.send(dict(error='Use the localhost SSH tunnel.'), 403)
                return
            route = urlsplit(self.path)
            query = parse_qs(route.query)
            try:
                if route.path == '/':
                    self.send(Path(__file__).with_name('ui.html').read_bytes(), content_type='text/html; charset=utf-8')
                elif route.path == '/api/config':
                    try:
                        devices, gpu_error = nvidia_gpus(), None
                    except RuntimeError as error:
                        devices, gpu_error = [], str(error)
                    self.send(dict(contracts=pipeline.contracts(), ollama_url=pipeline.ollama_url,
                                   contract_titles={c: read_json(pipeline.base(c) / 'authoring.json', {}).get('terms', {}).get('title', c)
                                                    for c in pipeline.contracts() if c.startswith('guided_')},
                                   model_defaults=pipeline.defaults,
                                   gpu_devices=devices if pipeline.service_settings is not None else [],
                                   gpu_detection_error=gpu_error, max_workers=32 if pipeline.service_settings is not None else 3,
                                   shared_endpoint_workers=pipeline.service_settings is None,
                                   worker_gpu_selection=True,
                                   same_model_workers=True,
                                   model_unloading=True,
                                   preferred_models=PREFERRED_MODELS))
                elif route.path == '/api/models':
                    self.send(dict(models=pipeline.models()))
                elif route.path == '/api/state':
                    self.send(pipeline.state(query['contract'][0]))
                elif route.path == '/api/overview':
                    self.send(pipeline.overview())
                elif route.path == '/api/benchmark':
                    self.send(dict(report=pipeline.benchmark_result()))
                elif route.path == '/api/evaluation':
                    self.send(pipeline.evaluation_result())
                elif route.path == '/api/authoring':
                    self.send(pipeline.guided_result(query['contract'][0]) if 'contract' in query
                              else dict(defaults=authoring.DEFAULTS, template_version=authoring.VERSION))
                elif route.path in ('/api/output', '/api/pdf'):
                    contract = query['contract'][0]
                    if route.path == '/api/pdf':
                        self.send((pipeline.base(contract) / 'input/contract.pdf').read_bytes(), content_type='application/pdf')
                    else:
                        path = pipeline.path(contract, query['stage'][0])
                        value = read_json(path)
                        if value is None:
                            self.send(dict(error='No completed output for this stage.'), 404)
                        else:
                            if query.get('format') == ['odrl']:
                                if query['stage'][0] != '2':
                                    raise ValueError('ODRL export is available for policy creation only.')
                                validate(value, 5, pipeline.root)
                                value = to_odrl(value, pipeline.root)
                            self.send(value)
                else:
                    self.send(dict(error='Not found'), 404)
            except (KeyError, ValueError) as exc:
                self.send(dict(error=str(exc)), 400)
            except Exception as exc:
                self.send(dict(error=str(exc)), 502)

        def do_POST(self):
            if not self.allowed():
                self.send(dict(error='Cross-origin requests are not permitted.'), 403)
                return
            try:
                size = int(self.headers.get('Content-Length', 0))
                if not 0 < size <= 65536:
                    raise ValueError('Invalid request size.')
                request = json.loads(self.rfile.read(size))
                if not isinstance(request, dict):
                    raise ValueError('Expected a JSON object.')
                if self.path == '/api/run':
                    self.send(pipeline.start(request), 202)
                elif self.path == '/api/reset':
                    self.send(pipeline.reset(request))
                elif self.path == '/api/benchmark':
                    self.send(pipeline.start_benchmark(request), 202)
                elif self.path == '/api/evaluation':
                    self.send(pipeline.start_evaluation(request), 202)
                elif self.path == '/api/authoring/preview':
                    content = authoring.draft(request)
                    self.send(dict(title=content['title'], clauses=content['clauses'], expected_rules=len(content['rules'])))
                elif self.path == '/api/authoring/create':
                    self.send(pipeline.create_guided(request), 201)
                elif self.path == '/api/cancel':
                    self.send(pipeline.stop())
                elif self.path == '/api/unload':
                    self.send(pipeline.start_unload(request), 202)
                else:
                    self.send(dict(error='Not found'), 404)
            except Exception as exc:
                self.send(dict(error=str(exc)), 400)

        def log_message(self, *args):
            pass
    return Handler


def main():
    parser = argparse.ArgumentParser(description='XFed private pipeline UI')
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--ollama-url', help='Ollama API endpoint')
    parser.add_argument('--config', type=Path, help='Model YAML supplying the endpoint and generation defaults')
    args = parser.parse_args()
    config = args.config
    if config is None and args.ollama_url is None and (args.root / 'config/models.yaml').is_file():
        config = args.root / 'config/models.yaml'
    pipeline = Pipeline(args.root, args.ollama_url or 'http://localhost:11434', config)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler(pipeline))
    print(f'XFed UI: http://127.0.0.1:{server.server_port}', flush=True)
    print(f'Ollama API: {pipeline.ollama_url}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pipeline.stop()
    finally:
        pipeline.stop()
        if pipeline.active_client:
            pipeline.active_client.close()
        server.server_close()


if __name__ == '__main__':
    main()
