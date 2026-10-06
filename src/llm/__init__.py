import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class OllamaClient:
    def __init__(self, base_url, model, timeout=300, num_ctx=32768):
        if not isinstance(base_url, str) or urlsplit(base_url).scheme not in ('http', 'https') or not urlsplit(base_url).hostname:
            raise ValueError('Set a valid HTTP(S) Ollama base_url.')
        if not isinstance(model, str) or not model.strip() or model == 'PLACEHOLDER':
            raise ValueError('Set relevance_classifier.model to an available Ollama model.')
        if not isinstance(timeout, (int, float)) or timeout <= 0 or not isinstance(num_ctx, int) or num_ctx <= 512:
            raise ValueError('timeout must be positive and num_ctx must exceed 512.')
        self.url = base_url.rstrip('/') + '/api/chat'
        self.model, self.timeout, self.num_ctx = model, timeout, num_ctx

    def generate(self, system, value, schema):
        from jsonschema import Draft202012Validator
        payload = dict(model=self.model, stream=False, format=schema,
                       messages=[dict(role='system', content=system),
                                 dict(role='user', content=json.dumps(value, ensure_ascii=False))],
                       options=dict(temperature=0, num_ctx=self.num_ctx, num_predict=512))
        request = Request(self.url, data=json.dumps(payload).encode('utf-8'),
                          headers={'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                result = json.load(response)
        except HTTPError as error:
            raise RuntimeError(f'Ollama HTTP {error.code}: check endpoint and model {self.model!r}.') from error
        except (URLError, TimeoutError, OSError) as error:
            raise RuntimeError(f'Ollama request failed at {self.url}: {error}') from error
        except (ValueError, UnicodeError) as error:
            raise ValueError('Ollama returned an invalid JSON response.') from error
        if not isinstance(result, dict) or result.get('done') is not True or result.get('done_reason') == 'length':
            raise ValueError('Ollama response incomplete or truncated; no classification accepted.')
        if result.get('prompt_eval_count', 0) >= self.num_ctx - 512:
            raise ValueError('Ollama context limit reached; increase num_ctx before accepting a classification.')
        try:
            answer = json.loads(result['message']['content'])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError('Ollama message must contain a JSON object conforming to the classification schema.') from error
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
                        timeout=role.get('timeout', 300), num_ctx=role.get('num_ctx', 32768))
