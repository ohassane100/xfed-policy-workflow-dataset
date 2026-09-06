"""Text-only PDF ingestion; scanned PDFs fail explicitly (no implicit OCR)."""
import argparse
from pathlib import Path
from pypdf import PdfReader


def extract_pages(pdf: Path) -> list[str]:
    reader = PdfReader(pdf)
    return [(page.extract_text(extraction_mode="layout") or "").strip() for page in reader.pages]


def extract_text(pdf: Path) -> str:
    pages = extract_pages(pdf)
    if not pages or any(not page for page in pages):
        raise ValueError("A PDF page has no extractable text. OCR is not implemented; supply a text PDF.")
    return "\n\n\f\n\n".join(pages) + "\n"


def ingest(pdf: Path, output: Path | None = None) -> Path:
    output = output or pdf.with_name("contract.txt")
    if output.resolve() == pdf.resolve() or output.suffix.lower() == '.pdf':
        raise ValueError("Text output must not overwrite a PDF")
    text = extract_text(pdf)
    if output.exists() and output.read_text(encoding='utf-8') != text:
        raise FileExistsError('Existing text differs; version or review it explicitly')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    print(ingest(args.pdf, args.output))


if __name__ == "__main__":
    main()
