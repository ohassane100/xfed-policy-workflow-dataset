from copy import deepcopy
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator
from xfed.policy_generation import check_traceability, generate, response_schema


ROOT = Path(__file__).resolve().parents[1]
ODRL_CONTEXT = [
    'http://www.w3.org/ns/odrl.jsonld',
    {'ids': 'https://w3id.org/idsa/core/', 'idsc': 'https://w3id.org/idsa/code/'},
]


def clause(identifier, text):
    return dict(chunk_id=identifier, clause_id=identifier, text=text,
                source=dict(pages=[1], block_ids=[f'b-{identifier}']), context=[])


def answer(item, relationship='primary', osdu=None):
    policy = dict(
        contract_id='test',
        odrlPolicy=dict(
            **{'@context': ODRL_CONTEXT, '@type': 'Agreement',
               'uid': 'urn:xfed:test:policy', 'profile': 'http://www.w3.org/ns/odrl/2/core'},
            obligation=[dict(uid='draft-rule', action='use')],
        ),
        traceability=[dict(
            ruleUid='draft-rule', ruleType='obligation',
            sourceClauses=[dict(chunk_id=item['chunk_id'], clause_id=item['clause_id'],
                                text=item['text'], relationship=relationship)],
        )],
    )
    if osdu is not None:
        policy['osdu'] = osdu
    return dict(policy=policy, reason='Drafted from the clause.')


class SequenceClient:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.prompts = []
        self.values = []
        self.output_limits = []

    def generate(self, prompt, value, schema, num_predict=None):
        self.prompts.append(prompt)
        self.values.append(deepcopy(value))
        self.output_limits.append(num_predict)
        result = next(self.answers)
        if isinstance(result, Exception):
            raise result
        return deepcopy(result)


