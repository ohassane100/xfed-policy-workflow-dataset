import argparse
from pathlib import Path

from xfed.common import write_output
from xfed.extraction import extract_document
from xfed.segmentation import segment
from xfed.relevance import classify
from xfed.enrichment import enrich
from xfed.llm import load_client


def main(argv=None):
    parser = argparse.ArgumentParser(description='XFed preprocessing stages 1.1-1.4')
    parser.add_argument('contract', choices=['contract_01', 'contract_02', 'contract_03', 'all'])
    parser.add_argument('--through', choices=['1.1', '1.2', '1.3', '1.4'], default='1.2')
    parser.add_argument('--root', type=Path, default=Path.cwd(), help='Repository root (default: current directory)')
    parser.add_argument('--config', type=Path, help='Model YAML (default: ROOT/config/models.yaml)')
    args = parser.parse_args(argv)
    client = None
    try:
        if args.through in ('1.3', '1.4'):
            client = load_client(args.config or args.root / 'config/models.yaml')
        contracts = ['contract_01', 'contract_02', 'contract_03'] if args.contract == 'all' else [args.contract]
        for contract in contracts:
            base = args.root / 'contracts' / contract
            output = base / 'output'
            stages = {
                'document_blocks.json': '1 preprocessing/1.1 extraction',
                'contract_chunks.json': '1 preprocessing/1.2 segmentation',
                'classified_chunks.json': '1 preprocessing/1.3 relevance',
                'enriched_relevance.json': '1 preprocessing/1.4 enrichment',
            }
            paths = {name: output / folder / name for name, folder in stages.items()}
            for folder in (*stages.values(), '2 policy_generation', '3 policy_review'):
                (output / folder).mkdir(parents=True, exist_ok=True)
            print(f'{contract}: extraction', flush=True)
            document = extract_document(base / 'input/contract.pdf', contract, args.root)
            # Downstream files from earlier runs must not appear current after a partial rerun.
            for name in ('contract_chunks.json', 'classified_chunks.json', 'enriched_relevance.json'):
                paths[name].unlink(missing_ok=True)
                (output / name).unlink(missing_ok=True)
            write_output(paths['document_blocks.json'], document, 1, args.root)
            if args.through == '1.1':
                continue
            chunks = segment(document, args.root)
            write_output(paths['contract_chunks.json'], chunks, 2, args.root)
            print(f"{contract}: {len(document['blocks'])} blocks, {len(chunks['chunks'])} chunks", flush=True)
            if args.through == '1.2':
                continue
            classified = classify(chunks, client, args.root)
            write_output(paths['classified_chunks.json'], classified, 3, args.root)
            if args.through == '1.3':
                continue
            classified, enriched = enrich(classified, client, args.root)
            write_output(paths['classified_chunks.json'], classified, 3, args.root)
            write_output(paths['enriched_relevance.json'], enriched, 4, args.root)
            pending = sum(c['classification']['label'] == 'NEEDS_CONTEXT' for c in classified['chunks'])
            print(f"{contract}: {len(enriched['items'])} relevant, {pending} still need context", flush=True)
    except Exception as error:
        parser.exit(1, f'XFed: {error}\n')


if __name__ == '__main__':
    main()
