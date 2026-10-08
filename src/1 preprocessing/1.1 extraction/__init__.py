"""Stage 1.1: structured PDF extraction."""

from pathlib import Path


def extract_document(pdf_path: Path, contract_id: str, root=Path('.')) -> dict:
    import re
    from collections import Counter
    import warnings
    import pymupdf
    from xfed.common import clause_number, validate

    if not Path(pdf_path).is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    blocks = []
    margins = {}
    with pymupdf.open(pdf_path) as document:
        if document.needs_pass:
            raise ValueError(f"Password-protected PDF: {pdf_path}")
        for page in document:
            raw = page.get_text('dict', sort=True, flags=pymupdf.TEXTFLAGS_TEXT)['blocks']
            sizes = Counter()
            for block in raw:
                for line in block.get('lines', []):
                    for span in line['spans']:
                        sizes[round(span['size'], 1)] += len(span['text'].strip())
            body_size = sizes.most_common(1)[0][0] if sizes else 12
            try:
                tables = [pymupdf.Rect(t.bbox) for t in page.find_tables().tables
                          if t.row_count >= 2 and t.col_count >= 2]
            except Exception as error:
                warnings.warn(f"Page {page.number + 1}: table detection unavailable: {error}")
                tables = []
            page_blocks = []
            # Slight baseline differences otherwise place a detached number after its text.
            raw.sort(key=lambda b: (round(b['bbox'][1] / 3), b['bbox'][0]))
            for block in raw:
                lines = block.get('lines', [])
                text = '\n'.join(''.join(s['text'] for s in line['spans']).rstrip() for line in lines).strip()
                if not text:
                    continue
                bbox = list(block['bbox'])
                spans = [s for line in lines for s in line['spans'] if s['text'].strip()]
                bold = sum(len(s['text']) for s in spans if s['flags'] & 16)
                kind = 'paragraph'
                if len(text) < 250 and (bold > len(text) * .65 or
                                       min(s['size'] for s in spans) > body_size * 1.15):
                    kind = 'heading'
                if any(t.contains(pymupdf.Point((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)) for t in tables):
                    kind = 'table'
                marginal = bbox[1] < page.rect.height * .075 or bbox[3] > page.rect.height * .87
                if marginal and re.fullmatch(r'\d+|(?:Page|Side)\s+\d+(?:\s+(?:of|av)\s+\d+)?', text, re.I):
                    kind = 'other'
                if (bbox[1] > page.rect.height * .72 and
                        max(s['size'] for s in spans) < body_size * .92 and
                        re.match(r'^\d+\s', text)):
                    kind = 'other'
                value = dict(block_id=f'b{len(blocks) + 1:06}', type=kind, text=text,
                             page=page.number + 1, bbox=bbox)
                number = clause_number(text)
                if number and kind != 'other':
                    value['numbering'] = number
                blocks.append(value)
                page_blocks.append(value)
                if marginal:
                    key = re.sub(r'\d+', '#', text)
                    margins.setdefault(key, []).append(value)
            if sum(bool(re.search(r'\.{4}', b['text'])) for b in page_blocks) >= 3:
                for b in page_blocks:
                    b['type'] = 'other'
                    b.pop('numbering', None)
            if not page_blocks:
                warnings.warn(f"Page {page.number + 1}: no extractable text (image-only or blank); OCR not performed")
    for repeated in margins.values():
        if len({b['page'] for b in repeated}) >= 3:
            for b in repeated:
                b['type'] = 'other'
                b.pop('numbering', None)
    if not blocks:
        raise ValueError('No text could be extracted from the PDF; OCR may be required.')
    return validate(dict(contract_id=contract_id, blocks=blocks), 1, root)

