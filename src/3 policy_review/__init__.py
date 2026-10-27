"""Review rules, omissions and classification against source evidence."""

from collections import defaultdict
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

from xfed.common import map_chunks, schema, validate
from xfed.enrichment import ContextIndex
from xfed.llm import OllamaConnectionError
from xfed.policy_generation import check_grounding, context_char_budget, request_value, rules, summarize_error


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def conflicts(policy):
    """Flag exact overlaps; do not resolve legal precedence automatically."""
    seen, result = {}, []
    for kind, rule in rules(policy['odrlPolicy']):
        if kind == 'duty':
            continue
        key = fingerprint({k: v for k, v in rule.items() if k not in ('uid', 'duty', 'remedy')})
        for previous_kind, previous_uid in seen.get(key, []):
            if previous_kind == kind:
                result.append(f'Duplicate rule content: {previous_uid} and {rule["uid"]}.')
            elif 'prohibition' in (previous_kind, kind):
                result.append(f'Potential conflicting modalities: {previous_uid} and {rule["uid"]}.')
        seen.setdefault(key, []).append((kind, rule['uid']))
    return result


def review_schema(candidates, root):
    entry = deepcopy(schema(6, root)['properties']['rule_reviews']['items'])
    entry['required'] = ['rule_uid', 'rule_type', 'verdict', 'reason']
    entry['properties']['reason'].update(minLength=1, maxLength=400)
    if candidates:
        entry['properties']['rule_uid']['enum'] = [row['rule']['uid'] for row in candidates]
    return dict(type='object', additionalProperties=False,
                required=['relevance', 'rule_reviews', 'coverage', 'reason', 'missing_rules'],
                properties=dict(
                    relevance=dict(type='string', enum=['RELEVANT', 'NOT_RELEVANT', 'NEEDS_CONTEXT']),
                    rule_reviews=dict(type='array', minItems=len(candidates), maxItems=len(candidates), items=entry),
                    coverage=dict(type='string', enum=['COMPLETE', 'MISSING_RULES', 'NO_RULES_NEEDED', 'NEEDS_CONTEXT']),
                    reason=dict(type='string', minLength=1, maxLength=600),
                    missing_rules=dict(type='array', items=dict(type='string', minLength=1, maxLength=600))))


def review_item(item, worker, prompt, contract, root, require_resolved=True):
    candidates = item['candidate_rules']
    expected = {row['rule']['uid']: row['rule_type'] for row in candidates}
    response_schema = review_schema(candidates, root)
    reviewer = dict(model=getattr(worker, 'model', None), model_digest=getattr(worker, 'model_digest', None))
    failure = None
    for attempt in range(2):
        try:
            budget = context_char_budget(worker, prompt=prompt) - len(json.dumps(candidates, ensure_ascii=False)) - 512
            value, supplied = request_value(contract, item, budget)
            if value is None or not supplied['context_complete']:
                raise ValueError('Review evidence exceeds the safe input budget; increase context tokens.')
            value['candidate_rules'] = candidates
            system = prompt if failure is None else prompt + '\nRepair the response format: ' + failure
            answer = worker.generate(system, value, response_schema, truncate=False)
            Draft202012Validator(response_schema).validate(answer)
            actual = {row['rule_uid']: row['rule_type'] for row in answer['rule_reviews']}
            if actual != expected or len(answer['rule_reviews']) != len(expected):
                raise ValueError('Review every supplied rule exactly once with its matching rule_type.')
            if answer['coverage'] == 'COMPLETE' and not candidates:
                raise ValueError('No candidate rules exist; use MISSING_RULES, NO_RULES_NEEDED or NEEDS_CONTEXT.')
            if bool(answer['missing_rules']) != (answer['coverage'] == 'MISSING_RULES'):
                raise ValueError('List missing rules if and only if coverage is MISSING_RULES.')
            if answer['coverage'] == 'NO_RULES_NEEDED' and any(row['verdict'] == 'ENTAILED' for row in answer['rule_reviews']):
                raise ValueError('NO_RULES_NEEDED conflicts with an ENTAILED in-scope rule; resolve the inconsistency.')
            if answer['relevance'] == 'NOT_RELEVANT' and answer['coverage'] in ('COMPLETE', 'MISSING_RULES'):
                raise ValueError('NOT_RELEVANT conflicts with in-scope operative rules; resolve relevance and coverage.')
            if answer['relevance'] == 'NEEDS_CONTEXT':
                answer['coverage'] = 'NEEDS_CONTEXT'
                answer['missing_rules'] = []
            if require_resolved and item.get('unresolved_references') and answer['coverage'] in ('COMPLETE', 'NO_RULES_NEEDED'):
                answer['coverage'] = 'NEEDS_CONTEXT'
                answer['reason'] += ' Unresolved references: ' + '; '.join(item['unresolved_references'])
            return dict(answer, chunk_id=item['chunk_id'], reviewer=reviewer,
                        unresolved_references=item.get('unresolved_references', []))
        except OllamaConnectionError:
            raise
        except (ValueError, ValidationError, RuntimeError) as error:
            if hasattr(worker, 'discard_last_response'):
                worker.discard_last_response()
            failure = summarize_error(error)
            if not attempt and 'output token limit reached' in failure.lower():
                failure += ' Return concise explanations within the schema limits; do not repeat source text.'
                continue
            if any(term in failure.lower() for term in ('safe input budget', 'context limit reached',
                                                        'output token limit reached')) or isinstance(error, RuntimeError):
                break
    return dict(chunk_id=item['chunk_id'], reviewer=reviewer, relevance='NEEDS_CONTEXT',
                unresolved_references=item.get('unresolved_references', []),
                coverage='NEEDS_CONTEXT', reason='Review failed: ' + failure, error=failure, missing_rules=[],
                rule_reviews=[dict(rule_uid=uid, rule_type=kind, verdict='AMBIGUOUS', reason='Review failed: ' + failure)
                              for uid, kind in expected.items()])


