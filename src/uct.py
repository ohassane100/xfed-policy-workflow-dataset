"""Structured drafting and dependency-aware reuse of the existing extraction pipeline."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from threading import RLock
from time import sleep

from xfed.common import validate, write_json
from xfed.policy_generation import generate, rule_description, rules
from xfed.relevance import classify_batch


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def is_cancelled(client):
    return any(event.is_set() for worker in getattr(client, 'clients', [client])
               if (event := getattr(worker, 'cancel', None)) is not None)


def check_contract(document):
    if not isinstance(document, dict) or set(document) != {'contract_id', 'title', 'sections', 'clauses'}:
        raise ValueError('A UCT contract needs contract_id, title, sections and clauses.')
    if not isinstance(document['contract_id'], str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', document['contract_id']):
        raise ValueError('Use a stable contract identifier containing letters, numbers, underscores or hyphens.')
    if not isinstance(document['title'], str) or not document['title'].strip() or len(document['title']) > 200:
        raise ValueError('A contract needs a title of at most 200 characters.')
    sections, clauses = document['sections'], document['clauses']
    if not isinstance(sections, list) or not 1 <= len(sections) <= 40:
        raise ValueError('Use 1 to 40 sections.')
    if not isinstance(clauses, list) or not 1 <= len(clauses) <= 150:
        raise ValueError('Use 1 to 150 clauses.')
    for section in sections:
        if (not isinstance(section, dict) or set(section) != {'id', 'title'}
                or not all(isinstance(section[k], str) and section[k].strip() for k in section)
                or len(section['title']) > 200):
            raise ValueError('Each section needs an id and title.')
    for row in [*sections, *clauses]:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', row['id']):
            raise ValueError('Section and clause IDs must be stable, short identifiers.')
    ids = {row['id'] for row in clauses}
    section_ids = {row['id'] for row in sections}
    if len(ids) != len(clauses) or len(section_ids) != len(sections):
        raise ValueError('Duplicate clause or section IDs.')
    for row in clauses:
        if (set(row) != {'id', 'section', 'text', 'depends_on'} or row['section'] not in section_ids
                or not isinstance(row['text'], str) or not row['text'].strip() or len(row['text']) > 6000):
            raise ValueError('Each clause needs a known section, nonempty text (maximum 6000 characters) and depends_on.')
        deps = row['depends_on']
        if (not isinstance(deps, list) or any(not isinstance(d, str) or d not in ids or d == row['id'] for d in deps)
                or len(set(deps)) != len(deps)):
            raise ValueError('Dependencies must be unique, existing clause IDs other than the clause itself.')
        # A typed reference must never quietly escape incremental invalidation.
        mentioned = set(re.findall(r'\bUCT-[A-Za-z0-9]+(?:[_.-][A-Za-z0-9]+)*', row['text']))
        if mentioned - ids:
            raise ValueError('Unknown UCT clause reference: ' + ', '.join(sorted(mentioned - ids)))
        if mentioned - {row['id']} - set(deps):
            raise ValueError('Add referenced UCT clause IDs to depends_on: ' + ', '.join(sorted(mentioned - set(deps) - {row['id']})))
    by_id = {row['id']: row for row in clauses}
    complete = set()
    def visit(identifier, trail):
        if identifier in trail:
            raise ValueError('Clause dependencies cannot contain cycles.')
        if identifier in complete:
            return
        for dependency in by_id[identifier]['depends_on']:
            visit(dependency, trail | {identifier})
        complete.add(identifier)
    for identifier in ids:
        visit(identifier, set())
    return document


def clause_inputs(document):
    """Native authoring bypasses PDF splitting, never classification or generation."""
    check_contract(document)
    rows = {row['id']: row for row in document['clauses']}
    headings = {row['id']: row['title'] for row in document['sections']}
    def source(row):
        # Virtual page 1 denotes a native UCT clause, not a PDF page.
        return dict(chunk_id=row['id'], clause_id=row['id'], heading=headings[row['section']],
                    text=row['text'], source=dict(pages=[1], block_ids=[row['id']]))
    result = []
    for row in rows.values():
        seen = set()
        def attach(identifier):
            for dependency in rows[identifier]['depends_on']:
                if dependency not in seen:
                    seen.add(dependency)
                    attach(dependency)
        attach(row['id'])
        # Declaration order has no semantic effect; deterministic ID order avoids incidental reuse misses.
        context = [dict(source(rows[identifier]), relationship='reference') for identifier in sorted(seen)]
        result.append(dict(source(row), context=context))
    return result


def contract_text(document):
    lines = [document['title'], 'Structured draft — not executed or approved.', '']
    for section in document['sections']:
        lines.append(section['title'])
        for row in document['clauses']:
            if row['section'] == section['id']:
                lines.extend([row['id'] + ': ' + row['text'], ''])
    return '\n'.join(lines)


def engine_identity(root, client):
    from xfed.evaluation import inference_files
    files = inference_files(root)
    files['src/uct.py'] = hashlib.sha256((Path(root) / 'src/uct.py').read_bytes()).hexdigest()
    return dict(code=fingerprint(files),
                model={key: deepcopy(getattr(client, key, None)) for key in (
                    'model', 'model_digest', 'url', 'num_ctx', 'num_predict', 'think', 'sampling',
                    'relevance_num_predict', 'relevance_think', 'relevance_compact', 'relevance_num_ctx')})


def extract_clause(item, client, root, contract='uct'):
    """Return the normal router decision, normal policy draft and normal diagnostics."""
    label = classify_batch([dict(clause={k: v for k, v in item.items() if k != 'context'},
                                 context=item['context'], unresolved_references=[])], client, root)[0]
    result = dict(classification=label, policy=None, report=None, failed=False)
    if label['label'] != 'NOT_RELEVANT':
        policy, report = generate(dict(contract_id=contract, items=[item]), client, root)
        result.update(policy=policy, report=report,
                      failed=any(row['outcome'] == 'rejected' for row in report['clauses']))
    return result


def assemble(document, artifacts, root):
    policy = None
    for row in document['clauses']:
        fragment = artifacts[row['id']]['result'].get('policy')
        if not fragment:
            continue
        if policy is None:
            policy = deepcopy(fragment)
            continue
        for kind in ('permission', 'prohibition', 'obligation'):
            policy['odrlPolicy'].setdefault(kind, []).extend(deepcopy(fragment['odrlPolicy'].get(kind, [])))
        for field in ('traceability', 'parameterBindings', 'partyBindings'):
            if fragment.get(field):
                policy.setdefault(field, []).extend(deepcopy(fragment[field]))
        if fragment['odrlPolicy']['@type'] == 'Set':
            policy['odrlPolicy']['@type'] = 'Set'
    if policy:
        if policy['contract_id'] != document['contract_id']:
            raise ValueError('Policy fragments must belong to the current contract.')
        validate(policy, 5, root)
    return policy


def human_policy(policy):
    if not policy:
        return 'No rules retained. This does not establish that the contract creates no obligations.'
    bindings = policy.get('parameterBindings', []) + policy.get('partyBindings', [])
    return '\n\n'.join(rule['uid'] + '\n' + rule_description(kind, rule, bindings)
                       for kind, rule in rules(policy['odrlPolicy']))


class UCTWorkspace:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'tmp/uct_workspace/state.json'
        self.lock = RLock()
        self.running = False
        self.data = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else dict(
            document=self.template(), revision=1, artifacts={}, generation=None, nodes=[], events=[])

        self.data.pop('evaluation', None)

    def template(self):
        return check_contract(json.loads((self.root / 'config/uct_template.json').read_text(encoding='utf-8'))['contract'])

    def persist(self):
        # Windows indexers may briefly hold the destination during atomic replacement.
        for attempt in range(4):
            try:
                write_json(self.path, self.data)
                return
            except PermissionError:
                if attempt == 3:
                    raise
                sleep(0.02 * (attempt + 1))

    def event(self, action, **values):
        self.data['events'].append(dict(time=datetime.now(timezone.utc).isoformat(), action=action, **values))

    def snapshot(self):
        with self.lock:
            result = deepcopy(self.data)
            result.pop('artifacts', None)
            result['running'] = self.running
            result['contract_text'] = contract_text(result['document'])
            result['policy_text'] = human_policy((result['generation'] or {}).get('policy'))
            return result

    def save(self, document, revision):
        check_contract(document)
        with self.lock:
            if self.running:
                raise ValueError('Wait for the active UCT job before editing the contract.')
            if revision != self.data['revision']:
                raise ValueError('The contract changed. Reload before saving or approving a proposal.')
            if document['contract_id'] != self.data['document']['contract_id']:
                raise ValueError('Keep the contract ID stable; edit the title and clauses instead.')
            if document == self.data['document']:
                return self.snapshot()
            self.data.update(document=deepcopy(document), revision=revision + 1, nodes=[])
            # Preserve reusable fragments internally, never expose the former aggregate as current.
            self.data['generation'] = None
            self.event('contract_updated', revision=revision + 1)
            self.persist()
            return self.snapshot()

    def run(self, client, progress=None):
        with self.lock:
            document, previous = deepcopy(self.data['document']), deepcopy(self.data['artifacts'])
            revision = self.data['revision']
        identity = engine_identity(self.root, client)
        artifacts, regenerated, reused = {}, [], []
        for index, item in enumerate(clause_inputs(document)):
            if is_cancelled(client):
                raise InterruptedError('UCT generation cancelled.')
            identifier = item['chunk_id']
            key = fingerprint(dict(contract=document['contract_id'], input=item, engine=identity))
            old = previous.get(identifier, {})
            if old.get('key') == key and not old['result'].get('failed'):
                artifacts[identifier] = old
                reused.append(identifier)
            else:
                try:
                    result = extract_clause(item, client, self.root, document['contract_id'])
                except InterruptedError:
                    raise
                except Exception as error:
                    result = dict(classification=None, policy=None, failed=True, error=str(error))
                artifacts[identifier] = dict(key=key, result=result)
                regenerated.append(identifier)
            # Keep completed clause work across cancellation without publishing a mixed aggregate.
            with self.lock:
                self.data['artifacts'] = {**previous, **deepcopy(artifacts)}
                self.persist()
            if progress:
                progress(index + 1, len(document['clauses']), identifier)
        policy = assemble(document, artifacts, self.root)
        generation = dict(revision=revision, policy=policy, policy_hash=fingerprint(policy),
                          engine=identity, regenerated=regenerated, reused=reused,
                          deleted=sorted(set(previous) - set(artifacts)),
                          clauses={key: value['result'] for key, value in artifacts.items()},
                          status='draft_needs_review', time=datetime.now(timezone.utc).isoformat())
        with self.lock:
            self.data.update(artifacts=artifacts, generation=generation, nodes=[])
            self.event('policy_generated', revision=revision, regenerated=regenerated, reused=reused)
            self.persist()
        return generation
