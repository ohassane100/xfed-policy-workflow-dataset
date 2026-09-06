"""Shared real-PDF preprocessing retaining layout and source provenance."""
from pathlib import Path
from src.ingestion.pdf_to_text import extract_pages
from src.utils.io import digest, read_json, write_json
from src.utils.paths import ROOT


def preprocess(contract: Path) -> dict:
    source = contract/'source'
    path = source/'metadata.json'
    meta = read_json(path)
    if meta['contract_id'] != contract.name or meta.get('source') != 'real_public_agreement':
        raise ValueError('Real-source metadata and matching contract ID required')
    if meta.get('pdf_sha256') != digest(source/'contract.pdf'):
        raise ValueError('Normalized PDF provenance mismatch')
    try:
        pages = extract_pages(source/'contract.pdf')
        empty = [i+1 for i, text in enumerate(pages) if not text]
        if not pages or all(not text for text in pages):
            raise ValueError('PDF has no extractable text; OCR/manual transcription required')
        text = '\n\n\f\n\n'.join(pages)+'\n'
        controls=sum(ord(char)<32 and char not in '\t\n\r\f' for char in text)
        replacements=text.count('\ufffd')
        output = source/'contract.txt'
        if output.exists() and output.read_text(encoding='utf-8') != text:
            raise ValueError('Existing extracted text differs; review/version it explicitly')
        output.write_text(text,encoding='utf-8')
        meta.update(preprocessing_status='needs_page_review' if empty else 'needs_text_review' if controls or replacements else 'complete',
                    text_sha256=digest(output), page_count=len(pages), empty_text_pages=empty,
                    text_quality={'control_characters':controls,'replacement_characters':replacements},
                    preprocessing={'engine':'pypdf', 'mode':'layout', 'ocr':False})
        meta.pop('preprocessing_error',None)
    except Exception as exc:
        meta.update(preprocessing_status='failed', preprocessing_error=str(exc))
    write_json(path,meta)
    return meta


def preprocess_all(root: Path = ROOT) -> None:
    for contract in sorted((root/'data/contracts').glob('contract_*')):
        if (contract/'source/contract.pdf').exists():
            print(contract.name, preprocess(contract)['preprocessing_status'])


if __name__ == '__main__':
    preprocess_all()
