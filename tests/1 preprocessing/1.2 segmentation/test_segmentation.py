from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[3]
from collections import Counter
from xfed.common import clause_number
from xfed.segmentation import segment

def document(*texts):
    return dict(contract_id='test', blocks=[dict(block_id=f'b{i}', text=text, page=i + 1,
                                               type='paragraph', bbox=[10, 10, 500, 700])
                                           for i, text in enumerate(texts)])

class SegmentationTests(unittest.TestCase):
    def test_numbering(self):
        for text, expected in [('1 Heading', '1'), ('1.1', '1.1'), ('1.2. Text', '1.2'),
                               ('3.4.1 Text', '3.4.1'), ('10.2(a) Text', '10.2(a)'),
                               ('Article 12 Access', '12'), ('2025 report', None),
                               ('1 January 2025', None), ('10% interest', None)]:
            with self.subTest(text=text):
                self.assertEqual(clause_number(text), expected)

    def test_prose_references_quantities_and_nested_lists(self):
        for text in ('Article 17) or risk of loss', 'Clause 5.4.1 does not entitle', '14 calendar days'):
            self.assertIsNone(clause_number(text))
        source = document('4 Access\n4.1 Choices', '(a) First choice', '(i) First condition',
                          '(ii) Second condition', '(b) Second choice', '4.2 End')
        chunks = segment(source, ROOT)['chunks']
        self.assertIn('4.1(a)(i)', [c.get('clause_id') for c in chunks])
        self.assertIn('4.1(a)(ii)', [c.get('clause_id') for c in chunks])

    def test_hierarchy_continuation_and_traceability(self):
        source = document('10 Access\n10.2 Sharing\n10.2(a) Data may be shared',
                          'only with permission.', '10.2(b) Copies must be deleted.', '11 Retention')
        output = segment(source, ROOT)
        index = {c['clause_id']: c for c in output['chunks']}
        self.assertIn('only with permission.', index['10.2(a)']['text'])
        self.assertEqual(index['10.2(a)']['source'], dict(pages=[1, 2], block_ids=['b0', 'b1']))
        self.assertEqual(index['10.2(a)']['parent_clause_id'], '10.2')
        self.assertEqual(index['10.2']['child_clause_ids'], ['10.2(a)', '10.2(b)'])
        self.assertEqual(index['10.2']['parent_clause_id'], '10')
        self.assert_text_preserved(source, output)

    def test_wrapped_reference_is_not_a_new_clause(self):
        source = document('2 Definitions\n2.1 Meaning',
                          'Agreement has the meaning in Article\n1.1.',
                          'Copies means duplicates.', '2.2 Interpretation')
        output = segment(source, ROOT)
        self.assertNotIn('1.1', [c.get('clause_id') for c in output['chunks']])
        definitions = [c for c in output['chunks'] if c['text'].startswith(('Agreement', 'Copies'))]
        self.assertTrue(all(c['parent_clause_id'] == '2.1' for c in definitions))

    def assert_text_preserved(self, source, output):
        chars = lambda items: Counter(c for item in items for c in item['text'] if not c.isspace())
        self.assertEqual(chars(source['blocks']), chars(output['chunks']))

    def test_paragraph_fallback(self):
        source = document('Data may be shared.', 'Copies must be deleted.')
        output = segment(source, ROOT)
        self.assertEqual([c['text'] for c in output['chunks']], [b['text'] for b in source['blocks']])
        self.assertTrue(all('clause_id' not in c for c in output['chunks']))

    def test_repeated_annex_numbers_and_margins(self):
        source = document('ANNEX I', '1 Access\n1.1 First clause', 'Page 1',
                          'continued.', 'ANNEX II', '1 Access\n1.1 Second clause')
        source['blocks'][2]['type'] = 'other'
        output = segment(source, ROOT)
        parents = [c for c in output['chunks'] if c.get('clause_id') == '1']
        self.assertEqual([c['child_clause_ids'] for c in parents], [['1.1'], ['1.1']])
        self.assertIn('continued.', next(c for c in output['chunks'] if c.get('clause_id') == '1.1')['text'])
        self.assert_text_preserved(source, output)
