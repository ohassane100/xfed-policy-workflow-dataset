from collections import defaultdict
from copy import deepcopy
from pathlib import Path
import re

from xfed.common import DEFINITION, NUMBER, scope_heading, scoped_chunks, validate
from xfed.relevance import classify_chunk


REFERENCES = re.compile(rf'\b(?:articles?|clauses?|sections?|paragraphs?|provisions?)\s+({NUMBER}(?:\s*(?:,|and|or|to|[-\u2013])\s*{NUMBER})*)', re.I)


class ContextIndex:
    def __init__(self, chunks):
        self.clauses = defaultdict(list)
        self.definitions = defaultdict(list)
        self.scopes = {}
        self.scope_names = defaultdict(list)
        self.sections = defaultdict(list)
        self.children = defaultdict(list)
        for scope, chunk in scoped_chunks(chunks):
            heading = scope_heading(chunk['heading']) if chunk.get('heading') else None
            if heading:
                self.scope_names[heading].append(scope)
            self.scopes[chunk['chunk_id']] = scope
            self.sections[scope].append(chunk)
            if 'parent_clause_id' in chunk:
                self.children[scope, chunk['parent_clause_id']].append(chunk)
            if 'clause_id' in chunk:
                self.clauses[scope, chunk['clause_id']].append(chunk)
            matches = list(DEFINITION.finditer(chunk['text']))
            for i, match in enumerate(matches):
                if re.search(r'\d', match[1]):
                    continue
                end = matches[i + 1].start() if i + 1 < len(matches) else len(chunk['text'])
                definition = {k: v for k, v in chunk.items() if k != 'classification'}
                definition['text'] = chunk['text'][match.start():end].strip()
                self.definitions[match[1].strip().casefold()].append((scope, definition))
        terms = sorted(self.definitions, key=len, reverse=True)
        self.terms = re.compile(r'(?<!\w)(' + '|'.join(re.escape(t) for t in terms) + r')(?!\w)', re.I) if terms else None

    def context_for(self, chunk):
        scope = self.scopes[chunk['chunk_id']]
        context, missing, seen = [], [], set()

        def attach(target, relationship, descendants=False):
            key = (target['chunk_id'], target['text'])
            if target['chunk_id'] == chunk['chunk_id'] or key in seen:
                return
            seen.add(key)
            value = {k: target[k] for k in ('chunk_id', 'clause_id', 'heading', 'text', 'source') if k in target}
            context.append(dict(value, relationship=relationship))
            if descendants:
                target_scope = self.scopes[target['chunk_id']]
                for child in self.children.get((target_scope, target.get('clause_id')), []):
                    attach(child, relationship, True)

        def resolve(number, relationship, descendants=False, target_scope=None):
            candidates = self.clauses.get((scope if target_scope is None else target_scope, number), [])
            if len(candidates) != 1:
                missing.append(f'{number}: ' + ('ambiguous provision' if candidates else 'provision not found in this section'))
                return None
            attach(candidates[0], relationship, descendants)
            return candidates[0]

        parent = chunk.get('parent_clause_id')
        visited_parents = set()
        qualified_sections = set()
        while parent and parent not in visited_parents:
            visited_parents.add(parent)
            target = resolve(parent, 'parent')
            parent = target.get('parent_clause_id') if target else None
        for match in REFERENCES.finditer(chunk['text']):
            suffix = chunk['text'][match.end():match.end() + 100]
            if re.match(r'\s+of\s+(?:the\s+)?(?:Regulation|Directive|Data Act|GDPR|[A-Z][\w -]*Act)\b', suffix):
                missing.append(match.group() + ': external legal reference')
                continue
            target_scope = scope
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
        for match in re.finditer(r'\b(?:Annex|Appendix|Exhibit)\s+(?:[IVXLCDM]+|[A-Z]|\d+)\b', chunk['text'], re.I):
            if match.span() in qualified_sections:
                continue
            choices = self.scope_names.get(match.group().upper(), [])
            if len(choices) != 1:
                missing.append(match.group() + ': missing or ambiguous section')
            elif choices[0] != scope:
                for target in self.sections[choices[0]]:
                    attach(target, 'reference')
        if self.terms:
            for term in dict.fromkeys(m[0].casefold() for m in self.terms.finditer(chunk['text'])):
                definitions = self.definitions[term]
                local = [d for s, d in definitions if s == scope]
                candidates = local or [d for s, d in definitions if s == 0]
                precise = [d for d in candidates if re.search(r'\b(?:means|shall mean|has the meaning)\b', d['text'].splitlines()[0])]
                if precise:
                    candidates = precise
                if len(candidates) == 1:
                    attach(candidates[0], 'definition')
                elif any(d['chunk_id'] == chunk['chunk_id'] for d in candidates):
                    continue
                elif candidates:
                    missing.append(term + ': ambiguous definition')
        return context, list(dict.fromkeys(missing))


def enrich(document, client, root=Path('.')):
    validate(document, 3, root)
    updated = deepcopy(document)
    index = ContextIndex(updated['chunks'])
    items = []
    for chunk in updated['chunks']:
        initial = chunk['classification']
        if initial['label'] == 'NOT_RELEVANT':
            continue
        context, missing = index.context_for(chunk)
        if initial['label'] == 'NEEDS_CONTEXT' or missing:
            decision = classify_chunk(chunk, client, root, context, missing)
            decision = dict(decision)
            decision['reason'] = (f"Initial {initial['label']}: {initial.get('reason', '')}; "
                                  f"Reclassified {decision['label']}: {decision.get('reason', '')}")
            chunk['classification'] = decision
        if missing:
            decision = dict(chunk['classification'])
            decision['reason'] = decision.get('reason', '') + '; Unresolved context: ' + '; '.join(missing)
            if decision['label'] == 'RELEVANT':
                decision['label'] = 'NEEDS_CONTEXT'
            chunk['classification'] = decision
        if chunk['classification']['label'] == 'RELEVANT':
            items.append(dict({k: v for k, v in chunk.items() if k != 'classification'}, context=context))
    return validate(updated, 3, root), validate(dict(contract_id=document['contract_id'], items=items), 4, root)
