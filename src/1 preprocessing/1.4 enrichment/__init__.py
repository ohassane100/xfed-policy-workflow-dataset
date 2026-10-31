from collections import defaultdict
from copy import deepcopy
from pathlib import Path
import re

from xfed.common import CLAUSE, DEFINITION, NUMBER, scope_heading, scoped_chunks, standalone_heading, validate


REFERENCES = re.compile(rf'\b(?:articles?|clauses?|sections?|paragraphs?|provisions?)\s+({NUMBER}(?:\s*(?:,|and|or|to|[-\u2013])\s*{NUMBER})*)', re.I)


class ContextIndex:
    def __init__(self, chunks, blocks=()):
        self.clauses = defaultdict(list)
        self.definitions = defaultdict(list)
        self.scopes = {}
        self.scope_names = defaultdict(list)
        self.sections = defaultdict(list)
        self.children = defaultdict(list)
        self.main_scopes = {}
        self.table_context = defaultdict(list)
        self.page_disclaimers = defaultdict(list)
        by_block = defaultdict(list)
        for chunk in chunks:
            for identifier in chunk['source']['block_ids']:
                by_block[identifier].append(chunk)
            if re.search(r'\b(?:not\s+a\s+substitute\s+for\s+(?:the\s+)?(?:licen[cs]e|agreement|contract)'
                         r'|non[- ]binding\s+summary|for\s+information(?:al\s+purposes)?\s+only)\b',
                         chunk['text'], re.I):
                for page in chunk['source']['pages']:
                    self.page_disclaimers[page].append(chunk)
        # Preserve table row identity without rewriting the original chunks.
        # PDF text blocks commonly split the asset, holder and restriction cells.
        groups, current, preceding = [], [], None
        for block in blocks:
            if current and (block.get('type') != 'table' or block['page'] != current[-1]['page']
                            or block.get('table_id') != current[-1].get('table_id')):
                groups.append((current, preceding))
                current = []
            if block.get('type') == 'table' and block.get('bbox'):
                current.append(block)
            elif block.get('type') != 'other':
                preceding = block
        if current:
            groups.append((current, preceding))
        for group, introduction in groups:
            rows = []
            for block in group:
                # With new extraction metadata, only use a positively identified
                # row. Older saved blocks retain the conservative baseline band.
                if block.get('table_id') and 'table_row' not in block and not block.get('table_header'):
                    continue
                if not rows or (block.get('table_row') != rows[-1][0].get('table_row')
                                or abs(block['bbox'][1] - rows[-1][0]['bbox'][1]) > 3):
                    rows.append([])
                rows[-1].append(block)
            headers = [block for block in group if block.get('table_header')]
            if not headers and rows and not group[0].get('table_id'):
                header_text = ' '.join(block['text'] for block in rows[0])
                if not re.search(r'\b(?:shall|must|may|only|not|no|means)\b', header_text, re.I):
                    headers = rows[0]
            for row in rows:
                supporting = [*headers, *row, *([introduction] if introduction else [])]
                for block in row:
                    for chunk in by_block[block['block_id']]:
                        self.table_context[chunk['chunk_id']].extend(
                            target for source in supporting for target in by_block[source['block_id']])
        main_scope = 0
        for scope, chunk in scoped_chunks(chunks):
            heading = scope_heading(chunk['heading']) if chunk.get('heading') else None
            if scope not in self.main_scopes:
                if not heading:
                    main_scope = scope
                self.main_scopes[scope] = main_scope
            if heading:
                self.scope_names[heading].append(scope)
            self.scopes[chunk['chunk_id']] = scope
            self.sections[scope].append(chunk)
            if 'parent_clause_id' in chunk:
                self.children[scope, chunk['parent_clause_id']].append(chunk)
            if 'clause_id' in chunk:
                self.clauses[scope, chunk['clause_id']].append(chunk)
            number = CLAUSE.match(chunk['text'])
            offset = number.end() if number else 0
            definition_text = chunk['text'][offset:]
            matches = list(DEFINITION.finditer(definition_text))
            for i, match in enumerate(matches):
                if re.search(r'\d', match[1]) or scope_heading(match[0]):
                    continue
                end = matches[i + 1].start() if i + 1 < len(matches) else len(definition_text)
                body = definition_text[match.end():end].strip()
                # Empty form fields such as repeated "Date: ______" are not definitions.
                if (not re.search(r'\w', re.sub(r'[_\[\].\-]', '', body))
                        or re.match(r'\s*[_\.]{3,}', body)):
                    continue
                definition = {k: v for k, v in chunk.items() if k != 'classification'}
                definition['text'] = chunk['text'][offset + match.start():offset + end].strip()
                self.definitions[match[1].strip().casefold()].append((scope, definition))
        terms = sorted(self.definitions, key=len, reverse=True)
        self.terms = {term: re.compile(r'(?<!\w)' + r'\s+'.join(map(re.escape, term.split())) + r'(?!\w)', re.I) for term in terms}

    def context_for(self, chunk, include_references=True):
        scope = self.scopes[chunk['chunk_id']]
        context, missing, seen, expanded = [], [], set(), set()

        def attach(target, relationship, descendants=False):
            key = (target['chunk_id'], target['text'])
            if target['chunk_id'] == chunk['chunk_id']:
                return
            if key not in seen:
                seen.add(key)
                value = {k: target[k] for k in ('chunk_id', 'clause_id', 'heading', 'text', 'source') if k in target}
                context.append(dict(value, relationship=relationship))
            if descendants and target['chunk_id'] not in expanded:
                expanded.add(target['chunk_id'])
                target_scope = self.scopes[target['chunk_id']]
                for child in self.children.get((target_scope, target.get('clause_id')), []):
                    attach(child, relationship, True)

        def resolve(number, relationship, descendants=False, target_scope=None):
            selected_scope = scope if target_scope is None else target_scope
            candidates = self.clauses.get((selected_scope, number), [])
            if not candidates and relationship == 'reference' and target_scope is None and self.main_scopes[scope] != scope:
                candidates = self.clauses.get((self.main_scopes[scope], number), [])
            if len(candidates) != 1:
                missing.append(f'{number}: ' + ('ambiguous provision' if candidates else 'provision not found in this section'))
                return None
            attach(candidates[0], relationship, descendants)
            return candidates[0]

        for target in self.table_context.get(chunk['chunk_id'], []):
            attach(target, 'other')
        for page in chunk['source']['pages']:
            for target in self.page_disclaimers.get(page, []):
                attach(target, 'other')

        parent = chunk.get('parent_clause_id')
        visited_parents = set()
        qualified_sections = set()
        while parent and parent not in visited_parents:
            visited_parents.add(parent)
            target = resolve(parent, 'parent')
            parent = target.get('parent_clause_id') if target else None
        # List introductions need their descendants as well as their ancestors.
        children = list(chunk.get('child_clause_ids', []))
        children.extend(child['clause_id'] for child in self.children.get((scope, chunk.get('clause_id')), [])
                        if 'clause_id' in child)
        if standalone_heading(chunk) and not chunk['text'].rstrip().endswith(':'):
            # A standalone heading does not need an entire section to classify its own meaning.
            children = []
        for child in dict.fromkeys(children):
            resolve(child, 'other', True)
        # A list item inherits restrictions from its parents, including their references.
        reference_text = '\n'.join([chunk['text']] + [entry['text'] for entry in context
                                                   if entry['relationship'] == 'parent'])
        for match in (REFERENCES.finditer(reference_text) if include_references else ()):
            suffix = reference_text[match.end():match.end() + 100]
            prefix = reference_text[max(0, match.start() - 70):match.start()]
            if (re.match(r'\s+(?:of\s+)?(?:the\s+)?(?:Regulation|Directive|Data Act|GDPR)\b', suffix, re.I)
                    or re.match(r'\s+of\s+(?:the\s+)?[A-Z][\w -]*Act\b', suffix)
                    or re.search(r'\b(?:GDPR|Data Act|Regulation\s+\(EU\)\s+\d+/\d+)\s*[,:(]?\s*$', prefix, re.I)):
                missing.append(match.group() + ': external legal reference')
                continue
            target_scope = None
            if re.match(r'\s+of\s+(?:(?:the|this)\s+)?(?:main\s+(?:agreement|contract)|agreement|contract)\b', suffix, re.I):
                target_scope = self.main_scopes[scope]
            qualifier = re.match(r'\s+(?:of|in)\s+(?:the\s+)?((?:Annex|Appendix|Exhibit)\s+(?:[IVXLCDM]+|[A-Z]|\d+)\b)', suffix, re.I)
            if qualifier:
                qualified_sections.add((match.end() + qualifier.start(1), match.end() + qualifier.end(1)))
                choices = self.scope_names.get(qualifier[1].upper(), [])
                if len(choices) != 1:
                    missing.append(qualifier[1] + ': missing or ambiguous section')
                    continue
                target_scope = choices[0]
            numbers = re.findall(NUMBER, match[1])
            if re.search(r'\bto\b|[-\u2013]', match[1]) and len(numbers) == 2:
                first, last = numbers
                a, b = first.rsplit('.', 1)[-1], last.rsplit('.', 1)[-1]
                prefix = first[:-len(a)]
                if a.isdigit() and b.isdigit() and prefix == last[:-len(b)] and 0 <= int(b) - int(a) <= 100:
                    numbers = [prefix + str(n) for n in range(int(a), int(b) + 1)]
                else:
                    missing.append(match.group() + ': range not resolved')
            for number in numbers:
                resolve(number, 'reference', True, target_scope)
        for match in (re.finditer(r'\b(?:Annex|Appendix|Exhibit)\s+(?:[IVXLCDM]+|[A-Z]|\d+)\b', reference_text, re.I)
                      if include_references else ()):
            if match.span() in qualified_sections:
                continue
            choices = self.scope_names.get(match.group().upper(), [])
            if len(choices) != 1:
                missing.append(match.group() + ': missing or ambiguous section')
            elif choices[0] != scope:
                for target in self.sections[choices[0]]:
                    attach(target, 'reference')
        if self.terms:
            texts = [chunk['text']] + [item['text'] for item in context]
            checked = set()
            while True:
                terms = [term for term, pattern in self.terms.items() if term not in checked
                         and any(pattern.search(text) for text in texts)]
                if not terms:
                    break
                for term in terms:
                    checked.add(term)
                    definitions = self.definitions[term]
                    local = [d for s, d in definitions if s == scope]
                    candidates = local or [d for s, d in definitions if s == self.main_scopes[scope]]
                    precise = [d for d in candidates if re.search(r'\b(?:means|shall mean|has the meaning)\b', d['text'].splitlines()[0], re.I)]
                    if precise:
                        candidates = precise
                    if len(candidates) == 1:
                        attach(candidates[0], 'definition')
                    elif any(d['chunk_id'] == chunk['chunk_id'] for d in candidates):
                        continue
                    elif candidates:
                        missing.append(term + ': ambiguous definition')
                texts = [chunk['text']] + [item['text'] for item in context]
        return context, list(dict.fromkeys(missing))


def enrich(document, root=Path('.'), progress=None, blocks=()):
    validate(document, 3, root)
    updated = deepcopy(document)
    index = ContextIndex(updated['chunks'], blocks)
    items = []
    for position, chunk in enumerate(updated['chunks']):
        if progress:
            progress(position, len(updated['chunks']), chunk['chunk_id'])
        initial = chunk['classification']
        if initial['label'] == 'NOT_RELEVANT':
            continue
        context, missing = index.context_for(chunk)
        item = dict({k: v for k, v in chunk.items() if k != 'classification'}, context=context)
        if missing:
            # Relevance and whether all policy conditions can be encoded are different decisions.
            item['unresolved_references'] = missing
        items.append(item)
    if progress:
        progress(len(updated['chunks']), len(updated['chunks']), 'Complete')
    return validate(updated, 3, root), validate(dict(contract_id=document['contract_id'], items=items), 4, root)
