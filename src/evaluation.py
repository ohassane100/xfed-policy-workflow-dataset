"""Evaluate task 1.3 classification against frozen source labels."""
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
from time import perf_counter
from xfed.common import map_chunks, write_json
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
    if gold.get('label') not in LABELS:
        raise ValueError('Unknown reference classification label.')
    return

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
    if any((len(splits) != 1 for splits in source_splits.values())):
        raise ValueError('The same source PDF cannot appear in both splits.')
    for task in ('classification',):
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
            for key in ('context',):
                if any((identifier not in data['contexts'] for identifier in value[key])):
                    raise ValueError('Reference context is missing.')
            validate_gold(task, case['gold'])
    for contract, source in data['sources'].items():
        if source.get('sampling') != 'full_document':
            continue
        inputs = {key: value for key, value in data['inputs'].items() if value['contract_id'] == contract}
        clauses = sorted((value['clause'] for value in inputs.values()), key=lambda clause: clause['chunk_id'])
        if len(clauses) != source['segmented_chunks'] or len({clause['chunk_id'] for clause in clauses}) != len(clauses) or digest(dict(contract_id=contract, chunks=clauses)) != source['segmentation_content_sha256']:
            raise ValueError('Full-document reference must retain every frozen source chunk unchanged.')
        for task in ('classification',):
            selected = [case['input_id'] for case in data[task] if case['input_id'] in inputs]
            if len(selected) != len(inputs) or set(selected) != set(inputs):
                raise ValueError('Classification must cover every full-document source chunk exactly once.')
    return data

def reference_summary(data, split='test'):
    if split not in ('dev', 'test'):
        raise ValueError('Choose dev or test.')
    selected = {task: [case for case in data[task] if case['split'] == split] for task in ('classification',)}
    return dict(version=data['version'], status=data['status'], split=split, sources={contract: deepcopy(source) for contract, source in data['sources'].items() if source['split'] == split}, counts={task: dict(total=len(cases)) for task, cases in selected.items()}, classification_labels=dict(Counter((case['gold']['label'] for case in selected['classification']))), reviewed={task: sum((case['gold']['review_status'] == 'reviewed' for case in cases)) for task, cases in selected.items()}, reference_sha256=digest(data), test_exposure=data['test_exposure'] if split == 'test' else None, limitations=data.get('limitations', []))

def review_packet(data, split='test', task='classification'):
    if task != 'classification':
        raise ValueError('Only classification review packets are supported.')
    return dict(reference_sha256=digest(data), split=split,
        cases=[dict(task='classification', case_id=case['case_id'], input=input_for(data, case),
                    gold=dict(label=None, review_status='draft', reviewer='', rationale=''))
               for case in data['classification'] if case['split'] == split],
        instructions='Read source evidence before predictions. Record a label, reviewer and rationale; leave uncertain entries draft.')


def apply_reviews(data, packet):
    """Apply explicitly reviewed annotations to a new reference, preserving inputs."""
    if packet.get('reference_sha256') != digest(data):
        raise ValueError('Review packet was created from a different reference.')
    if packet.get('split') not in ('dev', 'test'):
        raise ValueError('Review packet must declare dev or test.')
    result, seen, completed = (deepcopy(data), set(), 0)
    cases = {(task, case['case_id']): case for task in ('classification',) for case in result[task]}
    for entry in packet['cases']:
        key = (entry['task'], entry['case_id'])
        if key in seen or key not in cases or cases[key]['split'] != packet['split']:
            raise ValueError('Review packet has duplicate, unknown or wrong-split cases.')
        seen.add(key)
        if digest(entry['input']) != digest(input_for(data, cases[key])):
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
    paths = [root / file for file in ('src/evaluation.py', 'src/llm/__init__.py', 'src/llm/service.py', 'src/common/__init__.py', 'src/1 preprocessing/1.1 extraction/__init__.py', 'src/1 preprocessing/1.2 segmentation/__init__.py', 'src/1 preprocessing/1.3 relevance/__init__.py', 'src/1 preprocessing/1.4 enrichment/__init__.py', 'src/2 policy_generation/__init__.py', 'src/3 policy_review/__init__.py')]
    paths += sorted((root / 'prompts').glob('*.md')) + sorted((root / 'schemas').glob('*.json'))
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}

