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


def signatures(policy, core=False):
    """Ignore identifiers and ordering, never fuzzy-match away a qualification."""
    def encode(value):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)

    def logical(entry):
        group = next((key for key in ('all', 'any', 'exactly_one') if key in entry), None)
        if group:
            children = []
            for child in entry[group]:
                result = logical(child)
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
            result['conditions'] = logical({'all': entry.get('conditions', [])})
        for field in ('requires', 'remedies'):
            if entry.get(field):
                result[field] = sorted((rule(child) for child in entry[field]), key=encode)
        return result
    return [encode(rule(entry)) for entry in policy['rules']]


def score(reference, predictions, root=Path('.')):
    rows = {row['case_id']: row for row in predictions['cases']}
    known = {case['case_id'] for case in reference['cases']}
    if len(rows) != len(predictions['cases']) or set(rows) - known:
        raise ValueError('Duplicate or unknown prediction case IDs.')
    if predictions['reference_sha256'] != reference['sha256']:
        raise ValueError('Predictions belong to a different frozen reference.')
    results, totals = [], dict(matched=0, predicted=0, reference=0, core_matched=0, complete_clauses=0,
                              scored_clauses=0, failures=0, expected_no_rule=0, correct_no_rule=0)
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
        predicted_count = len((actual or {}).get('rules', []))
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
            missing_signatures=list((expected_sig - actual_sig).elements()),
            unmatched_signatures=list((actual_sig - expected_sig).elements())))
    precision = totals['matched'] / totals['predicted'] if totals['predicted'] else 0.0
    recall = totals['matched'] / totals['reference'] if totals['reference'] else 0.0
    return dict(protocol='frozen_ai_reference_agreement_v1', reference_sha256=reference['sha256'],
        complete=len(rows) == len(known), completed_cases=len(rows), total_cases=len(known),
        interpretation='Agreement with a provisional AI reference, not legal accuracy. Core matches ignore conditions and are diagnostic only. Equivalent wording may still mismatch.',
        totals=totals, exact_rule_agreement=dict(precision=precision, recall=recall,
            f1=2 * precision * recall / (precision + recall) if precision + recall else 0.0), cases=results)


def run(reference, client, root, output):
    from xfed.policy_generation import generate
    predictions = dict(reference_sha256=reference['sha256'], model=client.model,
        model_digests=[worker.model_digest for worker in client.clients],
        settings={key: getattr(client, key, None) for key in ('num_ctx', 'num_predict', 'think', 'workers')},
        pipeline_sha256=fingerprint({str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for folder in ('src', 'schemas', 'prompts') for path in (root / folder).rglob('*')
            if path.suffix in ('.py', '.json', '.md', '.html')}),
        scope='Isolated policy extraction using frozen supplied context; relevance routing and PDF extraction are not measured.', cases=[])
    def operation(case):
        row = dict(case_id=case['case_id'], source_sha256=case['source_sha256'])
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
                          num_ctx=32768, num_predict=8192, timeout=900, think=False)
        try:
            pool.preflight()
            run(reference, pool, args.root, args.output)
        finally:
            pool.close()


if __name__ == '__main__':
    main()
