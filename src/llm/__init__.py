import json
import math
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection, HTTPSConnection, HTTPException
from queue import Queue
import socket
from threading import Event, Lock
from time import perf_counter, sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class OllamaConnectionError(RuntimeError):
    """The endpoint could not complete a connection, rather than a model answer."""


class OllamaClient:
    def __init__(self, base_url, model, timeout=300, num_ctx=32768, num_predict=512, think='auto', main_gpu=None,
                 relevance_num_predict=4096, relevance_think='auto', relevance_batch_size=1, keep_alive='30m',
                 relevance_compact=False, relevance_num_ctx=0, relevance_num_batch=None, cancel=None, sampling=None):
        if not isinstance(base_url, str) or urlsplit(base_url).scheme not in ('http', 'https') or not urlsplit(base_url).hostname:
            raise ValueError('Set a valid HTTP(S) Ollama base_url.')
        if not isinstance(model, str) or not model.strip() or model == 'PLACEHOLDER':
            raise ValueError('Set relevance_classifier.model to an available Ollama model.')
        if not isinstance(timeout, (int, float)) or timeout <= 0 or not isinstance(num_ctx, int) or num_ctx <= 512:
            raise ValueError('timeout must be positive and num_ctx must exceed 512.')
        if not isinstance(num_predict, int) or not 128 <= num_predict < num_ctx:
            raise ValueError('num_predict must be at least 128 and less than num_ctx.')
        if think is not None and not isinstance(think, bool) and think != 'auto':
            raise ValueError('think must be auto, true, false, or null.')
        if main_gpu is not None and (type(main_gpu) is not int or main_gpu < 0):
            raise ValueError('main_gpu must be a nonnegative integer or omitted.')
        if type(relevance_num_predict) is not int or not 128 <= relevance_num_predict < num_ctx:
            raise ValueError('relevance_num_predict must be at least 128 and less than num_ctx.')
        if relevance_think is not None and not isinstance(relevance_think, bool) and relevance_think != 'auto':
            raise ValueError('relevance_think must be auto, true, false, or null.')
        if not isinstance(keep_alive, (str, int)) or isinstance(keep_alive, bool):
            raise ValueError('keep_alive must be an Ollama duration string or integer.')
        if type(relevance_batch_size) is not int or not 1 <= relevance_batch_size <= 16:
            raise ValueError('relevance_batch_size must be between 1 and 16.')
        if type(relevance_compact) is not bool:
            raise ValueError('relevance_compact must be true or false.')
        if type(relevance_num_ctx) is not int or (relevance_num_ctx != 0 and not 1024 <= relevance_num_ctx <= num_ctx):
            raise ValueError('relevance_num_ctx must be 0 (run context), or between 1024 and num_ctx.')
        if relevance_num_batch is not None and (type(relevance_num_batch) is not int or relevance_num_batch < 1):
            raise ValueError('relevance_num_batch must be a positive integer or omitted.')
        if sampling is not None and not isinstance(sampling, dict):
            raise ValueError('sampling must be a JSON object of generation options.')
        self.sampling = dict(sampling or {})
        bounds = dict(temperature=(0, 2), top_p=(0, 1), min_p=(0, 1), presence_penalty=(-2, 2), repeat_penalty=(0, 3))
        for key, value in self.sampling.items():
            if key in ('top_k', 'seed'):
                if type(value) is not int or not (0 <= value <= 1000 if key == 'top_k' else -1 <= value < 2**63):
                    raise ValueError('Invalid sampling option: ' + key)
            elif key not in bounds or type(value) not in (int, float) or not math.isfinite(value) or not bounds[key][0] <= value <= bounds[key][1]:
                raise ValueError('Invalid sampling option: ' + key)
        self.relevance_batch_size = relevance_batch_size
        self.relevance_num_predict, self.relevance_think = relevance_num_predict, relevance_think
        self.auto_think, self.auto_relevance_think = None, None
        self.relevance_compact = relevance_compact
        self.relevance_num_ctx, self.relevance_num_batch = relevance_num_ctx, relevance_num_batch
        self.keep_alive = keep_alive
        self._stats_lock = Lock()
        self.reset_stats()
        self.url = base_url.rstrip('/') + '/api/chat'
        self.model, self.timeout, self.num_ctx = model, timeout, num_ctx
        self.num_predict, self.think = num_predict, think
        self.main_gpu = main_gpu
        self.cancel = cancel if cancel is not None else Event()
        self._connections, self._connections_lock = {}, Lock()
        self.response_format = 'schema'
        self.schema_grammar_fallbacks = 0
        self.schema_grammar_error = None

    def cancel_requests(self):
        self.cancel.set()
        with self._connections_lock:
            connections = list(self._connections.items())
        for connection, transport in connections:
            transport = transport or connection.sock
            if transport is not None:
                try:
                    transport.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()

    def unload(self):
        base = self.url.removesuffix('/api/chat')
        request = Request(base + '/api/generate',
                          data=json.dumps(dict(model=self.model, keep_alive=0, stream=False)).encode(),
                          headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=10) as response:
            result = json.load(response)
        if not isinstance(result, dict) or result.get('done') is not True:
            raise RuntimeError(f'Ollama did not confirm unloading {self.model!r}.')
        deadline = perf_counter() + 5
        while True:
            with urlopen(base + '/api/ps', timeout=5) as response:
                loaded = json.load(response)['models']
            if not any(model.get('name') == self.model or
                       (getattr(self, 'model_digest', None) and model.get('digest') == self.model_digest)
                       for model in loaded):
                return self.model
            if perf_counter() >= deadline:
                raise RuntimeError(f'{self.model!r} is still loaded; another request may be using it. Try unloading again.')
            sleep(0.2)

    def reset_stats(self):
        with self._stats_lock:
            self.stats = dict(responses=0, request_seconds=0.0, load_seconds=0.0,
                              prompt_seconds=0.0, generation_seconds=0.0, prompt_tokens=0,
                              output_tokens=0, context_requests={})
            self.active_request = None
            self.last_request = None

    def metadata(self):
        result = {key: getattr(self, key, None) for key in
                  ('url', 'model', 'model_digest', 'timeout', 'num_ctx', 'num_predict', 'think',
                   'main_gpu', 'gpu', 'gpu_uuid', 'placement_status', 'observed_gpus', 'placement_error',
                   'relevance_num_predict', 'relevance_think', 'relevance_batch_size', 'keep_alive',
                   'relevance_compact', 'relevance_num_ctx', 'relevance_num_batch', 'auto_think', 'auto_relevance_think',
                   'response_format', 'schema_grammar_fallbacks', 'schema_grammar_error', 'sampling')}
        result['sampling'] = dict(self.sampling)
        with self._stats_lock:
            result['performance'] = dict(self.stats, context_requests=dict(self.stats['context_requests']))
            result['active_request'] = None
            if self.active_request:
                active = dict(self.active_request)
                active['elapsed_seconds'] = round(perf_counter() - active.pop('started'), 1)
                result['active_request'] = active
            result['last_request'] = dict(self.last_request) if self.last_request else None
        return result

    def preflight(self):
        with urlopen(self.url.removesuffix('/api/chat') + '/api/tags', timeout=10) as response:
            models = json.load(response)['models']
        model = next((m for m in models if m.get('name') == self.model), None)
        if not model or not model.get('digest'):
            raise ValueError(f'{self.model!r} is not installed at {self.url}.')
        self.model_digest = model['digest']
        self.auto_think, self.auto_relevance_think = None, None
        family = model.get('details', {}).get('family', '')
        if ((self.relevance_think == 'auto' or self.think == 'auto') and
                ('thinking' in model.get('capabilities', []) or family in ('qwen35', 'qwen35moe'))):
            request = Request(self.url.removesuffix('/api/chat') + '/api/show',
                              data=json.dumps(dict(model=self.model)).encode(),
                              headers={'Content-Type': 'application/json'})
            with urlopen(request, timeout=10) as response:
                details = json.load(response)
            if not isinstance(details, dict):
                raise ValueError('Ollama model details must be a JSON object.')
            controls = details.get('thinking') or {}
            values = controls.get('values') if isinstance(controls, dict) else None
            family = details.get('details', {}).get('family', model.get('details', {}).get('family', ''))
            # Older Ollama versions lack thinking.values. Qwen3.5 documents its on/off switch;
            # retain the model default for other families without explicit capability metadata.
            if isinstance(values, list):
                self.auto_think = True if any(value is True for value in values) else None
                self.auto_relevance_think = False if any(value is False for value in values) else None
            elif family in ('qwen35', 'qwen35moe'):
                self.auto_think, self.auto_relevance_think = True, False
        return self.metadata()

    def generate(self, system, value, schema, num_predict=None, think=None, num_ctx=None, num_batch=None,
                 truncate=None, compact_json=False):
        options = dict(num_predict=num_predict, think=think, num_ctx=num_ctx, num_batch=num_batch,
                       truncate=truncate, compact_json=compact_json)
        try:
            return self._generate(system, value, schema, **options)
        except RuntimeError as error:
            rejection = error.__cause__
            if (self.response_format != 'schema' or not isinstance(schema, dict)
                    or not isinstance(rejection, HTTPError) or rejection.code != 400
                    or 'failed to parse grammar' not in str(error).lower()):
                raise
            # Some Ollama backends reject otherwise valid response schemas.
            # JSON mode avoids that schema compiler; validation stays unchanged.
            with self._stats_lock:
                self.response_format = 'json'
                self.schema_grammar_fallbacks += 1
                self.schema_grammar_error = str(error)[:2000]
        return self._generate(system, value, schema, **options)

    def _generate(self, system, value, schema, num_predict=None, think=None, num_ctx=None, num_batch=None,
                  truncate=None, compact_json=False):
        from jsonschema import Draft202012Validator
        if self.cancel.is_set():
            raise InterruptedError('Model request cancelled.')
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
        if effective_think == 'auto':
            effective_think = self.auto_think
        if effective_think is not None and not isinstance(effective_think, bool):
            raise ValueError('think must be true, false, or omitted.')
        response_format = schema
        if self.response_format == 'json':
            response_format = 'json'
        # A decoding grammar constrains syntax but does not teach the model the
        # fields' meanings. Ground the prompt in the same schema we validate.
        system += ('\n\nReturn exactly one JSON object conforming to this JSON schema:\n' +
                   json.dumps(schema, ensure_ascii=False, separators=(',', ':')))
        payload = dict(model=self.model, stream=True, format=response_format, keep_alive=self.keep_alive,
                       messages=[dict(role='system', content=system),
                                 dict(role='user', content=json.dumps(value, ensure_ascii=False,
                                      **({'separators': (',', ':')} if compact_json else {})))],
                       options={'temperature': 0, 'num_ctx': context_limit, 'num_predict': output_limit, **self.sampling})
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
        endpoint = urlsplit(self.url)
        connection_type = HTTPSConnection if endpoint.scheme == 'https' else HTTPConnection
        connection = connection_type(endpoint.hostname, endpoint.port, timeout=self.timeout)
        path = endpoint.path + ('?' + endpoint.query if endpoint.query else '')
        started = perf_counter()
        result, content, thinking_chars = {}, [], 0
        response = None
        activity = dict(started=started, phase='Waiting for Ollama',
                        context_tokens=context_limit, output_limit=output_limit,
                        think=effective_think, answer_chars=0, thinking_chars=0)
        with self._stats_lock:
            self.active_request = activity
        with self._connections_lock:
            self._connections[connection] = None
        try:
            if self.cancel.is_set():
                raise InterruptedError('Model request cancelled.')
            connection.request('POST', path, json.dumps(payload).encode('utf-8'),
                               {'Content-Type': 'application/json'})
            with self._connections_lock:
                self._connections[connection] = connection.sock
            if self.cancel.is_set():
                raise InterruptedError('Model request cancelled.')
            response = connection.getresponse()
            if response.status >= 400:
                raise HTTPError(self.url, response.status, response.reason, response.headers, response)
            with response:
                for line in response:
                    if self.cancel.is_set():
                        raise InterruptedError('Model request cancelled.')
                    if not line.strip():
                        continue
                    if perf_counter() - started > self.timeout:
                        raise TimeoutError(f'Request exceeded {self.timeout} seconds.')
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError('Expected an Ollama response object.')
                    if event.get('error'):
                        raise RuntimeError(f'Ollama error for {self.model!r}: {event["error"]}')
                    message = event.get('message') or {}
                    answer = message.get('content') or ''
                    thinking = message.get('thinking') or ''
                    content.append(answer)
                    thinking_chars += len(thinking)
                    with self._stats_lock:
                        activity['answer_chars'] += len(answer)
                        activity['thinking_chars'] = thinking_chars
                        if answer:
                            activity['phase'] = 'Generating answer'
                        elif thinking:
                            activity['phase'] = 'Thinking'
                    result = event
                    if event.get('done') is True:
                        break
                result['message'] = dict(content=''.join(content))
        except HTTPError as error:
            if self.cancel.is_set():
                raise InterruptedError('Model request cancelled.') from error
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
        except (URLError, TimeoutError, OSError, HTTPException) as error:
            if self.cancel.is_set():
                raise InterruptedError('Model request cancelled.') from error
            raise OllamaConnectionError(f'Ollama request failed at {self.url}: {error}') from error
        except (ValueError, UnicodeError) as error:
            if self.cancel.is_set():
                raise InterruptedError('Model request cancelled.') from error
            raise ValueError('Ollama returned an invalid JSON response.') from error
        finally:
            with self._connections_lock:
                self._connections.pop(connection, None)
            if response is not None:
                response.close()
            connection.close()
            with self._stats_lock:
                self.last_request = dict(duration_seconds=round(perf_counter() - started, 3),
                                         context_tokens=context_limit, output_limit=output_limit,
                                         thinking_chars=thinking_chars, answer_chars=sum(map(len, content)),
                                         output_tokens=result.get('eval_count'), done_reason=result.get('done_reason'),
                                         sampling={'temperature': 0, **self.sampling})
                if self.active_request is activity:
                    self.active_request = None
        if self.cancel.is_set():
            raise InterruptedError('Model request cancelled.')
        if not isinstance(result, dict):
            raise ValueError('Ollama response incomplete or truncated; increase output tokens before accepting it.')
        if verify := getattr(self, 'verify_placement', None):
            verify()
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
            detail = (' Thinking consumed generation time before a complete answer was produced.'
                      if thinking_chars else '')
            raise ValueError(f'Ollama output token limit reached at {output_limit} tokens '
                             f'for {self.model!r}; answer characters: {len(result["message"]["content"])}.' + detail)
        try:
            answer = json.loads(result['message']['content'])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError('Ollama message must contain a JSON object conforming to the response schema.') from error
        Draft202012Validator(schema).validate(answer)
        return answer


