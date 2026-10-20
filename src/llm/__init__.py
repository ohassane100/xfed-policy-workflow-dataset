import json
from contextlib import contextmanager
import os
from pathlib import Path
from queue import Queue
import shlex
import shutil
import signal
import socket
import subprocess
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


@contextmanager
def gpu_workers(gpu_ids, base_url, model, log_dir, num_ctx=32768, timeout=300):
    """Serve the installed model through an isolated API process on each GPU."""
    devices = [int(value) for value in gpu_ids.split(',')]
    if any(gpu < 0 for gpu in devices) or len(set(devices)) != len(devices):
        raise ValueError('--gpus must contain distinct nonnegative GPU indices.')
    binary = os.environ.get('OLLAMA_BIN') or shutil.which('ollama') or '/local/ollama/bin/ollama'
    if not os.access(binary, os.X_OK):
        raise ValueError('GPU startup needs the Ollama server executable on Gorina5; '
                         'set OLLAMA_BIN to its path. API access alone cannot assign GPUs.')
    cache = os.environ.get('OLLAMA_MODELS')
    if not cache:
        request = Request(base_url.rstrip('/') + '/api/show',
                          data=json.dumps(dict(model=model)).encode(),
                          headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=10) as response:
            modelfile = json.load(response).get('modelfile', '')
        for line in modelfile.splitlines():
            if line.startswith('FROM '):
                blob = Path(shlex.split(line)[1])
                if blob.parent.name == 'blobs':
                    cache = str(blob.parent.parent)
                break
    if not cache or not all(os.access(Path(cache) / name, os.R_OK | os.X_OK)
                            for name in ('manifests', 'blobs')):
        raise ValueError('The existing model cache is not readable from this login. '
                         'Set OLLAMA_MODELS to a readable cache, or have the Ollama '
                         'service operator expose a separate API worker per GPU.')
    urls, identifiers = [], []
    for index, gpu in enumerate(devices):
        result = subprocess.run(['nvidia-smi', f'--id={gpu}', '--query-gpu=uuid,memory.used',
                                 '--format=csv,noheader,nounits'],
                                check=True, capture_output=True, text=True, timeout=10)
        uuid, used = result.stdout.strip().split(',')
        if int(used.strip()) > 1024:
            raise ValueError(f'GPU {gpu} is already in use ({used.strip()} MiB). Select free GPUs.')
        port = 11435 + index
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', port))
        urls.append(f'http://127.0.0.1:{port}')
        identifiers.append(uuid.strip())
    processes = []
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        for gpu, uuid, url in zip(devices, identifiers, urls):
            log = log_dir / f'gpu-{gpu}.log'
            env = dict(os.environ, CUDA_VISIBLE_DEVICES=uuid, OLLAMA_HOST=url,
                       OLLAMA_MODELS=str(cache), OLLAMA_NUM_PARALLEL='1',
                       OLLAMA_MAX_LOADED_MODELS='1', OLLAMA_NOPRUNE='1',
                       OLLAMA_NO_CLOUD='1', OLLAMA_VULKAN='0', GGML_VK_VISIBLE_DEVICES='-1')
            with log.open('w', encoding='utf-8') as stream:
                process = subprocess.Popen([binary, 'serve'], env=env, stdout=stream,
                                           stderr=subprocess.STDOUT, start_new_session=True)
            processes.append(process)
            print(f'Starting GPU {gpu}: {url} (log: {log})', flush=True)
            deadline = monotonic() + 30
            while True:
                if process.poll() is not None:
                    raise ValueError(f'GPU {gpu} worker exited. Inspect {log}.')
                try:
                    with urlopen(url + '/api/tags', timeout=1):
                        break
                except (URLError, OSError):
                    if monotonic() >= deadline:
                        raise ValueError(f'GPU {gpu} worker did not start. Inspect {log}.')
                    sleep(0.25)
            # Load now so CPU fallback or an unavailable model fails before the UI opens.
            request = Request(url + '/api/generate', data=json.dumps(dict(
                model=model, stream=False, keep_alive='10m', options=dict(num_ctx=num_ctx))).encode(),
                headers={'Content-Type': 'application/json'})
            with urlopen(request, timeout=timeout) as response:
                answer = json.load(response)
            if answer.get('error') or answer.get('done') is not True:
                raise ValueError(f'GPU {gpu} model load failed: {answer.get("error", "incomplete response")}')
            with urlopen(url + '/api/ps', timeout=10) as response:
                loaded = json.load(response).get('models', [])
            if not any(m.get('name') == model and m.get('size_vram', 0) > 0 for m in loaded):
                raise ValueError(f'GPU {gpu} worker did not load {model} onto the GPU. Inspect {log}.')
            print(f'GPU {gpu} ready: {model}', flush=True)
        yield urls
    finally:
        for process in processes:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)


