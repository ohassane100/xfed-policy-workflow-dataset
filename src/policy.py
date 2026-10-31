"""Readable policy documents and the existing ODRL compatibility export."""

from copy import deepcopy
from functools import lru_cache
import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator


@lru_cache(maxsize=16)
def odrl_schema(root=Path('.')):
    return json.loads((Path(root) / 'schemas/05_odrl_schema.json').read_text(encoding='utf-8'))


def validate_odrl(value, root=Path('.')):
    Draft202012Validator(odrl_schema(root)).validate(value)
    return value


def to_compact(value, chunks=(), root=Path('.')):
    """Store exact evidence once; preserve rule IDs, logic and optional metadata."""
    if 'odrlPolicy' not in value:
        return validate_policy(deepcopy(value), root)
    validate_odrl(value, root)
    locations = {chunk['chunk_id']: chunk.get('source', {}) for chunk in chunks}
    old = value['odrlPolicy']
    result = dict(version='2.0', contract_id=value['contract_id'], id=old['uid'],
                  type=old['@type'], rules=[], sources={})
    traces = {entry['ruleUid']: entry for entry in value['traceability']}
    if len(traces) != len(value['traceability']):
        raise ValueError('Duplicate traceability rule identifier.')

    def evidence(entries):
        primary, context = [], []
        for entry in entries:
            source = {key: deepcopy(entry[key]) for key in ('chunk_id', 'clause_id', 'text') if key in entry}
            source.update(deepcopy(locations.get(entry['chunk_id'], {})))
            key = entry['chunk_id']
            if key in result['sources'] and result['sources'][key] != source:
                key += ':' + hashlib.sha256(entry['text'].encode()).hexdigest()[:16]
            if key in result['sources'] and result['sources'][key] != source:
                raise ValueError('Conflicting source identifiers.')
            result['sources'][key] = source
            (primary if entry['relationship'] == 'primary' else context).append(key)
        if len(primary) != 1:
            raise ValueError('Every rule or binding needs exactly one primary source.')
        return dict(source=primary[0], **({'context': list(dict.fromkeys(context))} if context else {}))

    def condition(entry):
        for old_key, new_key in (('and', 'all'), ('or', 'any'), ('xone', 'exactly_one')):
            if old_key in entry:
                return {new_key: [condition(child) for child in entry[old_key]]}
        output = dict(field=entry['leftOperand'], op=entry['operator'])
        output['parameter' if 'rightOperandReference' in entry else 'value'] = deepcopy(
            entry.get('rightOperandReference', entry.get('rightOperand')))
        if 'uid' in entry:
            output['id'] = entry['uid']
        for old_key, new_key in (('dataType', 'type'), ('unit', 'unit')):
            if old_key in entry:
                output[new_key] = entry[old_key]
        return output

    def rule(entry, kind=None):
        trace = traces.pop(entry['uid'], None)
        expected = kind or 'duty'
        if trace is None or trace['ruleType'] != expected:
            raise ValueError('Every rule needs matching source traceability.')
        output = dict(id=entry['uid'])
        if kind:
            output['kind'] = kind
        for old_key, new_key in (('assignee', 'actor'), ('assigner', 'issuer'), ('action', 'action'), ('target', 'asset')):
            if old_key in entry:
                output[new_key] = entry[old_key]
        if entry.get('constraint'):
            output['conditions'] = [condition(part) for part in entry['constraint']]
        output.update(evidence(trace['sourceClauses']))
        for old_key, new_key in (('duty', 'requires'), ('remedy', 'remedies')):
            if entry.get(old_key):
                output[new_key] = [rule(child) for child in entry[old_key]]
        return output

    for kind in ('permission', 'prohibition', 'obligation'):
        result['rules'].extend(rule(entry, kind) for entry in old.get(kind, []))
    if traces:
        raise ValueError('Source traceability refers to a nonexistent rule.')
    for old_key, new_key in (('parameterBindings', 'parameters'), ('partyBindings', 'parties')):
        if value.get(old_key):
            result[new_key] = {}
            for entry in value[old_key]:
                if entry['uid'] in result[new_key]:
                    raise ValueError('Duplicate binding identifier.')
                result[new_key][entry['uid']] = dict(
                    **{('type' if key == 'dataType' else key): entry[key]
                       for key in ('label', 'dataType', 'role') if key in entry},
                    **evidence(entry['sourceClauses']))
    if value.get('osdu'):
        result['metadata'] = dict(osdu=deepcopy(value['osdu']))
    # Preserve explicitly supplied JSON-LD contexts/profiles in compatibility exports.
    defaults = ['http://www.w3.org/ns/odrl.jsonld',
                {'ids': 'https://w3id.org/idsa/core/', 'idsc': 'https://w3id.org/idsa/code/'}]
    if old['@context'] != defaults or old['profile'] != 'http://www.w3.org/ns/odrl/2/core':
        result['export_context'] = dict(context=deepcopy(old['@context']), profile=old['profile'])
    order = {chunk['chunk_id']: index for index, chunk in enumerate(chunks)}
    result['rules'].sort(key=lambda row: order.get(result['sources'][row['source']]['chunk_id'], len(order)))
    return validate_policy(result, root)


