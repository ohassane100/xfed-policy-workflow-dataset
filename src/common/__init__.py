import json
import re
from functools import lru_cache
from pathlib import Path
from tempfile import NamedTemporaryFile
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED


NUMBER = r"\d{1,3}(?:\.\d{1,3})*(?:\([a-z0-9]+\))*"
CLAUSE = re.compile(rf"^\s*(?:(?:Article|Section|Clause)\s+)?(?P<id>{NUMBER})[.):]?(?=\s|$)", re.I)
SCOPE = re.compile(r"^(ANNEX|APPENDIX|EXHIBIT)\s+([IVXLCDM]+|[A-Z]|\d+)\b", re.I)
DEFINITION = re.compile(r'(?m)^\s*["\u201c\u2018]?([A-Z][\w &/\-]{1,70}?)["\u201d\u2019]?\s*(?::|\bmeans\b|\bshall mean\b|\bhas the meaning\b)')
DURATION = re.compile(r'P(?=\d|T\d)(?:(?P<years>\d+)Y)?(?:(?P<months>\d+)M)?'
                      r'(?:(?P<days>\d+)D)?(?:T(?=\d)(?:(?P<hours>\d+)H)?'
                      r'(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?', re.I)


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


def standalone_heading(chunk):
    """Recognize explicit titles, including numbered titles with child clauses."""
    heading, text = chunk.get('heading', '').strip(), chunk['text'].strip()
    if not text:
        return False
    if not heading and chunk.get('child_clause_ids'):
        first = text.splitlines()[0]
        if re.fullmatch(r'[\d.() ]+', first):
            heading = first
    if not heading:
        return False
    numbered_title = (bool(chunk.get('child_clause_ids')) and text.startswith(heading + '\n')
                      and re.fullmatch(r'[\d.() ]+', heading))
    return bool((text == heading or numbered_title) and len(text.split()) <= 14
                and not re.search(r'\b(?:shall|must|may|means|is|are|will|can|only|not|no|without|'
                                  r'delete|retain|notify|disclose|provide|return|process)\b|[;:]\s*\S', text, re.I))


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
    names = ('document_blocks', 'contract_chunks', 'classified_chunks', 'enriched_relevance', 'policy', 'policy_review')
    path = Path(root) / 'schemas' / f'{stage:02}_{names[stage - 1]}_schema.json'
    return json.loads(path.read_text(encoding='utf-8'))


def validate(value, stage, root=Path('.')):
    from jsonschema import Draft202012Validator
    Draft202012Validator(schema(stage, root)).validate(value)
    return value


def write_output(path, value, stage, root=Path('.')):
    validate(value, stage, root)
    write_json(path, value)


def write_json(path, value):
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


def map_chunks(items, operation, client, progress=None, completed=None):
    """Bound in-flight work and collect results in source order."""
    items = list(items)
    workers = getattr(client, 'workers', 1)
    if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
        raise ValueError('workers must be a positive integer.')
    def apply(item):
        if hasattr(client, 'for_chunk'):
            with client.for_chunk(item.get('chunk_ids', item['chunk_id'])) as worker:
                return operation(item, worker)
        return operation(item, client)
    if progress:
        progress(0, len(items), 'Waiting for model responses')
    if workers == 1:
        results = []
        for item in items:
            results.append(apply(item))
            if completed:
                completed(item, results[-1])
            if progress:
                progress(len(results), len(items), item['chunk_id'])
        return results
    results = [None] * len(items)
    pool = ThreadPoolExecutor(max_workers=workers)
    pending, next_index, done_count = {}, 0, 0
    try:
        while next_index < len(items) or pending:
            while next_index < len(items) and len(pending) < workers:
                pending[pool.submit(apply, items[next_index])] = next_index
                next_index += 1
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                index = pending.pop(future)
                results[index] = future.result()
                if completed:
                    completed(items[index], results[index])
                done_count += 1
                if progress:
                    progress(done_count, len(items), items[index]['chunk_id'])
        return results
    finally:
        for future in pending:
            future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)
