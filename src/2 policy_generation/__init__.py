"""Generate traceable policy drafts from enriched clauses, one clause per request."""

import json
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
        primary = False
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
                primary = True
        if not primary:
            raise ValueError('Every generated rule needs the current clause as primary evidence.')
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
    evidence = '\n'.join(clause['text'] for clause in [item, *item['context']])

    def grounded_identifiers(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key != 'uid':
                    grounded_identifiers(child)
        elif isinstance(value, list):
            for child in value:
                grounded_identifiers(child)
        elif isinstance(value, str) and value.startswith('#') and value not in evidence:
            raise ValueError('Invented deployment identifier ' + repr(value) +
                             '; use descriptive labels grounded in the supplied evidence.')

    grounded_identifiers(fragment['odrlPolicy'])
    for tag in fragment.get('osdu', {}).get('legal', {}).get('legaltags', []):
        if tag not in evidence:
            raise ValueError('LegalTag names must be explicitly supplied in the evidence; omit unsupported OSDU metadata.')


CONTEXT_PRIORITY = {'parent': 0, 'definition': 1, 'other': 2, 'reference': 3, 'retrieved': 4}


def context_char_budget(client, num_predict=None, prompt=''):
    num_ctx = getattr(client, 'num_ctx', 16384)
    num_predict = getattr(client, 'num_predict', 4096) if num_predict is None else num_predict
    # Reserve output space and room for the system prompt; tokenization varies by model.
    return max(0, int(max(0, num_ctx - num_predict - 1024) * 2.5) - len(prompt))


def request_value(contract, item, budget):
    clause = {key: item[key] for key in ('chunk_id', 'clause_id', 'heading', 'text') if key in item}
    clause.update(context=[], context_complete=not bool(item['context']),
                  omitted_context_count=len(item['context']))
    value = dict(contract_id=contract, clause=clause)
    if len(json.dumps(value, ensure_ascii=False, separators=(',', ':'))) > budget:
        return None, clause

    contexts = [dict((key, context[key]) for key in
                     ('chunk_id', 'clause_id', 'text', 'relationship') if key in context)
                for context in item['context']]
    ordered = sorted(enumerate(contexts),
                     key=lambda pair: (CONTEXT_PRIORITY.get(pair[1]['relationship'], 5), pair[0]))
    selected = set()
    for index, context in ordered:
        clause['context'].append(context)
        if len(json.dumps(value, ensure_ascii=False, separators=(',', ':'))) <= budget:
            selected.add(index)
        else:
            clause['context'].pop()
    clause['context'] = [context for index, context in enumerate(contexts) if index in selected]
    clause['omitted_context_count'] = len(contexts) - len(selected)
    clause['context_complete'] = clause['omitted_context_count'] == 0
    if len(json.dumps(value, ensure_ascii=False, separators=(',', ':'))) > budget:
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
            answer = client.generate(prompt, value, output_schema, num_predict=output_limit)
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
        for source in trace['sourceClauses']:
            candidates = [c for c in [item, *item['context']]
                          if c['chunk_id'] == source['chunk_id']
                          and ('clause_id' not in source or c.get('clause_id') == source['clause_id'])
                          and ('text' not in source or c['text'] == source['text'])]
            if not candidates or len({c['text'] for c in candidates}) != 1:
                raise ValueError('Policy evidence ID/text must resolve to one exact supplied clause.')
            source.setdefault('text', candidates[0]['text'])
            if 'clause_id' in candidates[0]:
                source.setdefault('clause_id', candidates[0]['clause_id'])
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
                return None, report_clause(item, False, answer['reason'], supplied,
                                            context_retries, output_retries), False
            fragment = complete_fragment(answer['policy'], contract, supplied)
            validate_fragment(fragment, contract, supplied, root)
            return fragment, report_clause(item, True, answer['reason'], supplied,
                                            context_retries, output_retries), False
        except (ValueError, ValidationError) as error:
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
    results = map_chunks(document['items'], lambda item, worker: generate_item(
        item, worker, prompt, contract, request_schema, root), client, progress)
    for item, (fragment, entry, failed) in zip(document['items'], results):
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
    if progress:
        progress(len(document['items']), len(document['items']), 'Complete')
    if not output['traceability']:
        report['status'] = 'generation_rejected' if rejected else 'no_supported_rules'
        return None, report
    if rejected:
        report['status'] = 'draft_with_issues' if report.get('warnings') else 'draft_with_rejections'
    elif report.get('warnings'):
        report['status'] = 'draft_with_warnings'
    return validate(output, 5, root), report
