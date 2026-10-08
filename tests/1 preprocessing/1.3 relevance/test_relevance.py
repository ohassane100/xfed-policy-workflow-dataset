from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[3]
from copy import deepcopy
from io import BytesIO
import json
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import URLError
from jsonschema import ValidationError
from xfed.common import schema, validate
from xfed.llm import OllamaClient, load_client
from xfed.relevance import classify

def chunk(number, text, label='NOT_RELEVANT', **extra):
    return dict(chunk_id='c' + number, clause_id=number, text=text,
                source=dict(pages=[1], block_ids=['b' + number]),
                classification=dict(label=label), **extra)

def response(value):
    return BytesIO(json.dumps(value).encode())

class RelevanceTests(unittest.TestCase):
    def test_structured_request_and_preserved_source(self):
        original = dict(contract_id='test', chunks=[chunk('1', 'Data may be shared.')])
        del original['chunks'][0]['classification']
        before = deepcopy(original)
        client = OllamaClient('http://example.test:11434', 'chosen-model')
        answer = dict(done=True, message=dict(content=json.dumps(dict(label='RELEVANT', reason='Permission'))))
        with patch('xfed.llm.urlopen', return_value=response(answer)) as request:
            output = classify(original, client, ROOT)
        self.assertEqual(original, before)
        self.assertEqual({k: v for k, v in output['chunks'][0].items() if k != 'classification'}, before['chunks'][0])
        payload = json.loads(request.call_args.args[0].data)
        self.assertEqual(payload['format'], schema(3, ROOT)['properties']['chunks']['items']['properties']['classification'])
        self.assertEqual(payload['model'], 'chosen-model')
        self.assertFalse(payload['stream'])
        self.assertEqual(request.call_args.args[0].full_url, 'http://example.test:11434/api/chat')
        validate(output, 3, ROOT)

    def test_invalid_and_truncated_llm_responses(self):
        client = OllamaClient('http://example.test', 'test')
        decision_schema = schema(3, ROOT)['properties']['chunks']['items']['properties']['classification']
        for answer in [dict(done=False), dict(done=True, done_reason='length'),
                       dict(done=True, prompt_eval_count=32768, message=dict(content='{"label":"RELEVANT"}')),
                       dict(done=True, message=dict(content='not json')),
                       dict(done=True, message=dict(content='{"label":"MAYBE"}')),
                       dict(done=True, message=dict(content='{"label":"RELEVANT","extra":1}'))]:
            with self.subTest(answer=answer), patch('xfed.llm.urlopen', return_value=response(answer)):
                with self.assertRaises((ValueError, ValidationError)):
                    client.generate('prompt', {}, decision_schema)
        with patch('xfed.llm.urlopen', side_effect=URLError('offline')):
            with self.assertRaisesRegex(RuntimeError, 'Ollama request failed'):
                client.generate('prompt', {}, decision_schema)

    def test_configuration(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'models.yaml'
            path.write_text('relevance_classifier:\n  provider: ollama\n  base_url: http://example.test:1234\n  model: custom-model\n', encoding='utf-8')
            client = load_client(path)
            self.assertEqual(client.model, 'custom-model')
            self.assertEqual(client.url, 'http://example.test:1234/api/chat')
        with self.assertRaisesRegex(ValueError, 'model'):
            load_client(ROOT / 'config/models.example.yaml')