class PolicyGenerationTests(unittest.TestCase):
    def test_missing_primary_evidence_gets_one_correction_attempt(self):
        item = clause('c1', 'The recipient shall use the data only for billing.')
        client = SequenceClient([
            answer(item, 'context'),
            answer(item),
        ])

        policy, report = generate(dict(contract_id='test', items=[item]), client, ROOT)

        self.assertEqual(len(client.prompts), 2)
        self.assertIn('Correction:', client.prompts[1])
        self.assertEqual(report['status'], 'draft')
        self.assertTrue(report['clauses'][0]['generated'])
        self.assertEqual(len(policy['traceability']), 1)
        self.assertEqual(policy['traceability'][0]['ruleUid'], 'urn:xfed:test:c1:r1')

    def test_context_cannot_be_promoted_to_primary_without_a_correction(self):
        item = clause('c1', 'The current clause.')
        supporting = dict(chunk_id='c2', clause_id='2', text='Supporting clause.', relationship='reference',
                          source=dict(pages=[2], block_ids=['b-c2']))
        item['context'] = [supporting]
        invalid = answer(item)
        source = invalid['policy']['traceability'][0]['sourceClauses']
        source[0].update(chunk_id='c2', clause_id='2', text='Supporting clause.')
        client = SequenceClient([invalid, answer(item)])

        policy, report = generate(dict(contract_id='test', items=[item]), client, ROOT)

        self.assertEqual(len(client.prompts), 2)
        self.assertTrue(report['clauses'][0]['generated'])
        self.assertEqual(policy['traceability'][0]['sourceClauses'][0]['chunk_id'], 'c1')

    def test_inexact_evidence_text_gets_one_correction_attempt(self):
        item = clause('c1', 'The exact current clause text.')
        invalid = answer(item)
        invalid['policy']['traceability'][0]['sourceClauses'][0]['text'] += ' Altered.'
        client = SequenceClient([invalid, answer(item)])

        policy, report = generate(dict(contract_id='test', items=[item]), client, ROOT)

        self.assertEqual(len(client.prompts), 2)
        self.assertIn('Copy source chunk_id', client.prompts[1])
        self.assertTrue(report['clauses'][0]['generated'])
        self.assertEqual(policy['traceability'][0]['sourceClauses'][0]['text'], item['text'])

    def test_persistently_invalid_clause_is_rejected_and_later_clauses_continue(self):
        first = clause('c1', 'First clause.')
        second = clause('c2', 'Second clause.')
        client = SequenceClient([
            answer(first, 'context'),
            answer(first, 'context'),
            answer(second),
        ])

        policy, report = generate(dict(contract_id='test', items=[first, second]), client, ROOT)

        self.assertEqual(report['status'], 'draft_with_rejections')
        self.assertEqual([entry['generated'] for entry in report['clauses']], [False, True])
        self.assertIn('rejected after correction attempt', report['clauses'][0]['reason'])
        self.assertEqual(len(policy['traceability']), 1)
        self.assertEqual(policy['traceability'][0]['sourceClauses'][0]['chunk_id'], 'c2')

    def test_duplicate_context_ids_accept_any_exact_supplied_text_variant(self):
        item = clause('c1', 'Current clause.')
        item['context'] = [
            dict(chunk_id='d1', clause_id='1.1', text='Definition excerpt.', relationship='definition'),
            dict(chunk_id='d1', clause_id='1.1', text='Full definition text.', relationship='reference'),
        ]
        policy = answer(item)['policy']
        policy['traceability'][0]['sourceClauses'].append(dict(
            chunk_id='d1', clause_id='1.1', text='Definition excerpt.', relationship='context'))

        check_traceability(policy, item)

    def test_request_context_is_compact_and_bounded_with_omission_reported(self):
        item = clause('c1', 'The recipient may process information for billing.')
        item['context'] = [dict(chunk_id=f'r{i}', clause_id=f'2.{i}',
                                text=f'Reference {i}: ' + 'Supporting text. ' * 80,
                                relationship='reference',
                                source=dict(pages=[i + 1], block_ids=[f'b{i}']))
                           for i in range(12)]
        client = SequenceClient([answer(item)])
        client.num_ctx, client.num_predict = 4096, 512

        _, report = generate(dict(contract_id='test', items=[item]), client, ROOT)

        supplied = client.values[0]['clause']
        self.assertFalse(supplied['context_complete'])
        self.assertGreater(supplied['omitted_context_count'], 0)
        self.assertTrue(all('source' not in context for context in supplied['context']))
        self.assertEqual(report['clauses'][0]['omitted_context_count'], supplied['omitted_context_count'])

    def test_context_overflow_retries_with_less_context(self):
        item = clause('c1', 'The recipient may process information for billing.')
        item['context'] = [dict(chunk_id=f'r{i}', clause_id=f'2.{i}',
                                text=f'Reference {i}: ' + 'Supporting text. ' * 60,
                                relationship='reference',
                                source=dict(pages=[i + 1], block_ids=[f'b{i}']))
                           for i in range(8)]
        client = SequenceClient([
            ValueError('Ollama context limit reached; increase num_ctx before accepting a response.'),
            answer(item),
        ])
        client.num_ctx, client.num_predict = 4096, 512

        _, report = generate(dict(contract_id='test', items=[item]), client, ROOT)

        self.assertEqual(len(client.values), 2)
        self.assertGreater(len(client.values[0]['clause']['context']),
                           len(client.values[1]['clause']['context']))
        self.assertEqual(report['clauses'][0]['context_retries'], 1)

    def test_context_overflow_without_context_is_not_repeated(self):
        item = clause('c1', 'A self-contained policy clause.')
        client = SequenceClient([
            ValueError('Ollama context limit reached; increase num_ctx before accepting a response.'),
        ])

        policy, report = generate(dict(contract_id='test', items=[item]), client, ROOT)

        self.assertIsNone(policy)
        self.assertEqual(len(client.values), 1)
        self.assertEqual(report['status'], 'generation_rejected')

    def test_output_limit_retries_once_with_more_tokens(self):
        item = clause('c1', 'A clause with a detailed policy.')
        client = SequenceClient([
            ValueError('Ollama output token limit reached at 512 tokens.'),
            answer(item),
        ])
        client.num_ctx, client.num_predict = 4096, 512

        policy, report = generate(dict(contract_id='test', items=[item]), client, ROOT)

        self.assertEqual(client.output_limits, [512, 1024])
        self.assertTrue(report['clauses'][0]['generated'])
        self.assertEqual(report['clauses'][0]['output_retries'], 1)
        self.assertEqual(len(policy['traceability']), 1)

    def test_still_truncated_clause_is_skipped_and_later_clause_runs(self):
        first = clause('c1', 'A complex clause.')
        second = clause('c2', 'A concise clause.')
        client = SequenceClient([
            ValueError('Ollama output token limit reached at 512 tokens.'),
            ValueError('Ollama output token limit reached at 1024 tokens.'),
            answer(second),
        ])
        client.num_ctx, client.num_predict = 4096, 512

        policy, report = generate(dict(contract_id='test', items=[first, second]), client, ROOT)

        self.assertEqual([entry['generated'] for entry in report['clauses']], [False, True])
        self.assertIn('retried with 1024 output tokens', report['clauses'][0]['reason'])
        self.assertEqual(policy['traceability'][0]['sourceClauses'][0]['chunk_id'], 'c2')

    def test_truncated_clause_is_reported_and_later_clause_continues(self):
        first = clause('c1', 'A clause with a complex output.')
        second = clause('c2', 'A clause with a concise output.')
        client = SequenceClient([
            ValueError('Ollama response incomplete or truncated; no structured response accepted.'),
            answer(second),
        ])

        policy, report = generate(dict(contract_id='test', items=[first, second]), client, ROOT)

        self.assertEqual([entry['generated'] for entry in report['clauses']], [False, True])
        self.assertIn('incomplete or truncated', report['clauses'][0]['reason'])
        self.assertEqual(policy['traceability'][0]['sourceClauses'][0]['chunk_id'], 'c2')

    def test_schema_validation_failure_is_summarized_and_later_clause_continues(self):
        first = clause('c1', 'First clause.')
        first['context'] = [dict(chunk_id=f'r{i}', clause_id=f'2.{i}',
                                 text=f'Reference {i}: ' + 'Supporting text. ' * 300,
                                 relationship='reference',
                                 source=dict(pages=[i + 1], block_ids=[f'b{i}']))
                            for i in range(8)]
        second = clause('c2', 'Second clause.')
        client = SequenceClient([
            dict(unexpected_payload='bad structured response', reason='Invalid response.'),
            answer(second),
        ])

        policy, report = generate(dict(contract_id='test', items=[first, second]), client, ROOT)

        self.assertEqual([entry['generated'] for entry in report['clauses']], [False, True])
        self.assertLess(len(report['clauses'][0]['reason']), 500)
        self.assertIn('required property', report['clauses'][0]['reason'])
        self.assertGreater(report['clauses'][0]['omitted_context_count'], 0)
        self.assertEqual(policy['traceability'][0]['sourceClauses'][0]['chunk_id'], 'c2')

    def test_conflicting_osdu_metadata_warns_without_discarding_rules(self):
        first = clause('c1', 'First clause.')
        second = clause('c2', 'Second clause.')
        client = SequenceClient([
            answer(first, osdu=dict(legal=dict(legaltags=['tag-a'], otherRelevantDataCountries=['NO']))),
            answer(second, osdu=dict(legal=dict(legaltags=['tag-a'], otherRelevantDataCountries=['GB']))),
        ])

        policy, report = generate(dict(contract_id='test', items=[first, second]), client, ROOT)

        self.assertEqual(report['status'], 'draft_with_warnings')
        self.assertEqual(len(report['warnings']), 1)
        self.assertEqual(len(policy['traceability']), 2)
        self.assertEqual(policy['osdu']['legal']['otherRelevantDataCountries'], ['NO'])

    def test_nested_duty_identifiers_are_rewritten_with_traceability(self):
        item = clause('c1', 'The recipient may use the data and must notify the owner.')
        fragment = dict(
            contract_id='test',
            odrlPolicy=dict(
                **{'@context': ODRL_CONTEXT, '@type': 'Agreement', 'uid': 'draft-policy',
                   'profile': 'http://www.w3.org/ns/odrl/2/core'},
                permission=[dict(uid='permit', target='data', assigner='owner', assignee='recipient',
                                 action='use', duty=[dict(uid='notify', action='inform')])],
            ),
            traceability=[
                dict(ruleUid='permit', ruleType='permission', sourceClauses=[dict(
                    chunk_id='c1', clause_id='c1', text=item['text'], relationship='primary')]),
                dict(ruleUid='notify', ruleType='duty', sourceClauses=[dict(
                    chunk_id='c1', clause_id='c1', text=item['text'], relationship='primary')]),
            ],
        )
        client = SequenceClient([dict(policy=fragment, reason='Permission and duty are explicit.')])

        policy, _ = generate(dict(contract_id='test', items=[item]), client, ROOT)

        permission = policy['odrlPolicy']['permission'][0]
        self.assertEqual(permission['uid'], 'urn:xfed:test:c1:r1')
        self.assertEqual(permission['duty'][0]['uid'], 'urn:xfed:test:c1:r2')
        self.assertEqual([trace['ruleUid'] for trace in policy['traceability']], [
            'urn:xfed:test:c1:r1', 'urn:xfed:test:c1:r2',
        ])

    def test_duplicate_input_chunk_ids_are_rejected_before_model_calls(self):
        item = clause('c1', 'A policy clause.')
        client = SequenceClient([])

        with self.assertRaisesRegex(ValueError, 'unique chunk IDs'):
            generate(dict(contract_id='test', items=[item, deepcopy(item)]), client, ROOT)

        self.assertEqual(client.values, [])

    def test_response_schema_is_valid_and_has_no_unresolved_refs_or_regex_shorthands(self):
        schema = response_schema(ROOT)
        Draft202012Validator.check_schema(schema)
        serialized = json.dumps(schema)
        self.assertNotIn('"$ref"', serialized)
        self.assertNotIn(r'\\d', serialized)


if __name__ == '__main__':
    unittest.main()