def to_odrl(value, root=Path('.')):
    """Export the supported ODRL/IDS structure without asking a model to rewrite it."""
    if 'odrlPolicy' in value:
        return validate_odrl(deepcopy(value), root)
    if not value['rules']:
        raise ValueError('No extracted rules; there is no ODRL policy to export.')
    output = dict(contract_id=value['contract_id'], odrlPolicy={
        '@context': deepcopy(value.get('export_context', {}).get('context', [
            'http://www.w3.org/ns/odrl.jsonld',
            {'ids': 'https://w3id.org/idsa/core/', 'idsc': 'https://w3id.org/idsa/code/'}])),
        '@type': value['type'], 'uid': value['id'],
        'profile': value.get('export_context', {}).get('profile', 'http://www.w3.org/ns/odrl/2/core'),
    }, traceability=[])

    def evidence(entry):
        return [dict(**{key: deepcopy(source[key]) for key in ('chunk_id', 'clause_id', 'text') if key in source},
                     relationship=relationship)
                for identifier, relationship in [(entry['source'], 'primary'),
                    *((key, 'context') for key in entry.get('context', []))]
                for source in [value['sources'][identifier]]]

    def condition(entry):
        for key, old_key in (('all', 'and'), ('any', 'or'), ('exactly_one', 'xone')):
            if key in entry:
                return {old_key: [condition(child) for child in entry[key]]}
        result = dict(leftOperand=entry['field'], operator=entry['op'])
        result['rightOperandReference' if 'parameter' in entry else 'rightOperand'] = deepcopy(
            entry.get('parameter', entry.get('value')))
        if 'id' in entry:
            result['uid'] = entry['id']
        for key, old_key in (('type', 'dataType'), ('unit', 'unit')):
            if key in entry:
                result[old_key] = entry[key]
        return result

    def rule(entry, kind='duty'):
        result = dict(uid=entry['id'])
        for key, old_key in (('actor', 'assignee'), ('issuer', 'assigner'), ('action', 'action'), ('asset', 'target')):
            if key in entry:
                result[old_key] = entry[key]
        if entry.get('conditions'):
            result['constraint'] = [condition(child) for child in entry['conditions']]
        for key, old_key in (('requires', 'duty'), ('remedies', 'remedy')):
            if entry.get(key):
                result[old_key] = [rule(child) for child in entry[key]]
        output['traceability'].append(dict(ruleUid=entry['id'], ruleType=kind, sourceClauses=evidence(entry)))
        return result

    for entry in value['rules']:
        output['odrlPolicy'].setdefault(entry['kind'], []).append(rule(entry, entry['kind']))
    for key, old_key in (('parameters', 'parameterBindings'), ('parties', 'partyBindings')):
        if value.get(key):
            output[old_key] = [dict(uid=uid, status='unresolved', sourceClauses=evidence(entry),
                **{('dataType' if name == 'type' else name): entry[name]
                   for name in ('label', 'type', 'role') if name in entry})
                for uid, entry in value[key].items()]
    if value.get('metadata', {}).get('osdu'):
        output['osdu'] = deepcopy(value['metadata']['osdu'])
    return validate_odrl(output, root)


def validate_policy(value, root=Path('.')):
    definition = json.loads((Path(root) / 'schemas/05_policy_schema.json').read_text(encoding='utf-8'))
    Draft202012Validator(definition).validate(value)
    seen, references, used_parameters, used_parties = set(), set(), set(), set()

    def check(entry, nested=False):
        if entry['id'] in seen:
            raise ValueError('Duplicate policy rule identifier.')
        seen.add(entry['id'])
        refs = [entry['source'], *entry.get('context', [])]
        if len(refs) != len(set(refs)) or any(ref not in value['sources'] for ref in refs):
            raise ValueError('Source references must be distinct and resolve to the source catalogue.')
        references.update(refs)
        if entry.get('actor') in value.get('parties', {}):
            used_parties.add(entry['actor'])

        def conditions(entries):
            for item in entries:
                group = next((key for key in ('all', 'any', 'exactly_one') if key in item), None)
                if group:
                    conditions(item[group])
                elif 'parameter' in item:
                    used_parameters.add(item['parameter'])
                    binding = value.get('parameters', {}).get(item['parameter'])
                    if item['field'].casefold() == 'elapsedtime' and binding and binding['type'] != 'duration':
                        raise ValueError('An elapsedTime parameter must be a duration.')
        conditions(entry.get('conditions', []))
        for field, kind in (('requires', 'permission'), ('remedies', 'prohibition')):
            if entry.get(field) and (nested or entry.get('kind') != kind):
                raise ValueError(field + ' has an invalid parent rule.')
            for child in entry.get(field, []):
                check(child, True)

    for entry in value['rules']:
        check(entry)
    for field in ('parameters', 'parties'):
        for entry in value.get(field, {}).values():
            refs = [entry['source'], *entry.get('context', [])]
            if len(refs) != len(set(refs)) or any(ref not in value['sources'] for ref in refs):
                raise ValueError('Binding sources must resolve to distinct catalogue entries.')
            references.update(refs)
    if used_parameters != set(value.get('parameters', {})) or used_parties != set(value.get('parties', {})):
        raise ValueError('Every unresolved parameter or party must be declared exactly once and used.')
    if references != set(value['sources']):
        raise ValueError('Every catalogue source must be used by a rule or binding.')
    if value['rules']:
        to_odrl(value, root)
    return value


