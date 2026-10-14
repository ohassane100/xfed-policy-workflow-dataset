"""Private, loopback-only pipeline dashboard; run with python -m xfed.ui."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit, parse_qs
from urllib.request import urlopen

from xfed.common import validate, write_json, write_output
from xfed.extraction import extract_document
from xfed.segmentation import segment
from xfed.relevance import classify
from xfed.enrichment import enrich
from xfed.llm import OllamaClient
from xfed.policy_generation import generate


STAGES = {
    '1.1': ('1 preprocessing/1.1 extraction/document_blocks.json', 1, 'blocks'),
    '1.2': ('1 preprocessing/1.2 segmentation/contract_chunks.json', 2, 'chunks'),
    '1.3': ('1 preprocessing/1.3 relevance/classified_chunks.json', 3, 'chunks'),
    '1.4': ('1 preprocessing/1.4 enrichment/enriched_relevance.json', 4, 'items'),
    '2': ('2 policy_generation/policy.json', 5, 'traceability'),
}
PREFERRED_MODELS = ['gemma3:27b', 'llama3.1:70b', 'deepseek-r1:70b']


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


class Cancelled(Exception):
    pass


class CachedClient:
    def __init__(self, client, path, cancel, reuse):
        self.client, self.path, self.cancel = client, path, cancel
        self.settings = {key: getattr(client, key, None) for key in
                         ('url', 'model', 'num_ctx', 'num_predict', 'think', 'model_digest')}
        self.cache = read_json(path, {}) if reuse else {}

    def generate(self, prompt, value, schema):
        if self.cancel.is_set():
            raise Cancelled()
        key = hashlib.sha256(json.dumps([self.settings, prompt, value, schema],
                                       sort_keys=True).encode()).hexdigest()
        if key not in self.cache:
            self.cache[key] = self.client.generate(prompt, value, schema)
            write_json(self.path, self.cache)
        if self.cancel.is_set():
            raise Cancelled()
        return deepcopy(self.cache[key])


class Pipeline:
    def __init__(self, root, ollama_url):
        self.root = Path(root).resolve()
        self.ollama_url = ollama_url.rstrip('/')
        self.lock, self.cancel = threading.Lock(), threading.Event()
        self.job = dict(status='idle', stage=None, completed=0, total=0, message='Ready', log=[])

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
        return self.base(contract) / 'output' / STAGES[stage][0]

    def snapshot(self):
        with self.lock:
            return deepcopy(self.job)

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
        meta = read_json(self.base(contract) / 'output/.runs.json', {})
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
        report = self.base(contract) / 'output/2 policy_generation/generation_report.json'
        return dict(contract=contract, stages=stages, labels=labels, source_locations=locations,
                    report=read_json(report), job=self.snapshot())

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
            available = {m['name']: m for m in self.models()}
            if request.get('model') not in available:
                raise ValueError('Select a model installed on this Ollama server.')
            client = OllamaClient(self.ollama_url, request['model'],
                                  timeout=request.get('timeout', 600), num_ctx=request.get('num_ctx', 16384),
                                  num_predict=request.get('num_predict', 4096), think=request.get('think'))
            client.model_digest = available[request['model']].get('digest')
        with self.lock:
            if self.job['status'] in ('running', 'cancelling'):
                raise ValueError('A job is already running. Wait or cancel it first.')
            self.cancel.clear()
            self.job = dict(status='running', contract=contract, stage=selected[0], completed=0,
                            total=0, message='Starting', log=[])
        threading.Thread(target=self.run, args=(contract, selected, client, request.get('reuse', True)),
                         daemon=True).start()
        return self.snapshot()

    def overview(self):
        contracts = []
        for contract in self.contracts():
            state = self.state(contract)
            report = state['report']
            contracts.append(dict(contract=contract, stages=state['stages'], labels=state['labels'],
                                  source_bytes=(self.base(contract) / 'input/contract.pdf').stat().st_size,
                                  report=None if report is None else dict(
                                      status=report['status'],
                                      skipped=sum(not c['generated'] for c in report['clauses']),
                                      unresolved=report.get('unresolved_chunks', 0))))
        return dict(contracts=contracts, job=self.snapshot())

    def invalidate(self, contract, stage):
        stages = list(STAGES)
        for downstream in stages[stages.index(stage):]:
            self.path(contract, downstream).unlink(missing_ok=True)
        (self.base(contract) / 'output/2 policy_generation/generation_report.json').unlink(missing_ok=True)
        metadata = self.base(contract) / 'output/.runs.json'
        runs = read_json(metadata, {})
        for downstream in stages[stages.index(stage):]:
            runs.pop(downstream, None)
        write_json(metadata, runs)

    def run(self, contract, selected, raw_client, reuse):
        base = self.base(contract)
        try:
            client = CachedClient(raw_client, base / 'output/.llm_cache.json', self.cancel, reuse) if raw_client else None
            for stage in selected:
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
                    write_json(base / 'output/2 policy_generation/generation_report.json', report)
                self.progress(self.snapshot()['completed'], self.snapshot()['total'], 'Saving stage ' + stage)
                if result is not None:
                    write_output(self.path(contract, stage), result, STAGES[stage][1], self.root)
                meta_path = base / 'output/.runs.json'
                meta = read_json(meta_path, {})
                meta[stage] = dict(time=datetime.now(timezone.utc).isoformat(),
                                   model=raw_client.model if stage in ('1.3', '2') else None,
                                   options=vars(raw_client) if raw_client and stage in ('1.3', '2') else None,
                                   status='complete' if result is not None else 'no_supported_rules')
                write_json(meta_path, meta)
                with self.lock:
                    self.job['log'].append(f'{stage}: ' + ('saved' if result is not None else 'no supported rules'))
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
                                   preferred_models=PREFERRED_MODELS))
                elif route.path == '/api/models':
                    self.send(dict(models=pipeline.models()))
                elif route.path == '/api/state':
                    self.send(pipeline.state(query['contract'][0]))
                elif route.path == '/api/overview':
                    self.send(pipeline.overview())
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
    parser.add_argument('--ollama-url', default='http://localhost:11434')
    args = parser.parse_args()
    pipeline = Pipeline(args.root, args.ollama_url)
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler(pipeline))
    print(f'XFed UI: http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pipeline.stop()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
