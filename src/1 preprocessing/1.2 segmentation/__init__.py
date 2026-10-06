import re
from pathlib import Path

from xfed.common import DEFINITION, clause_number, parent_number, scope_heading, scoped_chunks, validate


def segment(document, root=Path('.')):
    validate(document, 1, root)
    blocks = document['blocks']
    candidates = [clause_number(line) for b in blocks if b.get('type') not in ('other', 'table')
                  for line in b['text'].splitlines()]
    numbered = len([x for x in candidates if x]) >= 2 or any(x and '.' in x for x in candidates)
    chunks = []
    active = None
    current_number = None
    section_number = None

    def start(text, block, number=None, heading=None):
        chunk = dict(chunk_id=f'c{len(chunks) + 1:06}', text=text,
                     source=dict(pages=[block['page']], block_ids=[block['block_id']]))
        if number:
            chunk['clause_id'] = number
        if heading:
            chunk['heading'] = heading
        chunks.append(chunk)
        return chunk

    for block in blocks:
        text = block['text']
        if block.get('type') == 'other':
            start(text, block)
            continue
        if scope_heading(text) and (block.get('type') == 'heading' or text.splitlines()[0].isupper()):
            active = None
            current_number = None
            section_number = None
            start(text, block, heading=text.splitlines()[0])
            continue
        if not numbered:
            start(text, block)
            continue
        if DEFINITION.match(text) and not clause_number(text) and block.get('type') != 'table':
            active = start(text, block)
            if section_number:
                active['parent_clause_id'] = section_number
            current_number = None
            continue
        if block.get('type') == 'heading' and not any(clause_number(line) for line in text.splitlines()):
            active = start(text, block, heading=text.splitlines()[0])
            if section_number:
                active['parent_clause_id'] = section_number
            current_number = None
            continue
        lines = text.splitlines(keepends=True)
        pieces = []
        for position, line in enumerate(lines):
            number = clause_number(line)
            if block.get('type') != 'heading' and re.match(r'\s*(?:Article|Section|Clause)\b', line, re.I):
                number = None
            if position and (re.search(r'\b(?:Article|Section|Clause)s?\s*$', lines[position - 1], re.I) or
                             (position == len(lines) - 1 and re.fullmatch(r'\s*[\d.()]+\s*', line))):
                number = None
            if re.fullmatch(r'\d+\s*', text):
                number = None
            if block.get('type') == 'table' or re.search(r'\.{4}', text):
                number = None
            letter = re.match(r'^\s*(?:\(([a-z]|[ivx]{2,4})\)|([a-z]|[ivx]{2,4})[.)])(?=\s|$)', line)
            if not number and letter and current_number and block.get('type') != 'table':
                base = current_number.split('(')[0]
                marker = letter[1] or letter[2]
                suffixes = re.findall(r'\(([^)]+)\)', current_number)
                if suffixes and re.fullmatch(r'i|ii|iii|iv|v|vi|vii|viii|ix|x', marker):
                    if len(suffixes) > 1:
                        base = parent_number(current_number)
                    elif marker == 'i' and suffixes[-1] != 'h':
                        base = current_number
                number = base + '(' + marker + ')'
            if number:
                current_number = number
                if not letter:
                    section_number = number
                pieces.append([number, line])
            elif pieces:
                pieces[-1][1] += line
            else:
                pieces.append([None, line])
        for number, part in pieces:
            part = part.strip()
            if not part:
                continue
            if number:
                heading = part.splitlines()[0] if block.get('type') == 'heading' else None
                active = start(part, block, number, heading)
            elif active is not None:
                active['text'] += '\n' + part
                for key, value in [('pages', block['page']), ('block_ids', block['block_id'])]:
                    if value not in active['source'][key]:
                        active['source'][key].append(value)
            else:
                start(part, block)
    index = {}
    for scope, chunk in scoped_chunks(chunks):
        if 'clause_id' in chunk:
            index.setdefault((scope, chunk['clause_id']), []).append(chunk)
    for scope, chunk in scoped_chunks(chunks):
        if chunk.get('parent_clause_id') and len(index.get((scope, chunk['parent_clause_id']), [])) != 1:
            chunk.pop('parent_clause_id')
        parent = parent_number(chunk.get('clause_id', ''))
        while parent:
            matches = index.get((scope, parent), [])
            if len(matches) == 1:
                chunk['parent_clause_id'] = parent
                children = matches[0].setdefault('child_clause_ids', [])
                if chunk['clause_id'] not in children:
                    children.append(chunk['clause_id'])
                break
            parent = parent_number(parent)
    return validate(dict(contract_id=document['contract_id'], chunks=chunks), 2, root)