def input_for(data, case):
    source = data['inputs'][case['input_id']]
    return dict(clause=deepcopy(source['clause']),
                context=[deepcopy(data['contexts'][identifier]) for identifier in source['context']],
                unresolved_references=list(source['unresolved_references']))


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None

def prf(tp, fp, fn):
    return dict(precision=ratio(tp, tp + fp), recall=ratio(tp, tp + fn), f1=ratio(2 * tp, 2 * tp + fp + fn), tp=tp, fp=fp, fn=fn)

def wilson(correct, total):
    if not total:
        return None
    z = 1.959963984540054
    p, scale = (correct / total, 1 + z * z / total)
    centre = (p + z * z / (2 * total)) / scale
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / scale
    return [max(0, centre - half), min(1, centre + half)]

def classification_metrics(rows, macro_labels=None):
    matrix = {label: dict.fromkeys([*LABELS, 'ERROR'], 0) for label in LABELS}
    for row in rows:
        predicted = row.get('label') if not row.get('error') and row.get('label') in LABELS else None
        matrix[row['gold']][predicted if predicted in LABELS else 'ERROR'] += 1
    counts = {}
    for label in LABELS:
        tp = matrix[label][label]
        support = sum(matrix[label].values())
        predicted = sum((matrix[actual][label] for actual in LABELS))
        counts[label] = dict(prf(tp, predicted - tp, support - tp), support=support, predicted=predicted, precision_wilson_ci95=wilson(tp, predicted), recall_wilson_ci95=wilson(tp, support))
    total = len(rows)
    correct = sum((matrix[label][label] for label in LABELS))
    macro_labels = list(macro_labels) if macro_labels is not None else [label for label in LABELS if counts[label]['support']]
    forwarded = [row for row in rows if not row.get('error') and row.get('label') in ('RELEVANT', 'NEEDS_CONTEXT')]
    positive = counts['RELEVANT']['support']
    return dict(n=total, accuracy=ratio(correct, total), accuracy_wilson_ci95=wilson(correct, total), majority_baseline_accuracy=ratio(max((sum(matrix[label].values()) for label in LABELS), default=0), total), routing=dict(forwarded=len(forwarded), coverage=ratio(len(forwarded), total), relevant_recall=ratio(sum((row['gold'] == 'RELEVANT' for row in forwarded)), positive), missed_relevant=sum((row['gold'] == 'RELEVANT' for row in rows)) - sum((row['gold'] == 'RELEVANT' for row in forwarded))), macro_labels=macro_labels, macro_precision=ratio(sum((counts[label]['precision'] or 0 for label in macro_labels)), len(macro_labels)), macro_recall=ratio(sum((counts[label]['recall'] or 0 for label in macro_labels)), len(macro_labels)), macro_f1=ratio(sum((counts[label]['f1'] or 0 for label in macro_labels)), len(macro_labels)), weighted_f1=ratio(sum((value['support'] * (value['f1'] or 0) for value in counts.values())), total), balanced_accuracy=ratio(sum((value['recall'] or 0 for value in counts.values())), sum((value['support'] > 0 for value in counts.values()))), per_class=counts, confusion_matrix=matrix, errors=sum((matrix[label]['ERROR'] for label in LABELS)), low_support_labels=[label for label, value in counts.items() if value['support'] < 30])

def cluster_interval(rows, operation, key, repetitions=500):
    if len(rows) < 2:
        return None
    groups = defaultdict(lambda: defaultdict(list))
    for row in rows:
        groups[row['contract']][row['group_id']].append(row)
    if sum((len(values) for values in groups.values())) < 2:
        return None
    rng, values = (random.Random(20261021), [])
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
    return [values[int((len(values) - 1) * 0.025)], values[int((len(values) - 1) * 0.975)]] if len(values) >= repetitions * 0.95 else None

