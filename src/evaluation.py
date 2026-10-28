"""Run and score frozen classification and policy references without changing pipeline outputs."""

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from uuid import uuid4
import hashlib
import json
import math
from pathlib import Path
import random
import re
from time import perf_counter
import unicodedata

from xfed.common import DURATION, constraint_leaves, map_chunks, write_json


LABELS = ('RELEVANT', 'NOT_RELEVANT', 'NEEDS_CONTEXT')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_gold(task, gold):
    if gold.get('review_status') not in ('draft', 'reviewed'):
        raise ValueError('Use draft or reviewed annotation status.')
    if gold['review_status'] == 'reviewed':
        if not isinstance(gold.get('reviewer'), str) or not gold['reviewer'].strip():
            raise ValueError('Reviewed annotations require a reviewer name.')
        if gold.get('requires_adjudication'):
            raise ValueError('Resolve requires_adjudication before marking an annotation reviewed.')
    if task == 'classification':
        if gold.get('label') not in LABELS:
            raise ValueError('Unknown reference classification label.')
        return
    if not isinstance(gold.get('rules'), list) or not isinstance(gold.get('alternatives', []), list):
        raise ValueError('Reference rules and alternatives must be lists.')
    for option in [gold['rules'], *gold.get('alternatives', [])]:
        if not isinstance(option, list):
            raise ValueError('Every alternative must be a rule list.')
        for rule in option:
            if rule.get('kind') not in ('permission', 'prohibition', 'obligation', 'duty'):
                raise ValueError('Unknown reference rule type.')
            if not all(isinstance(rule.get(key), str) and rule[key].strip() for key in ('action', 'target', 'assignee')):
                raise ValueError('Reference rules need action, target, and assignee strings.')
            list(constraint_leaves(rule.get('constraint', [])))


def load_reference(root=Path('.'), reference_path=None):
    data = json.loads((Path(reference_path) if reference_path else Path(root) / 'evaluation/reference.json').read_text(encoding='utf-8'))
    source_splits = defaultdict(set)
    for source in data['sources'].values():
        if source.get('split') not in ('dev', 'test'):
            raise ValueError('Source contracts require a dev or test split.')
        source_splits[source['sha256']].add(source['split'])
        path = Path(root) / source['pdf']
        if source['source_type'] != 'real' or hashlib.sha256(path.read_bytes()).hexdigest() != source['sha256']:
            raise ValueError('Benchmark source is not real or its PDF hash changed.')
    if any(len(splits) != 1 for splits in source_splits.values()):
        raise ValueError('The same source PDF cannot appear in both splits.')
    for task in ('classification', 'policy'):
        identifiers = set()
        for case in data[task]:
            if case['case_id'] in identifiers or case['input_id'] not in data['inputs']:
                raise ValueError('Reference cases need unique IDs and existing inputs.')
            identifiers.add(case['case_id'])
            value = data['inputs'][case['input_id']]
            if case.get('source_type') != 'real' or data['sources'][value['contract_id']]['source_type'] != 'real':
                raise ValueError('The benchmark accepts real source documents only.')
            if case.get('split') != data['sources'][value['contract_id']]['split']:
                raise ValueError('Every case must follow its source contract split.')
            for key in ('context', 'policy_context'):
                if any(identifier not in data['contexts'] for identifier in value[key]):
                    raise ValueError('Reference context is missing.')
            validate_gold(task, case['gold'])
    for contract, source in data['sources'].items():
        if source.get('sampling') != 'full_document':
            continue
        inputs = {key: value for key, value in data['inputs'].items() if value['contract_id'] == contract}
        clauses = sorted((value['clause'] for value in inputs.values()), key=lambda clause: clause['chunk_id'])
        if (len(clauses) != source['segmented_chunks'] or len({clause['chunk_id'] for clause in clauses}) != len(clauses)
                or digest(dict(contract_id=contract, chunks=clauses)) != source['segmentation_content_sha256']):
            raise ValueError('Full-document reference must retain every frozen source chunk unchanged.')
        for task in ('classification', 'policy'):
            selected = [case['input_id'] for case in data[task] if case['input_id'] in inputs]
            if len(selected) != len(inputs) or set(selected) != set(inputs):
                raise ValueError('Both tasks must cover every full-document source chunk exactly once.')
    return data


def reference_summary(data, split='test'):
    if split not in ('dev', 'test'):
        raise ValueError('Choose dev or test.')
    selected = {task: [case for case in data[task] if case['split'] == split]
                for task in ('classification', 'policy')}
    return dict(version=data['version'], status=data['status'], split=split,
                sources={contract: deepcopy(source) for contract, source in data['sources'].items() if source['split'] == split},
                counts={task: dict(total=len(cases)) for task, cases in selected.items()},
                classification_labels=dict(Counter(case['gold']['label'] for case in selected['classification'])),
                reviewed={task: sum(case['gold']['review_status'] == 'reviewed' for case in cases)
                          for task, cases in selected.items()},
                reference_sha256=digest(data), test_exposure=data['test_exposure'] if split == 'test' else None,
                limitations=data.get('limitations', []))


def review_packet(data, split='test', task='both'):
    """Export evidence without draft labels or predictions for blind annotation."""
    cases = []
    for name in ('classification', 'policy'):
        if task not in (name, 'both'):
            continue
        for case in data[name]:
            if case['split'] != split:
                continue
            gold = dict(review_status='draft', reviewer='', rationale='',
                        **({'label': None} if name == 'classification' else {'rules': None, 'alternatives': []}))
            cases.append(dict(task=name, case_id=case['case_id'],
                              input=input_for(data, case, name == 'policy'), gold=gold))
    return dict(reference_sha256=digest(data), split=split, cases=cases,
                sources={key: source for key, source in data['sources'].items() if source['split'] == split},
                instructions='Annotate source evidence independently before inspecting model outputs. Fill gold, '
                             'rationale and reviewer; mark completed entries reviewed. Leave unfinished entries draft. '
                             'Policy rules use kind, action, target, assignee and optional constraint; [] means no rule. '
                             'Record genuinely equivalent representations in alternatives. Do not alter input evidence.')


