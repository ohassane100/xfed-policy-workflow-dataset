import json
import re
from functools import lru_cache
from pathlib import Path
from tempfile import NamedTemporaryFile


NUMBER = r"\d{1,3}(?:\.\d{1,3})*(?:\([a-z0-9]+\))*"
CLAUSE = re.compile(rf"^\s*(?:(?:Article|Section|Clause)\s+)?(?P<id>{NUMBER})[.):]?(?=\s|$)", re.I)
SCOPE = re.compile(r"^(ANNEX|APPENDIX|EXHIBIT)\s+([IVXLCDM]+|[A-Z]|\d+)\b", re.I)
DEFINITION = re.compile(r'(?m)^\s*["\u201c\u2018]?([A-Z][\w &/\-]{1,70}?)["\u201d\u2019]?\s*(?::|\bmeans\b|\bshall mean\b|\bhas the meaning\b)')


def clause_number(text):
    match = CLAUSE.match(text)
    if not match:
        return None
    value = match['id']
    if re.match(r'^\s*\d+\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\b', text, re.I):
        return None
    if int(value.split('.')[0].split('(')[0]) > 199:
        return None
    rest = text[match.end():].lstrip()
    if rest and rest[0].islower():
        return None
    if rest.startswith(('%', '/', '-', ',', '\u2013')) or re.match(r"\d", rest):
        return None
    return value


def parent_number(value):
    if value.endswith(')'):
        return value[:value.rfind('(')]
    return value.rsplit('.', 1)[0] if '.' in value else None


def scope_heading(text):
    first = re.sub(r'^\[OPTION\]\s*', '', text.splitlines()[0].strip(), flags=re.I)
    match = SCOPE.match(first)
    if match and (first.isupper() or not first[match.end():].strip() or first[match.end():].lstrip().startswith(':')):
        return match.group().upper()
    return None


def scoped_chunks(chunks):
    scope, previous = 0, None
    for chunk in chunks:
        if chunk.get('heading') and scope_heading(chunk['heading']):
            scope += 1
            previous = None
        number = chunk.get('clause_id', '').split('.')[0].split('(')[0]
        # Bundled contracts can restart numbering without an explicit annex heading.
        if number == '1' and previous not in (None, '1'):
            scope += 1
        if number:
            previous = number
        yield scope, chunk


@lru_cache(maxsize=16)
def schema(stage, root=Path('.')):
    names = ('document_blocks', 'contract_chunks', 'classified_chunks', 'enriched_relevance')
    path = Path(root) / 'schemas' / f'{stage:02}_{names[stage - 1]}_schema.json'
    return json.loads(path.read_text(encoding='utf-8'))


def validate(value, stage, root=Path('.')):
    from jsonschema import Draft202012Validator
    Draft202012Validator(schema(stage, root)).validate(value)
    return value


def write_output(path, value, stage, root=Path('.')):
    validate(value, stage, root)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
