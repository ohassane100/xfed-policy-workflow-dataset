"""Local clause/interpretation retrieval with explicit approval for exact reuse."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import threading

from xfed.common import write_json


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def evidence(item):
    """Locations may change; words, scope, references and definitions may not."""
    def clause(value):
        return {key: ' '.join(value[key].split()) for key in ('text', 'clause_id', 'heading', 'relationship') if key in value}
    return dict(primary=clause(item), context=sorted(
        [clause(c) for c in item.get('context', [])], key=lambda c: json.dumps(c, sort_keys=True)),
        unresolved_references=sorted(item.get('unresolved_references', [])))


def profile(root, client):
    root = Path(root)
    paths = [*root.glob('prompts/*.md'), *root.glob('schemas/*.json'),
             *[p for p in (root / 'src').rglob('*.py') if p.name not in ('ui.py', 'authoring.py', 'dct_schedules.py')]]
    return dict(code=digest({p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in sorted(paths)}),
                model={key: getattr(client, key, None) for key in
                       ('model', 'model_digest', 'num_ctx', 'num_predict', 'think', 'sampling')})


def tokens(text):
    return set(re.findall(r'[\w-]+', text.casefold())) - {'the', 'a', 'an', 'of', 'to', 'and', 'in', 'this', 'clause'}


def rebind(plan, old, new):
    """Rewrite evidence identifiers only, never source literals or policy values."""
    old_entries, new_entries = [old, *old.get('context', [])], [new, *new.get('context', [])]
    ids = {}
    for entry in old_entries:
        matches = [c for c in new_entries if c['text'].split() == entry['text'].split()
                   and c.get('clause_id') == entry.get('clause_id')
                   and c.get('relationship') == entry.get('relationship')]
        if len({c['chunk_id'] for c in matches}) != 1:
            raise ValueError('Stored evidence cannot be mapped unambiguously to the current contract.')
        ids[entry['chunk_id']] = matches[0]['chunk_id']
    result = deepcopy(plan)
    for value in [*result.get('rules', []), *result.get('unrepresented', []), *result.get('parameters', [])]:
        refs = value.get('evidence')
        if isinstance(refs, dict):
            refs['primary'] = ids[refs['primary']]
            refs['supporting'] = [ids[c] for c in refs.get('supporting', [])]
        elif isinstance(refs, list):
            for ref in refs:
                ref['chunk_id'] = ids[ref['chunk_id']]
    return result


class CaseLibrary:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'evaluation/cases.json'
        self.lock = threading.RLock()
        self.cases = json.loads(self.path.read_text(encoding='utf-8')) if self.path.is_file() else {}

    def lookup(self, item, settings):
        current = evidence(item)
        query = tokens(item['text'])
        ranked = []
        with self.lock:
            for case in self.cases.values():
                words = tokens(case['item']['text'])
                score = len(query & words) / math.sqrt(max(1, len(query) * len(words)))
                exact = case['evidence'] == current
                approved = (case['approved'] and case['profile'] == settings
                            and bool(settings['model']['model_digest']))
                if exact or score >= .55:
                    ranked.append((exact and approved, exact, score, case))
            if not ranked:
                return dict(category='new', exact=False, reusable=False, examples=[])
            ranked.sort(key=lambda x: x[:3], reverse=True)
            ready, exact, score, best = ranked[0]
            examples = [dict(case_id=c['id'], text=c['item']['text'],
                rules=[{k: v for k, v in rule.items() if k not in ('id', 'source', 'context')}
                       for rule in c['rules']], approved=c['approved']) for *_, c in ranked[:2] if c['approved']]
            examples = [example for example in examples if len(json.dumps(example, ensure_ascii=False).encode()) <= 3000]
            return dict(category='established' if ready else 'familiar', exact=exact, reusable=ready,
                        case_id=best['id'], similarity=round(score, 3), examples=examples,
                        case=deepcopy(best))

    def record(self, item, entry, compact_rules, settings, contract):
        if (entry.get('outcome') != 'generated' or entry.get('omitted_context_count') or item.get('unresolved_references')
                or entry.get('withheld_plan_groups') or entry.get('withheld_rule_uids')
                or not entry.get('rule_plan', {}).get('rules')
                or entry['rule_plan'].get('unrepresented')
                or entry.get('semantic_check', {}).get('coverage') != 'COMPLETE'
                or not entry['semantic_check'].get('rule_reviews')
                or any(r['verdict'] != 'ENTAILED' for r in entry['semantic_check'].get('rule_reviews', []))):
            return None
        key = digest(dict(evidence=evidence(item), profile=settings, plan=entry['rule_plan']))[:24]
        with self.lock:
            if key not in self.cases:
                self.cases[key] = dict(id=key, evidence=evidence(item), profile=settings,
                    item=deepcopy(item), plan=deepcopy(entry['rule_plan']), rules=deepcopy(compact_rules),
                    contract=contract, approved=False, reuse_count=0,
                    created_at=datetime.now(timezone.utc).isoformat())
                write_json(self.path, self.cases)
        return key

    def reused(self, identifier):
        with self.lock:
            self.cases[identifier]['reuse_count'] += 1
            write_json(self.path, self.cases)

    def review(self, identifier, approved):
        if type(approved) is not bool:
            raise ValueError('approved must be true or false.')
        with self.lock:
            if identifier not in self.cases:
                raise ValueError('Unknown clause case.')
            if approved:
                selected = self.cases[identifier]
                for case in self.cases.values():
                    if case['evidence'] == selected['evidence'] and case['profile'] == selected['profile']:
                        case['approved'] = False
            self.cases[identifier].update(approved=approved, reviewed_at=datetime.now(timezone.utc).isoformat())
            write_json(self.path, self.cases)
        return self.summary()

    def summary(self):
        with self.lock:
            return dict(cases=[{key: deepcopy(case[key]) for key in
                ('id', 'item', 'rules', 'contract', 'approved', 'reuse_count', 'created_at')}
                for case in self.cases.values()])