def apply_reviews(data, packet):
    """Apply explicitly reviewed annotations to a new reference, preserving inputs."""
    if packet.get('reference_sha256') != digest(data):
        raise ValueError('Review packet was created from a different reference.')
    if packet.get('split') not in ('dev', 'test'):
        raise ValueError('Review packet must declare dev or test.')
    result, seen, completed = deepcopy(data), set(), 0
    cases = {(task, case['case_id']): case for task in ('classification', 'policy') for case in result[task]}
    for entry in packet['cases']:
        key = (entry['task'], entry['case_id'])
        if key in seen or key not in cases or cases[key]['split'] != packet['split']:
            raise ValueError('Review packet has duplicate, unknown or wrong-split cases.')
        seen.add(key)
        if digest(entry['input']) != digest(input_for(data, cases[key], key[0] == 'policy')):
            raise ValueError('Review packet evidence changed.')
        gold = entry['gold']
        if gold.get('review_status') != 'reviewed':
            continue
        validate_gold(key[0], gold)
        if not isinstance(gold.get('rationale'), str) or not gold['rationale'].strip():
            raise ValueError('Reviewed annotations require a source-based rationale.')
        cases[key]['gold'] = deepcopy(gold)
        completed += 1
    if not completed:
        raise ValueError('No completed reviews in packet; draft annotations are not human review.')
    return result


def inference_files(root):
    root = Path(root)
    paths = [root / file for file in ('src/evaluation.py', 'src/llm/__init__.py', 'src/llm/service.py',
             'src/common/__init__.py', 'src/1 preprocessing/1.1 extraction/__init__.py',
             'src/1 preprocessing/1.2 segmentation/__init__.py', 'src/1 preprocessing/1.3 relevance/__init__.py',
             'src/1 preprocessing/1.4 enrichment/__init__.py',
             'src/2 policy_generation/__init__.py', 'src/3 policy_review/__init__.py')]
    paths += sorted((root / 'prompts').glob('*.md')) + sorted((root / 'schemas').glob('*.json'))
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def input_for(data, case, policy=False):
    source = data['inputs'][case['input_id']]
    context = [deepcopy(data['contexts'][identifier]) for identifier in source['policy_context' if policy else 'context']]
    if policy:
        return dict(deepcopy(source['clause']), context=context,
                    unresolved_references=list(source.get('policy_unresolved_references', [])))
    return dict(clause=deepcopy(source['clause']), context=context,
                unresolved_references=list(source['unresolved_references']))


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def prf(tp, fp, fn):
    return dict(precision=ratio(tp, tp + fp), recall=ratio(tp, tp + fn),
                f1=ratio(2 * tp, 2 * tp + fp + fn), tp=tp, fp=fp, fn=fn)


def wilson(correct, total):
    if not total:
        return None
    z = 1.959963984540054
    p, scale = correct / total, 1 + z * z / total
    centre = (p + z * z / (2 * total)) / scale
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / scale
    return [max(0, centre - half), min(1, centre + half)]


def classification_metrics(rows, macro_labels=None):
    matrix = {label: dict.fromkeys([*LABELS, 'ERROR'], 0) for label in LABELS}
    for row in rows:
        predicted = row.get('label') if not row.get('error') else None
        matrix[row['gold']][predicted if predicted in LABELS else 'ERROR'] += 1
    counts = {}
    for label in LABELS:
        tp = matrix[label][label]
        support = sum(matrix[label].values())
        predicted = sum(matrix[actual][label] for actual in LABELS)
        counts[label] = dict(prf(tp, predicted - tp, support - tp), support=support, predicted=predicted,
                             precision_wilson_ci95=wilson(tp,predicted),recall_wilson_ci95=wilson(tp,support))
    total = len(rows)
    correct = sum(matrix[label][label] for label in LABELS)
    # The fixed gold-supported labels define the macro denominator. An absent
    # NEEDS_CONTEXT class is unmeasured, not a zero on an otherwise perfect run.
    macro_labels = list(macro_labels) if macro_labels is not None else [label for label in LABELS if counts[label]['support']]
    forwarded = [row for row in rows if not row.get('error') and row.get('label') in ('RELEVANT', 'NEEDS_CONTEXT')]
    positive = counts['RELEVANT']['support']
    return dict(n=total, accuracy=ratio(correct, total), accuracy_wilson_ci95=wilson(correct, total),
                majority_baseline_accuracy=ratio(max((sum(matrix[label].values()) for label in LABELS), default=0), total),
                routing=dict(forwarded=len(forwarded), coverage=ratio(len(forwarded), total),
                    relevant_recall=ratio(sum(row['gold'] == 'RELEVANT' for row in forwarded), positive),
                    missed_relevant=sum(row['gold'] == 'RELEVANT' for row in rows) -
                                    sum(row['gold'] == 'RELEVANT' for row in forwarded)),
                macro_labels=macro_labels,
                macro_precision=ratio(sum(counts[label]['precision'] or 0 for label in macro_labels), len(macro_labels)),
                macro_recall=ratio(sum(counts[label]['recall'] or 0 for label in macro_labels), len(macro_labels)),
                macro_f1=ratio(sum(counts[label]['f1'] or 0 for label in macro_labels), len(macro_labels)),
                weighted_f1=ratio(sum(value['support'] * (value['f1'] or 0) for value in counts.values()), total),
                balanced_accuracy=ratio(sum(value['recall'] or 0 for value in counts.values()),
                                        sum(value['support'] > 0 for value in counts.values())),
                per_class=counts, confusion_matrix=matrix,
                errors=sum(matrix[label]['ERROR'] for label in LABELS),
                low_support_labels=[label for label, value in counts.items() if value['support'] < 30])


