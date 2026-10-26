"""Run and score frozen classification and policy references without changing pipeline outputs."""

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4
import hashlib
import json
import math
from pathlib import Path
import random
import re
from time import perf_counter
import unicodedata

from xfed.common import map_chunks, write_json


LABELS = ('RELEVANT', 'NOT_RELEVANT', 'NEEDS_CONTEXT')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load_reference(root=Path('.')):
    data = json.loads((Path(root) / 'evaluation/reference.json').read_text(encoding='utf-8'))
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
            gold = case['gold']
            if gold.get('review_status') not in ('draft', 'reviewed'):
                raise ValueError('Use draft or reviewed annotation status.')
            if gold['review_status'] == 'reviewed' and (not isinstance(gold.get('reviewer'), str) or not gold['reviewer'].strip()):
                raise ValueError('Reviewed annotations need the actual independent reviewer name.')
            if task == 'classification' and gold['label'] not in LABELS:
                raise ValueError('Unknown reference classification label.')
            if task == 'policy':
                for option in [gold['rules'], *gold.get('alternatives', [])]:
                    for rule in option:
                        if rule['kind'] not in ('permission', 'prohibition', 'obligation', 'duty'):
                            raise ValueError('Unknown reference rule type.')
                        if not all(isinstance(rule.get(key),str) and rule[key] for key in ('action', 'target', 'assignee')):
                            raise ValueError('Reference rules need action, target, and assignee strings.')
                        if any(not all(key in value for key in ('leftOperand','operator','rightOperand')) for value in rule.get('constraint', [])):
                            raise ValueError('Reference constraints need leftOperand, operator and rightOperand.')
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
    return dict(n=total, accuracy=ratio(correct, total), accuracy_wilson_ci95=wilson(correct, total),
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
    constraints = []
    for entry in rule.get('constraint', []):
        part = {key: value(item) for key, item in entry.items() if key != 'uid'}
        if part.get('operator') in ('isanyof','isallof','isnoneof') and isinstance(part.get('rightOperand'),list):
            part['rightOperand'] = sorted(part['rightOperand'],key=lambda item:json.dumps(item,sort_keys=True))
        duration_unit = normal(entry.get('unit', ''))
        if isinstance(entry.get('rightOperand'), (int, float)) and duration_unit in ('hour', 'hours', 'day', 'days'):
            number = f"{entry['rightOperand']:g}"
            part['rightOperand'] = normal(('PT' + number + 'H') if duration_unit.startswith('hour') else ('P' + number + 'D'))
            part.pop('unit', None)
        constraints.append(json.dumps(part, sort_keys=True))
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
    return dict(n=len(rows), canonical_rules=prf(totals['tp'], totals['fp'], totals['fn']),
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
                raise ValueError('Independent review is required for all selected reference annotations.')
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
                                       **(dict(rule_differences=info['rule_differences']) if task == 'policy' else {})))
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
            metrics[task][name] = result
    if not metrics:
        raise ValueError('No evaluation cases were selected in the prediction manifest.')
    output.update(metrics=metrics, mismatches=mismatches, reference_sha256=digest(data),
                  test_exposure=data['test_exposure'] if report['split'] == 'test' else None,
                  scorer_sha256=hashlib.sha256((Path(root) / 'src/evaluation.py').read_bytes()).hexdigest(),
                  incomplete_tasks=[task for task in report['selected_cases'] if task not in report.get('execution', {})],
                  reference_status='reviewed' if all(value['reviewed'] == value['n'] for task in metrics.values() for value in task.values()) else 'provisional',
                  metric_notes=data.get('limitations', []), interval_note='Wilson intervals assume independent cases. Page-cluster bootstrap intervals resample pages within each fixed source contract (500 replicates); neither interval establishes generalisation to new contracts.')
    return output