class OllamaClient:
    def __init__(self, base_url, model, timeout=300, num_ctx=32768, num_predict=512, think=None):
        if not isinstance(base_url, str) or urlsplit(base_url).scheme not in ('http', 'https') or not urlsplit(base_url).hostname:
            raise ValueError('Set a valid HTTP(S) Ollama base_url.')
        if not isinstance(model, str) or not model.strip() or model == 'PLACEHOLDER':
            raise ValueError('Set relevance_classifier.model to an available Ollama model.')
        if not isinstance(timeout, (int, float)) or timeout <= 0 or not isinstance(num_ctx, int) or num_ctx <= 512:
            raise ValueError('timeout must be positive and num_ctx must exceed 512.')
        if not isinstance(num_predict, int) or not 128 <= num_predict < num_ctx:
            raise ValueError('num_predict must be at least 128 and less than num_ctx.')
        if think is not None and not isinstance(think, bool):
            raise ValueError('think must be true, false, or omitted.')
        self.url = base_url.rstrip('/') + '/api/chat'
        self.model, self.timeout, self.num_ctx = model, timeout, num_ctx
        self.num_predict, self.think = num_predict, think

    def metadata(self):
        return {key: getattr(self, key, None) for key in
                ('url', 'model', 'model_digest', 'timeout', 'num_ctx', 'num_predict', 'think')}

    def preflight(self):
        with urlopen(self.url.removesuffix('/api/chat') + '/api/tags', timeout=10) as response:
            models = json.load(response)['models']
        model = next((m for m in models if m.get('name') == self.model), None)
        if not model or not model.get('digest'):
            raise ValueError(f'{self.model!r} is not installed at {self.url}.')
        self.model_digest = model['digest']
        return self.metadata()

    def generate(self, system, value, schema, num_predict=None):
        from jsonschema import Draft202012Validator
        output_limit = self.num_predict if num_predict is None else num_predict
        if not isinstance(output_limit, int) or not 128 <= output_limit < self.num_ctx:
            raise ValueError('num_predict must be at least 128 and less than num_ctx.')
        payload = dict(model=self.model, stream=False, format=schema,
                       messages=[dict(role='system', content=system),
                                 dict(role='user', content=json.dumps(value, ensure_ascii=False))],
                       options=dict(temperature=0, num_ctx=self.num_ctx, num_predict=output_limit))
        if self.think is not None:
            payload['think'] = self.think
        request = Request(self.url, data=json.dumps(payload).encode('utf-8'),
                          headers={'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                result = json.load(response)
        except HTTPError as error:
            try:
                detail = error.read(8192).decode('utf-8', errors='replace').strip()
                try:
                    decoded = json.loads(detail)
                    if isinstance(decoded, dict):
                        detail = str(decoded.get('error', detail))
                except ValueError:
                    pass
            except OSError:
                detail = ''
            if 'context' in detail.lower() and any(word in detail.lower() for word in ('exceed', 'limit')):
                raise ValueError(f'Ollama context limit reached: {detail[:1000]}') from error
            raise RuntimeError(f'Ollama HTTP {error.code} for {self.model!r}: '
                               f'{detail[:2000] or "No error details returned."}') from error
        except (URLError, TimeoutError, OSError) as error:
            raise RuntimeError(f'Ollama request failed at {self.url}: {error}') from error
        except (ValueError, UnicodeError) as error:
            raise ValueError('Ollama returned an invalid JSON response.') from error
        if not isinstance(result, dict):
            raise ValueError('Ollama response incomplete or truncated; increase output tokens before accepting it.')
        if result.get('done') is not True:
            output_tokens = result.get('eval_count')
            if isinstance(output_tokens, int) and output_tokens >= output_limit:
                raise ValueError(f'Ollama output token limit reached at {output_limit} tokens; '
                                 'increase output tokens before accepting it.')
            raise ValueError('Ollama response incomplete or truncated; increase output tokens before accepting it.')
        if result.get('done_reason') == 'length':
            prompt_tokens = result.get('prompt_eval_count')
            output_tokens = result.get('eval_count')
            if (isinstance(prompt_tokens, int) and isinstance(output_tokens, int)
                    and prompt_tokens + output_tokens >= self.num_ctx):
                raise ValueError('Ollama context limit reached; reduce input context or increase num_ctx.')
            raise ValueError(f'Ollama output token limit reached at {output_limit} tokens; '
                             'increase output tokens before accepting it.')
        try:
            answer = json.loads(result['message']['content'])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError('Ollama message must contain a JSON object conforming to the response schema.') from error
        Draft202012Validator(schema).validate(answer)
        return answer


class OllamaPool:
    """One bounded request slot per endpoint, or explicitly configured slots."""

    def __init__(self, base_urls, model, workers_per_endpoint=1, **options):
        if (not isinstance(base_urls, list) or not base_urls or
                any(not isinstance(url, str) for url in base_urls) or
                len(set(url.rstrip('/') for url in base_urls)) != len(base_urls)):
            raise ValueError('base_urls must contain distinct Ollama endpoints.')
        if (not isinstance(workers_per_endpoint, int) or isinstance(workers_per_endpoint, bool)
                or not 1 <= workers_per_endpoint <= 32):
            raise ValueError('workers_per_endpoint must be between 1 and 32.')
        self.clients = [OllamaClient(url, model, **options) for url in base_urls]
        first = self.clients[0]
        for key in ('model', 'timeout', 'num_ctx', 'num_predict', 'think'):
            setattr(self, key, getattr(first, key))
        self.url = first.url
        self.workers_per_endpoint = workers_per_endpoint
        self.workers = len(self.clients) * workers_per_endpoint
        self._available = Queue()
        for _ in range(workers_per_endpoint):
            for client in self.clients:
                self._available.put(client)

    def preflight(self):
        for client in self.clients:
            client.preflight()
        digests = {client.model_digest for client in self.clients}
        if len(digests) != 1:
            raise ValueError('All worker endpoints must serve the same model digest.')
        self.model_digest = digests.pop()
        return self.metadata()

    def metadata(self):
        result = self.clients[0].metadata()
        result.update(base_urls=[c.url.removesuffix('/api/chat') for c in self.clients],
                      workers=self.workers, workers_per_endpoint=self.workers_per_endpoint)
        return result

    def generate(self, *args, **kwargs):
        client = self._available.get()
        try:
            return client.generate(*args, **kwargs)
        finally:
            self._available.put(client)


def load_client(path):
    import yaml
    with open(path, encoding='utf-8') as stream:
        config = yaml.safe_load(stream)
    settings = config.get('relevance_classifier') if isinstance(config, dict) else None
    if not isinstance(settings, dict) or settings.get('provider') != 'ollama':
        raise ValueError('Configuration needs relevance_classifier with provider: ollama.')
    execution = config.get('execution', {})
    if not isinstance(execution, dict):
        raise ValueError('execution must be a mapping.')
    urls = execution.get('base_urls', [settings.get('base_url')])
    return OllamaPool(urls, settings.get('model'),
                      workers_per_endpoint=execution.get('workers_per_endpoint', 1),
                      timeout=settings.get('timeout', 300), num_ctx=settings.get('num_ctx', 32768),
                      num_predict=settings.get('num_predict', 512), think=settings.get('think'))