def normal(value):
    if isinstance(value, str):
        value = re.sub(r'^(?:https?://www.w3.org/ns/odrl/2/|odrl:|ids:|idsc:)', '', value)
        return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC',value).casefold()).strip()
    if isinstance(value, list):
        return [normal(item) for item in value]
    if isinstance(value, dict):
        return {key: normal(item) for key, item in sorted(value.items())}
    return value


def canonical_rule(rule, data, contract):
    aliases = {normal(key): normal(value) for key, value in
               {**data['value_aliases'], **data['contract_aliases'].get(contract, {})}.items()}
    actions = {normal(key): normal(value) for key, value in data['action_aliases'].items()}
    def value(item):
        if isinstance(item, list):
            return [value(part) for part in item]
        normalized = normal(item)
        return aliases.get(normalized, normalized) if isinstance(normalized, str) else normalized
    factors = dict(days=86400, hours=3600, minutes=60, seconds=1)
    def fixed_duration(parts):
        with localcontext() as context:
            context.prec = max(28, sum(len(str(part or '')) for part in parts.values()) + 20)
            seconds = sum(Decimal(parts.get(key) or '0') * factor for key, factor in factors.items())
            text = format(seconds, 'f')
            return 'pt' + (text.rstrip('0').rstrip('.') if '.' in text else text) + 's'
    def constraint(entry):
        for operator in ('and', 'or', 'xone'):
            if operator in entry:
                children = [constraint(child) for child in entry[operator]]
                # AND/OR are associative; XOR with more than two inputs means
                # exactly one, so nested XOR must not be flattened.
                if operator in ('and', 'or'):
                    children = [part for child in children for part in
                                (child[operator] if operator in child else [child])]
                return {operator: sorted(children, key=lambda child: json.dumps(child, sort_keys=True))}
        part = {key: value(item) for key, item in entry.items() if key != 'uid'}
        if part.get('operator') in ('isanyof','isallof','isnoneof') and isinstance(part.get('rightOperand'),list):
            part['rightOperand'] = sorted(part['rightOperand'],key=lambda item:json.dumps(item,sort_keys=True))
        duration_unit = normal(entry.get('unit', ''))
        unit_key = duration_unit.rstrip('s') + 's'
        if (normal(entry['leftOperand']) == 'elapsedtime' and type(entry.get('rightOperand')) in (int, float)
                and unit_key in factors and Decimal(str(entry['rightOperand'])).is_finite()):
            part['rightOperand'] = fixed_duration({unit_key: str(entry['rightOperand'])})
            part.pop('unit', None)
        # Fixed-length durations are quantitative values, not free-text labels.
        # Calendar months/years have no fixed conversion to days or seconds.
        if normal(entry['leftOperand']) == 'elapsedtime' and isinstance(part.get('rightOperand'), str):
            parsed = DURATION.fullmatch(part['rightOperand'])
            if parsed and not (parsed['years'] or parsed['months']):
                part['rightOperand'] = fixed_duration(parsed.groupdict())
                # An explicit unit is redundant only for the matching single component.
                if unit_key in factors and parsed[unit_key] is not None and all(
                        not Decimal(parsed[key] or '0') for key in factors if key != unit_key):
                    part.pop('unit', None)
        return part
    conjunction = constraint({'and': rule.get('constraint', [])})['and']
    constraints = [json.dumps(part, sort_keys=True) for part in conjunction]
    return dict(kind=rule['kind'], action=actions.get(normal(rule['action']), normal(rule['action'])),
                target=value(rule.get('target', '')), assignee=value(rule.get('assignee', '')),
                constraint=sorted(constraints))


def policy_rules(fragment):
    values = []
    if fragment:
        for kind in ('permission', 'prohibition', 'obligation'):
            for rule in fragment['odrlPolicy'].get(kind, []):
                values.append(dict(rule, kind=kind))
                for field in ('duty', 'remedy'):
                    for child in rule.get(field, []):
                        values.append(dict(dict(target=rule.get('target', ''), assignee=rule.get('assignee', '')),
                                           **dict(child, kind='duty')))
    return values


RULE_FIELDS = ('kind', 'action', 'assignee', 'target', 'constraint')


