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
from xfed.policy_generation import generate
from xfed.benchmark import DEFAULT_VARIANTS, VARIANTS, compare_settings
from xfed.evaluation import load_reference, reference_summary, run_evaluation


STAGES = {
    '1.1': ('1 preprocessing/1.1 extraction/document_blocks.json', 1, 'blocks'),
    '1.2': ('1 preprocessing/1.2 segmentation/contract_chunks.json', 2, 'chunks'),
    '1.3': ('1 preprocessing/1.3 relevance/classified_chunks.json', 3, 'chunks'),
    '1.4': ('1 preprocessing/1.4 enrichment/enriched_relevance.json', 4, 'items'),
    '2': ('2 policy_generation/policy.json', 5, 'traceability'),
}
PREFERRED_MODELS = ['llama3.1:70b', 'deepseek-r1:70b', 'gemma3:27b']


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
                         ('model', 'num_ctx', 'num_predict', 'think', 'model_digest')}
        # Verified replicas share cached answers; endpoint/GPU placement is not semantic.
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
        settings = dict(self.settings, num_predict=output_limit, think=effective_think)
        if num_ctx is not None:
            settings['num_ctx'] = num_ctx
        for key, value_setting in (('num_batch', num_batch), ('truncate', truncate)):
            if value_setting is not None:
                settings[key] = value_setting
        if compact_json:
            settings['compact_json'] = True
        key = hashlib.sha256(json.dumps([settings, prompt, value, schema],
                                       sort_keys=True).encode()).hexdigest()
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