def verify_sources(value, chunks):
    """Check citations against original segmented clauses, including PDF locations."""
    original = {chunk['chunk_id']: chunk for chunk in chunks}
    if len(original) != len(chunks):
        raise ValueError('Original chunk identifiers must be unique.')
    for source in value['sources'].values():
        chunk = original.get(source['chunk_id'])
        if chunk is None or not source['text'] or source['text'] not in chunk['text']:
            raise ValueError('Policy evidence does not match the original clause.')
        if 'clause_id' in source and source['clause_id'] != chunk.get('clause_id'):
            raise ValueError('Policy clause number does not match the original clause.')
        for key in ('pages', 'block_ids'):
            if key in source and source[key] != chunk.get('source', {}).get(key):
                raise ValueError('Policy source location does not match the original clause.')
    return len(value['sources'])


def readable_rules(value):
    """Explain the saved JSON deterministically; never regenerate its meaning."""
    import re
    labels = dict(recipient='Recipient', purpose='Purpose', spatial='Storage country',
                  elapsedTime='Elapsed time from the stated trigger', event='Trigger',
                  approval='Approval', compliance='Required condition', security='Security', encryption='Encryption',
                  dateTime='Date', payAmount='Charge', scope='Scope', discretion='Discretion',
                  completeness='Record completeness', postponementDuration='Requested publication delay')
    operators = dict(eq='is', neq='is not', lt='is less than', lteq='is at most', gt='is more than',
                     gteq='is at least', isAnyOf='is one of', isNoneOf='is none of', isAllOf='includes all of')
    def literal(item):
        if isinstance(item, str):
            match = re.fullmatch(r'P(\d+)D|PT(\d+)H', item)
            if match:
                amount, unit = (match[1], 'day') if match[1] else (match[2], 'hour')
                return amount + ' ' + unit + ('' if amount == '1' else 's')
            return item
        return json.dumps(item, ensure_ascii=False)
    def condition(item):
        for group, description in (('all', 'All of'), ('any', 'At least one of'), ('exactly_one', 'Exactly one of')):
            if group in item:
                return description + ': (' + '; '.join(condition(child) for child in item[group]) + ')'
        right = ('unresolved parameter ' + item['parameter'] if 'parameter' in item else literal(item['value']))
        return labels.get(item['field'], item['field']) + ' ' + operators.get(item['op'], item['op']) + ' ' + right
    def explain(rule):
        modality = dict(permission='may', prohibition='must not', obligation='must').get(rule.get('kind'), 'must')
        operation = ('keep ' + rule['asset'] + ' confidential' if rule['action'] == 'keepConfidential'
                     else 'provide access to ' + rule['asset'] if rule['action'] == 'provideAccess'
                     else 'request a publication delay for ' + rule['asset'] if rule['action'] == 'requestDelay'
                     else re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', rule['action']).lower() + ' ' + rule['asset'])
        return dict(id=rule['id'], summary=rule['actor'] + ' ' + modality + ' ' + operation + '.',
                    conditions=[condition(item) for item in rule.get('conditions', [])],
                    source=deepcopy(value['sources'][rule['source']]), rule=deepcopy(rule),
                    supporting_sources=[deepcopy(value['sources'][key]) for key in rule.get('context', [])],
                    requires=[explain(child) for child in rule.get('requires', [])],
                    remedies=[explain(child) for child in rule.get('remedies', [])])
    rows = [explain(rule) for rule in value['rules']]
    def source_order(row):
        source = row['source']
        number = source.get('clause_id') or source['chunk_id']
        return (source.get('pages', [0])[0], tuple(part.zfill(12) if part.isdigit() else part for part in re.split(r'(\d+)', number)))
    return sorted(rows, key=source_order)