def reference_label_compatibility(data, split):
    """Flag reference labels unreachable under literal-source compilation.

    This conservative check allows all supplied context and declared aliases;
    reachability does not establish entailment or correctness of a reference.
    """
    issues, blocked, operative = [], [], 0
    for case in data['policy']:
        if case['split'] != split or not case['gold']['rules']:
            continue
        operative += 1
        contract = data['inputs'][case['input_id']]['contract_id']
        item = input_for(data, case, True)
        spans = [re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', clause['text']).casefold()).strip()
                 for clause in [item, *item['context']]]
        aliases = {normal(key): normal(value) for key, value in
                   {**data['value_aliases'], **data['contract_aliases'].get(contract, {})}.items()}
        option_blocked = []
        for option_index, option in enumerate([case['gold']['rules'], *case['gold'].get('alternatives', [])]):
            missing = []
            for rule_index, rule in enumerate(option):
                canonical = canonical_rule(rule, data, contract)
                for field in ('assignee', 'target'):
                    candidates = {rule[field], canonical[field],
                                  *(key for key, value in aliases.items() if value == canonical[field])}
                    if not any(any(re.search(r'(?<!\w)' + re.escape(normal(candidate)) + r'(?!\w)', span)
                                   for span in spans) for candidate in candidates):
                        missing.append(dict(rule_index=rule_index, field=field, label=rule[field]))
            option_blocked.append(bool(missing))
            if missing:
                issues.append(dict(case_id=case['case_id'], option_index=option_index, unavailable_labels=missing))
        if all(option_blocked):
            blocked.append(case['case_id'])
    return dict(operative_cases=operative, cases_with_no_literal_option=len(blocked),
                blocked_case_ids=blocked, details=issues,
                scope='Actor/asset label reachability only, including supplied context and declared aliases; '
                      'not a semantic correctness score. Review source-label and reference-format compatibility.')


def align_rules(expected, actual):
    """Maximum-weight one-to-one field alignment, for diagnostics only.

    Hungarian assignment with dummy rows/columns avoids rewarding duplicate
    predictions or choosing a greedy pairing that hides a better global match.
    No fuzzy or LLM judgments contribute to the strict rule score.
    """
    size = max(len(expected), len(actual))
    weights = [[sum(gold[key] == predicted[key] for key in RULE_FIELDS)
                for predicted in actual] for gold in expected]
    u, v, pairs, previous = [0] * (size + 1), [0] * (size + 1), [0] * (size + 1), [0] * (size + 1)
    for row in range(1, size + 1):
        pairs[0] = row
        column, costs, used = 0, [float('inf')] * (size + 1), [False] * (size + 1)
        while True:
            used[column] = True
            current, delta, following = pairs[column], float('inf'), 0
            for candidate in range(1, size + 1):
                if used[candidate]:
                    continue
                weight = weights[current - 1][candidate - 1] if current <= len(expected) and candidate <= len(actual) else 0
                cost = -weight - u[current] - v[candidate]
                if cost < costs[candidate]:
                    costs[candidate], previous[candidate] = cost, column
                if costs[candidate] < delta:
                    delta, following = costs[candidate], candidate
            for candidate in range(size + 1):
                if used[candidate]:
                    u[pairs[candidate]] += delta
                    v[candidate] -= delta
                else:
                    costs[candidate] -= delta
            column = following
            if not pairs[column]:
                break
        while column:
            following = previous[column]
            pairs[column], column = pairs[following], following
    aligned = [(pairs[column] - 1, column - 1) for column in range(1, size + 1)
               if 0 < pairs[column] <= len(expected) and column <= len(actual)
               and weights[pairs[column] - 1][column - 1] > 0]
    slots = {}
    for key in RULE_FIELDS:
        matched = sum(expected[left][key] == actual[right][key] for left, right in aligned)
        slots[key] = dict(tp=matched, fp=len(actual) - matched, fn=len(expected) - matched)
    return slots, [dict(expected=left, predicted=right,
                        differing_fields=[key for key in RULE_FIELDS if expected[left][key] != actual[right][key]])
                   for left, right in sorted(aligned)]


def policy_counts(case, prediction, data, root=Path('.')):
    from xfed.policy_generation import validate_fragment
    contract = data['inputs'][case['input_id']]['contract_id']
    fragment = prediction.get('policy')
    failed = bool(prediction.get('error') or prediction.get('failed'))
    validation_error = None
    if fragment is not None:
        try:
            validate_fragment(fragment, contract, input_for(data, case, True), root)
        except Exception as error:
            validation_error = str(error)
            failed = True
    actual = [] if failed else [canonical_rule(rule, data, contract) for rule in policy_rules(fragment)]
    options = [case['gold']['rules'], *case['gold'].get('alternatives', [])]
    best = None
    for option in options:
        expected = [canonical_rule(rule, data, contract) for rule in option]
        gold_count = Counter(json.dumps(rule, sort_keys=True) for rule in expected)
        predicted_count = Counter(json.dumps(rule, sort_keys=True) for rule in actual)
        tp = sum((gold_count & predicted_count).values())
        counts = dict(tp=tp, fp=len(actual) - tp, fn=len(expected) - tp,
                      exact=not failed and gold_count == predicted_count,
                      generation_correct=not failed and bool(expected) == bool(fragment),
                      failed=failed, generated=fragment is not None and not failed,
                      expected_rules=len(expected), predicted_rules=len(actual),
                      validation_error=validation_error,
                      schema_and_traceability_valid=fragment is not None and not failed)
        counts['outcome'] = ('failed' if failed else 'skipped_by_relevance' if prediction.get('skipped_by_relevance')
                             else prediction.get('generation_report', {}).get('outcome') or
                             ('generated' if fragment else 'abstained'))
        counts['field_slots'], counts['field_alignment'] = align_rules(expected, actual)
        counts['rule_differences'] = dict(
            missing=[json.loads(rule) for rule, count in (gold_count - predicted_count).items() for _ in range(count)],
            unexpected=[json.loads(rule) for rule, count in (predicted_count - gold_count).items() for _ in range(count)])
        # Additional diagnostics preserve rule scope in every constraint comparison.
        def conditions(rules):
            result = Counter()
            for rule in rules:
                base = json.dumps({key: value for key, value in rule.items() if key != 'constraint'}, sort_keys=True)
                result.update((base, entry) for entry in rule['constraint'])
            return result
        gold_constraints, predicted_constraints = conditions(expected), conditions(actual)
        matched = sum((gold_constraints & predicted_constraints).values())
        counts['constraints'] = dict(tp=matched, fp=sum(predicted_constraints.values()) - matched,
                                     fn=sum(gold_constraints.values()) - matched)
        if best is None or (counts['exact'], counts['tp'], -counts['fn']) > (best['exact'], best['tp'], -best['fn']):
            best = counts
    return best