class OllamaPool:
    """Bound concurrent requests and keep each chunk's retries on one worker."""

    def __init__(self, base_urls, model=None, workers_per_endpoint=1, worker_models=None, workers=None,
                 worker_gpus=None, **options):
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
        if worker_gpus is not None:
            if (not self.parallel_models or not isinstance(worker_gpus, list) or
                    len(worker_gpus) != len(worker_models) or
                    any(gpu is not None and (type(gpu) is not int or gpu < 0) for gpu in worker_gpus)):
                raise ValueError('Choose one nonnegative GPU index or Auto for each parallel model.')
            explicit_gpus = [gpu for gpu in worker_gpus if gpu is not None]
            if len(set(explicit_gpus)) != len(explicit_gpus):
                raise ValueError('Choose different GPU indices for parallel models, or leave placement on Auto.')
        self.clients = []
        for url in base_urls:
            for index, name in enumerate(worker_models if self.parallel_models else [model] * workers_per_endpoint):
                settings = dict(options)
                if worker_gpus is not None:
                    settings['main_gpu'] = worker_gpus[index]
                self.clients.append(OllamaClient(url, name, **settings))
        first = self.clients[0]
        for key in ('model', 'timeout', 'num_ctx', 'num_predict', 'think', 'main_gpu',
                    'relevance_num_predict', 'relevance_think', 'relevance_batch_size', 'keep_alive',
                    'relevance_compact', 'relevance_num_ctx', 'relevance_num_batch', 'sampling'):
            setattr(self, key, getattr(first, key))
        self.url = first.url
        self.model = ' + '.join(dict.fromkeys(c.model for c in self.clients))
        self.workers_per_endpoint = workers_per_endpoint
        capacity = len(self.clients)
        if workers is not None and (type(workers) is not int or not 1 <= workers <= capacity):
            raise ValueError(f'workers must be between 1 and {capacity}.')
        self.workers = capacity if workers is None else workers
        self._available = Queue()
        self._lock = Lock()
        self.reset_assignments()
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
            self.assigned = [0] * len(self.clients)
            self.failed = [0] * len(self.clients)
            self.active_chunks = [[] for _ in self.clients]

    @contextmanager
    def for_chunk(self, chunk_id):
        client = self._available.get()
        index = self.clients.index(client)
        chunk_ids = []
        try:
            chunk_ids = [chunk_id] if isinstance(chunk_id, str) else list(chunk_id)
            with self._lock:
                for identifier in chunk_ids:
                    self.assignments[identifier] = index
                self.assigned[index] += len(chunk_ids)
                self.active_chunks[index] = chunk_ids
            yield client
            with self._lock:
                self.completed[index] += len(chunk_ids)
        except Exception:
            with self._lock:
                self.failed[index] += len(chunk_ids)
            raise
        finally:
            with self._lock:
                self.active_chunks[index] = []
            self._available.put(client)

    def metadata(self):
        result = self.clients[0].metadata()
        result.update(base_urls=list(dict.fromkeys(c.url.removesuffix('/api/chat') for c in self.clients)),
                      model=self.model, model_digest=getattr(self, 'model_digest', None),
                      model_digests=dict(getattr(self, 'model_digests', {})), parallel_models=self.parallel_models,
                      worker_gpus=[c.main_gpu for c in self.clients],
                      models=[c.metadata() for c in self.clients],
                      workers=self.workers, workers_per_endpoint=self.workers_per_endpoint)
        with self._lock:
            result.update(assignments=dict(self.assignments), completed=list(self.completed),
                          assigned=list(self.assigned), failed=list(self.failed),
                          active_chunks=[list(chunks) for chunks in self.active_chunks])
        return result

    def generate(self, *args, **kwargs):
        client = self._available.get()
        try:
            return client.generate(*args, **kwargs)
        finally:
            self._available.put(client)

    def cancel_requests(self):
        for client in self.clients:
            client.cancel_requests()

    def close(self):
        """Shared endpoints are owned by their administrator, not this pool."""

    def unload(self):
        targets = {(client.url, client.model): client for client in self.clients}
        result = dict(unloaded=[], errors=[])
        with ThreadPoolExecutor(max_workers=min(32, len(targets))) as executor:
            pending = [(client, executor.submit(client.unload)) for client in targets.values()]
            for client, future in pending:
                try:
                    result['unloaded'].append(future.result())
                except Exception as error:
                    result['errors'].append(dict(model=client.model, error=str(error)))
        return result


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
    service = execution.get('service')
    pool_options = dict(workers_per_endpoint=execution.get('workers_per_endpoint', 1))
    pool_type = OllamaPool
    if service is not None:
        from .service import OllamaServicePool
        pool_type = OllamaServicePool
        pool_options = dict(service=service, worker_count=execution.get('workers', 1),
                            worker_gpus=execution.get('worker_gpus'))
    return pool_type(urls, settings.get('model'), **pool_options,
                      timeout=settings.get('timeout', 300), num_ctx=settings.get('num_ctx', 32768),
                      num_predict=settings.get('num_predict', 512), think=settings.get('think', 'auto'),
                      main_gpu=settings.get('main_gpu'),
                      relevance_num_predict=settings.get('relevance_num_predict', 4096),
                      relevance_think=settings.get('relevance_think', 'auto'),
                      relevance_batch_size=settings.get('relevance_batch_size', 1),
                      relevance_compact=settings.get('relevance_compact', False),
                      relevance_num_ctx=settings.get('relevance_num_ctx', 0),
                      relevance_num_batch=settings.get('relevance_num_batch'),
                      sampling=settings.get('sampling'),
                      keep_alive=settings.get('keep_alive', '30m'))