def run_evaluation(data, client, root=Path('.'), task='both', split='test', progress=None, cancel=None, completed=None):
    from xfed.relevance import classify_batch, fits
    from xfed.policy_generation import generate_item, response_schema
    if task not in ('classification', 'policy', 'both'):
        raise ValueError('Choose classification, policy or both.')
    if split not in ('dev', 'test'):
        raise ValueError('Choose dev or test.')
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
                  evaluation_scope='Stage classification and isolated policy extraction with frozen source context; policy scores do not include upstream classification errors.',
                  selected_cases={name:[case['case_id'] for case in cases] for name,cases in selected.items() if cases},
                  case_manifest={name: {case['case_id']: dict(gold=deepcopy(case['gold']),
                                    input=input_for(data, case, name == 'policy')) for case in cases}
                                 for name, cases in selected.items() if cases},
                  code_sha256={str(path):hashlib.sha256((Path(root)/path).read_bytes()).hexdigest() for path in
                               ['src/evaluation.py','src/llm/__init__.py','src/llm/service.py','src/1 preprocessing/1.3 relevance/__init__.py','src/2 policy_generation/__init__.py']},
                  prompts={name: hashlib.sha256((Path(root)/'prompts'/file).read_bytes()).hexdigest()
                           for name, file in [('classification','relevance_classifier.md'),('policy','policy_generator.md')]})
    client.preflight()
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
                except Exception as error:
                    return [dict(error=str(error)) for _ in group['group']]
            results = map_chunks(work,classify,client,lambda done,total,message:checkpoint(done,total,'Classification requests'))
            for group, values in zip(groups,results):
                for (case,item),value in zip(group,values):
                    report['predictions'][name][case['case_id']] = dict(value,input_sha256=digest(item))
        else:
            prompt=(Path(root)/'prompts/policy_generator.md').read_text(encoding='utf-8')
            output_schema=response_schema(root)
            def policy(case,worker):
                checkpoint()
                item=input_for(data,case,True)
                try:
                    fragment,entry,failed=generate_item(item,worker,prompt,data['inputs'][case['input_id']]['contract_id'],output_schema,root)
                    return dict(policy=fragment,generation_report=entry,failed=failed,input_sha256=digest(item))
                except InterruptedError:
                    raise
                except Exception as error:
                    return dict(policy=None,error=str(error),input_sha256=digest(item))
            work=[dict(case,chunk_id=case['case_id']) for case in cases]
            values=map_chunks(work,policy,client,lambda done,total,message:checkpoint(done,total,'Policy cases'))
            report['predictions'][name]=dict(zip([case['case_id'] for case in cases],values))
        if inference_files(root) != report['inference_sha256'] or digest(load_reference(root)) != report['reference_sha256']:
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
    parser.add_argument('command',choices=['describe','run','score'])
    parser.add_argument('--root',type=Path,default=Path.cwd())
    parser.add_argument('--split',choices=['dev','test'],default='test',help='Contract 05 by default; use dev for previously inspected Contracts 01-03.')
    parser.add_argument('--task',choices=['classification','policy','both'],default='both')
    parser.add_argument('--model')
    parser.add_argument('--worker-models',nargs='+')
    parser.add_argument('--base-url',default='http://127.0.0.1:11434')
    parser.add_argument('--config',type=Path)
    parser.add_argument('--predictions',type=Path)
    parser.add_argument('--require-reviewed',action='store_true')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args(argv)
    root=args.root.resolve()
    data=load_reference(root)
    if args.output and (args.output.resolve().is_relative_to(root/'contracts') or args.output.resolve().is_relative_to(root/'evaluation')):
        parser.error('Save run reports outside contracts/ and evaluation/ to preserve source outputs and references.')
    if args.command=='describe':
        print(json.dumps(reference_summary(data,args.split),indent=2))
        return
    if args.command=='score':
        if not args.predictions:
            parser.error('score requires --predictions.')
        report=json.loads(args.predictions.read_text(encoding='utf-8'))
    else:
        from xfed.llm import OllamaPool,load_client
        if args.config:
            if args.model or args.worker_models:
                parser.error('Use --config or explicit model selection.')
            client=load_client(args.config)
        else:
            names=args.worker_models
            model=args.model or (names[0] if names else None)
            if not model:
                parser.error('run requires --model, --worker-models, or --config.')
            client=OllamaPool([args.base_url],model,worker_models=names,num_predict=4096,timeout=600)
        if args.require_reviewed:
            chosen=[case for name in ('classification','policy') if args.task in (name,'both') for case in data[name]
                    if case['split']==args.split]
            if any(case['gold']['review_status']!='reviewed' for case in chosen):
                parser.error('Selected references still need independent review.')
        try:
            report=run_evaluation(data,client,root,args.task,split=args.split,
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
