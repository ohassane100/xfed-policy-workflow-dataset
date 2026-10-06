from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

from jsonschema import ValidationError

from xfed.common import schema, validate
from xfed.enrichment import ContextIndex, enrich
from xfed.llm import OllamaClient, load_client
from xfed.relevance import classify
from xfed.__main__ import main


ROOT = Path(__file__).resolve().parents[1]


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