def policy_metrics(rows):
    totals = {key: sum(row[key] for row in rows) for key in ('tp', 'fp', 'fn', 'exact', 'generation_correct', 'failed', 'generated', 'expected_rules', 'predicted_rules')}
    operative = [row for row in rows if row['expected_rules']]
    controls = [row for row in rows if not row['expected_rules']]
    generated_operative = sum(row['generated'] for row in operative)
    return dict(n=len(rows), canonical_rules=prf(totals['tp'], totals['fp'], totals['fn']),
                rule_precision_wilson_ci95=wilson(totals['tp'], totals['predicted_rules']),
                all_abstain_baseline_accuracy=ratio(len(controls), len(rows)),
                operative=dict(n=len(operative), exact=sum(row['exact'] for row in operative),
                    exact_accuracy=ratio(sum(row['exact'] for row in operative), len(operative)),
                    generation_coverage=ratio(generated_operative, len(operative)),
                    selective_exact_accuracy=ratio(sum(row['exact'] for row in operative), generated_operative)),
                controls=dict(n=len(controls), exact=sum(row['exact'] for row in controls)),
                outcomes=dict(Counter(row.get('outcome', 'unknown') for row in rows)),
                field_diagnostics={key: prf(*(sum(row.get('field_slots', {}).get(key, {}).get(count, 0)
                                                for row in rows) for count in ('tp', 'fp', 'fn')))
                                   for key in RULE_FIELDS},
                constraints=prf(*(sum(row['constraints'][key] for row in rows) for key in ('tp', 'fp', 'fn'))),
                exact_case_accuracy=ratio(totals['exact'], len(rows)),
                exact_case_wilson_ci95=wilson(totals['exact'], len(rows)),
                generate_or_abstain_accuracy=ratio(totals['generation_correct'], len(rows)),
                failures=totals['failed'], generated=totals['generated'], expected_rules=totals['expected_rules'],
                schema_and_traceability_valid_on_accepted=ratio(sum(row['schema_and_traceability_valid'] for row in rows), totals['generated']))


def cluster_interval(rows, operation, key, repetitions=500):
    if len(rows) < 2:
        return None
    groups = defaultdict(lambda: defaultdict(list))
    for row in rows:
        groups[row['contract']][row['group_id']].append(row)
    if sum(len(values) for values in groups.values()) < 2:
        return None
    rng, values = random.Random(20261021), []
    for _ in range(repetitions):
        sample = []
        for group in groups.values():
            members = list(group.values())
            for picked in rng.choices(members, k=len(members)):
                sample.extend(picked)
        value = operation(sample)
        for component in key.split('.'):
            value = value[component]
        if value is not None:
            values.append(value)
    values.sort()
    return [values[int((len(values)-1)*.025)], values[int((len(values)-1)*.975)]] if len(values) >= repetitions*.95 else None