def score_report(data, report, root=Path('.'), require_reviewed=False):
    if report.get('reference_version') != data['version']:
        raise ValueError('Reference version changed; rerun the benchmark instead of rescoring an older selection.')
    if report.get('reference_sha256') != digest(data):
        raise ValueError('Reference content or annotations changed; rerun against the frozen reference.')
    if report.get('split') not in ('dev', 'test'):
        raise ValueError('Reports must declare dev or test.')
    if report.get('task') != 'classification':
        raise ValueError('Only classification reports can be scored.')
    output = deepcopy(report)
    metrics, mismatches = ({}, [])
    for task, operation in [('classification', classification_metrics)]:
        predictions = report.get('predictions', {}).get(task, {})
        selected = report.get('selected_cases', {}).get(task, [])
        expected = {case['case_id'] for case in data[task] if report['task'] == task and case['split'] == report['split']}
        if set(selected) != expected:
            raise ValueError('The manifest must cover the entire declared reference selection; missing predictions are counted as errors.')
        if not selected:
            continue
        cases = {case['case_id']: case for case in data[task]}
        if len(set(selected)) != len(selected) or any((identifier not in cases for identifier in selected)):
            raise ValueError('The evaluation manifest needs unique, known case IDs.')
        if any((identifier not in selected for identifier in predictions)):
            raise ValueError('Predictions include unknown reference IDs.')
        cohorts = defaultdict(list)
        for identifier in selected:
            case = cases[identifier]
            expected_hash = digest(input_for(data, case))
            prediction = predictions.get(identifier, dict(error='Missing prediction', input_sha256=expected_hash))
            if prediction.get('input_sha256') != expected_hash:
                raise ValueError('Evaluation inputs changed; predictions cannot be reused against different evidence.')
            if require_reviewed and case['gold']['review_status'] != 'reviewed':
                raise ValueError('Selected reference annotations are not marked reviewed.')
            info = dict(case_id=identifier, contract=data['inputs'][case['input_id']]['contract_id'], group_id=case['group_id'], reviewed=case['gold']['review_status'] == 'reviewed')
            info.update(gold=case['gold']['label'], label=prediction.get('label'), error=prediction.get('error'))
            correct = not info['error'] and info['gold'] == info['label']
            if not correct:
                mismatches.append(dict(task=task, case_id=identifier, expected=case['gold'], predicted=prediction))
            cohorts[case['source_type'] + '/' + case['cohort']].append(info)
        metrics[task] = {}
        for name, rows in cohorts.items():
            labels = [label for label in LABELS if any((row['gold'] == label for row in rows))]
            operation = lambda sample, labels=labels: classification_metrics(sample, labels)
            result = operation(rows)
            interval_key = 'macro_f1'
            result[interval_key + '_page_cluster_ci95'] = cluster_interval(rows, operation, interval_key)
            result['reviewed'] = sum((row['reviewed'] for row in rows))
            result['aggregate'] = False
            result['by_contract'] = {contract: operation([row for row in rows if row['contract'] == contract]) for contract in sorted({row['contract'] for row in rows})}
            contract_scores = [value['macro_f1'] for value in result['by_contract'].values() ]
            result['contract_macro_f1'] = ratio(sum((value or 0 for value in contract_scores)), len(contract_scores))
            metrics[task][name] = result
    if not metrics:
        raise ValueError('No evaluation cases were selected in the prediction manifest.')
    output.update(metrics=metrics, mismatches=mismatches, reference_sha256=digest(data), test_exposure=data['test_exposure'] if report['split'] == 'test' else None, scorer_sha256=hashlib.sha256((Path(root) / 'src/evaluation.py').read_bytes()).hexdigest(), incomplete_tasks=[task for task in report['selected_cases'] if task not in report.get('execution', {})], reference_status='reviewed' if all((value['reviewed'] == value['n'] for task in metrics.values() for value in task.values())) else 'provisional', metric_notes=data.get('limitations', []), interval_note='Wilson intervals assume independent cases. Page-cluster bootstrap intervals resample pages within each fixed source contract (500 replicates); neither interval establishes generalisation to new contracts.')
    output['evaluation_protocol'] = 'classification_v1'
    if report['split'] == 'test' and 'contract_05' in data['sources']:
        output['metric_notes'].append('Contract 05 results have been inspected during development. Use this exposed agreement diagnostically; reserve new independently annotated contracts for final validation.')
    return output

