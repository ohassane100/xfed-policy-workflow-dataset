"""Generate traceable policy drafts from enriched clauses, one clause per request."""

from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator

from xfed.common import schema, validate


def rules(policy):
    for kind in ('permission', 'prohibition', 'obligation'):
        for rule in policy.get(kind, []):
            yield kind, rule
            for field in ('duty', 'remedy'):
                for duty in rule.get(field, []):
                    yield 'duty', duty


def check_traceability(policy, item):
    evidence = {c['chunk_id']: c for c in [item, *item['context']]}
    entries = {}
    for trace in policy['traceability']:
        if trace['ruleUid'] in entries:
            raise ValueError('Duplicate traceability rule identifier.')
        entries[trace['ruleUid']] = trace
        primary = False
        for source in trace['sourceClauses']:
            original = evidence.get(source['chunk_id'])
            if original is None or source['text'] != original['text']:
                raise ValueError('Policy evidence must match supplied chunk IDs and text exactly.')
            if source.get('clause_id') is not None and source['clause_id'] != original.get('clause_id'):
                raise ValueError('Policy clause identifier does not match its source.')
            if source['relationship'] == 'primary':
                if source['chunk_id'] != item['chunk_id']:
                    raise ValueError('Supporting context cannot introduce an independent primary rule.')
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
                  if key not in ('$defs', '$schema')}
        if isinstance(result.get('pattern'), str):
            result['pattern'] = result['pattern'].replace(r'\d', '[0-9]')
        return result

    policy = inline(original)
    odrl = policy['properties']['odrlPolicy']
    # Keep Ollama's grammar within its supported subset; validate the full schema afterwards.
    odrl['properties']['@context'] = {'const': [
        'http://www.w3.org/ns/odrl.jsonld',
        {'ids': 'https://w3id.org/idsa/core/', 'idsc': 'https://w3id.org/idsa/code/'},
    ]}
    alternatives = odrl.pop('anyOf')
    policy['properties']['odrlPolicy'] = {'anyOf': [
        dict(deepcopy(odrl), required=list(dict.fromkeys(odrl['required'] + choice['required'])))
        for choice in alternatives
    ]}
    return dict(type='object', required=['policy', 'reason'], additionalProperties=False,
                properties=dict(policy={'anyOf': [policy, {'type': 'null'}]},
                                reason=dict(type='string', minLength=1)))


def generate(document, client, root=Path('.'), progress=None):
    validate(document, 4, root)
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
    for position, item in enumerate(document['items']):
        if progress:
            progress(position, len(document['items']), item['chunk_id'])
        value = dict(contract_id=contract, clause=item)
        answer = client.generate(prompt, value, request_schema)
        Draft202012Validator(request_schema).validate(answer)
        if answer['policy'] is None:
            report['clauses'].append(dict(chunk_id=item['chunk_id'], generated=False,
                                          reason=answer['reason']))
            continue
        fragment = answer['policy']
        try:
            validate_fragment(fragment, contract, item, root)
        except ValueError as error:
            if str(error) == 'Every generated rule needs the current clause as primary evidence.':
                correction = (
                    f'{prompt}\n\nCorrection: The previous response failed validation: {error} '
                    'For every rule, cite this clause itself as primary evidence using its exact '
                    'chunk_id and complete text. Cite supporting clauses only as context. If this '
                    'clause does not directly support a rule, return policy null.'
                )
                answer = client.generate(correction, value, request_schema)
                Draft202012Validator(request_schema).validate(answer)
                if answer['policy'] is None:
                    report['clauses'].append(dict(chunk_id=item['chunk_id'], generated=False,
                                                  reason=answer['reason']))
                    continue
                fragment = answer['policy']
                try:
                    validate_fragment(fragment, contract, item, root)
                except ValueError as retry_error:
                    report['clauses'].append(dict(
                        chunk_id=item['chunk_id'], generated=False,
                        reason=f'Generated policy rejected after correction attempt: {retry_error}'))
                    rejected = True
                    continue
            else:
                report['clauses'].append(dict(chunk_id=item['chunk_id'], generated=False,
                                              reason=f'Generated policy rejected: {error}'))
                rejected = True
                continue
        report['clauses'].append(dict(chunk_id=item['chunk_id'], generated=True,
                                      reason=answer['reason']))
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
        if 'osdu' in fragment:
            existing = output.setdefault('osdu', {})
            for key, value in fragment['osdu'].items():
                if key in existing and existing[key] != value:
                    raise ValueError('Conflicting OSDU metadata requires manual resolution.')
                existing[key] = value
    if progress:
        progress(len(document['items']), len(document['items']), 'Complete')
    if not output['traceability']:
        report['status'] = 'generation_rejected' if rejected else 'no_supported_rules'
        return None, report
    if rejected:
        report['status'] = 'draft_with_rejections'
    return validate(output, 5, root), report
