"""Small, frozen AI-reference comparison; scores are agreement, not legal accuracy."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from functools import lru_cache

from jsonschema import ValidationError

from xfed.common import write_json
from xfed.policy import to_compact, validate_policy, verify_sources


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load_reference(root=Path('.'), path=None):
    root = Path(root)
    path = Path(path) if path else root / 'evaluation/policy_reference.json'
    reference = json.loads(path.read_text(encoding='utf-8'))
    seen = set()
    for case in reference['cases']:
        if case['case_id'] in seen or case['status'] not in ('provisional', 'needs_review'):
            raise ValueError('Duplicate reference case or unsupported reference status.')
        seen.add(case['case_id'])
        if fingerprint(case['input']) != case['source_sha256']:
            raise ValueError('Frozen reference input changed: ' + case['case_id'])
        pdf = root / 'contracts' / case['contract_id'] / 'input/contract.pdf'
        if hashlib.sha256(pdf.read_bytes()).hexdigest() != case['pdf_sha256']:
            raise ValueError('Original PDF changed: ' + case['case_id'])
        originals = json.loads((root / 'contracts' / case['contract_id'] /
            'output/1 preprocessing/1.2 segmentation/contract_chunks.json').read_text(encoding='utf-8'))['chunks']
        primary = next((row for row in originals if row['chunk_id'] == case['input']['chunk_id']), None)
        if primary is None or primary['text'] != case['input']['text'] or primary['source'] != case['input']['source']:
            raise ValueError('Frozen primary clause no longer matches original segmentation.')
        verify_sources(dict(sources={str(index): {**{key: item[key] for key in ('chunk_id', 'clause_id', 'text') if key in item},
                       **item.get('source', {})} for index, item in enumerate(case['input'].get('context', []))}), originals)
        validate_policy(case['expected'], root)
        verify_sources(case['expected'], originals)
    reference['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    return reference


def normalized(value):
    if isinstance(value, str):
        value = unicodedata.normalize('NFKC', value).translate(str.maketrans({'\u2019': "'", '\u2018': "'"}))
        return ' '.join(value.casefold().split())
    if isinstance(value, list):
        return [normalized(item) for item in value]
    if isinstance(value, dict):
        return {key: normalized(item) for key, item in value.items()}
    return value


def condition_logic(conditions):
    """Canonical Boolean meaning over literal comparisons; no text similarity.

    Equalities are opaque predicates. Proving equivalence for every Boolean
    assignment is conservative: it cannot establish that two paraphrases mean
    the same thing. Bounded reduced decision trees avoid a new solver dependency.
    """
    atoms = set()
    def group(operator, children):
        children = tuple(children)
        if all(isinstance(child, bool) for child in children):
            return (all(children) if operator == 'all' else any(children) if operator == 'any'
                    else sum(children) == 1)
        return (operator, children)
    def convert(entry):
        for key in ('all', 'any', 'exactly_one'):
            if key in entry:
                return group(key, map(convert, entry[key]))
        comparison = dict(entry)
        operator = comparison['op']
        if operator in ('isanyof', 'isnoneof') and isinstance(comparison.get('value'), list):
            children = [convert(dict(comparison, op='eq', value=value)) for value in comparison['value']]
            result = group('any', children)
            return ('not', result) if operator == 'isnoneof' else result
        if operator == 'neq':
            return ('not', convert(dict(comparison, op='eq')))
        atom = json.dumps(comparison, sort_keys=True, ensure_ascii=False)
        atoms.add(atom)
        return ('atom', atom)
    expression = group('all', map(convert, conditions))
    ordered = sorted(atoms)
    if len(ordered) > 16:
        return dict(unproven_bounded_logic=conditions)
    @lru_cache(maxsize=None)
    def substitute(node, atom, value):
        operator, children = node
        if operator == 'atom':
            return value if children == atom else node
        if operator == 'not':
            result = substitute(children, atom, value)
            return not result if isinstance(result, bool) else ('not', result)
        values = [child if isinstance(child, bool) else substitute(child, atom, value) for child in children]
        remaining = [child for child in values if not isinstance(child, bool)]
        if operator == 'all':
            if False in values: return False
            if not remaining: return True
        elif operator == 'any':
            if True in values: return True
            if not remaining: return False
        else:
            count = sum(child is True for child in values)
            if count > 1: return False
            if not remaining: return count == 1
            if count == 1: return ('not', group('any', remaining))
        return remaining[0] if len(remaining) == 1 else group(operator, remaining)
    @lru_cache(maxsize=None)
    def decision(index, node):
        if isinstance(node, bool): return node
        if index == len(ordered):
            # Only an empty logical group can remain without any variables.
            return node[0] == 'all'
        atom = ordered[index]
        low = decision(index + 1, substitute(node, atom, False))
        high = decision(index + 1, substitute(node, atom, True))
        return low if low == high else (atom, low, high)
    return decision(0, expression)


def signatures(policy, core=False, logical=False):
    """Ignore identifiers and ordering, never fuzzy-match away a qualification."""
    def encode(value):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)

    def logical_entry(entry):
        group = next((key for key in ('all', 'any', 'exactly_one') if key in entry), None)
        if group:
            children = []
            for child in entry[group]:
                result = logical_entry(child)
                if group != 'exactly_one' and group in result:
                    children.extend(result[group])
                else:
                    children.append(result)
            return {group: sorted(children, key=encode)}
        result = {key: normalized(child) for key, child in entry.items() if key not in ('id', 'parameter')}
        if 'parameter' in entry:
            binding = policy['parameters'][entry['parameter']]
            result['unresolved'] = dict(label=normalized(binding['label']), type=binding['type'])
        if result['field'] == 'elapsedtime' and isinstance(result.get('value'), str):
            match = re.fullmatch(r'p(?:(\d+)d)?(?:t(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?)?', result['value'])
            if match and any(match.groups()):
                days, hours, minutes, seconds = [int(item or 0) for item in match.groups()]
                result['value'] = dict(duration_seconds=days * 86400 + hours * 3600 + minutes * 60 + seconds)
        if result['op'] in ('isanyof', 'isnoneof', 'isallof') and isinstance(result.get('value'), list):
            result['value'] = sorted(result['value'], key=encode)
        return result

    def rule(entry):
        result = {key: normalized(entry[key]) for key in ('kind', 'actor', 'issuer', 'action', 'asset') if key in entry}
        for key in ('actor', 'asset'):
            if isinstance(result.get(key), str):
                result[key] = re.sub(r'^the\s+', '', result[key])
        if entry.get('actor') in policy.get('parties', {}):
            result['actor'] = dict(unresolved_role=policy['parties'][entry['actor']]['role'])
        if not core:
            conditions = logical_entry({'all': entry.get('conditions', [])})
            result['conditions'] = condition_logic(conditions['all']) if logical else conditions
        for field in ('requires', 'remedies'):
            if entry.get(field):
                result[field] = sorted((rule(child) for child in entry[field]), key=encode)
        return result
    return [encode(rule(entry)) for entry in policy['rules']]


def component_comparison(expected, actual):
    """One-to-one diagnostic alignment, never fuzzy-match a rule into correctness."""
    def parts(policy):
        return [json.loads(value) for value in signatures(policy, logical=True)]
    reference, predicted = parts(expected), parts(actual)
    required = ('kind', 'actor', 'action', 'asset', 'conditions')
    optional = ('issuer', 'requires', 'remedies')
    fields = lambda left, right: [*required, *[field for field in optional if field in left or field in right]]
    def matching(i, j):
        return {field: reference[i].get(field) == predicted[j].get(field)
                for field in fields(reference[i], predicted[j])}
    # The extraction grammar bounds per-clause rules to twelve. Fail explicitly
    # instead of silently using an order-dependent greedy fallback on huge input.
    if len(predicted) > 16:
        raise ValueError('Too many predicted rules for bounded component alignment.')
    @lru_cache(maxsize=None)
    def align(index, used):
        if index == len(reference): return (0, ())
        best_score, best_pairs = align(index + 1, used)
        for j in range(len(predicted)):
            if used & (1 << j): continue
            agreements = matching(index, j)
            weight = sum((4 if key in ('kind', 'action') else 2 if key in ('actor', 'asset') else 1)
                         for key, equal in agreements.items() if equal)
            if all(agreements.values()): weight += 1000
            later_score, later_pairs = align(index + 1, used | (1 << j))
            if weight + later_score > best_score:
                best_score, best_pairs = weight + later_score, ((index, j), *later_pairs)
        return best_score, best_pairs
    pairs = align(0, 0)[1]
    counts = {field: dict(matched=0, reference=0, predicted=0) for field in (*required, *optional)}
    for collection, side in ((reference, 'reference'), (predicted, 'predicted')):
        for entry in collection:
            for field in (*required, *[key for key in optional if key in entry]):
                counts[field][side] += 1
    rows, complete = [], 0
    for i, j in pairs:
        agreements = matching(i, j)
        for field, equal in agreements.items():
            counts[field]['matched'] += int(equal)
        complete += int(all(agreements.values()))
        rows.append(dict(reference_rule_id=expected['rules'][i]['id'], prediction_rule_id=actual['rules'][j]['id'],
                         components=agreements, complete_agreement=all(agreements.values())))
    return dict(matched_rules=complete, fields=counts, alignment=rows,
        missing_reference_rule_ids=[expected['rules'][i]['id'] for i in range(len(reference)) if not any(i == left for left, _ in pairs)],
        unmatched_prediction_rule_ids=[actual['rules'][j]['id'] for j in range(len(predicted)) if not any(j == right for _, right in pairs)])


def score(reference, predictions, root=Path('.')):
    rows = {row['case_id']: row for row in predictions['cases']}
    known = {case['case_id'] for case in reference['cases']}
    if len(rows) != len(predictions['cases']) or set(rows) - known:
        raise ValueError('Duplicate or unknown prediction case IDs.')
    if predictions['reference_sha256'] != reference['sha256']:
        raise ValueError('Predictions belong to a different frozen reference.')
    results, totals = [], dict(matched=0, predicted=0, reference=0, core_matched=0, complete_clauses=0,
                              scored_clauses=0, failures=0, expected_no_rule=0, correct_no_rule=0)
    logical_matches, component_totals = 0, {}
    for case in reference['cases']:
        row = rows.get(case['case_id'])
        if case['status'] != 'provisional':
            results.append(dict(case_id=case['case_id'], status='excluded_needs_review', notes=case['notes']))
            continue
        totals['scored_clauses'] += 1
        expected = case['expected']
        actual = row.get('policy') if row else None
        error = (row or {}).get('error')
        if row and row.get('source_sha256') != case['source_sha256']:
            raise ValueError('Prediction input changed: ' + case['case_id'])
        if row is None:
            error = 'Missing prediction.'
        if actual is not None:
            try:
                actual = to_compact(actual, root=root)
                if actual['contract_id'] != case['contract_id']:
                    raise ValueError('Wrong contract identifier.')
                originals = [case['input'], *case['input'].get('context', [])]
                # Supplied context can contain several excerpts of one chunk.
                for source in actual['sources'].values():
                    candidates = [item for item in originals if item['chunk_id'] == source['chunk_id']]
                    if not any(source['text'] in item['text'] and
                               all(key not in source or source[key] == item.get('source', {}).get(key)
                                   for key in ('pages', 'block_ids')) for item in candidates):
                        raise ValueError('Prediction evidence is outside the frozen supplied context.')
                def all_rules(entries):
                    for entry in entries:
                        yield entry
                        yield from all_rules(entry.get('requires', []))
                        yield from all_rules(entry.get('remedies', []))
                if any(actual['sources'][rule['source']]['chunk_id'] != case['input']['chunk_id']
                       for rule in all_rules(actual['rules'])):
                    raise ValueError('Prediction imported a context-only rule.')
            except (ValueError, KeyError, TypeError, ValidationError) as exc:
                error = str(exc).splitlines()[0]
        submitted_rules = actual.get('rules') if isinstance(actual, dict) else None
        predicted_count = len(submitted_rules) if isinstance(submitted_rules, list) else int(actual is not None)
        try:
            comparison = component_comparison(expected, actual if actual and not error else dict(expected, rules=[]))
        except ValueError as exc:
            error = str(exc)
            comparison = component_comparison(expected, dict(expected, rules=[]))
        if error:
            for field in ('kind', 'actor', 'action', 'asset', 'conditions'):
                comparison['fields'][field]['predicted'] = predicted_count
            for field in ('issuer', 'requires', 'remedies'):
                comparison['fields'][field]['predicted'] = sum(isinstance(rule, dict) and field in rule
                                                              for rule in (submitted_rules if isinstance(submitted_rules, list) else []))
        logical_matches += comparison['matched_rules']
        for field, counts in comparison['fields'].items():
            target = component_totals.setdefault(field, dict(matched=0, reference=0, predicted=0))
            for key, number in counts.items(): target[key] += number
        expected_sig = Counter(signatures(expected))
        actual_sig = Counter(signatures(actual)) if actual and not error else Counter()
        matches = sum((expected_sig & actual_sig).values())
        core_matches = sum((Counter(signatures(expected, True)) &
                            Counter(signatures(actual, True))).values()) if actual and not error else 0
        complete = not error and expected_sig == actual_sig
        totals['matched'] += matches
        totals['predicted'] += predicted_count
        totals['reference'] += len(expected['rules'])
        totals['core_matched'] += core_matches
        totals['complete_clauses'] += int(complete)
        totals['failures'] += int(bool(error))
        totals['expected_no_rule'] += int(not expected['rules'])
        totals['correct_no_rule'] += int(not expected['rules'] and complete)
        results.append(dict(case_id=case['case_id'], status='failed' if error else 'compared', error=error,
            complete_agreement=complete, matched=matches, core_matched=core_matches,
            predicted=predicted_count, reference=len(expected['rules']),
            component_comparison=comparison,
            missing_signatures=list((expected_sig - actual_sig).elements()),
            unmatched_signatures=list((actual_sig - expected_sig).elements())))
    precision = totals['matched'] / totals['predicted'] if totals['predicted'] else 0.0
    recall = totals['matched'] / totals['reference'] if totals['reference'] else 0.0
    def metrics(matched, predicted, expected):
        precision = matched / predicted if predicted else 0.0
        recall = matched / expected if expected else 0.0
        return dict(matched=matched, predicted=predicted, reference=expected, precision=precision, recall=recall,
                    f1=2 * precision * recall / (precision + recall) if precision + recall else 0.0)
    return dict(protocol='frozen_ai_reference_agreement_v1', reference_sha256=reference['sha256'],
        complete=len(rows) == len(known), completed_cases=len(rows), total_cases=len(known),
        interpretation='Agreement with a provisional AI reference, not legal accuracy. Core matches ignore conditions and are diagnostic only. Equivalent wording may still mismatch.',
        totals=totals, exact_rule_agreement=dict(precision=precision, recall=recall,
            f1=2 * precision * recall / (precision + recall) if precision + recall else 0.0),
        logical_rule_agreement=metrics(logical_matches, totals['predicted'], totals['reference']),
        component_agreement={field: metrics(counts['matched'], counts['predicted'], counts['reference'])
                             for field, counts in component_totals.items() if counts['reference'] or counts['predicted']},
        diagnostic_scope='Component and propositional-logic agreement only; free-text meaning, source completeness and legal correctness are not established.',
        cases=results)


def run(reference, client, root, output, include_unreviewed=False):
    from xfed.policy_generation import generate
    predictions = dict(reference_sha256=reference['sha256'], model=client.model,
        model_digests=[worker.model_digest for worker in client.clients],
        settings={key: getattr(client, key, None) for key in ('num_ctx', 'num_predict', 'think', 'workers')},
        pipeline_sha256=fingerprint({str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for folder in ('src', 'schemas', 'prompts') for path in (root / folder).rglob('*')
            if path.suffix in ('.py', '.json', '.md', '.html')}),
        scope='Isolated policy extraction using frozen supplied context; relevance routing and PDF extraction are not measured.', cases=[])
    predictions['include_unreviewed'] = include_unreviewed
    def operation(case):
        row = dict(case_id=case['case_id'], source_sha256=case['source_sha256'])
        if case['status'] != 'provisional' and not include_unreviewed:
            return dict(row, policy=None, skipped='Excluded before tuning; no model request needed.')
        try:
            with client.for_chunk(case['case_id']) as worker:
                policy, report = generate(dict(contract_id=case['contract_id'], items=[deepcopy(case['input'])]), worker, root)
                row.update(policy=policy, generation_report=report)
                if report['status'] == 'generation_rejected':
                    row['error'] = 'Generation rejected; inspect generation_report.'
        except Exception as exc:
            row.update(policy=None, error=str(exc))
        return row
    # Bound submitted work to the user-authorized concurrency; no remote workers/services.
    with ThreadPoolExecutor(max_workers=client.workers) as executor:
        futures = [executor.submit(operation, case) for case in reference['cases']]
        for future in as_completed(futures):
            predictions['cases'].append(future.result())
            predictions['complete'] = len(predictions['cases']) == len(reference['cases'])
            write_json(output, predictions)
            write_json(Path(output).with_name(Path(output).stem + '-scores.json'), score(reference, predictions, root))
            row = predictions['cases'][-1]
            print(f"{len(predictions['cases'])}/{len(reference['cases'])} {row['case_id']}: {row.get('error', 'saved')}", flush=True)
    return predictions


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['describe', 'run', 'score'])
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--reference', type=Path)
    parser.add_argument('--predictions', type=Path)
    parser.add_argument('--output', type=Path, default=Path('tmp/policy-pilot.json'))
    parser.add_argument('--base-url', default='http://127.0.0.1:11435')
    parser.add_argument('--model', default='qwen3.5:122b-a10b')
    parser.add_argument('--workers', type=int, choices=[1, 2, 3], default=1)
    parser.add_argument('--think', choices=['on', 'off'], default='on')
    parser.add_argument('--output-tokens', type=int, default=12288)
    parser.add_argument('--include-unreviewed', action='store_true', help='Also run the pre-excluded diagnostic cases; never score them.')
    args = parser.parse_args()
    reference = load_reference(args.root, args.reference)
    if args.command == 'describe':
        print(json.dumps(dict(version=reference['version'], sha256=reference['sha256'], cases=len(reference['cases']),
            scored=sum(case['status'] == 'provisional' for case in reference['cases']), status=reference['status']), indent=2))
    elif args.command == 'score':
        if args.predictions is None:
            parser.error('--predictions is required for score')
        write_json(args.output, score(reference, json.loads(args.predictions.read_text(encoding='utf-8')), args.root))
    else:
        from xfed.llm import OllamaPool
        pool = OllamaPool([args.base_url], args.model, workers_per_endpoint=args.workers,
                          num_ctx=32768, num_predict=args.output_tokens, timeout=900, think=args.think == 'on')
        try:
            pool.preflight()
            run(reference, pool, args.root, args.output, args.include_unreviewed)
        finally:
            pool.close()


if __name__ == '__main__':
    main()
