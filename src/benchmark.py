"""Compare same-model relevance settings through Ollama's HTTP API, without replacing outputs."""

import argparse
from collections import Counter
import json
from pathlib import Path
from time import perf_counter

from xfed.common import map_chunks, validate, write_json
from xfed.llm import OllamaClient, OllamaPool
from xfed.relevance import classification_batches, classify_batch, request_bytes


VARIANTS = ('baseline', 'batch4', 'compact', 'context', 'output', 'batch8', 'batch16', 'prefill512', 'prefill1024', 'prefill2048',
            'prefill4096', 'thinking_off')
DEFAULT_VARIANTS = ('baseline', 'batch4', 'compact', 'context', 'output', 'batch8', 'prefill4096')


def settings_for(name, num_ctx):
    settings = dict(relevance_compact=False, relevance_num_ctx=num_ctx,
                    relevance_num_predict=512, relevance_batch_size=1 if name == 'baseline' else 4)
    if name not in ('baseline', 'batch4'):
        settings['relevance_compact'] = True
    if name not in ('baseline', 'batch4', 'compact'):
        settings['relevance_num_ctx'] = min(8192, num_ctx)
    if name not in ('baseline', 'batch4', 'compact', 'context'):
        settings['relevance_num_predict'] = 128
    if name in ('batch8', 'thinking_off') or name.startswith('prefill'):
        settings['relevance_batch_size'] = 8
    if name == 'batch16':
        settings['relevance_batch_size'] = 16
    if name.startswith('prefill'):
        settings['relevance_num_batch'] = int(name.removeprefix('prefill'))
    if name == 'thinking_off':
        settings['relevance_think'] = False
    # Classification output limits now apply to the whole request, independently of policy output.
    settings['relevance_num_predict'] *= settings['relevance_batch_size']
    return settings


def selected_chunks(document, limit, root):
    chunks = document['chunks']
    if limit >= len(chunks):
        return {chunk['chunk_id'] for chunk in chunks}
    # Spread the sample over the contract, including clauses with the most supporting evidence.
    baseline = OllamaClient('http://127.0.0.1:11434', 'sampling', num_predict=4096,
                            relevance_batch_size=1, relevance_compact=False, relevance_num_ctx=32768)
    groups = classification_batches(document, baseline, root)
    longest = sorted(groups, key=lambda group: request_bytes(group['items'], baseline, root), reverse=True)
    chosen = {group['chunk_id'] for group in longest[:min(2, limit)]}
    for index in range(limit):
        if len(chosen) == limit:
            break
        chosen.add(chunks[round(index * (len(chunks) - 1) / max(1, limit - 1))]['chunk_id'])
    return chosen


def run_variant(document, chunk_ids, client, root, progress=None):
    groups = classification_batches(document, client, root, chunk_ids)
    if not groups:
        raise ValueError('No clauses selected for benchmarking.')
    # Warm exactly the allocation being measured, then exclude that request from the timing/counters.
    first = groups[0]
    with client.for_chunk(first['chunk_ids']) as worker:
        classify_batch(first['items'], worker, root, first['num_ctx'])
    client.reset_assignments()
    started = perf_counter()
    decisions = map_chunks(groups, lambda group, worker:
                           classify_batch(group['items'], worker, root, group['num_ctx']), client, progress)
    elapsed = perf_counter() - started
    labels = {item['clause']['chunk_id']: decision['label']
              for group, values in zip(groups, decisions) for item, decision in zip(group['items'], values)}
    metadata = client.metadata()
    performance = metadata['models'][0]['performance']
    if not performance['responses']:
        raise ValueError('No API classifications completed; check the input/context budgets before measuring speed.')
    budget_skips = sum('safe input budget' in value.get('reason', '').lower()
                       for values in decisions for value in values)
    return dict(duration_seconds=elapsed, clauses=len(labels), planned_requests=len(groups),
                input_bytes=sum(request_bytes(group['items'], client, root) for group in groups),
                planned_context_tokens=sorted({group['num_ctx'] for group in groups}),
                clauses_per_second=round(len(labels) / elapsed, 3),
                output_tokens_per_second=round(performance['output_tokens'] / elapsed, 3),
                budget_skips=budget_skips, label_counts=dict(Counter(labels.values())), labels=labels, options=metadata)


