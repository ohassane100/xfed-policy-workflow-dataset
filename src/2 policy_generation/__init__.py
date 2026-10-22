"""Generate traceable policy drafts from enriched clauses, one clause per request."""

import json
import re
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

from xfed.common import map_chunks, schema, validate


def rules(policy):
    for kind in ('permission', 'prohibition', 'obligation'):
        for rule in policy.get(kind, []):
            yield kind, rule
            for field in ('duty', 'remedy'):
                for duty in rule.get(field, []):
                    yield 'duty', duty


def check_traceability(policy, item):
    evidence = {}
    for clause in [item, *item['context']]:
        evidence.setdefault(clause['chunk_id'], []).append(clause)
    entries = {}
    for trace in policy['traceability']:
        if trace['ruleUid'] in entries:
            raise ValueError('Duplicate traceability rule identifier.')
        entries[trace['ruleUid']] = trace
        primary = 0
        for source in trace['sourceClauses']:
            candidates = evidence.get(source['chunk_id'], [])
            original = next((clause for clause in candidates
                             if source['text'] == clause['text']
                             and (source.get('clause_id') is None
                                  or source['clause_id'] == clause.get('clause_id'))), None)
            if original is None:
                raise ValueError('Policy evidence must match supplied chunk IDs and text exactly.')
            if source['relationship'] == 'primary':
                if source['chunk_id'] != item['chunk_id']:
                    raise ValueError('Supporting context cannot introduce an independent primary rule.')
                if source['text'] != item['text']:
                    raise ValueError('Primary evidence must match the current clause text exactly.')
                primary += 1
        if primary != 1:
            raise ValueError('Every generated rule needs the current clause exactly once as primary evidence.')
    identifiers = set()
    for kind, rule in rules(policy['odrlPolicy']):
        uid = rule['uid']
        if uid in identifiers:
            raise ValueError('Duplicate policy rule identifier.')
        identifiers.add(uid)
        if uid not in entries or entries[uid]['ruleType'] != kind:
            raise ValueError('Every rule, including nested duties, needs matching traceability.')
    if identifiers != set(entries):
        raise ValueError('Traceability contains a nonexistent rule.')


def validate_fragment(fragment, contract, item, root):
    validate(fragment, 5, root)
    if fragment['contract_id'] != contract:
        raise ValueError('Generated policy changed the contract identifier.')
    check_traceability(fragment, item)
    if any(not rule.get('assignee', '').strip() for rule in fragment['odrlPolicy'].get('obligation', [])):
        raise ValueError('A standalone obligation must identify the responsible party in assignee.')
    evidence = '\n'.join(clause['text'] for clause in [item, *item['context']])
    check_grounding(fragment, evidence)
    def content(value):
        if isinstance(value, dict):
            return {key: content(child) for key, child in value.items() if key != 'uid'}
        if isinstance(value, list):
            return [content(child) for child in value]
        return value
    for kind in ('permission', 'prohibition', 'obligation'):
        seen = set()
        for rule in fragment['odrlPolicy'].get(kind, []):
            key = json.dumps(content(rule), sort_keys=True)
            if key in seen:
                raise ValueError('Duplicate ' + kind + ' rule; represent each distinct rule once.')
            seen.add(key)


