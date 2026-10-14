from copy import deepcopy
from pathlib import Path
import unittest

from xfed.enrichment import ContextIndex, enrich
from xfed.relevance import classify


ROOT = Path(__file__).resolve().parents[1]


def chunk(identifier, text, number=None, parent=None, children=None, label=None, heading=None):
    value = dict(chunk_id=identifier, text=text,
                 source=dict(pages=[1], block_ids=['b' + identifier]))
    for key, item in (('clause_id', number), ('parent_clause_id', parent),
                      ('child_clause_ids', children), ('heading', heading)):
        if item is not None:
            value[key] = item
    if label:
        value['classification'] = dict(label=label)
    return value


class RecordingClient:
    def __init__(self, decide):
        self.decide = decide
        self.calls = []

    def generate(self, prompt, value, schema):
        self.calls.append(deepcopy(value))
        return dict(label=self.decide(value))


class RelevanceContextTests(unittest.TestCase):
    def test_first_pass_supplies_parent_and_preserves_source(self):
        parent = chunk('p', 'Data may be used only for the following purposes:', '3.1.1',
                       children=['3.1.1(a)'])
        child = chunk('c', '(a) performing an agreement with the User;', '3.1.1(a)', '3.1.1')
        document = dict(contract_id='test', chunks=[parent, child])
        original = deepcopy(document)

        def decide(value):
            if value['clause']['chunk_id'] == 'c':
                self.assertTrue(any(item['chunk_id'] == 'p' and item['text'] == parent['text']
                                    for item in value['context']))
            return 'RELEVANT'

        result = classify(document, RecordingClient(decide), ROOT)
        self.assertEqual(document, original)
        for before, after in zip(original['chunks'], result['chunks']):
            self.assertEqual(before, {k: v for k, v in after.items() if k != 'classification'})

    def test_list_introduction_receives_all_descendants(self):
        parent = chunk('p', 'The Data Holder undertakes not to use the Data:', '3.1.2',
                       children=['3.1.2(a)'])
        child = chunk('c', '(a) for these purposes:', '3.1.2(a)', '3.1.2')
        grandchild = chunk('g', '(i) developing a competing product;', '3.1.2(a)(i)', '3.1.2(a)')
        context, missing = ContextIndex([parent, child, grandchild]).context_for(parent)
        self.assertEqual({item['chunk_id'] for item in context}, {'c', 'g'})
        self.assertEqual(missing, [])

    def test_children_can_be_found_without_declared_child_ids(self):
        parent = chunk('p', 'Permitted uses:', '3.1')
        child = chunk('c', '(a) issuing invoices;', '3.1(a)', '3.1')
        context, missing = ContextIndex([parent, child]).context_for(parent)
        self.assertEqual([item['chunk_id'] for item in context], ['c'])
        self.assertEqual(missing, [])

    def test_missing_parent_is_reported_to_first_pass(self):
        child = chunk('c', '(a) issuing invoices;', '3.1(a)', '3.1')

        def decide(value):
            self.assertIn('3.1: provision not found in this section', value['unresolved_references'])
            return 'NEEDS_CONTEXT'

        result = classify(dict(contract_id='test', chunks=[child]), RecordingClient(decide), ROOT)
        self.assertEqual(result['chunks'][0]['classification']['label'], 'NEEDS_CONTEXT')

    def test_missing_declared_child_is_reported(self):
        parent = chunk('p', 'Data may be used for:', '3.1', children=['3.1(a)'])
        context, missing = ContextIndex([parent]).context_for(parent)
        self.assertEqual(context, [])
        self.assertIn('3.1(a): provision not found in this section', missing)

    def test_definitions_used_by_parent_are_attached_to_child(self):
        definition = chunk('d', 'Personal Data means information identifying a person.')
        parent = chunk('p', 'Personal Data may only be used for:', '3.1')
        child = chunk('c', '(a) issuing invoices;', '3.1(a)', '3.1')
        context, missing = ContextIndex([definition, parent, child]).context_for(child)
        self.assertTrue(any(item['chunk_id'] == 'd' and item['relationship'] == 'definition'
                            for item in context))
        self.assertEqual(missing, [])

    def test_first_pass_defers_cross_references_to_enrichment(self):
        provision = chunk('c', 'The Data may be used as specified in clause 3.2.', '3.1')
        reference = chunk('r', 'Data may only be processed for billing.', '3.2')
        client = RecordingClient(lambda value: 'NEEDS_CONTEXT')
        classify(dict(contract_id='test', chunks=[provision, reference]), client, ROOT)
        self.assertEqual(client.calls[0]['context'], [])
        context, missing = ContextIndex([provision, reference]).context_for(provision)
        self.assertEqual([item['chunk_id'] for item in context], ['r'])
        self.assertEqual(missing, [])

    def test_enrichment_does_not_reclassify_excluded_clauses(self):
        parent = chunk('p', 'Data may only be used for these purposes:', '3.1', label='RELEVANT')
        child = chunk('c', '(a) issuing invoices;', '3.1(a)', '3.1', label='NOT_RELEVANT')
        document = dict(contract_id='test', chunks=[parent, child])
        original = deepcopy(document)

        updated, enriched = enrich(document, ROOT)
        self.assertEqual(document, original)
        self.assertEqual(updated['chunks'][1]['classification']['label'], 'NOT_RELEVANT')
        self.assertEqual({item['chunk_id'] for item in enriched['items']}, {'p'})

    def test_unresolved_reference_still_prevents_policy_input(self):
        provision = chunk('c', 'Use of Data is subject to clause 3.2.', '3.1', label='RELEVANT')
        updated, enriched = enrich(dict(contract_id='test', chunks=[provision]), ROOT)
        self.assertEqual(updated['chunks'][0]['classification']['label'], 'NEEDS_CONTEXT')
        self.assertEqual(enriched['items'], [])

    def test_missing_parent_of_excluded_clause_is_reconsidered(self):
        child = chunk('c', '(a) issuing invoices;', '3.1(a)', '3.1', label='NOT_RELEVANT')
        updated, enriched = enrich(dict(contract_id='test', chunks=[child]), ROOT)
        self.assertEqual(updated['chunks'][0]['classification']['label'], 'NOT_RELEVANT')
        self.assertEqual(enriched['items'], [])

    def test_unrelated_excluded_text_is_ignored(self):
        document = dict(contract_id='test', chunks=[chunk('c', '162', label='NOT_RELEVANT')])
        updated, enriched = enrich(document, ROOT)
        self.assertEqual(updated, document)
        self.assertEqual(enriched['items'], [])

    def test_annex_headings_are_not_indexed_as_definitions(self):
        heading = chunk('h', 'ANNEX II: MODEL CONTRACTUAL TERMS', heading='ANNEX II: MODEL CONTRACTUAL TERMS')
        index = ContextIndex([heading])
        self.assertNotIn('annex ii', index.definitions)

    def test_parent_lookup_stays_within_annex(self):
        first = chunk('h1', 'ANNEX II: FIRST CONTRACT', heading='ANNEX II: FIRST CONTRACT')
        parent1 = chunk('p1', 'Data may be used for advertising:', '3.1')
        second = chunk('h2', 'ANNEX III: SECOND CONTRACT', heading='ANNEX III: SECOND CONTRACT')
        parent2 = chunk('p2', 'Data may only be used for billing:', '3.1')
        child = chunk('c', '(a) issuing invoices;', '3.1(a)', '3.1')
        context, missing = ContextIndex([first, parent1, second, parent2, child]).context_for(child)
        self.assertEqual([item['chunk_id'] for item in context], ['p2'])
        self.assertEqual(missing, [])


if __name__ == '__main__':
    unittest.main()
