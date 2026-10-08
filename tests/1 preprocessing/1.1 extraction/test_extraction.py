from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[3]
from collections import Counter
from tempfile import TemporaryDirectory
import pymupdf
from jsonschema import ValidationError
from xfed.common import write_output
from xfed.extraction import extract_document
from xfed.segmentation import segment

def document(*texts):
    return dict(contract_id='test', blocks=[dict(block_id=f'b{i}', text=text, page=i + 1,
                                               type='paragraph', bbox=[10, 10, 500, 700])
                                           for i, text in enumerate(texts)])

class ExtractionTests(unittest.TestCase):
    def test_real_pdf_extraction_and_table_detection(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'contract.pdf'
            with pymupdf.open() as pdf:
                page = pdf.new_page()
                page.insert_text((72, 80), '1 Access', fontsize=16, fontname='hebo')
                page.insert_text((72, 115), '1.1 Data must stay private; no copying.')
                for y in (200, 230, 260):
                    page.draw_line((72, y), (350, y))
                for x in (72, 200, 350):
                    page.draw_line((x, 200), (x, 260))
                for x, y, text in [(80, 220, 'Data'), (210, 220, 'Location'),
                                   (80, 250, 'Results'), (210, 250, 'Norway')]:
                    page.insert_text((x, y), text)
                page = pdf.new_page()
                page.insert_text((72, 80), 'The restriction continues on this page.')
                pdf.save(path)
            extracted = extract_document(path, 'test', ROOT)
            self.assertEqual({b['page'] for b in extracted['blocks']}, {1, 2})
            self.assertTrue(any(b['type'] == 'heading' for b in extracted['blocks']))
            self.assertTrue(any(b['type'] == 'table' for b in extracted['blocks']))
            self.assertTrue(all(len(b['bbox']) == 4 for b in extracted['blocks']))
            self.assertIn('1.1 Data must stay private; no copying.', '\n'.join(b['text'] for b in extracted['blocks']))
            self.assert_text_preserved(extracted, segment(extracted, ROOT))

    def assert_text_preserved(self, source, output):
        chars = lambda items: Counter(c for item in items for c in item['text'] if not c.isspace())
        self.assertEqual(chars(source['blocks']), chars(output['chunks']))

    def test_schema_failure_does_not_overwrite(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'document_blocks.json'
            path.write_text('previous', encoding='utf-8')
            invalid = document('Text')
            invalid['blocks'][0]['page'] = 0
            with self.assertRaises(ValidationError):
                write_output(path, invalid, 1, ROOT)
            self.assertEqual(path.read_text(), 'previous')

    def test_blank_pdf_is_not_a_successful_extraction(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'blank.pdf'
            with pymupdf.open() as pdf:
                pdf.new_page()
                pdf.save(path)
            with self.assertWarnsRegex(UserWarning, 'no extractable text'):
                with self.assertRaisesRegex(ValueError, 'No text'):
                    extract_document(path, 'test', ROOT)