def check_grounding(fragment, evidence):
    """Conservative literal checks; semantic entailment still requires evidence review."""

    def grounded_identifiers(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key not in ('uid', '@context', 'profile'):
                    grounded_identifiers(child)
        elif isinstance(value, list):
            for child in value:
                grounded_identifiers(child)
        elif isinstance(value, str):
            vocabulary = value.startswith(('http://www.w3.org/ns/odrl/2/', 'https://www.w3.org/ns/odrl/2/',
                                            'https://w3id.org/idsa/'))
            if value.startswith(('#', 'urn:', 'did:', 'http://', 'https://')) and not vocabulary and value not in evidence:
                raise ValueError('Invented deployment identifier ' + repr(value) +
                                 '; use descriptive labels grounded in the supplied evidence.')
            for date in re.findall(r'\b\d{4}-\d{2}-\d{2}\b', value):
                if date not in evidence:
                    raise ValueError('Unverified calendar date ' + date + '; preserve source wording instead of inventing a date.')

    grounded_identifiers(fragment['odrlPolicy'])
    allowed_operators = {'eq', 'neq', 'lt', 'lteq', 'gt', 'gteq', 'isA', 'hasPart',
                         'isPartOf', 'isAllOf', 'isAnyOf', 'isNoneOf'}
    for _, rule in rules(fragment['odrlPolicy']):
        for field in ('assigner', 'assignee'):
            if rule.get(field, '').strip().casefold() in {'data', 'personal data', 'project data', 'use', 'modify', 'any', '*'}:
                raise ValueError('Rule ' + field + ' must identify a party, not an asset or action.')
        if rule.get('target', '').strip().casefold() in {'any', '*', 'use', 'modify'}:
            raise ValueError('Rule target must identify the affected source asset; a wildcard or action is insufficient.')
        for field in ('action', 'target', 'assigner', 'assignee'):
            if field in rule and not rule[field].strip():
                raise ValueError('Rule ' + field + ' must not be empty.')
        if not rule['action'].startswith(('http://', 'https://')) and re.search(r'\b(?:and|or)\b|[,;]', rule['action'], re.I):
            raise ValueError('Use one atomic action per rule; split compound actions with their conditions preserved.')
        for constraint in rule.get('constraint', []):
            if constraint['leftOperand'].casefold() in {'clause_id', 'clauseid', 'referenceclause', 'business day'}:
                raise ValueError('A clause citation or definition is evidence, not an executable condition; represent its actual restriction.')
            operator = constraint['operator'].rsplit('/', 1)[-1].removeprefix('odrl:')
            if operator not in allowed_operators:
                raise ValueError('Unsupported constraint operator: ' + constraint['operator'])
    for tag in fragment.get('osdu', {}).get('legal', {}).get('legaltags', []):
        if tag not in evidence:
            raise ValueError('LegalTag names must be explicitly supplied in the evidence; omit unsupported OSDU metadata.')


CONTEXT_PRIORITY = {'parent': 0, 'definition': 1, 'other': 2, 'reference': 3, 'retrieved': 4}


def context_char_budget(client, num_predict=None, prompt=''):
    num_ctx = getattr(client, 'num_ctx', 16384)
    num_predict = getattr(client, 'num_predict', 4096) if num_predict is None else num_predict
    # Reserve output space and room for the system prompt; tokenization varies by model.
    return max(0, int(max(0, num_ctx - num_predict - 1024) * 2.5) - len(prompt.encode('utf-8')))


def request_value(contract, item, budget):
    clause = {key: item[key] for key in ('chunk_id', 'clause_id', 'heading', 'text') if key in item}
    clause.update(context=[], context_complete=not bool(item['context']),
                  omitted_context_count=len(item['context']))
    if item.get('unresolved_references'):
        clause['unresolved_references'] = item['unresolved_references']
    value = dict(contract_id=contract, clause=clause)
    size = len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
    if size > budget:
        return None, clause

    contexts = [dict((key, context[key]) for key in
                     ('chunk_id', 'clause_id', 'text', 'relationship') if key in context)
                for context in item['context']]
    ordered = sorted(enumerate(contexts),
                     key=lambda pair: (CONTEXT_PRIORITY.get(pair[1]['relationship'], 5), pair[0]))
    selected = set()
    for index, context in ordered:
        addition = len(json.dumps(context, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) + bool(selected)
        if size + addition <= budget:
            selected.add(index)
            size += addition
    clause['context'] = [context for index, context in enumerate(contexts) if index in selected]
    clause['omitted_context_count'] = len(contexts) - len(selected)
    clause['context_complete'] = clause['omitted_context_count'] == 0
    if len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > budget:
        return None, clause
    return value, clause


def generate_answer(client, prompt, contract, item, output_schema):
    num_ctx = getattr(client, 'num_ctx', 16384)
    initial_output_limit = getattr(client, 'num_predict', 4096)
    maximum_output_limit = max(initial_output_limit, min(num_ctx - 1024, initial_output_limit * 2))
    output_limit = initial_output_limit
    context_retries, output_retries = 0, 0
    for _ in range(2):
        budget = context_char_budget(client, output_limit, prompt)
        value, supplied_clause = request_value(contract, item, budget)
        if value is None:
            raise ValueError('Current clause exceeds the safe input budget; increase context tokens or reduce output tokens.')
        if not supplied_clause['context_complete']:
            raise ValueError('Supporting context exceeds the safe input budget; increase context tokens. '
                             f"{supplied_clause['omitted_context_count']} context entries do not fit.")
        try:
            answer = client.generate(prompt, value, output_schema, num_predict=output_limit, truncate=False)
            return answer, supplied_clause, context_retries, output_retries
        except ValueError as error:
            message = str(error).lower()
            if 'output token limit reached' in message:
                last = getattr(client, 'last_request', None) or {}
                if last.get('thinking_chars') and not last.get('answer_chars'):
                    raise ValueError(f'{error} Reasoning exhausted the budget without an answer; '
                                     'use Policy thinking Auto or Off instead of repeating the request.') from error
                if output_retries == 0 and output_limit < maximum_output_limit:
                    output_limit = maximum_output_limit
                    output_retries += 1
                    continue
                raise ValueError(f'{error} Policy clause was retried with '
                                 f'{output_limit} output tokens.') from error
            raise
    raise ValueError('Policy response could not be completed within the token budget.')


def recoverable_item_error(error):
    if isinstance(error, ValidationError):
        return True
    if not isinstance(error, ValueError):
        return False
    message = str(error).lower()
    return any(text in message for text in (
        'context limit reached', 'exceeds the safe input budget',
        'output token limit reached', 'incomplete or truncated', 'invalid json response',
        'message must contain a json object',
    ))


def summarize_error(error):
    if isinstance(error, ValidationError):
        # anyOf(policy, null) otherwise prints the entire draft and hides the
        # actionable error, such as an empty metadata object or missing field.
        pending, leaves = [error], []
        while pending:
            candidate = pending.pop()
            if candidate.context:
                pending.extend(candidate.context)
            elif not (candidate.validator == 'type' and candidate.validator_value == 'null'):
                leaves.append(candidate)
        if leaves:
            error = min(leaves, key=lambda candidate: (
                candidate.validator not in ('required', 'additionalProperties', 'minProperties'),
                len(candidate.absolute_path)))
        path = '.'.join(map(str, error.absolute_path)) or 'response'
        message = f'{path}: {error.message}'
    else:
        message = str(error).splitlines()[0]
    return message[:500]


def report_clause(item, generated, reason, supplied_clause=None, context_retries=0, output_retries=0):
    result = dict(chunk_id=item['chunk_id'], generated=generated, reason=reason)
    if supplied_clause and supplied_clause['omitted_context_count']:
        result['omitted_context_count'] = supplied_clause['omitted_context_count']
    if context_retries:
        result['context_retries'] = context_retries
    if output_retries:
        result['output_retries'] = output_retries
    return result


def merge_osdu(output, fragment, report, chunk_id):
    if 'osdu' not in fragment:
        return
    existing = output.setdefault('osdu', {})
    conflicts = []
    for key, value in fragment['osdu'].items():
        if key in report.get('osdu_conflicts', []):
            continue
        if key in existing and existing[key] != value:
            conflicts.append(key)
            existing.pop(key)
        else:
            existing[key] = value
    if conflicts:
        report.setdefault('osdu_conflicts', []).extend(conflicts)
        report.setdefault('warnings', []).append(dict(
            chunk_id=chunk_id,
            message='Conflicting OSDU metadata omitted for manual resolution: ' + ', '.join(conflicts),
        ))
    if not existing:
        output.pop('osdu', None)


def response_schema(root=Path('.')):
    original = schema(5, root)

    def inline(value):
        if isinstance(value, list):
            return [inline(child) for child in value]
        if not isinstance(value, dict):
            return value
        if '$ref' in value:
            reference = value['$ref']
            if not reference.startswith('#/$defs/'):
                raise ValueError('Policy generation requires local schema definitions.')
            return inline(dict(original['$defs'][reference.removeprefix('#/$defs/')],
                               **{key: child for key, child in value.items() if key != '$ref'}))
        result = {key: inline(child) for key, child in value.items()
                  if key not in ('$defs', '$schema', 'description', 'title')}
        if isinstance(result.get('pattern'), str):
            result['pattern'] = result['pattern'].replace(r'\d', '[0-9]')
        return result

    policy = inline(original)
    # An empty optional metadata object asserts nothing. Accept that transport
    # representation, then omit it before strict validation of the saved policy.
    policy['properties']['osdu'].pop('minProperties', None)
    odrl = policy['properties']['odrlPolicy']
    # Generate only variable content; restore fixed fields and evidence locally.
    for key in ('@context', '@type', 'uid', 'profile'):
        odrl['properties'].pop(key)
    odrl.pop('anyOf')
    odrl['required'] = []
    policy['required'].remove('contract_id')
    policy['properties'].pop('contract_id')
    evidence = policy['properties']['traceability']['items']['properties']['sourceClauses']['items']
    evidence['required'].remove('text')
    evidence['properties'].pop('text')
    return dict(type='object', required=['policy', 'reason'], additionalProperties=False,
                properties=dict(policy={'anyOf': [policy, {'type': 'null'}]},
                                reason=dict(type='string', minLength=1)))


def complete_fragment(fragment, contract, item):
    """Fill deterministic metadata and evidence without changing model rules."""
    fragment = deepcopy(fragment)
    if fragment.get('osdu') == {}:
        fragment.pop('osdu')
    fragment.setdefault('contract_id', contract)
    policy = fragment['odrlPolicy']
    for key, value in (
        ('@context', ['http://www.w3.org/ns/odrl.jsonld',
                      {'ids': 'https://w3id.org/idsa/core/', 'idsc': 'https://w3id.org/idsa/code/'}]),
        ('@type', 'Agreement'), ('uid', f'urn:xfed:{contract}:policy'),
        ('profile', 'http://www.w3.org/ns/odrl/2/core'),
    ):
        policy.setdefault(key, value)
    for trace in fragment['traceability']:
        sources = []
        for source in trace['sourceClauses']:
            candidates = [c for c in [item, *item['context']]
                          if c['chunk_id'] == source['chunk_id']
                          and ('clause_id' not in source or c.get('clause_id') == source['clause_id'])
                          and ('text' not in source or c['text'] == source['text'])]
            if not candidates or len({c.get('clause_id') for c in candidates}) != 1:
                raise ValueError('Policy evidence ID must resolve to supplied evidence with one clause identifier.')
            if source['relationship'] == 'primary' and len({c['text'] for c in candidates}) != 1:
                raise ValueError('Primary evidence ID must resolve to one exact supplied clause.')
            # One source chunk may supply several literal definition excerpts. Cite all
            # supplied excerpts for that ID rather than inventing a combined quotation.
            seen = set()
            for candidate in candidates:
                if candidate['text'] in seen:
                    continue
                seen.add(candidate['text'])
                entry = dict(source, text=candidate['text'])
                if 'clause_id' in candidate:
                    entry.setdefault('clause_id', candidate['clause_id'])
                sources.append(entry)
        trace['sourceClauses'] = sources
    return fragment


def generate_item(item, client, prompt, contract, request_schema, root):
    supplied, context_retries, output_retries = None, 0, 0
    correction = prompt
    for attempt in range(2):
        try:
            answer, supplied, retry_count, output_retry_count = generate_answer(
                client, correction, contract, item, request_schema)
            context_retries += retry_count
            output_retries += output_retry_count
            Draft202012Validator(request_schema).validate(answer)
            if answer['policy'] is None:
                entry = report_clause(item, False, answer['reason'], supplied, context_retries, output_retries)
                entry['outcome'] = 'abstained_needs_review'
                return None, entry, False
            fragment = complete_fragment(answer['policy'], contract, supplied)
            validate_fragment(fragment, contract, supplied, root)
            return fragment, report_clause(item, True, answer['reason'], supplied,
                                            context_retries, output_retries), False
        except (ValueError, ValidationError) as error:
            if hasattr(client, 'discard_last_response'):
                client.discard_last_response()
            if recoverable_item_error(error) and not isinstance(error, ValidationError):
                return None, report_clause(item, False, f'Generation skipped: {summarize_error(error)}',
                                            supplied, context_retries, output_retries), True
            if attempt:
                return None, report_clause(
                    item, False, f'Generated policy rejected after correction attempt: {summarize_error(error)}',
                    supplied, context_retries, output_retries), True
            correction = (
                f'{prompt}\n\nCorrection: The previous response failed validation: {summarize_error(error)} '
                'Repair that validation error. Every rule, including nested duties and remedies, '
                'must have exactly one traceability entry of the matching ruleType and a unique uid. '
                'Copy source chunk_id and optional clause_id exactly from the supplied evidence. '
                'Every rule must cite this current clause as primary. Supporting clauses are context only. '
                'Omit source text and deterministic policy metadata as required by the response schema. '
                'If a rule cannot be supported faithfully, return policy null.'
            )


def generate(document, client, root=Path('.'), progress=None):
    validate(document, 4, root)
    chunk_ids = [item['chunk_id'] for item in document['items']]
    if len(chunk_ids) != len(set(chunk_ids)):
        raise ValueError('Enriched policy inputs must have unique chunk IDs.')
    request_schema = response_schema(root)
    prompt = (Path(root) / 'prompts/policy_generator.md').read_text(encoding='utf-8')
    contract = document['contract_id']
    output = dict(contract_id=contract, odrlPolicy={
        '@context': ['http://www.w3.org/ns/odrl.jsonld',
                     {'ids': 'https://w3id.org/idsa/core/', 'idsc': 'https://w3id.org/idsa/code/'}],
        '@type': 'Agreement', 'uid': f'urn:xfed:{contract}:policy',
        'profile': 'http://www.w3.org/ns/odrl/2/core',
    }, traceability=[])
    report = dict(contract_id=contract, status='draft', clauses=[])
    rejected = False
    def operation(item, worker):
        fragment, entry, failed = generate_item(item, worker, prompt, contract, request_schema, root)
        entry.update(model=getattr(worker, 'model', None), model_digest=getattr(worker, 'model_digest', None))
        return fragment, entry, failed

    results = map_chunks(document['items'], operation, client, progress)
    for item, (fragment, entry, failed) in zip(document['items'], results):
        entry.setdefault('outcome', 'rejected' if failed else 'generated')
        entry['review_required'] = True
        report['clauses'].append(entry)
        rejected = rejected or failed
        if fragment is None:
            continue
        merge_osdu(output, fragment, report, item['chunk_id'])
        id_map = {}
        for number, (_, rule) in enumerate(rules(fragment['odrlPolicy']), 1):
            uid = f"urn:xfed:{contract}:{item['chunk_id']}:r{number}"
            id_map[rule['uid']] = uid
            rule['uid'] = uid
        for trace in fragment['traceability']:
            trace['ruleUid'] = id_map[trace['ruleUid']]
        for kind in ('permission', 'prohibition', 'obligation'):
            if fragment['odrlPolicy'].get(kind):
                output['odrlPolicy'].setdefault(kind, []).extend(fragment['odrlPolicy'][kind])
        output['traceability'].extend(fragment['traceability'])
    report['summary'] = {outcome: sum(entry['outcome'] == outcome for entry in report['clauses'])
                         for outcome in ('generated', 'abstained_needs_review', 'rejected')}
    report['summary']['input_clauses'] = len(document['items'])
    report['summary']['rules'] = len(output['traceability'])
    report['models'] = {}
    for entry in report['clauses']:
        counts = report['models'].setdefault(entry['model'] or 'unknown', dict(
            generated=0, abstained_needs_review=0, rejected=0))
        counts[entry['outcome']] += 1
    for model, counts in report['models'].items():
        if counts['generated'] == 0 and counts['abstained_needs_review'] >= 5:
            report.setdefault('warnings', []).append(dict(
                model=model, message='This model generated no rules across at least five clauses. '
                                     'Inspect abstentions in policy review before treating throughput as useful work.'))
    if progress:
        progress(len(document['items']), len(document['items']), 'Complete')
    if not output['traceability']:
        report['status'] = 'generation_rejected' if rejected else 'no_supported_rules'
        return None, report
    if rejected:
        report['status'] = 'draft_with_issues' if report.get('warnings') else 'draft_with_rejections'
    elif report.get('warnings'):
        report['status'] = 'draft_with_warnings'
    elif report['summary']['abstained_needs_review']:
        report['status'] = 'draft_with_abstentions'
    return validate(output, 5, root), report
