from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.request import urlopen

from xfed.common import write_json, write_output
from xfed.ui import CachedClient, Pipeline, handler


ROOT = Path(__file__).resolve().parents[1]


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        shutil.copytree(ROOT / 'schemas', self.root / 'schemas')
        for name in ('contract_01', 'contract_02'):
            path = self.root / 'contracts' / name / 'input/contract.pdf'
            path.parent.mkdir(parents=True)
            path.write_bytes(b'fixture')
        self.pipeline = Pipeline(self.root, 'http://localhost:11434')

    def test_overview_without_outputs(self):
        result = self.pipeline.overview()
        self.assertEqual(len(result['contracts']), 2)
        self.assertEqual(result['job']['status'], 'idle')
        self.assertTrue(all(c['source_bytes'] == 7 for c in result['contracts']))
        self.assertTrue(all(not s['ready'] for c in result['contracts'] for s in c['stages'].values()))

    def test_saved_metrics_and_activity_without_contract_text(self):
        document = dict(contract_id='contract_01', chunks=[dict(chunk_id='c1', text='Private evidence.',
                         source=dict(pages=[1], block_ids=['b1']),
                         classification=dict(label='NEEDS_CONTEXT'))])
        write_output(self.pipeline.path('contract_01', '1.3'), document, 3, self.root)
        write_json(self.root / 'contracts/contract_01/output/.runs.json', {
            '1.3': dict(time='2026-10-14T10:00:00Z', model='llama3.1:70b', status='complete')})
        result = self.pipeline.overview()
        contract = result['contracts'][0]
        self.assertEqual(contract['labels']['NEEDS_CONTEXT'], 1)
        self.assertEqual(contract['stages']['1.3']['count'], 1)
        self.assertEqual(contract['stages']['1.3']['run']['model'], 'llama3.1:70b')
        self.assertNotIn('Private evidence.', json.dumps(result))
        self.assertNotIn('source_locations', contract)

    def test_report_summary_and_current_failure(self):
        write_json(self.root / 'contracts/contract_01/output/2 policy_generation/generation_report.json',
                   dict(status='draft', unresolved_chunks=2,
                        warnings=[dict(chunk_id='c1', message='Metadata conflict.')],
                        clauses=[dict(generated=False), dict(generated=True)]))
        self.pipeline.update(status='failed', contract='contract_01', stage='2', message='Request rejected')
        result = self.pipeline.overview()
        self.assertEqual(result['contracts'][0]['report'],
                         dict(status='draft', skipped=1, unresolved=2, warnings=1))
        self.assertEqual(result['job']['message'], 'Request rejected')

    def test_reset_generated_removes_stage_outputs_metadata_and_model_cache(self):
        base = self.root / 'contracts/contract_01/output'
        for stage in ('1.1', '1.2', '1.3', '1.4', '2'):
            path = self.pipeline.path('contract_01', stage)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{}', encoding='utf-8')
        write_json(base / '2 policy_generation/generation_report.json', {'status': 'draft'})
        write_json(base / '.runs.json', {stage: {'status': 'complete'}
                                        for stage in ('1.1', '1.2', '1.3', '1.4', '2')})
        write_json(base / '.llm_cache.json', {'cached': True})

        self.pipeline.reset_generated('contract_01')

        self.assertTrue(all(not self.pipeline.path('contract_01', stage).exists()
                            for stage in ('1.1', '1.2', '1.3', '1.4', '2')))
        self.assertFalse((base / '2 policy_generation/generation_report.json').exists())
        self.assertFalse((base / '.runs.json').exists())
        self.assertFalse((base / '.llm_cache.json').exists())

    def test_run_through_two_forces_fresh_model_requests(self):
        self.pipeline.models = Mock(return_value=[{'name': 'test-model', 'digest': 'abc'}])

        with patch('xfed.ui.threading.Thread') as thread:
            self.pipeline.start(dict(contract='contract_01', stage='2', mode='through',
                                     model='test-model', reuse=True))

        args = thread.call_args.kwargs['args']
        self.assertEqual(args[1], ['1.1', '1.2', '1.3', '1.4', '2'])
        self.assertFalse(args[3])
        self.assertTrue(args[4])

    def test_overview_http_endpoint(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.pipeline))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f'http://127.0.0.1:{server.server_port}/api/overview', timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(len(json.load(response)['contracts']), 2)
        finally:
            server.shutdown()
            server.server_close()

    def test_browser_disconnect_does_not_raise(self):
        request_type = handler(self.pipeline)
        request = request_type.__new__(request_type)
        request.send_response = Mock()
        request.send_header = Mock()
        request.end_headers = Mock(side_effect=ConnectionResetError())
        request.wfile = Mock()
        request.send(dict(contracts=[]))
        request.wfile.write.assert_not_called()

    def test_model_cache_separates_output_token_limits(self):
        class Client:
            url = 'http://localhost:11434'
            model = 'test-model'
            num_ctx = 4096
            num_predict = 512
            think = None
            model_digest = 'test-digest'

            def __init__(self):
                self.calls = []

            def generate(self, prompt, value, schema, num_predict=None):
                self.calls.append(num_predict)
                return dict(num_predict=num_predict)

        client = Client()
        cached = CachedClient(client, self.root / '.llm_cache.json', threading.Event(), True)

        with patch('xfed.ui.write_json'):
            first = cached.generate('prompt', {}, {}, num_predict=512)
            retried = cached.generate('prompt', {}, {}, num_predict=1024)
            reused = cached.generate('prompt', {}, {}, num_predict=512)

        self.assertEqual(client.calls, [512, 1024])
        self.assertEqual(first, reused)
        self.assertEqual(retried, dict(num_predict=1024))


if __name__ == '__main__':
    unittest.main()