def score_report(data, report, root=Path('.'), require_reviewed=False):
    if report.get('reference_version') != data['version']:
        raise ValueError('Reference version changed; rerun the benchmark instead of rescoring an older selection.')
    if report.get('reference_sha256') != digest(data):
        raise ValueError('Reference content or annotations changed; rerun against the frozen reference.')
    if report.get('split') not in ('dev', 'test'):
        raise ValueError('Reports must declare dev or test.')
    output = deepcopy(report)
    metrics, mismatches = {}, []
    for task, operation in [('classification', classification_metrics), ('policy', policy_metrics)]:
        predictions = report.get('predictions', {}).get(task, {})
        selected = report.get('selected_cases', {}).get(task, [])
        expected = {case['case_id'] for case in data[task] if report['task'] in (task, 'both')
                    and case['split'] == report['split']}
        if set(selected) != expected:
            raise ValueError('The manifest must cover the entire declared reference selection; missing predictions are counted as errors.')
        if not selected:
            continue
        cases = {case['case_id']: case for case in data[task]}
        if len(set(selected)) != len(selected) or any(identifier not in cases for identifier in selected):
            raise ValueError('The evaluation manifest needs unique, known case IDs.')
        if any(identifier not in selected for identifier in predictions):
            raise ValueError('Predictions include unknown reference IDs.')
        cohorts = defaultdict(list)
        for identifier in selected:
            case = cases[identifier]
            expected_hash = digest(input_for(data, case, task == 'policy'))
            prediction = predictions.get(identifier, dict(error='Missing prediction', input_sha256=expected_hash))
            if prediction.get('input_sha256') != expected_hash:
                raise ValueError('Evaluation inputs changed; predictions cannot be reused against different evidence.')
            if require_reviewed and case['gold']['review_status'] != 'reviewed':
                raise ValueError('Selected reference annotations are not marked reviewed.')
            info = dict(case_id=identifier, contract=data['inputs'][case['input_id']]['contract_id'],
                        group_id=case['group_id'], reviewed=case['gold']['review_status'] == 'reviewed')
            if task == 'classification':
                info.update(gold=case['gold']['label'], label=prediction.get('label'), error=prediction.get('error'))
                correct = not info['error'] and info['gold'] == info['label']
            else:
                info.update(policy_counts(case, prediction, data, root))
                correct = info['exact']
            if not correct:
                mismatches.append(dict(task=task, case_id=identifier, expected=case['gold'], predicted=prediction,
                                       scoring_error=info.get('validation_error'),
                                       **(dict(rule_differences=info['rule_differences'],
                                               field_alignment=info['field_alignment']) if task == 'policy' else {})))
            cohorts[case['source_type']+'/'+case['cohort']].append(info)
        if task == 'policy':
            for source_type in ('real',):
                combined = [row for name, rows in list(cohorts.items()) if name.startswith(source_type+'/') for row in rows]
                if combined:
                    full_document = all(data['sources'][row['contract']].get('sampling') == 'full_document' for row in combined)
                    cohorts[source_type + ('/full_document' if full_document else '/all_curated_cases')] = combined
        metrics[task] = {}
        for name, rows in cohorts.items():
            if task == 'classification':
                labels = [label for label in LABELS if any(row['gold'] == label for row in rows)]
                operation = lambda sample, labels=labels: classification_metrics(sample, labels)
            result = operation(rows)
            interval_key = 'macro_f1' if task == 'classification' else 'exact_case_accuracy'
            result[interval_key + '_page_cluster_ci95'] = cluster_interval(rows, operation, interval_key)
            if task == 'policy':
                result['canonical_rule_f1_page_cluster_ci95'] = cluster_interval(rows, operation, 'canonical_rules.f1')
            result['reviewed'] = sum(row['reviewed'] for row in rows)
            result['aggregate'] = task == 'policy' and name.endswith(('/all_curated_cases', '/full_document'))
            result['by_contract'] = {contract: operation([row for row in rows if row['contract'] == contract]) for contract in sorted({row['contract'] for row in rows})}
            contract_scores = [(value['macro_f1'] if task == 'classification' else value['canonical_rules']['f1'])
                               for value in result['by_contract'].values()
                               if task == 'classification' or value['expected_rules']]
            result['contract_macro_f1'] = ratio(sum(value or 0 for value in contract_scores), len(contract_scores))
            metrics[task][name] = result
    if not metrics:
        raise ValueError('No evaluation cases were selected in the prediction manifest.')
    output.update(metrics=metrics, mismatches=mismatches, reference_sha256=digest(data),
                  test_exposure=data['test_exposure'] if report['split'] == 'test' else None,
                  scorer_sha256=hashlib.sha256((Path(root) / 'src/evaluation.py').read_bytes()).hexdigest(),
                  incomplete_tasks=[task for task in report['selected_cases'] if task not in report.get('execution', {})],
                  reference_status='reviewed' if all(value['reviewed'] == value['n'] for task in metrics.values() for value in task.values()) else 'provisional',
                  metric_notes=data.get('limitations', []), interval_note='Wilson intervals assume independent cases. Page-cluster bootstrap intervals resample pages within each fixed source contract (500 replicates); neither interval establishes generalisation to new contracts.')
    output['evaluation_protocol'] = 'strict_rules_with_source_compatibility_diagnostics_v3'
    output['metric_notes'] = [*output['metric_notes'],
        'Aligned field diagnostics give partial credit only for debugging, never for complete-rule correctness. '
        'No approximate string or model-judge similarity changes strict precision or recall.']
    if report['split'] == 'test' and 'contract_05' in data['sources']:
        output['metric_notes'].append('Contract 05 results have been inspected during development. '
                                     'Use this exposed agreement diagnostically; reserve new independently annotated contracts for final validation.')
    policy = next((value for value in metrics.get('policy', {}).values() if value['aggregate']), None)
    if policy is not None:
        compatibility = reference_label_compatibility(data, report['split'])
        output['reference_output_compatibility'] = compatibility
        if compatibility['cases_with_no_literal_option']:
            output['metric_notes'].append('Some actor/asset reference labels cannot be emitted under literal-source '
                'compilation with the declared aliases. Strict scores are format-conformance results, not standalone '
                'semantic quality measurements; inspect reference_output_compatibility before interpreting them.')
        precision, recall = policy['canonical_rules']['precision'], policy['canonical_rules']['recall']
        interval = policy['rule_precision_wilson_ci95']
        point_targets = precision is not None and recall is not None and precision >= .99 and recall >= .99
        output['quality_assessment'] = dict(
            precision_target=.99, recall_target=.99,
            point_targets_met=point_targets,
            reference_targets_verified=point_targets and output['reference_status'] == 'reviewed',
            precision_lower_bound_target_met=bool(interval and interval[0] >= .99),
            source_contracts=len(policy['by_contract']),
            reference_status=output['reference_status'],
            note='Targets apply to frozen reference rules. Field diagnostics are not rule correctness. '
                 'Unreviewed labels, one exposed agreement and same-model checks cannot establish production accuracy. '
                 'Rule-level Wilson intervals assume independence and are descriptive, not a deployment guarantee.')
    return output


