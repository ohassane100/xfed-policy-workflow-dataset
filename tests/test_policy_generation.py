from copy import deepcopy
from pathlib import Path
import unittest

from xfed.policy_generation import generate


ROOT = Path(__file__).resolve().parents[1]
ODRL_CONTEXT = [
    'http://www.w3.org/ns/odrl.jsonld',
    {'ids': 'https://w3id.org/idsa/core/', 'idsc': 'https://w3id.org/idsa/code/'},
]


def clause(identifier, text):
    return dict(chunk_id=identifier, clause_id=identifier, text=text,
                source=dict(pages=[1], block_ids=[f'b-{identifier}']), context=[])


def answer(item, relationship='primary'):
    return dict(policy=dict(
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
    ), reason='Drafted from the clause.')


class SequenceClient:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.prompts = []

    def generate(self, prompt, value, schema):
        self.prompts.append(prompt)
        return deepcopy(next(self.answers))


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


if __name__ == '__main__':
    unittest.main()
