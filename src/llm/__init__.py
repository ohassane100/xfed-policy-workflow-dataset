import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


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


def load_client(path):
    import yaml
    with open(path, encoding='utf-8') as stream:
        config = yaml.safe_load(stream)
    role = config.get('relevance_classifier') if isinstance(config, dict) else None
    if not isinstance(role, dict) or role.get('provider') != 'ollama':
        raise ValueError('Configuration needs relevance_classifier with provider: ollama.')
    return OllamaClient(role.get('base_url'), role.get('model'),
                        timeout=role.get('timeout', 300), num_ctx=role.get('num_ctx', 32768),
                        num_predict=role.get('num_predict', 512), think=role.get('think'))