def review(policy, enriched, classified, client, root=Path('.'), progress=None, generation_report=None):
    validate(enriched, 4, root)
    validate(classified, 3, root)
    if policy is not None:
        validate(policy, 5, root)
    contract = classified['contract_id']
    if enriched['contract_id'] != contract or (policy and policy['contract_id'] != contract):
        raise ValueError('Review inputs must belong to the same contract.')
    chunks = {chunk['chunk_id']: chunk for chunk in classified['chunks']}
    if len(chunks) != len(classified['chunks']):
        raise ValueError('Review source chunk IDs must be unique.')
    index = ContextIndex(classified['chunks'])
    prepared = {item['chunk_id']: item for item in enriched['items']}
    candidates, evidence_errors, blocked_reviews = defaultdict(list), [], []
    traces = {trace['ruleUid']: trace for trace in (policy or {}).get('traceability', [])}
    entries = list(rules(policy['odrlPolicy'])) if policy else []
    if len(traces) != len((policy or {}).get('traceability', [])):
        evidence_errors.append('Duplicate traceability identifiers.')
    if len({rule['uid'] for _, rule in entries}) != len(entries):
        evidence_errors.append('Duplicate rule identifiers.')
    if set(traces) != {rule['uid'] for _, rule in entries}:
        evidence_errors.append('Rule and traceability identifiers do not match.')
    for kind, rule in entries:
        trace = traces.get(rule['uid'], {})
        primary = [source for source in trace.get('sourceClauses', []) if source['relationship'] == 'primary']
        if len(primary) != 1 or primary[0]['chunk_id'] not in chunks or trace.get('ruleType') != kind:
            evidence_errors.append('Invalid primary evidence or ruleType for ' + rule['uid'])
            blocked_reviews.append(dict(rule_uid=rule['uid'], rule_type=kind, verdict='AMBIGUOUS',
                                        reason='Rule cannot be reviewed: invalid primary evidence or ruleType.'))
            continue
        identifier = primary[0]['chunk_id']
        if kind == 'obligation' and not rule.get('assignee', '').strip():
            evidence_errors.append('Standalone obligation has no responsible assignee: ' + rule['uid'])
        source_texts = []
        for source in trace['sourceClauses']:
            original = chunks.get(source['chunk_id'])
            if (original is None or not source['text'].strip() or source['text'] not in original['text']
                    or (source['relationship'] == 'primary' and source['text'] != original['text'])
                    or ('clause_id' in source and source['clause_id'] != original.get('clause_id'))):
                evidence_errors.append('Source text/identifier mismatch for ' + rule['uid'])
            elif original is not None:
                source_texts.append(source['text'])
        try:
            check_grounding(dict(odrlPolicy=dict(obligation=[rule])), '\n'.join(source_texts))
        except ValueError as error:
            evidence_errors.append(rule['uid'] + ': ' + str(error))
        candidates[identifier].append(dict(rule_type=kind, rule=deepcopy(rule),
                                           source_chunk_ids=[source['chunk_id'] for source in trace['sourceClauses']]))
    # All clauses are checked, including negatives and abstentions, to detect lost rules.
    items = []
    for identifier, chunk in chunks.items():
        context, missing = index.context_for(chunk)
        item = deepcopy(prepared.get(identifier) or dict(
            {key: value for key, value in chunk.items() if key != 'classification'}, context=context))
        if item['text'] != chunk['text']:
            raise ValueError('Enriched primary text changed before review: ' + identifier)
        # Preserve every valid source actually cited, even if retrieval has changed since generation.
        present = {(source['chunk_id'], source['text']) for source in item['context']}
        for candidate in candidates[identifier]:
            for source in traces[candidate['rule']['uid']]['sourceClauses']:
                original = chunks.get(source['chunk_id'])
                key = (source['chunk_id'], source['text'])
                if (source['relationship'] == 'context' and key not in present and original is not None
                        and source['text'].strip() and source['text'] in original['text']):
                    item['context'].append(dict(original, text=source['text'], relationship='reference'))
                    item['context'][-1].pop('classification', None)
                    present.add(key)
        for source in item['context']:
            original = chunks.get(source['chunk_id'])
            if (original is None or not source['text'].strip() or source['text'] not in original['text']
                    or source['source'] != original['source']
                    or ('clause_id' in source and source['clause_id'] != original.get('clause_id'))):
                raise ValueError('Enriched supporting evidence changed before review: ' + identifier)
        if missing:
            item['unresolved_references'] = list(dict.fromkeys([*item.get('unresolved_references', []), *missing]))
        item['candidate_rules'] = candidates[identifier]
        items.append(item)
    all_evidence = '\n'.join(chunk['text'] for chunk in chunks.values())
    for tag in (policy or {}).get('osdu', {}).get('legal', {}).get('legaltags', []):
        if tag not in all_evidence:
            evidence_errors.append('Unsupported LegalTag name: ' + tag)
    prompt = (Path(root) / 'prompts/policy_reviewer.md').read_text(encoding='utf-8')
    rows = map_chunks(items, lambda item, worker: review_item(item, worker, prompt, contract, root), client, progress)
    generated_by = {entry['chunk_id']: entry for entry in (generation_report or {}).get('clauses', [])}
    rule_reviews, clause_reviews = blocked_reviews, []
    for row in rows:
        identifier = row['chunk_id']
        rule_reviews.extend(row.pop('rule_reviews'))
        original = chunks[identifier]['classification']['label']
        row['classification_label'] = original
        row['classification_disagreement'] = original != row['relevance']
        row['rule_uids'] = [candidate['rule']['uid'] for candidate in candidates[identifier]]
        generator = generated_by.get(identifier, {})
        digest = row['reviewer']['model_digest']
        if digest and generator.get('model_digest'):
            row['same_model_as_generator'] = digest == generator['model_digest']
        elif generator.get('model'):
            row['same_model_as_generator'] = row['reviewer']['model'] == generator['model']
        else:
            row['same_model_as_generator'] = None
        clause_reviews.append(row)
    overlap = conflicts(policy) if policy else []
    incomplete = bool(evidence_errors or any(row.get('error') or row['coverage'] == 'NEEDS_CONTEXT'
                                           or row['unresolved_references'] for row in clause_reviews))
    issues = bool(overlap or any(entry['verdict'] != 'ENTAILED' for entry in rule_reviews)
                  or any(row['classification_disagreement'] or row['coverage'] == 'MISSING_RULES' for row in clause_reviews))
    status = 'incomplete' if incomplete else 'changes_requested' if issues else 'reviewed_no_issues_found'
    summary = dict(clauses=len(clause_reviews), rules=len(rule_reviews),
                   entailed_rules=sum(entry['verdict'] == 'ENTAILED' for entry in rule_reviews),
                   missing_rule_clauses=sum(row['coverage'] == 'MISSING_RULES' for row in clause_reviews),
                   classification_disagreements=sum(row['classification_disagreement'] for row in clause_reviews),
                   failed_reviews=sum(bool(row.get('error')) for row in clause_reviews),
                   same_model_reviews=sum(row['same_model_as_generator'] is True for row in clause_reviews))
    return validate(dict(review_id=f'urn:xfed:{contract}:review', policy_uid=(policy or {}).get('odrlPolicy', {}).get(
        'uid', f'urn:xfed:{contract}:policy'), reviewer_id='xfed-evidence-review', status=status,
        requires_human_approval=True, rule_reviews=rule_reviews, clause_reviews=clause_reviews,
        summary=summary,
        evidence_errors=evidence_errors, conflicts=overlap,
        input_hashes=dict(policy=fingerprint(policy), enriched=fingerprint(enriched), classified=fingerprint(classified)),
        prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest()), 6, root)
