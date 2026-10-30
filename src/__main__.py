import argparse
from pathlib import Path

from xfed.common import write_output, write_json
from xfed.extraction import extract_document
from xfed.segmentation import segment
from xfed.relevance import classify
from xfed.enrichment import enrich
from xfed.llm import load_client
from xfed.policy_generation import generate
from xfed import authoring
# Generation uses a clause evidence check; the separate review stage is disabled.


def main(argv=None):
    parser = argparse.ArgumentParser(description='XFed preprocessing, policy generation and evidence review')
    parser.add_argument('contract', help='Existing contract directory name, or all')
    parser.add_argument('--through', choices=['1.1', '1.2', '1.3', '1.4', '2'], default='1.2')
    parser.add_argument('--root', type=Path, default=Path.cwd(), help='Repository root (default: current directory)')
    parser.add_argument('--config', type=Path, help='Model YAML (default: ROOT/config/models.yaml)')
    args = parser.parse_args(argv)
    available = sorted(p.name for p in (args.root / 'contracts').iterdir()
                       if p.is_dir() and (p / 'input/contract.pdf').is_file())
    if args.contract != 'all' and args.contract not in available:
        parser.error('Choose an existing contract directory name, or all.')
    client = None
    try:
        if args.through in ('1.3', '1.4', '2'):
            client = load_client(args.config or args.root / 'config/models.yaml')
            client.preflight()
        contracts = available if args.contract == 'all' else [args.contract]
        for contract in contracts:
            base = args.root / 'contracts' / contract
            output = base / 'output'
            stages = {
                'document_blocks.json': '1 preprocessing/1.1 extraction',
                'contract_chunks.json': '1 preprocessing/1.2 segmentation',
                'classified_chunks.json': '1 preprocessing/1.3 relevance',
                'enriched_relevance.json': '1 preprocessing/1.4 enrichment',
                'policy.json': '2 policy_generation',
                'policy_review.json': '3 policy_review',
            }
            paths = {name: output / folder / name for name, folder in stages.items()}
            for folder in (*stages.values(), '2 policy_generation', '3 policy_review'):
                (output / folder).mkdir(parents=True, exist_ok=True)
            print(f'{contract}: extraction', flush=True)
            document = extract_document(base / 'input/contract.pdf', contract, args.root)
            # Downstream files from earlier runs must not appear current after a partial rerun.
            for name in ('contract_chunks.json', 'classified_chunks.json', 'enriched_relevance.json', 'policy.json', 'policy_review.json'):
                paths[name].unlink(missing_ok=True)
                (output / name).unlink(missing_ok=True)
            (output / '2 policy_generation/generation_report.json').unlink(missing_ok=True)
            (output / '.runs.json').unlink(missing_ok=True)
            (output / 'guided_evaluation.json').unlink(missing_ok=True)
            write_output(paths['document_blocks.json'], document, 1, args.root)
            if args.through == '1.1':
                continue
            chunks = segment(document, args.root)
            write_output(paths['contract_chunks.json'], chunks, 2, args.root)
            print(f"{contract}: {len(document['blocks'])} blocks, {len(chunks['chunks'])} chunks", flush=True)
            if args.through == '1.2':
                continue
            classified = classify(chunks, client, args.root, blocks=document['blocks'])
            write_output(paths['classified_chunks.json'], classified, 3, args.root)
            if args.through == '1.3':
                continue
            classified, enriched = enrich(classified, args.root, blocks=document['blocks'])
            write_output(paths['classified_chunks.json'], classified, 3, args.root)
            write_output(paths['enriched_relevance.json'], enriched, 4, args.root)
            pending = sum(c['classification']['label'] == 'NEEDS_CONTEXT' for c in classified['chunks'])
            print(f"{contract}: {len(enriched['items'])} relevant, {pending} still need context", flush=True)
            if args.through == '2':
                policy, report = generate(enriched, client, args.root, classified=classified)
                report['unresolved_chunks'] = pending
                write_json(output / '2 policy_generation/generation_report.json', report)
                if policy is not None:
                    write_output(paths['policy.json'], policy, 5, args.root)
                if (base / 'authoring.json').is_file():
                    write_json(output / 'guided_evaluation.json', authoring.evaluate(base, policy, classified['chunks'], args.root))
                print(f"{contract}: {report['status']}", flush=True)
                if report['status'] == 'generation_rejected':
                    raise ValueError('No policy generated. Inspect rejected clauses in generation_report.json.')
    except Exception as error:
        parser.exit(1, f'XFed: {error}\n')
    finally:
        if client:
            client.close()


if __name__ == '__main__':
    main()
