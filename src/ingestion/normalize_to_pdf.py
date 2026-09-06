"""Preserve originals; copy PDFs or convert office/HTML files with LibreOffice."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from pypdf import PdfReader
from src.utils.io import digest, read_json, write_json
from src.utils.paths import ROOT


def normalize(contract: Path, converter: str | None = None) -> dict:
    source = contract / 'source'
    meta_path = source / 'metadata.json'
    meta = read_json(meta_path)
    originals = list(source.glob('original.*'))
    if len(originals) != 1:
        raise ValueError('Exactly one preserved original is required')
    original = originals[0]
    if digest(original) != meta['original_sha256']:
        raise ValueError('Original hash mismatch')
    output = source / 'contract.pdf'
    if output.exists():
        if meta.get('pdf_sha256') != digest(output):
            raise ValueError('Existing normalized PDF hash mismatch')
        return meta
    try:
        if original.suffix.lower() == '.pdf':
            PdfReader(original)
            shutil.copyfile(original, output)
            engine = 'original_pdf_byte_copy'
        else:
            executable = converter or os.getenv('LIBREOFFICE_PATH') or shutil.which('soffice')
            if not executable:
                raise RuntimeError('LibreOffice required for DOC/DOCX/HTML: set LIBREOFFICE_PATH')
            with tempfile.TemporaryDirectory(prefix='contract-normalize-') as tmp:
                profile = (Path(tmp)/'profile').as_uri()
                completed = subprocess.run([executable, f'-env:UserInstallation={profile}', '--headless',
                                            '--convert-to', 'pdf', '--outdir', tmp, str(original.resolve())],
                                           capture_output=True, text=True, timeout=120)
                converted = Path(tmp)/(original.stem+'.pdf')
                if completed.returncode or not converted.exists():
                    raise RuntimeError('Conversion failed: ' + completed.stderr + completed.stdout)
                PdfReader(converted)
                shutil.copyfile(converted, output)
            engine = 'libreoffice'
        meta.update(ingestion_status='normalized', normalization_engine=engine, pdf_sha256=digest(output))
        meta.pop('normalization_error', None)
    except Exception as exc:
        meta.update(ingestion_status='normalization_required', normalization_error=str(exc))
    write_json(meta_path, meta)
    return meta


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('contract', nargs='?', type=Path)
    parser.add_argument('--converter')
    args = parser.parse_args()
    contracts = [args.contract] if args.contract else sorted((ROOT/'data/contracts').glob('contract_*'))
    for contract in contracts:
        if list((contract/'source').glob('original.*')):
            print(contract.name, normalize(contract,args.converter)['ingestion_status'])


if __name__ == '__main__':
    main()