def run_evaluation(data, client, root=Path('.'), task='classification', split='test', progress=None, cancel=None, completed=None, reference_path=None):
    from xfed.llm import OllamaConnectionError
    from xfed.relevance import classify_batch, fits
    if task not in ('classification',):
        raise ValueError('Only classification evaluation is supported.')
    if split not in ('dev', 'test'):
        raise ValueError('Choose dev or test.')
    selected = {name: [case for case in data[name] if case['split'] == split] for name in ('classification',) if task == name}
    if not any(selected.values()):
        raise ValueError('No reference cases match this selection.')

    def checkpoint(done=0, total=0, message=''):
        if cancel is not None and cancel.is_set():
            raise InterruptedError('Evaluation cancelled.')
        if progress and message:
            progress(done, total, message)
    report = dict(status='running', run_id=str(uuid4()), started_at=datetime.now(timezone.utc).isoformat(), reference_sha256=digest(data), split=split, source_documents={cid: deepcopy(value) for cid, value in data['sources'].items() if value['split'] == split}, inference_sha256=inference_files(root), reference_version=data['version'], benchmark='real-contracts', source='real', task=task, predictions={}, execution={}, evaluation_scope='Task 1.3 classification with frozen source context; PDF extraction and context retrieval are not evaluated.', selected_cases={name: [case['case_id'] for case in cases] for name, cases in selected.items() if cases}, case_manifest={name: {case['case_id']: dict(gold=deepcopy(case['gold']), input=input_for(data, case)) for case in cases} for name, cases in selected.items() if cases}, code_sha256={str(path): hashlib.sha256((Path(root) / path).read_bytes()).hexdigest() for path in ['src/evaluation.py', 'src/llm/__init__.py', 'src/llm/service.py', 'src/1 preprocessing/1.3 relevance/__init__.py',]}, prompts={name: hashlib.sha256((Path(root) / 'prompts' / file).read_bytes()).hexdigest() for name, file in [('classification', 'relevance_classifier.md')]})
    live_rows = defaultdict(list)
    report['live_metrics_scope'] = 'Completed cases only; provisional while inference runs. '
    report['progress'] = {name: dict(completed=0, total=len(cases)) for name, cases in selected.items()}
    report['live_metrics'] = {}

    def record(name, case, item, prediction):
        prediction = dict(prediction, input_sha256=digest(item))
        report['predictions'][name][case['case_id']] = prediction
        row = dict(gold=case['gold']['label'], label=prediction.get('label'), error=prediction.get('error'))
        operation = classification_metrics
        live_rows[name].append(row)
        report['progress'][name]['completed'] = len(live_rows[name])
        report['live_metrics'][name] = {'completed': dict(operation(live_rows[name]), aggregate=False)}

    def save_checkpoint():
        if completed:
            completed(deepcopy(report))

    def connection_stopped(error):
        report.update(status='failed', error=str(error), infrastructure_error=True, finished_at=datetime.now(timezone.utc).isoformat())
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
        checkpoint(message='Starting ' + name + ' evaluation')
        client.reset_assignments()
        report['predictions'][name] = {}
        started = perf_counter()
        num_ctx = client.relevance_num_ctx or client.num_ctx
        prepared = [(case, input_for(data, case)) for case in cases]
        groups, current, last = ([], [], None)
        for case, item in prepared:
            boundary = (data['inputs'][case['input_id']]['contract_id'], case['cohort'], case.get('variant'))
            if current and (boundary != last or len(current) >= client.relevance_batch_size or item['clause']['chunk_id'] in {entry[1]['clause']['chunk_id'] for entry in current} or (not fits([entry[1] for entry in current] + [item], client, root, num_ctx))):
                groups.append(current)
                current = []
            current.append((case, item))
            last = boundary
        if current:
            groups.append(current)
        work = [dict(chunk_id=group[0][0]['case_id'], chunk_ids=[case['case_id'] for case, _ in group], group=group) for group in groups]

        def classify(group, worker):
            checkpoint()
            try:
                values = classify_batch([item for _, item in group['group']], worker, root, num_ctx)
                for value in values:
                    if any((text in value.get('reason', '').lower() for text in ('safe input budget', 'context limit reached'))):
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
        run_work(work, classify, lambda done, total, message: checkpoint(done, total, 'Classification requests'), classification_completed)
        if inference_files(root) != report['inference_sha256'] or digest(load_reference(root, reference_path)) != report['reference_sha256']:
            raise ValueError('Code, prompts, schemas or references changed during evaluation; restart with frozen inputs.')
        report['execution'][name] = dict(duration_seconds=perf_counter() - started, options=client.metadata())
        if completed:
            completed(report)
    report['status'] = 'complete'
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    checkpoint(message='Scoring frozen references')
    return score_report(data, report, root)

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['describe', 'run', 'score', 'review-export', 'review-apply'])
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--split', choices=['dev', 'test'], default='test', help='Contract 05 by default; use dev for previously inspected Contracts 01-03.')
    parser.add_argument('--task', choices=['classification'], default='classification')
    parser.add_argument('--model')
    parser.add_argument('--worker-models', nargs='+')
    parser.add_argument('--workers', type=int, default=1, help='Concurrent requests to the existing endpoint; does not start model services.')
    parser.add_argument('--base-url', default='http://127.0.0.1:11434')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--predictions', type=Path)
    parser.add_argument('--reference', type=Path, help='Use a separately reviewed frozen reference.')
    parser.add_argument('--annotations', type=Path, help='Completed blind annotation packet for review-apply.')
    parser.add_argument('--require-reviewed', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    data = load_reference(root, args.reference)
    if args.output and (args.output.resolve().is_relative_to(root / 'contracts') or args.output.resolve().is_relative_to(root / 'evaluation')):
        parser.error('Save run reports outside contracts/ and evaluation/ to preserve source outputs and references.')
    if args.output and any((args.output.resolve() == path.resolve() for path in (args.reference, args.annotations) if path)):
        parser.error('Output must not overwrite the reference or annotation input.')
    if args.command == 'describe':
        print(json.dumps(reference_summary(data, args.split), indent=2))
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
    if args.command == 'score':
        if not args.predictions:
            parser.error('score requires --predictions.')
        report = json.loads(args.predictions.read_text(encoding='utf-8'))
    else:
        from xfed.llm import OllamaPool, load_client
        if args.config:
            if args.model or args.worker_models or args.workers != 1:
                parser.error('Use --config or explicit model selection.')
            client = load_client(args.config)
        else:
            names = args.worker_models
            model = args.model or (names[0] if names else None)
            if not model:
                parser.error('run requires --model, --worker-models, or --config.')
            client = OllamaPool([args.base_url], model, worker_models=names, workers_per_endpoint=args.workers, num_predict=4096, timeout=600)
        if args.require_reviewed:
            chosen = [case for name in ('classification',) if args.task == name for case in data[name] if case['split'] == args.split]
            if any((case['gold']['review_status'] != 'reviewed' for case in chosen)):
                parser.error('Selected annotations are not marked reviewed.')
        try:
            report = run_evaluation(data, client, root, args.task, split=args.split, reference_path=args.reference, progress=lambda done, total, message: print(f'{message}: {done}/{total}', flush=True), completed=(lambda result: write_json(args.output, result)) if args.output else None)
        finally:
            client.close()
    report = score_report(data, report, root, args.require_reviewed)
    if args.output:
        write_json(args.output, report)
    print(json.dumps(dict(reference_status=report['reference_status'], metrics=report['metrics']), indent=2))
    if any((value.get('errors', value.get('failures', 0)) for task in report['metrics'].values() for value in task.values())):
        parser.exit(1, 'Some predictions failed or are missing; they remain in the evaluation denominators.\n')
if __name__ == '__main__':
    main()