def run_evaluation(data, client, root=Path('.'), task='both', split='test', progress=None, cancel=None, completed=None,
                   policy_mode='isolated', reference_path=None):
    from xfed.llm import OllamaConnectionError
    from xfed.relevance import classify_batch, fits
    from xfed.policy_generation import generate_item, response_schema
    if task not in ('classification', 'policy', 'both'):
        raise ValueError('Choose classification, policy or both.')
    if split not in ('dev', 'test'):
        raise ValueError('Choose dev or test.')
    if policy_mode not in ('isolated', 'gated') or (policy_mode == 'gated' and task != 'both'):
        raise ValueError('Gated policy evaluation requires both tasks; otherwise choose isolated.')
    selected = {name: [case for case in data[name] if case['split'] == split]
                for name in ('classification', 'policy') if task in (name, 'both')}
    if not any(selected.values()):
        raise ValueError('No reference cases match this selection.')
    def checkpoint(done=0, total=0, message=''):
        if cancel is not None and cancel.is_set():
            raise InterruptedError('Evaluation cancelled.')
        if progress and message:
            progress(done, total, message)
    report = dict(status='running', run_id=str(uuid4()), started_at=datetime.now(timezone.utc).isoformat(),
                  reference_sha256=digest(data), split=split,
                  source_documents={cid: deepcopy(value) for cid, value in data['sources'].items() if value['split'] == split},
                  inference_sha256=inference_files(root),
                  reference_version=data['version'], benchmark='real-contracts', source='real', task=task, predictions={}, execution={},
                  policy_mode=policy_mode,
                  evaluation_scope=('Classification-gated policy extraction with frozen source context; includes relevance routing errors, '
                                    'but does not re-evaluate PDF extraction or context retrieval.' if policy_mode == 'gated' else
                                    'Stage classification and isolated policy extraction with frozen source context; policy scores do not include upstream classification errors.'),
                  selected_cases={name:[case['case_id'] for case in cases] for name,cases in selected.items() if cases},
                  case_manifest={name: {case['case_id']: dict(gold=deepcopy(case['gold']),
                                    input=input_for(data, case, name == 'policy')) for case in cases}
                                 for name, cases in selected.items() if cases},
                  code_sha256={str(path):hashlib.sha256((Path(root)/path).read_bytes()).hexdigest() for path in
                               ['src/evaluation.py','src/llm/__init__.py','src/llm/service.py','src/1 preprocessing/1.3 relevance/__init__.py','src/2 policy_generation/__init__.py']},
                  prompts={name: hashlib.sha256((Path(root)/'prompts'/file).read_bytes()).hexdigest()
                           for name, file in [('classification','relevance_classifier.md'),('policy','policy_generator.md')]})
    live_rows = defaultdict(list)
    report['live_metrics_scope'] = 'Completed cases only; provisional while inference runs. '
    report['progress'] = {name: dict(completed=0, total=len(cases)) for name, cases in selected.items()}
    report['live_metrics'] = {}
    def record(name, case, item, prediction):
        prediction = dict(prediction, input_sha256=digest(item))
        report['predictions'][name][case['case_id']] = prediction
        if name == 'classification':
            row = dict(gold=case['gold']['label'], label=prediction.get('label'), error=prediction.get('error'))
            operation = classification_metrics
        else:
            row = policy_counts(case, prediction, data, root)
            operation = policy_metrics
        live_rows[name].append(row)
        report['progress'][name]['completed'] = len(live_rows[name])
        report['live_metrics'][name] = {'completed': dict(operation(live_rows[name]), aggregate=name == 'policy')}
    def save_checkpoint():
        if completed:
            completed(deepcopy(report))
    def connection_stopped(error):
        report.update(status='failed', error=str(error), infrastructure_error=True,
                      finished_at=datetime.now(timezone.utc).isoformat())
        save_checkpoint()
    def run_work(work, operation, progress, completed):
        try:
            return map_chunks(work, operation, client, progress, completed=completed)
        except OllamaConnectionError as error:
            connection_stopped(error)
            raise
    save_checkpoint()
    try:
        client.preflight()
    except (OSError, OllamaConnectionError) as error:
        connection_stopped(error)
        raise
    for name, cases in selected.items():
        if not cases:
            continue
        checkpoint(message='Starting '+name+' evaluation')
        client.reset_assignments()
        report['predictions'][name] = {}
        started = perf_counter()
        if name == 'classification':
            num_ctx = client.relevance_num_ctx or client.num_ctx
            prepared = [(case, input_for(data, case)) for case in cases]
            groups, current, last = [], [], None
            for case, item in prepared:
                boundary = (data['inputs'][case['input_id']]['contract_id'],case['cohort'],case.get('variant'))
                if current and (boundary != last or len(current) >= client.relevance_batch_size or
                                item['clause']['chunk_id'] in {entry[1]['clause']['chunk_id'] for entry in current} or
                                not fits([entry[1] for entry in current]+[item],client,root,num_ctx)):
                    groups.append(current)
                    current=[]
                current.append((case,item));last=boundary
            if current:
                groups.append(current)
            work = [dict(chunk_id=group[0][0]['case_id'],chunk_ids=[case['case_id'] for case,_ in group],group=group) for group in groups]
            def classify(group, worker):
                checkpoint()
                try:
                    values = classify_batch([item for _,item in group['group']],worker,root,num_ctx)
                    for value in values:
                        if any(text in value.get('reason','').lower() for text in ('safe input budget','context limit reached')):
                            value['error'] = value['reason']
                    return values
                except InterruptedError:
                    raise
                except OllamaConnectionError:
                    raise
                except Exception as error:
                    return [dict(error=str(error)) for _ in group['group']]
            def classification_completed(group, values):
                for (case, item), value in zip(group['group'], values):
                    record(name, case, item, value)
                save_checkpoint()
            run_work(work,classify,lambda done,total,message:checkpoint(done,total,'Classification requests'),
                     classification_completed)
        else:
            prompt=(Path(root)/'prompts/policy_generator.md').read_text(encoding='utf-8')
            output_schema=response_schema(root)
            def policy(case,worker):
                checkpoint()
                item=input_for(data,case,True)
                routing = None
                def result(**prediction):
                    if routing is not None:
                        prediction['routing_classification'] = routing
                    return prediction
                try:
                    if policy_mode == 'gated':
                        classification_case = next((entry for entry in selected['classification']
                                                    if entry['input_id'] == case['input_id']), None)
                        routing_input = input_for(data, case)
                        if classification_case:
                            decision = report['predictions']['classification'].get(classification_case['case_id'], {})
                        else:
                            # Policy and classification samples need not overlap.
                            # Classify uncovered inputs without inventing relevance gold.
                            decision = classify_batch([routing_input], worker, root)[0]
                        routing = dict(prediction=deepcopy(decision), input_sha256=digest(routing_input),
                                       benchmark_case_id=classification_case['case_id'] if classification_case else None)
                        if decision.get('error') or decision.get('label') not in LABELS:
                            return result(policy=None, error='Upstream classification failed or is missing.')
                        if decision['label'] == 'NOT_RELEVANT':
                            return result(policy=None, skipped_by_relevance=True)
                    fragment,entry,failed=generate_item(item,worker,prompt,data['inputs'][case['input_id']]['contract_id'],output_schema,root)
                    return result(policy=fragment,generation_report=entry,failed=failed)
                except InterruptedError:
                    raise
                except OllamaConnectionError:
                    raise
                except Exception as error:
                    return result(policy=None,error=str(error))
            work=[dict(case,chunk_id=case['case_id']) for case in cases]
            def policy_completed(case, value):
                record(name, case, input_for(data, case, True), value)
                save_checkpoint()
            run_work(work,policy,lambda done,total,message:checkpoint(done,total,'Policy cases'),
                     policy_completed)
        if inference_files(root) != report['inference_sha256'] or digest(load_reference(root, reference_path)) != report['reference_sha256']:
            raise ValueError('Code, prompts, schemas or references changed during evaluation; restart with frozen inputs.')
        report['execution'][name]=dict(duration_seconds=perf_counter()-started,options=client.metadata())
        if completed:
            completed(report)
    report['status'] = 'complete'
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    checkpoint(message='Scoring frozen references')
    return score_report(data,report,root)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['describe','run','score','review-export','review-apply'])
    parser.add_argument('--root',type=Path,default=Path.cwd())
    parser.add_argument('--split',choices=['dev','test'],default='test',help='Contract 05 by default; use dev for previously inspected Contracts 01-03.')
    parser.add_argument('--task',choices=['classification','policy','both'],default='both')
    parser.add_argument('--policy-mode', choices=['isolated', 'gated'], default='isolated',
                        help='gated includes classification routing errors; requires --task both.')
    parser.add_argument('--model')
    parser.add_argument('--worker-models',nargs='+')
    parser.add_argument('--workers', type=int, default=1,
                        help='Concurrent requests to the existing endpoint; does not start model services.')
    parser.add_argument('--base-url',default='http://127.0.0.1:11434')
    parser.add_argument('--config',type=Path)
    parser.add_argument('--predictions',type=Path)
    parser.add_argument('--reference', type=Path, help='Use a separately reviewed frozen reference.')
    parser.add_argument('--annotations', type=Path, help='Completed blind annotation packet for review-apply.')
    parser.add_argument('--require-reviewed',action='store_true')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args(argv)
    root=args.root.resolve()
    data=load_reference(root, args.reference)
    if args.output and (args.output.resolve().is_relative_to(root/'contracts') or args.output.resolve().is_relative_to(root/'evaluation')):
        parser.error('Save run reports outside contracts/ and evaluation/ to preserve source outputs and references.')
    if args.output and any(args.output.resolve() == path.resolve() for path in (args.reference, args.annotations) if path):
        parser.error('Output must not overwrite the reference or annotation input.')
    if args.command=='describe':
        print(json.dumps(reference_summary(data,args.split),indent=2))
        return
    if args.command in ('review-export', 'review-apply'):
        if not args.output:
            parser.error('Review commands require --output; the original reference is preserved.')
        if args.command == 'review-export':
            value = review_packet(data, args.split, args.task)
        else:
            if not args.annotations:
                parser.error('review-apply requires --annotations.')
            value = apply_reviews(data, json.loads(args.annotations.read_text(encoding='utf-8')))
        write_json(args.output, value)
        print('Saved ' + str(args.output))
        return
    if args.command=='score':
        if not args.predictions:
            parser.error('score requires --predictions.')
        report=json.loads(args.predictions.read_text(encoding='utf-8'))
    else:
        from xfed.llm import OllamaPool,load_client
        if args.config:
            if args.model or args.worker_models or args.workers != 1:
                parser.error('Use --config or explicit model selection.')
            client=load_client(args.config)
        else:
            names=args.worker_models
            model=args.model or (names[0] if names else None)
            if not model:
                parser.error('run requires --model, --worker-models, or --config.')
            client=OllamaPool([args.base_url],model,worker_models=names,workers_per_endpoint=args.workers,
                              num_predict=4096,timeout=600)
        if args.require_reviewed:
            chosen=[case for name in ('classification','policy') if args.task in (name,'both') for case in data[name]
                    if case['split']==args.split]
            if any(case['gold']['review_status']!='reviewed' for case in chosen):
                parser.error('Selected annotations are not marked reviewed.')
        try:
            report=run_evaluation(data,client,root,args.task,split=args.split,policy_mode=args.policy_mode,
                                  reference_path=args.reference,
                                  progress=lambda done,total,message:print(f'{message}: {done}/{total}',flush=True),
                                  completed=(lambda result:write_json(args.output,result)) if args.output else None)
        finally:
            client.close()
    report=score_report(data,report,root,args.require_reviewed)
    if args.output:
        write_json(args.output,report)
    print(json.dumps(dict(reference_status=report['reference_status'],metrics=report['metrics']),indent=2))
    if any(value.get('errors', value.get('failures', 0)) for task in report['metrics'].values() for value in task.values()):
        parser.exit(1,'Some predictions failed or are missing; they remain in the evaluation denominators.\n')


if __name__=='__main__':
    main()
