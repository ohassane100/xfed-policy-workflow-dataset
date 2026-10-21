import json
from contextlib import contextmanager
from queue import Queue
from threading import Lock
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class OllamaClient:
    def __init__(self, base_url, model, timeout=300, num_ctx=32768, num_predict=512, think=None, main_gpu=None,
                 relevance_num_predict=128, relevance_think=None, relevance_batch_size=4, keep_alive='30m',
                 relevance_compact=True, relevance_num_ctx=0, relevance_num_batch=None):
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
        if main_gpu is not None and (type(main_gpu) is not int or main_gpu < 0):
            raise ValueError('main_gpu must be a nonnegative integer or omitted.')
        if type(relevance_num_predict) is not int or not 128 <= relevance_num_predict < num_ctx:
            raise ValueError('relevance_num_predict must be at least 128 and less than num_ctx.')
        if relevance_think is not None and not isinstance(relevance_think, bool):
            raise ValueError('relevance_think must be true, false, or omitted.')
        if not isinstance(keep_alive, (str, int)) or isinstance(keep_alive, bool):
            raise ValueError('keep_alive must be an Ollama duration string or integer.')
        if type(relevance_batch_size) is not int or not 1 <= relevance_batch_size <= 16:
            raise ValueError('relevance_batch_size must be between 1 and 16.')
        if type(relevance_compact) is not bool:
            raise ValueError('relevance_compact must be true or false.')
        if type(relevance_num_ctx) is not int or (relevance_num_ctx != 0 and not 1024 <= relevance_num_ctx <= num_ctx):
            raise ValueError('relevance_num_ctx must be 0 (automatic), or between 1024 and num_ctx.')
        if relevance_num_batch is not None and (type(relevance_num_batch) is not int or relevance_num_batch < 1):
            raise ValueError('relevance_num_batch must be a positive integer or omitted.')
        self.relevance_batch_size = relevance_batch_size
        self.relevance_num_predict, self.relevance_think = relevance_num_predict, relevance_think
        self.relevance_compact = relevance_compact
        self.relevance_num_ctx, self.relevance_num_batch = relevance_num_ctx, relevance_num_batch
        self.keep_alive = keep_alive
        self._stats_lock = Lock()
        self.reset_stats()
        self.url = base_url.rstrip('/') + '/api/chat'
        self.model, self.timeout, self.num_ctx = model, timeout, num_ctx
        self.num_predict, self.think = num_predict, think
        self.main_gpu = main_gpu

    def reset_stats(self):
        with self._stats_lock:
            self.stats = dict(responses=0, request_seconds=0.0, load_seconds=0.0,
                              prompt_seconds=0.0, generation_seconds=0.0, prompt_tokens=0,
                              output_tokens=0, context_requests={})

    def metadata(self):
        result = {key: getattr(self, key, None) for key in
                  ('url', 'model', 'model_digest', 'timeout', 'num_ctx', 'num_predict', 'think',
                   'main_gpu', 'relevance_num_predict', 'relevance_think', 'relevance_batch_size', 'keep_alive',
                   'relevance_compact', 'relevance_num_ctx', 'relevance_num_batch')}
        with self._stats_lock:
            result['performance'] = dict(self.stats, context_requests=dict(self.stats['context_requests']))
        return result

    def preflight(self):
        with urlopen(self.url.removesuffix('/api/chat') + '/api/tags', timeout=10) as response:
            models = json.load(response)['models']
        model = next((m for m in models if m.get('name') == self.model), None)
        if not model or not model.get('digest'):
            raise ValueError(f'{self.model!r} is not installed at {self.url}.')
        self.model_digest = model['digest']
        return self.metadata()

    def generate(self, system, value, schema, num_predict=None, think=None, num_ctx=None, num_batch=None,
                 truncate=None, compact_json=False):
        from jsonschema import Draft202012Validator
        output_limit = self.num_predict if num_predict is None else num_predict
        context_limit = self.num_ctx if num_ctx is None else num_ctx
        if type(context_limit) is not int or context_limit <= 512:
            raise ValueError('num_ctx must exceed 512.')
        if not isinstance(output_limit, int) or not 128 <= output_limit < context_limit:
            raise ValueError('num_predict must be at least 128 and less than num_ctx.')
        if num_batch is not None and (type(num_batch) is not int or num_batch < 1):
            raise ValueError('num_batch must be a positive integer or omitted.')
        if truncate is not None and type(truncate) is not bool:
            raise ValueError('truncate must be true, false, or omitted.')
        effective_think = self.think if think is None else think
        if effective_think is not None and not isinstance(effective_think, bool):
            raise ValueError('think must be true, false, or omitted.')
        payload = dict(model=self.model, stream=False, format=schema, keep_alive=self.keep_alive,
                       messages=[dict(role='system', content=system),
                                 dict(role='user', content=json.dumps(value, ensure_ascii=False,
                                      **({'separators': (',', ':')} if compact_json else {})))],
                       options=dict(temperature=0, num_ctx=context_limit, num_predict=output_limit))
        if num_batch is not None:
            payload['options']['num_batch'] = num_batch
        if truncate is not None:
            payload['truncate'] = truncate
            if not truncate:
                payload['shift'] = False
        if effective_think is not None:
            payload['think'] = effective_think
        if self.main_gpu is not None:
            payload['options']['main_gpu'] = self.main_gpu
        request = Request(self.url, data=json.dumps(payload).encode('utf-8'),
                          headers={'Content-Type': 'application/json'})
        started = perf_counter()
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
            if 'context' in detail.lower() and any(word in detail.lower() for word in ('exceed', 'limit', 'too long')):
                raise ValueError(f'Ollama context limit reached: {detail[:1000]}') from error
            raise RuntimeError(f'Ollama HTTP {error.code} for {self.model!r}: '
                               f'{detail[:2000] or "No error details returned."}') from error
        except (URLError, TimeoutError, OSError) as error:
            raise RuntimeError(f'Ollama request failed at {self.url}: {error}') from error
        except (ValueError, UnicodeError) as error:
            raise ValueError('Ollama returned an invalid JSON response.') from error
        if not isinstance(result, dict):
            raise ValueError('Ollama response incomplete or truncated; increase output tokens before accepting it.')
        with self._stats_lock:
            self.stats['responses'] += 1
            contexts = self.stats['context_requests']
            contexts[str(context_limit)] = contexts.get(str(context_limit), 0) + 1
            self.stats['request_seconds'] += perf_counter() - started
            for key, source, divisor in (
                ('load_seconds', 'load_duration', 1e9),
                ('prompt_seconds', 'prompt_eval_duration', 1e9),
                ('generation_seconds', 'eval_duration', 1e9),
                ('prompt_tokens', 'prompt_eval_count', 1),
                ('output_tokens', 'eval_count', 1),
            ):
                value = result.get(source)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                    self.stats[key] += value / divisor
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
                    and prompt_tokens + output_tokens >= context_limit):
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
    """Bound concurrent requests and keep each chunk's retries on one worker."""

    def __init__(self, base_urls, model=None, workers_per_endpoint=1, worker_models=None, workers=None, **options):
        if (not isinstance(base_urls, list) or not base_urls or
                any(not isinstance(url, str) for url in base_urls) or
                len(set(url.rstrip('/') for url in base_urls)) != len(base_urls)):
            raise ValueError('base_urls must contain distinct Ollama endpoints.')
        if (not isinstance(workers_per_endpoint, int) or isinstance(workers_per_endpoint, bool)
                or not 1 <= workers_per_endpoint <= 32):
            raise ValueError('workers_per_endpoint must be between 1 and 32.')
        if worker_models is not None:
            if (not isinstance(worker_models, list) or not 2 <= len(worker_models) <= 32 or
                    any(not isinstance(name, str) or not name.strip() for name in worker_models) or
                    len(set(worker_models)) != len(worker_models)):
                raise ValueError('Choose between 2 and 32 distinct worker models.')
            if model is not None and model != worker_models[0]:
                raise ValueError('The primary model must be the first worker model.')
            if workers_per_endpoint != 1:
                raise ValueError('Use one request per model when parallelizing different models.')
        self.parallel_models = worker_models is not None
        self.clients = [OllamaClient(url, name, **options)
                        for url in base_urls for name in (worker_models if self.parallel_models else [model])]
        first = self.clients[0]
        for key in ('model', 'timeout', 'num_ctx', 'num_predict', 'think', 'main_gpu',
                    'relevance_num_predict', 'relevance_think', 'relevance_batch_size', 'keep_alive',
                    'relevance_compact', 'relevance_num_ctx', 'relevance_num_batch'):
            setattr(self, key, getattr(first, key))
        self.url = first.url
        self.model = ' + '.join(dict.fromkeys(c.model for c in self.clients))
        self.workers_per_endpoint = workers_per_endpoint
        capacity = len(self.clients) * workers_per_endpoint
        if workers is not None and (type(workers) is not int or not 1 <= workers <= capacity):
            raise ValueError(f'workers must be between 1 and {capacity}.')
        self.workers = capacity if workers is None else workers
        self._available = Queue()
        self._lock = Lock()
        self.reset_assignments()
        for _ in range(workers_per_endpoint):
            for client in self.clients:
                self._available.put(client)

    def preflight(self):
        for client in self.clients:
            client.preflight()
        digests = {}
        for client in self.clients:
            if digests.setdefault(client.model, client.model_digest) != client.model_digest:
                raise ValueError('Each model must have the same digest on every worker endpoint.')
        if not self.parallel_models and len(set(digests.values())) != 1:
            raise ValueError('All worker endpoints must serve the same model digest.')
        if self.parallel_models and len(set(digests.values())) != len(digests):
            raise ValueError('Choose different models; some selected names refer to the same model digest.')
        self.model_digests = digests
        self.model_digest = None if self.parallel_models else next(iter(digests.values()))
        return self.metadata()

    def reset_assignments(self):
        for client in self.clients:
            client.reset_stats()
        with self._lock:
            self.assignments = {}
            self.completed = [0] * len(self.clients)

    @contextmanager
    def for_chunk(self, chunk_id):
        client = self._available.get()
        index = self.clients.index(client)
        try:
            chunk_ids = [chunk_id] if isinstance(chunk_id, str) else list(chunk_id)
            with self._lock:
                for identifier in chunk_ids:
                    self.assignments[identifier] = index
            yield client
            with self._lock:
                self.completed[index] += len(chunk_ids)
        finally:
            self._available.put(client)

    def metadata(self):
        result = self.clients[0].metadata()
        result.update(base_urls=list(dict.fromkeys(c.url.removesuffix('/api/chat') for c in self.clients)),
                      model=self.model, model_digest=getattr(self, 'model_digest', None),
                      model_digests=dict(getattr(self, 'model_digests', {})), parallel_models=self.parallel_models,
                      models=[c.metadata() for c in self.clients],
                      workers=self.workers, workers_per_endpoint=self.workers_per_endpoint)
        with self._lock:
            result.update(assignments=dict(self.assignments), completed=list(self.completed))
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
                      num_predict=settings.get('num_predict', 512), think=settings.get('think'),
                      main_gpu=settings.get('main_gpu'),
                      relevance_num_predict=settings.get('relevance_num_predict', 128),
                      relevance_think=settings.get('relevance_think'),
                      relevance_batch_size=settings.get('relevance_batch_size', 4),
                      relevance_compact=settings.get('relevance_compact', True),
                      relevance_num_ctx=settings.get('relevance_num_ctx', 0),
                      relevance_num_batch=settings.get('relevance_num_batch'),
                      keep_alive=settings.get('keep_alive', '30m'))
