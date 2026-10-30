"""Guided contracts: document authoring and a separate, source-bound reference.

No extraction stage imports this module or reads its saved authoring/reference
files. Only the rendered PDF enters stages 1.1 through 2.
"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from uuid import uuid4

from xfed.common import constraint_truth, write_json
from xfed.policy import validate_policy, verify_sources
from xfed.policy_evaluation import component_comparison, normalized


VERSION = 'guided-2'
SUPPORTED_VERSIONS = ('guided-1', VERSION)
DEFAULTS = dict(title='Data Collaboration Agreement', provider='North Research Institute',
    recipient='Harbour Analytics', data='Research Dataset', purpose='joint research',
    read=True, purpose_limit=True, sharing_limit=True, consent_exception=True,
    confidentiality=True, secure_storage=True, deletion=True, delete_days=30,
    notification=True, notify_hours=72)
FLAGS = ('read', 'purpose_limit', 'sharing_limit', 'consent_exception', 'confidentiality',
         'secure_storage', 'deletion', 'notification')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_terms(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULTS):
        raise ValueError('Provide all guided fields and no additional fields.')
    value = deepcopy(value)
    for key in ('title', 'provider', 'recipient', 'data', 'purpose'):
        text = value[key]
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 120 or any(ord(c) < 32 for c in text):
            raise ValueError(key + ' must be one line containing 1 to 120 printable characters.')
        if re.search(r'\b(?:Provider|Recipient|Parties)\s+(?:shall|must|may|will|can)\b', text, re.I):
            raise ValueError(key + ' must be a label, not an additional contract clause.')
        value[key] = ' '.join(text.split())
        if key != 'title' and normalized(value[key]) in ('provider', 'recipient', 'data', 'incident', 'parties', 'permitted purpose'):
            raise ValueError(key + ' must name an entity or purpose rather than repeat a defined role.')
    for key in FLAGS:
        if type(value[key]) is not bool: raise ValueError(key + ' must be true or false.')
    for key, maximum in (('delete_days', 3650), ('notify_hours', 168)):
        if type(value[key]) is not int or not 1 <= value[key] <= maximum:
            raise ValueError(key + ' must be a whole number from 1 to ' + str(maximum) + '.')
    if normalized(value['provider']) == normalized(value['recipient']):
        raise ValueError('Provider and Recipient must be distinct parties.')
    if not any(value[key] for key in FLAGS if key != 'consent_exception'):
        raise ValueError('Select at least one policy term.')
    return value


def draft(terms):
    """Each supported policy term has its own clause and explicit applicability."""
    t = validate_terms(terms)
    clauses, rules = [], []
    def clause(number, text, rule=None):
        if rule:
            labels = {'Provider': t['provider'], 'Recipient': t['recipient'], 'Data': t['data'], 'Permitted Purpose': t['purpose']}
            # Operative clauses contain explicit names; the extraction model
            # need not recover identities from the authoring record.
            text = re.sub(r'\b(?:Permitted Purpose|Provider|Recipient|Data)\b', lambda m: labels[m[0]], text)
        clauses.append(dict(clause_id=number, text=text))
        if rule: rules.append(dict(id='reference:' + number, source=number, **rule))
    def condition(field, value, op='eq'): return dict(field=field, op=op, value=value)
    def rule(kind, action, asset='Data', conditions=()):
        return dict(kind=kind, actor='Recipient', action=action, asset=asset,
                    **({'conditions': list(conditions)} if conditions else {}))
    clause('1', 'Parties and definitions')
    clause('1.1', 'Provider means ' + t['provider'] + '.')
    clause('1.2', 'Recipient means ' + t['recipient'] + '.')
    clause('1.3', 'Data means ' + t['data'] + '.')
    clause('1.4', 'Permitted Purpose means ' + t['purpose'] + '.')
    clause('1.5', 'Parties means Provider and Recipient.')
    if t['notification']:
        clause('1.6', 'Incident means unauthorized access to Data or accidental loss of Data.')
    clause('2', 'Collaboration and administration')
    clause('2.1', 'The Parties intend to collaborate on the Permitted Purpose.')
    clause('2.2', 'Each Party bears its own administrative expenses.')
    if any(t[k] for k in ('read', 'purpose_limit', 'sharing_limit')): clause('3', 'Access, use and sharing')
    if t['read']:
        clause('3.1', 'Recipient may read Data for the Permitted Purpose.',
               rule('permission', 'read', conditions=[condition('purpose', 'Permitted Purpose')]))
    if t['purpose_limit']:
        clause('3.2', 'Recipient must not use Data for any purpose other than the Permitted Purpose.',
               rule('prohibition', 'use', conditions=[condition('purpose', 'Permitted Purpose', 'neq')]))
    if t['sharing_limit']:
        exception = " without Provider's prior written consent" if t['consent_exception'] else ''
        conditions = [condition('recipient', 'third party')]
        if t['consent_exception']: conditions.append(condition('approval', "Provider's prior written consent", 'neq'))
        clause('3.3', 'Recipient must not share Data with any third party' + exception + '.',
               rule('prohibition', 'share', conditions=conditions))
    if t['confidentiality'] or t['secure_storage']: clause('4', 'Information protection')
    if t['confidentiality']:
        clause('4.1', 'Recipient must keep Data confidential.', rule('obligation', 'keepConfidential'))
    if t['secure_storage']:
        clause('4.2', 'Recipient must store Data in a secure manner.',
               rule('obligation', 'store', conditions=[condition('security', 'secure')]))
    if t['deletion'] or t['notification']: clause('5', 'Deletion and incident notification')
    if t['deletion']:
        clause('5.1', f"Recipient must delete Data within {t['delete_days']} days after termination of this Agreement.",
               rule('obligation', 'delete', conditions=[condition('elapsedTime', 'P' + str(t['delete_days']) + 'D', 'lteq'),
                                                      condition('event', 'termination')]))
    if t['notification']:
        clause('5.2', f"Recipient must notify Provider of any Incident within {t['notify_hours']} hours after becoming aware of the Incident.",
               rule('obligation', 'notify', asset='Incident', conditions=[condition('recipient', 'Provider'),
                   condition('elapsedTime', 'PT' + str(t['notify_hours']) + 'H', 'lteq'), condition('event', 'incident awareness')]))
    clause('6', 'General arrangements')
    clause('6.1', 'The Parties will arrange project meetings separately.')
    clause('6.2', 'This Agreement may be executed in counterparts.')
    return dict(template_version=VERSION, title=t['title'], clauses=clauses, rules=rules)


def render_pdf(terms):
    """Use the existing PDF dependency; no reference data is embedded in the PDF."""
    import pymupdf
    content = draft(terms)
    pdf = pymupdf.open()
    page, y = None, 0
    def new_page():
        nonlocal page, y
        page = pdf.new_page(width=595, height=842)
        page.insert_text((54, 41), 'DATA COLLABORATION', fontname='hebo', fontsize=9, color=(.18, .33, .46))
        page.draw_line((54, 52), (541, 52), color=(.8, .85, .9), width=.6)
        y = 78
    def lines(text, size, font):
        result, line = [], ''
        for word in text.split():
            if pymupdf.get_text_length(word, fontname=font, fontsize=size) > 487:
                raise ValueError('A field contains a word too long for the contract page.')
            proposed = (line + ' ' + word).strip()
            if line and pymupdf.get_text_length(proposed, fontname=font, fontsize=size) > 487:
                result.append(line); line = word
            else: line = proposed
        return [*result, line] if line else result
    new_page()
    for line in lines(content['title'], 20, 'hebo'):
        page.insert_text((54, y), line, fontname='hebo', fontsize=20, color=(.10, .19, .27)); y += 26
    y += 13
    for entry in content['clauses']:
        heading = '.' not in entry['clause_id']
        text = entry['clause_id'] + ('  ' if heading else ' ') + entry['text']
        size, font, leading = (12, 'hebo', 17) if heading else (10.5, 'helv', 15.5)
        wrapped = lines(text, size, font)
        needed = len(wrapped) * leading + (45 if heading else 10)
        if y + needed > 764: new_page()
        if heading: y += 12
        for line in wrapped:
            page.insert_text((54, y), line, fontname=font, fontsize=size, color=(.11, .15, .19)); y += leading
        y += 9
    for p in pdf:
        p.insert_text((54, 804), f'Page {p.number + 1} of {len(pdf)}', fontsize=8, color=(.4, .45, .5))
    data = pdf.tobytes(garbage=4, deflate=True, no_new_id=True)
    pdf.close()
    with pymupdf.open(stream=data, filetype='pdf') as check:
        extracted = ' '.join(p.get_text() for p in check)
        # Detect missing glyphs or truncated field values before saving anything.
        for key in ('title', 'provider', 'recipient', 'data', 'purpose'):
            if ' '.join(terms[key].split()) not in ' '.join(extracted.split()):
                raise ValueError('The PDF could not faithfully render field ' + key + '.')
    return data


def create(root, terms):
    terms = validate_terms(terms)
    pdf = render_pdf(terms)
    identifier = 'guided_' + uuid4().hex[:12]
    base = Path(root) / 'contracts' / identifier
    reference = draft(terms)
    reference.update(contract_id=identifier, pdf_sha256=hashlib.sha256(pdf).hexdigest())
    manifest = dict(template_version=VERSION, terms=terms, terms_sha256=digest(terms),
                    pdf_sha256=reference['pdf_sha256'], reference_sha256=digest(reference))
    (base / 'input').mkdir(parents=True, exist_ok=False)
    (base / 'input/contract.pdf').write_bytes(pdf)
    write_json(base / 'authoring.json', manifest)
    write_json(base / 'reference.json', reference)
    return dict(contract=identifier, title=terms['title'], expected_rules=len(reference['rules']))


def load(base):
    base = Path(base)
    if not (base / 'authoring.json').is_file() or not (base / 'reference.json').is_file():
        raise ValueError('This contract has no guided-authoring reference.')
    manifest = json.loads((base / 'authoring.json').read_text(encoding='utf-8'))
    reference = json.loads((base / 'reference.json').read_text(encoding='utf-8'))
    if (manifest['template_version'] not in SUPPORTED_VERSIONS or reference['template_version'] != manifest['template_version']
            or digest(manifest['terms']) != manifest['terms_sha256']
            or digest(reference) != manifest['reference_sha256']
            or hashlib.sha256((base / 'input/contract.pdf').read_bytes()).hexdigest() != manifest['pdf_sha256']
            or reference['pdf_sha256'] != manifest['pdf_sha256']):
        raise ValueError('Guided terms, reference or source PDF changed; create a new contract revision.')
    if reference['contract_id'] != base.name:
        raise ValueError('Guided reference belongs to another contract.')
    return manifest, reference


def canonical(policy, terms):
    """Explicit template/definition aliases only. Unknown wording stays unmatched."""
    policy = deepcopy(policy)
    def key(text): return re.sub(r'^the\s+', '', normalized(text)).strip(' .')
    parties = {key(terms['provider']): 'Provider', 'provider': 'Provider',
               key(terms['recipient']): 'Recipient', 'recipient': 'Recipient'}
    assets = {key(terms['data']): 'Data', 'data': 'Data',
              'incident': 'Incident', 'incidents': 'Incident', 'any incident': 'Incident', 'any incidents': 'Incident'}
    purposes = {key(terms['purpose']), 'for ' + key(terms['purpose']), 'permitted purpose', 'for the permitted purpose', 'for permitted purpose'}
    consents = {"provider's prior written consent", "the provider's prior written consent", 'prior written consent of provider',
                key(terms['provider'] + "'s prior written consent"), 'prior written consent of ' + key(terms['provider'])}
    events = {'termination of this agreement': 'termination', 'termination of the agreement': 'termination',
              'after termination of this agreement': 'termination', 'upon termination of this agreement': 'termination',
              'termination': 'termination', 'becoming aware of the incident': 'incident awareness',
              'after becoming aware of the incident': 'incident awareness', 'awareness of the incident': 'incident awareness',
              'incident awareness': 'incident awareness', 'becoming aware of an incident': 'incident awareness'}
    def condition(entry):
        for group in ('all', 'any', 'exactly_one'):
            if group in entry: return {group: [condition(c) for c in entry[group]]}
        result = deepcopy(entry)
        field = result['field'].casefold()
        value = result.get('value')
        def mapped(v):
            if not isinstance(v, str): return v
            label = key(v)
            if field in ('purpose', 'scope') and label in purposes: return 'Permitted Purpose'
            if field == 'recipient' and label in ('third party', 'a third party', 'any third party', 'third parties', 'any third parties'):
                return 'third party'
            if field == 'recipient': return parties.get(label, v)
            if field == 'approval' and label in consents: return "Provider's prior written consent"
            if field == 'security' and label in ('secure', 'securely', 'in a secure manner', 'secure manner', 'secure storage'):
                return 'secure'
            if field in ('event', 'timing') and label in events: return events[label]
            return v
        if isinstance(value, list): result['value'] = [mapped(v) for v in value]
        elif 'value' in result: result['value'] = mapped(value)
        if field == 'scope' and result.get('value') == 'Permitted Purpose': result['field'] = 'purpose'
        if field == 'timing' and result.get('value') in ('termination', 'incident awareness'): result['field'] = 'event'
        scalar = result.get('value')
        inferred = ('duration' if field == 'elapsedtime' and isinstance(scalar, str) and re.fullmatch(r'P[\dYMDTHS.]+', scalar, re.I)
                    else 'boolean' if type(scalar) is bool else 'number' if type(scalar) in (int, float)
                    else 'string' if isinstance(scalar, str) else None)
        if inferred and result.get('type') == inferred: result.pop('type')
        return result
    def rule(entry):
        for field in ('actor', 'asset', 'issuer'):
            if field in entry:
                bindings = assets if field == 'asset' else parties
                entry[field] = bindings.get(key(entry[field]), entry[field])
        entry['action'] = {'inform': 'notify', 'keepconfidential': 'keepConfidential'}.get(entry['action'].casefold(), entry['action'])
        entry['conditions'] = [condition(c) for c in entry.get('conditions', [])]
        # An explicitly identified Provider issuer is compatible with this
        # template's access right; an incorrect issuer remains a mismatch.
        if entry.get('kind') == 'permission' and entry.get('issuer') == 'Provider': entry.pop('issuer')
        for field in ('requires', 'remedies'):
            for child in entry.get(field, []): rule(child)
    for entry in policy['rules']: rule(entry)
    return policy


def evaluate(base, policy, chunks, root):
    manifest, reference = load(base)
    terms = manifest['terms']
    expected_rules = reference['rules']
    expected_sources = {c['clause_id']: dict(chunk_id='authored:' + c['clause_id'], clause_id=c['clause_id'],
        text=c['clause_id'] + ' ' + c['text']) for c in reference['clauses'] if any(r['source'] == c['clause_id'] for r in expected_rules)}
    expected = dict(version='2.0', contract_id=reference['contract_id'], id='reference', type='Set',
                    rules=expected_rules, sources=expected_sources)
    actual = policy or dict(expected, rules=[], sources={})
    validate_policy(actual, root)
    if actual['contract_id'] != reference['contract_id']: raise ValueError('Wrong policy contract.')
    if policy is not None and policy.get('document', {}).get('sha256') != manifest['pdf_sha256']:
        raise ValueError('The policy does not identify this frozen source PDF.')
    authored_text = {c['clause_id']: normalized(c['clause_id'] + ' ' + c['text']) for c in reference['clauses']}
    original = {c['chunk_id']: c for c in chunks}
    for chunk in chunks:
        cid = chunk.get('clause_id')
        if cid in authored_text and authored_text[cid] not in normalized(chunk['text']):
            raise ValueError('Segmented clause differs from the frozen authored document: ' + cid)
    verify_sources(actual, chunks)
    expected, actual = canonical(expected, terms), canonical(actual, terms)
    clause_ids = [r['source'] for r in expected_rules]
    actual_clauses = {r['id']: original[actual['sources'][r['source']]['chunk_id']].get('clause_id') for r in actual['rules']}
    comparisons, matched = [], 0
    for clause in dict.fromkeys([*clause_ids, *actual_clauses.values()]):
        left = dict(expected, rules=[r for r in expected['rules'] if r['source'] == clause])
        right = dict(actual, rules=[r for r in actual['rules'] if actual_clauses[r['id']] == clause])
        compared = component_comparison(left, right)
        matched += compared['matched_rules']
        comparisons.append(dict(clause_id=clause, expected=len(left['rules']), extracted=len(right['rules']), **compared))
    predicted, count = len(actual['rules']), len(expected_rules)
    precision, recall = matched / predicted if predicted else 0, matched / count if count else 0
    routing = {c.get('clause_id') for c in chunks
               if c.get('classification', {}).get('label') in ('RELEVANT', 'NEEDS_CONTEXT')}
    probes = []
    def odrl_condition(c):
        for a, b in (('all', 'and'), ('any', 'or'), ('exactly_one', 'xone')):
            if a in c: return {b: [odrl_condition(child) for child in c[a]]}
        return dict(leftOperand=c['field'], operator=c['op'], **({'rightOperandReference': c['parameter']} if 'parameter' in c else {'rightOperand': c['value']}))
    scenarios = []
    if terms['purpose_limit']:
        scenarios += [('3.2', 'Permitted purpose', dict(purpose='Permitted Purpose'), False),
                      ('3.2', 'Other purpose', dict(purpose='other purpose'), True)]
    if terms['sharing_limit']:
        scenarios += [('3.3', 'Third party without consent', dict(recipient='third party', approval='absent'), True),
                      ('3.3', 'Internal recipient', dict(recipient='Recipient', approval='absent'), False)]
        scenarios.append(('3.3', 'Third party with consent', dict(recipient='third party', approval="Provider's prior written consent"), not terms['consent_exception']))
    for clause, label, state, expected_result in scenarios:
        bans = [r for r in actual['rules'] if actual_clauses[r['id']] == clause and r['kind'] == 'prohibition'
                and r['actor'] == 'Recipient' and r['asset'] == 'Data' and r['action'] == ('use' if clause == '3.2' else 'share')]
        values = [constraint_truth([odrl_condition(c) for c in r.get('conditions', [])], state) for r in bans]
        result = True if any(v is True for v in values) else None if not values or any(v is None for v in values) else False
        probes.append(dict(clause_id=clause, scenario=label, expected_prohibition_applies=expected_result,
                           actual_prohibition_applies=result, passed=result is expected_result))
    # These test satisfaction of the encoded deadline, not whether an overdue
    # obligation disappears. The source clock starts at the named event.
    for enabled, clause, action, value, suffix, event in (
            (terms['deletion'], '5.1', 'delete', terms['delete_days'], 'D', 'termination'),
            (terms['notification'], '5.2', 'notify', terms['notify_hours'], 'H', 'incident awareness')):
        if not enabled: continue
        obligations = [r for r in actual['rules'] if actual_clauses[r['id']] == clause and r['kind'] == 'obligation'
                       and r['actor'] == 'Recipient' and r['action'] == action
                       and r['asset'] == ('Data' if action == 'delete' else 'Incident')]
        for amount, expected_result, label in ((value, True, 'At deadline'), (value + 1, False, 'Beyond deadline')):
            duration = ('P' if suffix == 'D' else 'PT') + str(amount) + suffix
            state = dict(elapsedTime=duration, event=event, recipient='Provider')
            values = [constraint_truth([odrl_condition(c) for c in r.get('conditions', [])], state) for r in obligations]
            result = False if any(v is False for v in values) else None if not values or any(v is None for v in values) else True
            probes.append(dict(clause_id=clause, scenario=label + ' / ' + action, criterion='deadline satisfaction',
                               expected_satisfied=expected_result, actual_satisfied=result, passed=result is expected_result))
    return dict(protocol='guided_source_reference_v1', scope='Generated contracts only; template meanings and aliases, not arbitrary-contract legal accuracy.',
        complete=True, contract_id=reference['contract_id'], template_version=reference['template_version'],
        terms_sha256=manifest['terms_sha256'], pdf_sha256=manifest['pdf_sha256'], reference_sha256=manifest['reference_sha256'],
        expected_rules=count, extracted_rules=predicted, matched_rules=matched, precision=precision, recall=recall,
        f1=2 * precision * recall / (precision + recall) if precision + recall else 0,
        complete_policy=matched == count == predicted, routed_operative_clauses=sum(c in routing for c in clause_ids),
        expected_operative_clauses=len(clause_ids), clauses=comparisons, behavior_checks=probes)
