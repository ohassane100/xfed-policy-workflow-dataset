from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import Mock
from urllib.request import urlopen

from xfed.common import write_json, write_output
from xfed.ui import Pipeline, handler


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
                   dict(status='draft', unresolved_chunks=2, clauses=[dict(generated=False), dict(generated=True)]))
        self.pipeline.update(status='failed', contract='contract_01', stage='2', message='Request rejected')
        result = self.pipeline.overview()
        self.assertEqual(result['contracts'][0]['report'], dict(status='draft', skipped=1, unresolved=2))
        self.assertEqual(result['job']['message'], 'Request rejected')

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


if __name__ == '__main__':
    unittest.main()
