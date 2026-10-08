from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
from io import BytesIO
import json
import shutil
from tempfile import TemporaryDirectory
from unittest.mock import patch
from xfed.__main__ import main

def response(value):
    return BytesIO(json.dumps(value).encode())

class PipelineTests(unittest.TestCase):
    def test_runner_all_contracts_with_mocked_http(self):
        import pymupdf

        def reply(request, **kwargs):
            payload = json.loads(request.data)
            value = json.loads(payload['messages'][1]['content'])
            number = value['clause'].get('clause_id')
            label = 'NOT_RELEVANT'
            if number == '1.1':
                label = 'RELEVANT' if value['context'] else 'NEEDS_CONTEXT'
            return response(dict(done=True, message=dict(content=json.dumps(dict(label=label)))))

        with TemporaryDirectory() as folder:
            root = Path(folder)
            for name in ('schemas', 'prompts'):
                (root / name).mkdir()
            for stage in range(1, 5):
                path = next((ROOT / 'schemas').glob(f'{stage:02}_*.json'))
                shutil.copyfile(path, root / 'schemas' / path.name)
            shutil.copyfile(ROOT / 'prompts/relevance_classifier.md', root / 'prompts/relevance_classifier.md')
            config = root / 'models.yaml'
            config.write_text('relevance_classifier:\n  provider: ollama\n  base_url: http://example.test\n  model: test\n', encoding='utf-8')
            for n in range(1, 4):
                path = root / 'contracts' / f'contract_{n:02}' / 'input/contract.pdf'
                path.parent.mkdir(parents=True)
                with pymupdf.open() as pdf:
                    page = pdf.new_page()
                    page.insert_text((72, 80), '1 Access\n1.1 Data may be shared under Article 2.\n2 Obtain consent.')
                    pdf.save(path)
            with patch('xfed.llm.urlopen', side_effect=reply):
                main(['all', '--through', '1.4', '--root', str(root), '--config', str(config)])
            for n in range(1, 4):
                output = root / 'contracts' / f'contract_{n:02}' / 'output'
                self.assertEqual(len(list(output.glob('*.json'))), 4)
                result = json.loads((output / 'enriched_relevance.json').read_text(encoding='utf-8'))
                self.assertEqual([c['clause_id'] for c in result['items']], ['1.1'])
                self.assertEqual({c['clause_id'] for c in result['items'][0]['context']}, {'1', '2'})
            main(['contract_01', '--through', '1.1', '--root', str(root)])
            output = root / 'contracts/contract_01/output'
            self.assertEqual([p.name for p in output.glob('*.json')], ['document_blocks.json'])
