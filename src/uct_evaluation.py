"""Separate, provisional synthetic UCT cohort; never pooled with real agreements."""

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path

from xfed.common import write_json
from xfed.evaluation import align_rules, canonical_rule, policy_rules, prf
from xfed.uct import clause_inputs, engine_identity, extract_clause, fingerprint


def load_cases(root):
    root = Path(root)
    reference = json.loads((root / 'evaluation/uct_reference.json').read_text(encoding='utf-8'))
    template = json.loads((root / 'config/uct_template.json').read_text(encoding='utf-8'))['contract']
    cases = []
    for variant in reference['variants']:
        document = deepcopy(template)
        document['contract_id'] = variant['id']
        for clause in document['clauses']:
            for old, new in variant['replacements'].items():
                clause['text'] = clause['text'].replace(old, new)
        for case in reference['clauses']:
            item = next(row for row in clause_inputs(document) if row['chunk_id'] == case['clause_id'])
            gold = deepcopy(case['gold'])
            # Gold is authored before inference; substitutions only rename the declared synthetic scenario.
            encoded = json.dumps(gold)
            for old, new in variant['replacements'].items():
                encoded = encoded.replace(old, new)
            gold = json.loads(encoded)
            cases.append(dict(id=variant['id'] + '/' + case['clause_id'], contract_id=variant['id'],
                              split=variant['split'], category=case['category'], item=item, gold=gold))
    return reference, cases


def score(case, result, reference):
    data = dict(value_aliases=reference['value_aliases'], contract_aliases={}, action_aliases=reference['action_aliases'])
    actual = [canonical_rule(row, data, case['contract_id']) for row in policy_rules(result.get('policy'))]
    expected = [canonical_rule(row, data, case['contract_id']) for row in case['gold']['rules']]
    counts = lambda rows: Counter(json.dumps(row, sort_keys=True) for row in rows)
    gold, predicted = counts(expected), counts(actual)
    matched = sum((gold & predicted).values())
    slots, alignment = align_rules(expected, actual)
    label = (result.get('classification') or {}).get('label')
    return dict(relevance_correct=label == case['gold']['relevance'],
                exact=not result.get('failed') and gold == predicted,
                tp=matched, fp=len(actual) - matched, fn=len(expected) - matched,
                field_slots=slots, field_alignment=alignment,
                missing=[json.loads(row) for row, n in (gold - predicted).items() for _ in range(n)],
                unexpected=[json.loads(row) for row, n in (predicted - gold).items() for _ in range(n)])


def summary(rows, total):
    completed = len(rows)
    values = [row['scores'] for row in rows]
    operative = [row for row in rows if row['gold']['rules']]
    controls = [row for row in rows if not row['gold']['rules']]
    slots = {}
    for field in ('kind', 'action', 'assignee', 'target', 'constraint'):
        slots[field] = prf(*(sum(v['field_slots'][field][key] for v in values) for key in ('tp', 'fp', 'fn')))
    return dict(completed=completed, selected=total, failed=sum(bool(row['result'].get('failed')) for row in rows),
                relevance_accuracy=sum(v['relevance_correct'] for v in values) / completed if completed else None,
                exact_clause_accuracy=sum(v['exact'] for v in values) / completed if completed else None,
                operative_cases=len(operative), exact_operative_clauses=sum(row['scores']['exact'] for row in operative),
                control_cases=len(controls), correct_no_rule_controls=sum(row['scores']['exact'] for row in controls),
                rules=prf(*(sum(v[key] for v in values) for key in ('tp', 'fp', 'fn'))), field_slots=slots)


def run(root, client, split='dev', limit=None, progress=None, cancelled=None):
    root = Path(root)
    reference, cases = load_cases(root)
    if split not in ('dev', 'test'):
        raise ValueError('Choose dev or test UCT variants.')
    cases = [row for row in cases if row['split'] == split]
    if limit is not None:
        if type(limit) is not int or not 1 <= limit <= len(cases):
            raise ValueError('Invalid UCT evaluation limit.')
        # Round-robin categories before taking a short smoke subset.
        categories = {}
        for row in cases:
            categories.setdefault(row['category'], []).append(row)
        ordered = []
        while any(categories.values()):
            for group in categories.values():
                if group:
                    ordered.append(group.pop(0))
        cases = ordered[:limit]
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    path = root / 'evaluation/runs' / ('uct_' + timestamp + '.json')
    report = dict(cohort='synthetic_uct_v1', reference_status=reference['status'], split=split,
                  status='running', run_id=timestamp, source_hash=fingerprint(reference),
                  template_hash=fingerprint(json.loads((root / 'config/uct_template.json').read_text(encoding='utf-8'))),
                  engine=engine_identity(root, client), selected_ids=[row['id'] for row in cases], rows=[],
                  limitations='Assistant-authored provisional gold; related template variants, not unseen real contracts. '
                  'Strict canonical and field scores require source review of mismatches; node votes are not extraction truth.')
    def checkpoint():
        report['summary'] = summary(report['rows'], len(cases))
        write_json(path, report)
        if progress:
            progress(len(report['rows']), len(cases), 'UCT benchmark')
    checkpoint()
    for case in cases:
        if cancelled and cancelled.is_set():
            report['status'] = 'cancelled'
            break
        try:
            result = extract_clause(case['item'], client, root, case['contract_id'])
        except InterruptedError:
            report['status'] = 'cancelled'
            break
        except Exception as error:
            result = dict(classification=None, policy=None, failed=True, error=str(error))
        row = dict(case, result=result, scores=score(case, result, reference))
        report['rows'].append(row)
        checkpoint()
    else:
        report['status'] = 'complete'
    checkpoint()
    report['path'] = str(path)
    write_json(path, report)
    return report