class Pipeline:
    def __init__(self, root, ollama_url, config=None, replica_urls=None, workers_per_endpoint=None):
        self.root = Path(root).resolve()
        self.active_client = None
        self.ollama_url = ollama_url.rstrip('/')
        self.worker_config = load_client(config) if config else None
        self.defaults = dict(model=None, timeout=600, num_ctx=32768, num_predict=4096, think=None,
                             relevance_num_predict=128, relevance_think=None, relevance_batch_size=4,
                             relevance_compact=True, relevance_num_ctx=0, relevance_num_batch=None, keep_alive='30m')
        if self.worker_config:
            self.ollama_url = self.worker_config.clients[0].url.removesuffix('/api/chat')
            self.defaults.update({key: getattr(self.worker_config, key) for key in self.defaults})
        self.replica_urls = replica_urls if replica_urls is not None else (
            [c.url.removesuffix('/api/chat') for c in self.worker_config.clients]
            if self.worker_config else [self.ollama_url])
        self.workers_per_endpoint = workers_per_endpoint if workers_per_endpoint is not None else (
            self.worker_config.workers_per_endpoint if self.worker_config else 1)
        # Validate endpoint/concurrency settings at startup, before any output changes.
        validated = OllamaPool(self.replica_urls, 'configuration-check',
                               workers_per_endpoint=self.workers_per_endpoint)
        self.worker_capacity = validated.workers
        self.ollama_url = self.replica_urls[0].rstrip('/')
        self.lock, self.cancel = threading.Lock(), threading.Event()
        self.job = dict(status='idle', stage=None, completed=0, total=0, message='Ready', log=[])
        self.benchmark_report = None
        self.evaluation_report = None

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
                                   completed=metadata['completed'][index])
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
        urls = self.replica_urls
        common = None
        for url in urls:
            with urlopen(url + '/api/tags', timeout=10) as response:
                models = {m['name']: m for m in json.load(response)['models']}
            common = models if common is None else {
                name: model for name, model in common.items()
                if name in models and model.get('digest') == models[name].get('digest')}
        if not common and len(urls) > 1:
            raise ValueError('GPU workers do not share an installed model with the same digest.')
        return list(common.values())

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
                                 count=len(value.get(records, [])) if isinstance(value, dict) else 0,
                                 error=error, run=meta.get(stage))
        classified = read_json(self.path(contract, '1.3'), {})
        labels = dict.fromkeys(['RELEVANT', 'NOT_RELEVANT', 'NEEDS_CONTEXT'], 0)
        for chunk in classified.get('chunks', []):
            label = chunk.get('classification', {}).get('label')
            if label in labels:
                labels[label] += 1
        report = self.output(contract) / '2 policy_generation/generation_report.json'
        return dict(contract=contract, stages=stages, labels=labels, source_locations=locations,
                    report=read_json(report), job=self.snapshot())

    def make_client(self, request):
        available = {m['name']: m for m in self.models()}
        model = request.get('model')
        if not isinstance(model, str) or model not in available:
            raise ValueError('Choose an installed primary model.')
        parallel = request.get('parallel', False)
        if type(parallel) is not bool:
            raise ValueError('parallel must be true or false.')
        worker_models = request.get('worker_models')
        if parallel:
            if (not isinstance(worker_models, list) or not 2 <= len(worker_models) <= 32 or
                    any(not isinstance(name, str) or name not in available for name in worker_models) or
                    len(set(worker_models)) != len(worker_models) or worker_models[0] != model):
                raise ValueError('Choose 2 to 32 distinct installed models, starting with the primary model.')
            workers = len(worker_models)
            endpoints, workers_per_endpoint = [self.ollama_url], 1
        else:
            if worker_models is not None:
                raise ValueError('Enable parallelization to use multiple models.')
            workers = request.get('workers', self.worker_capacity)
            if type(workers) is not int or not 1 <= workers <= self.worker_capacity:
                raise ValueError(f'workers must be between 1 and {self.worker_capacity}.')
            # Use all configured replicas before adding requests to an endpoint.
            endpoints = self.replica_urls[:min(workers, len(self.replica_urls))]
            workers_per_endpoint = self.workers_per_endpoint
        client = OllamaPool(endpoints, model, workers_per_endpoint=workers_per_endpoint,
                            workers=workers, worker_models=worker_models,
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
        client.preflight()
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
            if not self.state(contract)['stages'][previous]['ready']:
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
                            total=0, message='Starting', log=[])
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
        OllamaPool([self.replica_urls[0]], model, num_predict=128, **options)
        options['variants'] = variants
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
            report = compare_settings(document, self.replica_urls[0], model, self.root, limit=limit,
                                      progress=self.progress, completed=completed, cancel=self.cancel, **options)
            failed = any(result['status'] == 'failed' for result in report['runs'])
            self.update(status='failed' if failed else 'complete',
                        message='Comparison finished; inspect timings and changed labels before choosing settings')
        except (Cancelled, InterruptedError):
            self.update(status='cancelled', message='Comparison cancelled; completed measurements are available')
        except Exception as error:
            self.update(status='failed', message=str(error))

    def benchmark_result(self):
        with self.lock:
            return deepcopy(self.benchmark_report)

    def start_evaluation(self, request):
        options = dict(task=request.get('task', 'both'), split=request.get('split', 'dev'),
                       source=request.get('source', 'all'))
        if (options['task'] not in ('classification', 'policy', 'both') or options['split'] not in ('dev', 'test')
                or options['source'] not in ('all', 'real', 'synthetic')):
            raise ValueError('Choose a supported evaluation task, split and source type.')
        data = load_reference(self.root)
        if not any(case['split'] == options['split'] and (options['source'] == 'all' or case['source_type'] == options['source'])
                   for name in ('classification', 'policy') if options['task'] in (name, 'both') for case in data[name]):
            raise ValueError('No reference cases match this evaluation selection.')
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
        def completed(report):
            with self.lock:
                self.evaluation_report = deepcopy(report)
                self.job['log'].append('Saved ' + ', '.join(report['execution']) + ' evaluation predictions')
        try:
            report = run_evaluation(data, client, self.root, progress=self.progress,
                                    cancel=self.cancel, completed=completed, **options)
            with self.lock:
                self.evaluation_report = report
            errors = sum(value.get('errors', value.get('failures', 0)) for task in report['metrics'].values() for value in task.values() if not value.get('aggregate'))
            self.update(status='complete', message=f"Evaluation complete; {errors} failed cases; references: {report['reference_status']}")
        except (Cancelled, InterruptedError):
            self.update(status='cancelled', message='Evaluation cancelled; completed task predictions are available')
        except Exception as error:
            self.update(status='failed', message=str(error))

    def evaluation_result(self):
        with self.lock:
            report = deepcopy(self.evaluation_report)
        return dict(reference=reference_summary(load_reference(self.root)), report=report)

    def invalidate(self, contract, stage):
        stages = list(STAGES)
        for downstream in stages[stages.index(stage):]:
            self.path(contract, downstream).unlink(missing_ok=True)
        (self.output(contract) / '2 policy_generation/generation_report.json').unlink(missing_ok=True)
        metadata = self.output(contract) / '.runs.json'
        runs = read_json(metadata, {})
        for downstream in stages[stages.index(stage):]:
            runs.pop(downstream, None)
        if runs:
            write_json(metadata, runs)
        else:
            metadata.unlink(missing_ok=True)

    def reset_generated(self, contract):
        self.invalidate(contract, '1.1')
        (self.output(contract) / '.llm_cache.json').unlink(missing_ok=True)

    def run(self, contract, selected, raw_client, reuse, fresh_run=False):
        base = self.base(contract)
        try:
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
                    result = classify(source, client, self.root, self.progress)
                elif stage == '1.4':
                    updated, result = enrich(source, self.root, self.progress)
                    self.progress(len(updated['chunks']), len(updated['chunks']), 'Saving enrichment')
                    write_output(self.path(contract, '1.3'), updated, 3, self.root)
                else:
                    result, report = generate(source, client, self.root, self.progress)
                    report['unresolved_chunks'] = self.state(contract)['labels']['NEEDS_CONTEXT']
                    self.progress(len(source['items']), len(source['items']), 'Saving policy draft')
                    write_json(self.output(contract) / '2 policy_generation/generation_report.json', report)
                self.progress(self.snapshot()['completed'], self.snapshot()['total'], 'Saving stage ' + stage)
                if result is not None:
                    write_output(self.path(contract, stage), result, STAGES[stage][1], self.root)
                meta_path = self.output(contract) / '.runs.json'
                meta = read_json(meta_path, {})
                meta[stage] = dict(time=datetime.now(timezone.utc).isoformat(),
                                   duration_seconds=round(perf_counter() - stage_started, 3),
                                   model=raw_client.model if stage in ('1.3', '2') else None,
                                   options=raw_client.metadata() if raw_client and stage in ('1.3', '2') else None,
                                   status=report['status'] if stage == '2' else 'complete')
                write_json(meta_path, meta)
                with self.lock:
                    self.job['log'].append(f'{stage}: ' + (report['status'] if stage == '2' else 'saved')
                                           + f" ({meta[stage]['duration_seconds']:.3f}s)")
                if stage == '2' and report['status'] == 'generation_rejected':
                    self.update(status='failed', message='No policy generated. Inspect the rejected clauses in the generation report.')
                    return
            self.update(status='complete', message='Finished')
        except Cancelled:
            self.update(status='cancelled', message='Cancelled. Completed model requests are saved for retry.')
        except Exception as exc:
            self.update(status='failed', message=str(exc))

    def stop(self):
        with self.lock:
            if self.job['status'] == 'running':
                self.cancel.set()
                self.job.update(status='cancelling', message='Waiting for the current operation to finish')
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
                    self.send(dict(contracts=pipeline.contracts(), ollama_url=pipeline.ollama_url,
                                   model_defaults=pipeline.defaults,
                                   workers=pipeline.worker_capacity,
                                   replica_urls=pipeline.replica_urls,
                                   workers_per_endpoint=pipeline.workers_per_endpoint,
                                   preferred_models=PREFERRED_MODELS))
                elif route.path == '/api/models':
                    self.send(dict(models=pipeline.models(),
                                   workers=pipeline.worker_capacity))
                elif route.path == '/api/state':
                    self.send(pipeline.state(query['contract'][0]))
                elif route.path == '/api/overview':
                    self.send(pipeline.overview())
                elif route.path == '/api/benchmark':
                    self.send(dict(report=pipeline.benchmark_result()))
                elif route.path == '/api/evaluation':
                    self.send(pipeline.evaluation_result())
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
                elif self.path == '/api/benchmark':
                    self.send(pipeline.start_benchmark(request), 202)
                elif self.path == '/api/evaluation':
                    self.send(pipeline.start_evaluation(request), 202)
                elif self.path == '/api/cancel':
                    self.send(pipeline.stop())
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
    parser.add_argument('--ollama-url', help='Override automatic worker configuration with one server')
    parser.add_argument('--config', type=Path, help='Model YAML supplying worker endpoints and concurrency')
    parser.add_argument('--replica-urls', help='Comma-separated Ollama endpoints serving the same model')
    parser.add_argument('--workers-per-endpoint', type=int, help='Concurrent requests per endpoint; match OLLAMA_NUM_PARALLEL')
    args = parser.parse_args()
    replica_urls = [url.strip() for url in args.replica_urls.split(',')] if args.replica_urls else None
    config = args.config
    if config is None and args.ollama_url is None and (args.root / 'config/models.yaml').is_file():
        config = args.root / 'config/models.yaml'
    pipeline = Pipeline(args.root, args.ollama_url or 'http://localhost:11434', config,
                        replica_urls, args.workers_per_endpoint)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler(pipeline))
    print(f'XFed UI: http://127.0.0.1:{server.server_port}', flush=True)
    print(f'Single-model request slots: {pipeline.worker_capacity}; endpoints: ' +
          ', '.join(pipeline.replica_urls), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pipeline.stop()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