def compare_settings(document, base_url, model, root=Path('.'), limit=24, num_ctx=32768, timeout=600,
                     variants=DEFAULT_VARIANTS, progress=None, completed=None, cancel=None, think=None):
    chunk_ids = selected_chunks(document, limit or len(document['chunks']), root)
    report = dict(contract=document['contract_id'], model=model, sampled_chunk_ids=sorted(chunk_ids), runs=[])
    reference, digest = None, None
    def checkpoint(done, total, message):
        if cancel is not None and cancel.is_set():
            raise InterruptedError('Benchmark cancelled.')
        if progress:
            progress(done, total, message)
    for name in dict.fromkeys(['baseline', *variants]):
        if name not in VARIANTS:
            raise ValueError(f'Unknown benchmark variant: {name}')
        checkpoint(0, 0, name + ': warming model')
        settings = settings_for(name, num_ctx)
        client = OllamaPool([base_url], model, num_ctx=num_ctx, num_predict=min(4096, num_ctx - 1), think=think,
                            timeout=timeout, workers_per_endpoint=1, **settings)
        try:
            client.preflight()
            if digest is not None and client.model_digest != digest:
                raise ValueError('Model digest changed during the comparison; results are not comparable.')
            digest = client.model_digest
            result = run_variant(document, chunk_ids, client, root,
                                 lambda done, total, message: checkpoint(done, total, f'{name}: {done}/{total} requests'))
            result.update(name=name, settings=settings, status='complete')
            if name == 'baseline':
                reference = result
            if reference:
                changed = [identifier for identifier in reference['labels']
                           if result['labels'].get(identifier) != reference['labels'][identifier]]
                result.update(changed_chunk_ids=changed, label_agreement=1 - len(changed) / len(chunk_ids),
                              speedup=round(reference['duration_seconds'] / result['duration_seconds'], 3))
        except InterruptedError:
            raise
        except Exception as error:
            if cancel is not None and cancel.is_set():
                raise InterruptedError('Benchmark cancelled.') from error
            result = dict(name=name, settings=settings, status='failed', error=str(error))
        report['runs'].append(result)
        report['model_digest'] = digest
        if completed:
            completed(result, report)
        if name == 'baseline' and result['status'] == 'failed':
            break
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('contract', choices=['contract_01', 'contract_02', 'contract_03', 'contract_04'])
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--base-url', default='http://127.0.0.1:11434')
    parser.add_argument('--model', required=True, help='One installed model, used for every comparison')
    parser.add_argument('--limit', type=int, default=24, help='Spread sample size; use 0 for the full contract')
    parser.add_argument('--num-ctx', type=int, default=32768)
    parser.add_argument('--timeout', type=int, default=600)
    parser.add_argument('--variants', nargs='+', choices=VARIANTS,
                        default=list(DEFAULT_VARIANTS))
    parser.add_argument('--output', type=Path, help='Optional comparison report; never a pipeline output path')
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error('--limit must be nonnegative.')
    root = args.root.resolve()
    base = root / 'contracts' / args.contract
    if args.output and args.output.resolve().is_relative_to((root / 'contracts').resolve()):
        parser.error('Save benchmark reports outside contracts/ to preserve pipeline files.')
    source = base / 'output/1 preprocessing/1.2 segmentation/contract_chunks.json'
    if source.is_file():
        document = json.loads(source.read_text(encoding='utf-8'))
        validate(document, 2, root)
    else:
        from xfed.extraction import extract_document
        from xfed.segmentation import segment
        document = segment(extract_document(base / 'input/contract.pdf', args.contract, root), root)
    def completed(result, report):
        if result['status'] == 'complete':
            stats = result['options']['models'][0]['performance']
            print(f"{result['name']}: {result['duration_seconds']:.3f}s; {stats['responses']} API responses; "
                  f"{stats['output_tokens']:.0f} output tokens; "
                  f"{result.get('label_agreement', 0):.1%} labels match baseline", flush=True)
        else:
            print(f"{result['name']} FAILED: {result['error']}", flush=True)
        if args.output:
            write_json(args.output, report)
    report = compare_settings(document, args.base_url, args.model, root, args.limit, args.num_ctx,
                              args.timeout, args.variants,
                              progress=lambda done, total, message: print(message, flush=True),
                              completed=completed)
    if any(result['status'] == 'failed' for result in report['runs']):
        parser.exit(1, 'Some comparisons failed; inspect their errors before selecting settings.\n')


if __name__ == '__main__':
    main()
