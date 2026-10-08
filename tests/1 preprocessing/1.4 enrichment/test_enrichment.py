from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[3]
from copy import deepcopy
from unittest.mock import Mock
from jsonschema import ValidationError
from xfed.common import validate
from xfed.enrichment import ContextIndex, enrich

def chunk(number, text, label='NOT_RELEVANT', **extra):
    return dict(chunk_id='c' + number, clause_id=number, text=text,
                source=dict(pages=[1], block_ids=['b' + number]),
                classification=dict(label=label), **extra)

class EnrichmentTests(unittest.TestCase):
    def test_parent_reference_definition_and_reclassification(self):
        document = dict(contract_id='test', chunks=[
            chunk('1', 'Definitions\nData: recorded information.'),
            chunk('10', '10 Sharing', child_clause_ids=['10.2']),
            chunk('10.2', 'Data may be shared only under Article 12.', 'NEEDS_CONTEXT', parent_clause_id='10'),
            chunk('12', '12 Conditions', child_clause_ids=['12.1']),
            chunk('12.1', '12.1 Obtain consent.', parent_clause_id='12'),
        ])
        original = deepcopy(document)
        client = Mock()
        client.generate.return_value = dict(label='RELEVANT', reason='Consent required')
        updated, enriched = enrich(document, client, ROOT)
        self.assertEqual(document, original)
        item = enriched['items'][0]
        self.assertEqual(item['chunk_id'], 'c10.2')
        self.assertEqual({c['relationship'] for c in item['context']}, {'parent', 'reference', 'definition'})
        self.assertEqual({c['clause_id'] for c in item['context']}, {'1', '10', '12', '12.1'})
        self.assertEqual(updated['chunks'][2]['classification']['label'], 'RELEVANT')
        self.assertIn('Initial NEEDS_CONTEXT', updated['chunks'][2]['classification']['reason'])
        self.assertEqual(len(updated['chunks']), len(original['chunks']))
        client.generate.assert_called_once()
        self.assertEqual(client.generate.call_args.args[1]['context'], item['context'])
        validate(updated, 3, ROOT)
        validate(enriched, 4, ROOT)

    def test_missing_context_stays_pending_and_external_reference_not_local(self):
        document = dict(contract_id='test', chunks=[
            chunk('1', 'See Article 99 and Article 12 of the Data Act.', 'NEEDS_CONTEXT'),
            chunk('12', '12 A different local clause.')])
        client = Mock()
        client.generate.return_value = dict(label='RELEVANT')
        updated, enriched = enrich(document, client, ROOT)
        self.assertEqual(updated['chunks'][0]['classification']['label'], 'NEEDS_CONTEXT')
        self.assertIn('99', updated['chunks'][0]['classification']['reason'])
        self.assertIn('external', updated['chunks'][0]['classification']['reason'])
        self.assertEqual(enriched['items'], [])
        self.assertEqual(client.generate.call_args.args[1]['context'], [])

    def test_not_relevant_and_still_ambiguous_reclassifications(self):
        for label in ('NOT_RELEVANT', 'NEEDS_CONTEXT'):
            client = Mock()
            client.generate.return_value = dict(label=label)
            document = dict(contract_id='test', chunks=[chunk('1', 'Unclear scope.', 'NEEDS_CONTEXT')])
            updated, enriched = enrich(document, client, ROOT)
            self.assertEqual(updated['chunks'][0]['classification']['label'], label)
            self.assertEqual(enriched['items'], [])

    def test_same_number_in_different_annexes(self):
        chunks = [chunk('a', 'ANNEX I', heading='ANNEX I'), chunk('12', 'First provision'),
                  chunk('b', 'ANNEX II', heading='ANNEX II'),
                  dict(chunk('12', 'Second provision'), chunk_id='second12'),
                  chunk('14', 'See Article 12.')]
        context, missing = ContextIndex(chunks).context_for(chunks[-1])
        self.assertEqual(missing, [])
        self.assertEqual([c['text'] for c in context], ['Second provision'])

    def test_ambiguous_reference_is_not_guessed(self):
        chunks = [chunk('12', 'One'), dict(chunk('12', 'Two'), chunk_id='other12'),
                  chunk('14', 'See Article 12.')]
        context, missing = ContextIndex(chunks).context_for(chunks[-1])
        self.assertEqual(context, [])
        self.assertIn('ambiguous', missing[0])

    def test_definition_lookup_prefers_meaning_over_address_label(self):
        chunks = [chunk('1', 'Company means the data owner.'), chunk('2', 'Company:\nPostal address'),
                  chunk('3', 'Company may share Data.')]
        context, missing = ContextIndex(chunks).context_for(chunks[-1])
        self.assertEqual(missing, [])
        self.assertEqual([c['text'] for c in context], ['Company means the data owner.'])

    def test_explicit_annex_reference_and_number_range(self):
        chunks = [chunk('a', 'ANNEX I', heading='ANNEX I'),
                  chunk('12', 'First', child_clause_ids=['12.1']),
                  chunk('12.1', 'Child', parent_clause_id='12'), chunk('13', 'Second'),
                  chunk('b', 'ANNEX II', heading='ANNEX II'),
                  chunk('14', 'See Articles 12 to 13 of Annex I.')]
        context, missing = ContextIndex(chunks).context_for(chunks[-1])
        self.assertEqual(missing, [])
        self.assertEqual({c['text'] for c in context}, {'First', 'Child', 'Second'})

    def test_schema_rejects_missing_source_context(self):
        with self.assertRaises(ValidationError):
            validate(dict(contract_id="test", items=[dict(text="invented")]), 4, ROOT)
