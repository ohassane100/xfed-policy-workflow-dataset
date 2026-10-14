from io import BytesIO
import json
import unittest
from unittest.mock import patch

from xfed.llm import OllamaClient


RESPONSE_SCHEMA = {
    'type': 'object',
    'required': ['ok'],
    'properties': {'ok': {'type': 'boolean'}},
}


class OllamaClientTests(unittest.TestCase):
    def client(self):
        return OllamaClient('http://localhost:11434', 'test-model', num_ctx=1024, num_predict=512)

    @patch('xfed.llm.urlopen')
    def test_completed_response_is_not_rejected_for_reserved_output_budget(self, urlopen):
        result = dict(done=True, done_reason='stop', prompt_eval_count=600, eval_count=30,
                      message=dict(content='{"ok": true}'))
        urlopen.return_value.__enter__.return_value = BytesIO(json.dumps(result).encode())

        answer = self.client().generate('system', {'clause': 'text'}, RESPONSE_SCHEMA)

        self.assertEqual(answer, {'ok': True})

    @patch('xfed.llm.urlopen')
    def test_context_exhaustion_is_distinguished_from_output_limit(self, urlopen):
        result = dict(done=True, done_reason='length', prompt_eval_count=980, eval_count=44,
                      message=dict(content='{}'))
        urlopen.return_value.__enter__.return_value = BytesIO(json.dumps(result).encode())

        with self.assertRaisesRegex(ValueError, 'context limit reached'):
            self.client().generate('system', {'clause': 'text'}, RESPONSE_SCHEMA)

    @patch('xfed.llm.urlopen')
    def test_output_limit_does_not_trigger_context_retries(self, urlopen):
        result = dict(done=True, done_reason='length', prompt_eval_count=100, eval_count=128,
                      message=dict(content='{}'))
        urlopen.return_value.__enter__.return_value = BytesIO(json.dumps(result).encode())

        with self.assertRaisesRegex(ValueError, 'output token limit reached'):
            self.client().generate('system', {'clause': 'text'}, RESPONSE_SCHEMA)

    @patch('xfed.llm.urlopen')
    def test_incomplete_response_at_output_cap_is_classified_as_output_limit(self, urlopen):
        result = dict(done=False, prompt_eval_count=700, eval_count=512,
                      message=dict(content='{}'))
        urlopen.return_value.__enter__.return_value = BytesIO(json.dumps(result).encode())

        with self.assertRaisesRegex(ValueError, 'output token limit reached'):
            self.client().generate('system', {'clause': 'text'}, RESPONSE_SCHEMA, num_predict=512)


if __name__ == '__main__':
    unittest.main()
