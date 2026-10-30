"""Data Contract Template (DCT): authoring and a separate source-bound reference.

No extraction stage imports this module or reads its saved authoring/reference
files. Only the rendered PDF enters stages 1.1 through 2.
"""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from datetime import date, timedelta
from uuid import uuid4

from xfed.common import constraint_truth, write_json
from xfed.policy import validate_policy, verify_sources
from xfed.policy_evaluation import component_comparison, normalized


VERSION = 'dct-3'
SUPPORTED_VERSIONS = ('guided-1', 'guided-2', 'guided-3', 'dct-1', 'dct-2', VERSION)
STORAGE_COUNTRIES = ('Norway', 'Sweden', 'Denmark', 'Finland', 'Iceland', 'Germany', 'France', 'Netherlands')
DEFAULTS = dict(title='Data Collaboration Agreement', provider='North Research Institute',
    recipient='Harbour Analytics', data='Research Dataset', purpose='joint research',
    read=True, purpose_limit=True, sharing_limit=True, consent_exception=True,
    confidentiality=True, secure_storage=True, deletion=True, delete_days=30,
    notification=True, notify_hours=72, encryption=False, storage_limit=False,
    storage_countries=['Norway', 'Sweden'], legal_disclosure=False, legal_retention=False)
ADVANCED = ('encryption', 'storage_limit', 'storage_countries', 'legal_disclosure', 'legal_retention')
PROJECT_DEFAULTS = dict(competition_limit=False, platform_access=False, records_access=False,
    background_access=False, publication=False, platform='Project Data Portal', records='Project Processing Records',
    background='Background Research Materials', results='Aggregate Research Results', work_plan='Agreed Research Work Plan',
    project_start='2026-11-01', project_end='2027-10-31', publication_notice_days=30, publication_delay_days=90)
DEFAULTS.update(PROJECT_DEFAULTS)
STEWARDSHIP_DEFAULTS = dict(authorized_access=False, no_reidentification=False, attribution=False,
    error_reporting=False, deletion_certificate=False, provider_delivery=False,
    authorized_users='Approved Project Researchers', documentation='Dataset Documentation', certificate='Data Deletion Certificate',
    delivery_days=5, certificate_days=7, error_hours=24, data_description='')
DEFAULTS.update(STEWARDSHIP_DEFAULTS)
FLAGS = ('read', 'purpose_limit', 'sharing_limit', 'consent_exception', 'confidentiality',
         'secure_storage', 'deletion', 'notification', 'encryption', 'storage_limit',
         'legal_disclosure', 'legal_retention', 'competition_limit', 'platform_access', 'records_access', 'background_access', 'publication',
         'authorized_access', 'no_reidentification', 'attribution', 'error_reporting', 'deletion_certificate', 'provider_delivery')
RESEARCH_PRESET = dict(DEFAULTS, **{key: True for key in FLAGS},
    title='North Sea Energy Research Data Sharing Agreement', provider='Fjord Energy Research Institute', recipient='Bergen Applied Analytics',
    data='Offshore Energy Research Dataset', purpose='offshore energy efficiency and safety research',
    platform='North Sea Research Portal', records='Research Processing Audit Records', background='Legacy Turbine Research Materials',
    results='Aggregate Energy Research Results', work_plan='Offshore Research Work Plan',
    authorized_users='Approved Energy Project Researchers', documentation='Energy Dataset Documentation',
    data_description='Pseudonymised turbine-performance, maintenance and personnel-occupancy observations from the North Sea research programme, together with the associated measurement metadata.',
    delete_days=14, notify_hours=48)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_terms(value):
    previous_fields = set(DEFAULTS) - set(STEWARDSHIP_DEFAULTS)
    legacy_fields = previous_fields - set(PROJECT_DEFAULTS)
    if isinstance(value, dict) and set(value) in (previous_fields, legacy_fields, legacy_fields - set(ADVANCED)):
        value = dict(DEFAULTS, **value)  # Saved v1/v2 forms remain usable.
    if not isinstance(value, dict) or set(value) != set(DEFAULTS):
        raise ValueError('Provide all DCT fields and no additional fields.')
    value = deepcopy(value)
    for key in ('title', 'provider', 'recipient', 'data', 'purpose', 'platform', 'records', 'background', 'results', 'work_plan',
                'authorized_users', 'documentation', 'certificate'):
        text = value[key]
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 120 or any(ord(c) < 32 for c in text):
            raise ValueError(key + ' must be one line containing 1 to 120 printable characters.')
        if re.search(r'\b(?:Provider|Recipient|Parties)\s+(?:shall|must|may|will|can)\b', text, re.I):
            raise ValueError(key + ' must be a label, not an additional contract clause.')
        value[key] = ' '.join(text.split())
        if key != 'title' and normalized(value[key]) in ('provider', 'recipient', 'data', 'incident', 'parties', 'permitted purpose',
                'platform', 'records', 'background', 'results', 'work plan', 'security requirements', 'confidential information',
                'authorized users', 'documentation', 'certificate', 'error'):
            raise ValueError(key + ' must name an entity or purpose rather than repeat a defined role.')
    description = value['data_description']
    if (not isinstance(description, str) or len(description) > 600 or any(ord(c) < 32 for c in description)
            or re.search(r'\b(?:shall|must|may|will)\b', description, re.I)):
        raise ValueError('Dataset description must be one descriptive line of at most 600 characters, without additional rules.')
    value['data_description'] = ' '.join(description.split())
    for key in FLAGS:
        if type(value[key]) is not bool: raise ValueError(key + ' must be true or false.')
    countries = value['storage_countries']
    if (not isinstance(countries, list) or not 1 <= len(countries) <= 4
            or any(not isinstance(c, str) or c not in STORAGE_COUNTRIES for c in countries)
            or len(set(countries)) != len(countries)):
        raise ValueError('Choose 1 to 4 distinct storage countries from: ' + ', '.join(STORAGE_COUNTRIES) + '.')
    for key, maximum in (('delete_days', 3650), ('notify_hours', 168), ('publication_notice_days', 365), ('publication_delay_days', 3650),
                         ('delivery_days', 365), ('certificate_days', 365), ('error_hours', 168)):
        if type(value[key]) is not int or not 1 <= value[key] <= maximum:
            raise ValueError(key + ' must be a whole number from 1 to ' + str(maximum) + '.')
    for key in ('project_start', 'project_end'):
        try:
            if not isinstance(value[key], str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value[key]): raise ValueError()
            date.fromisoformat(value[key])
        except ValueError:
            raise ValueError(key + ' must be a valid YYYY-MM-DD date.') from None
    if value['project_end'] < value['project_start']: raise ValueError('Project end must be on or after project start.')
    active_assets = ['data', *[field for field, enabled in (('platform', value['platform_access']), ('records', value['records_access']),
        ('background', value['background_access']), ('results', value['publication'] or value['attribution']),
        ('documentation', value['provider_delivery'] or value['error_reporting']), ('certificate', value['deletion_certificate'])) if enabled]]
    if len({normalized(value[k]) for k in active_assets}) != len(active_assets):
        raise ValueError('Selected resources must have distinct names.')
    if normalized(value['provider']) == normalized(value['recipient']):
        raise ValueError('Provider and Recipient must be distinct parties.')
    if value['authorized_access'] and normalized(value['authorized_users']) in {normalized(value[k]) for k in ('provider', 'recipient')}:
        raise ValueError('The authorised researcher group must have a distinct name from Provider and Recipient.')
    if not any(value[key] for key in FLAGS if key not in ('consent_exception', 'legal_disclosure', 'legal_retention')):
        raise ValueError('Select at least one policy term.')
    return value


def draft(terms):
    """Each supported policy term has its own clause and explicit applicability."""
    t = validate_terms(terms)
    clauses, rules, classifications = [], [], {}
    def clause(number, text, rule=None, relevant=None):
        if rule:
            labels = {'Provider': t['provider'], 'Recipient': t['recipient'], 'Data': t['data'], 'Permitted Purpose': t['purpose'],
                      'Platform': t['platform'], 'Records': t['records'], 'Background': t['background'], 'Results': t['results'], 'Work Plan': t['work_plan'],
                      'Authorized Users': t['authorized_users'], 'Documentation': t['documentation'], 'Certificate': t['certificate']}
            # Operative clauses contain explicit names; the extraction model
            # need not recover identities from the authoring record.
            text = re.sub(r'\b(?:' + '|'.join(sorted(map(re.escape, labels), key=len, reverse=True)) + r')\b', lambda m: labels[m[0]], text)
        clauses.append(dict(clause_id=number, text=text))
        if relevant is None: relevant = bool(rule) or number.startswith('1.') and number != '1.5'
        classifications[number] = dict(label='RELEVANT' if relevant else 'NOT_RELEVANT',
            rationale=('Creates an information-handling rule.' if rule else 'Defines the scope or meaning of information-handling terms.' if relevant
                       else 'Heading or general administration without a material information-handling term.'))
        if rule: rules.append(dict(id='reference:' + number, source=number, **rule))
    def condition(field, value, op='eq'): return dict(field=field, op=op, value=value)
    def rule(kind, action, asset='Data', conditions=(), actor='Recipient'):
        return dict(kind=kind, actor=actor, action=action, asset=asset,
                    **({'conditions': list(conditions)} if conditions else {}))
    clause('1', 'Parties and definitions')
    clause('1.1', 'Provider means ' + t['provider'] + '.')
    clause('1.2', 'Recipient means ' + t['recipient'] + '.')
    clause('1.3', 'Data means ' + t['data'] + '.' + (' The dataset comprises ' + t['data_description'].rstrip('.') + '.' if t['data_description'] else ''))
    clause('1.4', 'Permitted Purpose means ' + t['purpose'] + '.')
    clause('1.5', 'Parties means Provider and Recipient.')
    if t['notification']:
        clause('1.6', 'Incident means unauthorized access to Data or accidental loss of Data.')
    if t['background_access'] or t['publication']:
        clause('1.7', 'Project Period means the inclusive period from ' + t['project_start'] + ' to ' + t['project_end'] + '.')
    if t['background_access']: clause('1.8', 'Work Plan means ' + t['work_plan'] + '.')
    if t['platform_access']:
        clause('1.9', 'Platform means ' + t['platform'] + '.')
        clause('1.10', 'Security Requirements means multi-factor authentication and an approved project account, assessed together as a continuing access requirement.')
    if t['records_access']: clause('1.11', 'Records means ' + t['records'] + '.')
    if t['background_access']: clause('1.12', 'Background means ' + t['background'] + '.')
    if t['publication'] or t['attribution']:
        clause('1.13', 'Results means ' + t['results'] + '.')
    if t['publication']:
        clause('1.14', 'Confidential Information means Data and project information marked confidential by Provider.')
    if t['authorized_access']: clause('1.15', 'Authorized Users means ' + t['authorized_users'] + ', limited to Recipient personnel registered for this Project and bound by its confidentiality requirements.')
    if t['provider_delivery'] or t['error_reporting']: clause('1.16', 'Documentation means ' + t['documentation'] + ', comprising the data dictionary and processing notes accompanying Data.')
    if t['deletion_certificate']: clause('1.17', 'Certificate means ' + t['certificate'] + ', a signed record of the copies deleted and any copies retained under a legal retention requirement.')
    if t['error_reporting']: clause('1.18', 'Error means a material inaccuracy discovered in Data or Documentation.')
    clause('2', 'Collaboration and administration')
    clause('2.1', 'The Parties intend to collaborate on the Permitted Purpose.', relevant=True)
    clause('2.2', 'Each Party bears its own administrative expenses.')
    if any(t[k] for k in ('read', 'purpose_limit', 'sharing_limit', 'competition_limit')): clause('3', 'Access, use and sharing')
    if t['read']:
        clause('3.1', 'Recipient may read Data for Permitted Purpose.',
               rule('permission', 'read', conditions=[condition('purpose', 'Permitted Purpose')]))
    if t['purpose_limit']:
        clause('3.2', 'Recipient must not use Data for any purpose other than Permitted Purpose.',
               rule('prohibition', 'use', conditions=[condition('purpose', 'Permitted Purpose', 'neq')]))
    if t['sharing_limit']:
        exception = " without Provider's prior written consent" if t['consent_exception'] else ''
        if t['legal_disclosure']: exception += ', unless disclosure is required by law'
        conditions = [condition('recipient', 'third party')]
        if t['consent_exception']: conditions.append(condition('approval', "Provider's prior written consent", 'neq'))
        if t['legal_disclosure']: conditions.append(condition('compliance', 'disclosure required by law', 'neq'))
        clause('3.3', 'Recipient must not share Data with any third party' + exception + '.',
               rule('prohibition', 'share', conditions=conditions))
    if t['competition_limit']:
        clause('3.4', 'Recipient must not use Data for developing competing products.',
               rule('prohibition', 'use', conditions=[condition('purpose', 'developing competing products')]))
    if any(t[k] for k in ('confidentiality', 'secure_storage', 'encryption', 'storage_limit')):
        clause('4', 'Information protection')
    if t['confidentiality']:
        clause('4.1', 'Recipient must keep Data confidential.', rule('obligation', 'keepConfidential'))
    if t['secure_storage']:
        clause('4.2', 'Recipient must store Data in a secure manner.',
               rule('obligation', 'store', conditions=[condition('security', 'secure')]))
    if t['encryption']:
        clause('4.3', 'Recipient must encrypt Data using AES-256.',
               rule('obligation', 'encrypt', conditions=[condition('security', 'AES-256')]))
    if t['storage_limit']:
        countries = ' or '.join(t['storage_countries'])
        clause('4.4', 'Recipient must not store Data in any country other than ' + countries + '.',
               rule('prohibition', 'store', conditions=[condition('spatial', t['storage_countries'], 'isNoneOf')]))
    if t['deletion'] or t['notification']: clause('5', 'Deletion and incident notification')
    if t['deletion']:
        conditions = [condition('elapsedTime', 'P' + str(t['delete_days']) + 'D', 'lteq'), condition('event', 'termination')]
        exception = ''
        if t['legal_retention']:
            exception = ', unless retention is required by law'
            conditions.append(condition('compliance', 'retention required by law', 'neq'))
        clause('5.1', f"Recipient must delete Data within {t['delete_days']} days after termination of this Agreement{exception}.",
               rule('obligation', 'delete', conditions=conditions))
    if t['notification']:
        clause('5.2', f"Recipient must notify Provider of any Incident within {t['notify_hours']} hours after becoming aware of the Incident.",
               rule('obligation', 'notify', asset='Incident', conditions=[condition('recipient', 'Provider'),
                   condition('elapsedTime', 'PT' + str(t['notify_hours']) + 'H', 'lteq'), condition('event', 'incident awareness')]))
    if t['platform_access']:
        clause('6', 'Controlled platform access')
        clause('6.1', "Provider may grant Recipient direct electronic access to Platform at Provider's sole discretion, provided Recipient meets and continues to meet the Security Requirements.",
               rule('permission', 'provideAccess', 'Platform', actor='Provider', conditions=[condition('recipient', 'Recipient'),
                    condition('discretion', "Provider's sole discretion"), condition('security', 'Security Requirements'),
                    condition('scope', 'direct electronic access')]))
        clause('6.2', 'Recipient must not read Platform if Recipient does not meet the Security Requirements.',
               rule('prohibition', 'read', 'Platform', conditions=[condition('security', 'Security Requirements', 'neq')]))
    if t['records_access']:
        clause('7', 'Processing records and audit access')
        scope = 'for all processing of ' + t['data'] + ' performed under this Agreement'
        clause('7.1', 'Recipient must maintain complete Records for all processing of Data performed under this Agreement.',
               rule('obligation', 'maintain', 'Records', conditions=[condition('completeness', 'complete'), condition('scope', scope)]))
        clause('7.2', 'Recipient must make the Records described in clause 7.1 available to Provider upon request.',
               rule('obligation', 'provideAccess', 'Records', conditions=[condition('recipient', 'Provider'), condition('event', 'request'), condition('scope', scope)]))
    if t['background_access']:
        clause('8', 'Background materials and work scope')
        clause('8.1', f"From {t['project_start']} through {t['project_end']}, Recipient may read Background at no charge, to the extent necessary for its own work in accordance with the Work Plan.",
               rule('permission', 'read', 'Background', conditions=[condition('dateTime', t['project_start'], 'gteq'),
                    condition('dateTime', t['project_end'], 'lteq'), condition('payAmount', 0),
                    condition('scope', 'necessary for its own work'), condition('compliance', 'Work Plan')]))
        clause('8.2', "Recipient must only use Background for Permitted Purpose or with Provider's prior written consent.",
               rule('prohibition', 'use', 'Background', conditions=[condition('purpose', 'Permitted Purpose', 'neq'),
                    condition('approval', "Provider's prior written consent", 'neq')]))
    if t['publication']:
        clause('9', 'Publication notice, objections and delay')
        clause('9.1', 'Recipient must notify Provider of Results before publication.',
               rule('obligation', 'notify', 'Results', conditions=[condition('recipient', 'Provider'), condition('timing', 'before publication')]))
        clause('9.2', f"Provider may request a delay of publication of Results of no more than {t['publication_delay_days']} days, within {t['publication_notice_days']} days after the publication notice is issued.",
               rule('permission', 'requestDelay', 'Results', actor='Provider', conditions=[condition('postponementDuration', 'P'+str(t['publication_delay_days'])+'D', 'lteq'),
                    condition('elapsedTime', 'P'+str(t['publication_notice_days'])+'D', 'lteq'), condition('event', 'publication notice')]))
        clause('9.3', f"Once {t['publication_notice_days']} days have elapsed since the publication notice was issued, Recipient may publish Results if Provider has confirmed no objection and no Confidential Information is disclosed.",
               rule('permission', 'publish', 'Results', conditions=[condition('elapsedTime', 'P'+str(t['publication_notice_days'])+'D', 'gteq'),
                    condition('event', 'publication notice'), condition('approval', 'Provider confirmation of no objection'),
                    condition('compliance', 'no Confidential Information disclosed')]))
        clause('9.4', f"Once {t['publication_delay_days']} days have elapsed since Provider's objection, Recipient may publish Results if Provider's objections have been addressed and no Confidential Information is disclosed.",
               rule('permission', 'publish', 'Results', conditions=[condition('elapsedTime', 'P'+str(t['publication_delay_days'])+'D', 'gteq'),
                    condition('event', "Provider's objection"), condition('approval', "Provider's objections addressed"),
                    condition('compliance', 'no Confidential Information disclosed')]))
    if any(t[k] for k in ('authorized_access', 'no_reidentification', 'attribution', 'error_reporting', 'deletion_certificate', 'provider_delivery')):
        clause('10', 'Delivery, responsible use and project close-out')
    if t['authorized_access']:
        clause('10.1', 'Recipient must not provide access to Data to anyone other than Authorized Users.',
               rule('prohibition', 'provideAccess', conditions=[condition('recipient', 'Authorized Users', 'neq')]))
    if t['no_reidentification']:
        clause('10.2', 'Recipient must not reidentify individuals from Data.', rule('prohibition', 'reidentify'))
    if t['attribution']:
        clause('10.3', 'Recipient must attribute Provider when publishing Results.',
               rule('obligation', 'attribute', 'Results', conditions=[condition('recipient', 'Provider'), condition('timing', 'when publishing')]))
    if t['error_reporting']:
        clause('10.4', f"Recipient must notify Provider of any Error within {t['error_hours']} hours after discovering the Error.",
               rule('obligation', 'notify', 'Error', conditions=[condition('recipient', 'Provider'), condition('elapsedTime', 'PT'+str(t['error_hours'])+'H', 'lteq'), condition('event', 'error discovery')]))
    if t['deletion_certificate']:
        clause('10.5', f"Recipient must submit Certificate to Provider within {t['certificate_days']} days after deletion of Data.",
               rule('obligation', 'submit', 'Certificate', conditions=[condition('recipient', 'Provider'), condition('elapsedTime', 'P'+str(t['certificate_days'])+'D', 'lteq'), condition('event', 'data deletion')]))
    if t['provider_delivery']:
        for number, asset in (('10.6', 'Data'), ('10.7', 'Documentation')):
            clause(number, f"Provider must share {asset} with Recipient within {t['delivery_days']} days after this Agreement is signed.",
                   rule('obligation', 'share', asset, actor='Provider', conditions=[condition('recipient', 'Recipient'),
                       condition('elapsedTime', 'P'+str(t['delivery_days'])+'D', 'lteq'), condition('event', 'agreement signature')]))
    clause('11', 'Ownership and general arrangements')
    clause('11.1', 'Each Party retains ownership of the materials it contributes. This Agreement transfers no ownership of Data'
           + (' or Background.' if t['background_access'] else '.'), relevant=True)
    clause('11.2', 'The Parties will arrange project meetings separately.')
    clause('11.3', 'This Agreement is governed by Norwegian law. Disputes are submitted to the courts of Oslo.')
    clause('11.4', 'Neither Party warrants the commercial viability of the research project.')
    clause('11.5', 'This Agreement may be executed in counterparts.')
    return dict(template_version=VERSION, title=t['title'], clauses=clauses, rules=rules, classification_terms=classifications)


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
    identifier = 'dct_' + uuid4().hex[:12]
    base = Path(root) / 'contracts' / identifier
    reference = draft(terms)
    reference.update(contract_id=identifier, pdf_sha256=hashlib.sha256(pdf).hexdigest())
    (base / 'input').mkdir(parents=True, exist_ok=False)
    (base / 'input/contract.pdf').write_bytes(pdf)
    from xfed.extraction import extract_document
    from xfed.segmentation import segment
    chunks = segment(extract_document(base / 'input/contract.pdf', identifier, root), root)['chunks']
    authored = {c['clause_id']: c for c in reference['clauses']}
    classification = []
    for chunk in chunks:
        cid, text = chunk.get('clause_id'), normalized(chunk['text'])
        if cid in authored:
            if text != normalized(cid + ' ' + authored[cid]['text']):
                raise ValueError('DCT segmentation changed the authored clause: ' + cid)
            gold = reference['classification_terms'][cid]
        elif text in (normalized(terms['title']), 'data collaboration') or re.fullmatch(r'page \d+ of \d+', text):
            gold = dict(label='NOT_RELEVANT', rationale='Document title, running header or page number.')
        else:
            raise ValueError('DCT classification reference has an unmapped PDF chunk: ' + chunk['chunk_id'])
        classification.append(dict(chunk=deepcopy(chunk), **gold))
    if {c.get('clause_id') for c in chunks} - {None} != set(authored):
        raise ValueError('DCT segmentation must retain every authored clause exactly once.')
    reference['classification'] = classification
    reference.pop('classification_terms')
    manifest = dict(template_version=VERSION, terms=terms, terms_sha256=digest(terms),
                    pdf_sha256=reference['pdf_sha256'], reference_sha256=digest(reference))
    write_json(base / 'authoring.json', manifest)
    write_json(base / 'reference.json', reference)
    return dict(contract=identifier, title=terms['title'], expected_rules=len(reference['rules']))


def load(base):
    base = Path(base)
    if not (base / 'authoring.json').is_file() or not (base / 'reference.json').is_file():
        raise ValueError('This contract has no DCT authoring reference.')
    manifest = json.loads((base / 'authoring.json').read_text(encoding='utf-8'))
    reference = json.loads((base / 'reference.json').read_text(encoding='utf-8'))
    if (manifest['template_version'] not in SUPPORTED_VERSIONS or reference['template_version'] != manifest['template_version']
            or digest(manifest['terms']) != manifest['terms_sha256']
            or digest(reference) != manifest['reference_sha256']
            or hashlib.sha256((base / 'input/contract.pdf').read_bytes()).hexdigest() != manifest['pdf_sha256']
            or reference['pdf_sha256'] != manifest['pdf_sha256']):
        raise ValueError('DCT terms, reference or source PDF changed; create a new contract revision.')
    if reference['contract_id'] != base.name:
        raise ValueError('DCT reference belongs to another contract.')
    return manifest, reference


def canonical(policy, terms):
    """Explicit template/definition aliases only. Unknown wording stays unmatched."""
    policy = deepcopy(policy)
    def key(text): return re.sub(r'^the\s+', '', normalized(text)).strip(' .')
    parties = {key(terms['provider']): 'Provider', 'provider': 'Provider',
               key(terms['recipient']): 'Recipient', 'recipient': 'Recipient'}
    if terms.get('authorized_access'):
        parties.update({key(terms['authorized_users']): 'Authorized Users', 'authorized users': 'Authorized Users'})
    assets = {key(terms['data']): 'Data', 'data': 'Data',
              'incident': 'Incident', 'incidents': 'Incident', 'any incident': 'Incident', 'any incidents': 'Incident'}
    if terms.get('error_reporting'): assets.update({'error': 'Error', 'any error': 'Error', 'errors': 'Error'})
    for field, flag in (('platform', 'platform_access'), ('records', 'records_access'), ('background', 'background_access'),
                        ('results', 'publication'), ('results', 'attribution'), ('documentation', 'provider_delivery'), ('certificate', 'deletion_certificate')):
        if terms.get(flag): assets.update({field: field.title(), key(terms[field]): field.title()})
    purposes = {key(terms['purpose']), 'for ' + key(terms['purpose']), 'permitted purpose', 'for the permitted purpose', 'for permitted purpose'}
    consents = {"provider's prior written consent", "the provider's prior written consent", 'prior written consent of provider',
                key(terms['provider'] + "'s prior written consent"), 'prior written consent of ' + key(terms['provider'])}
    events = {'termination of this agreement': 'termination', 'termination of the agreement': 'termination',
              'after termination of this agreement': 'termination', 'upon termination of this agreement': 'termination',
              'termination': 'termination', 'becoming aware of the incident': 'incident awareness',
              'after becoming aware of the incident': 'incident awareness', 'awareness of the incident': 'incident awareness',
              'incident awareness': 'incident awareness', 'becoming aware of an incident': 'incident awareness'}
    events.update({phrase: 'request' for phrase in ('request', 'upon request', "provider's request", key(terms['provider']+"'s request"))})
    events.update({phrase: 'publication notice' for phrase in ('publication notice', 'the publication notice is issued',
        'publication notice is issued', 'publication notice was issued', 'the publication notice was issued', 'publication notice issued')})
    events.update({phrase: "Provider's objection" for phrase in ("provider's objection", key(terms['provider']+"'s objection"))})
    events.update({phrase: 'error discovery' for phrase in ('error discovery', 'discovering the error', 'after discovering the error')})
    events.update({phrase: 'data deletion' for phrase in ('data deletion', 'deletion of data', key('deletion of '+terms['data']))})
    events.update({phrase: 'agreement signature' for phrase in ('agreement signature', 'this agreement is signed', 'the agreement is signed', 'signature of this agreement')})
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
            if field == 'purpose' and label in ('developing competing products', 'for developing competing products'):
                return 'developing competing products'
            if field == 'recipient' and label in ('third party', 'a third party', 'any third party', 'third parties', 'any third parties'):
                return 'third party'
            if field == 'recipient': return parties.get(label, v)
            if field == 'approval' and label in consents: return "Provider's prior written consent"
            if field == 'approval' and label in ('provider confirmation of no objection', 'provider has confirmed no objection',
                    key(terms['provider']+' confirmation of no objection'), key(terms['provider']+' has confirmed no objection')):
                return 'Provider confirmation of no objection'
            if field == 'approval' and label in ("provider's objections addressed", "provider's objections have been addressed",
                    key(terms['provider']+"'s objections addressed"), key(terms['provider']+"'s objections have been addressed")):
                return "Provider's objections addressed"
            if field == 'discretion' and label in ("provider's sole discretion", key(terms['provider']+"'s sole discretion"),
                    'sole discretion of '+key(terms['provider'])): return "Provider's sole discretion"
            if field in ('security', 'compliance') and label in ('security requirements', 'the security requirements',
                    'meets and continues to meet the security requirements', 'meeting and continuing to meet the security requirements'):
                return 'Security Requirements'
            if field == 'compliance' and label in ('work plan', 'in accordance with the work plan', key(terms['work_plan']),
                    'in accordance with '+key(terms['work_plan']), 'in accordance with the '+key(terms['work_plan'])):
                return 'Work Plan'
            if field == 'compliance' and label in ('no confidential information disclosed', 'no confidential information is disclosed'):
                return 'no Confidential Information disclosed'
            if field == 'scope' and label in ('necessary for its own work', 'to the extent necessary for its own work'):
                return 'necessary for its own work'
            record_scope = 'for all processing of ' + terms['data'] + ' performed under this Agreement'
            if field == 'scope' and label in (key(record_scope), key(record_scope.removeprefix('for '))):
                return record_scope
            if field == 'security' and label in ('secure', 'securely', 'in a secure manner', 'secure manner', 'secure storage'):
                return 'secure'
            if field in ('security', 'encryption') and label in ('aes-256', 'aes 256', 'using aes-256'):
                return 'AES-256'
            if field == 'compliance' and label in ('disclosure required by law', 'disclosure is required by law'):
                return 'disclosure required by law'
            if field == 'compliance' and label in ('retention required by law', 'retention is required by law'):
                return 'retention required by law'
            if field in ('event', 'timing') and label in events: return events[label]
            if field == 'timing' and label in ('before publication', 'prior to publication'): return 'before publication'
            return v
        if isinstance(value, list): result['value'] = [mapped(v) for v in value]
        elif 'value' in result: result['value'] = mapped(value)
        if field == 'scope' and result.get('value') == 'Permitted Purpose': result['field'] = 'purpose'
        if field == 'timing' and result.get('value') in ('termination', 'incident awareness', 'request', 'publication notice', "Provider's objection"):
            result['field'] = 'event'
        if field == 'compliance' and result.get('value') == 'Security Requirements': result['field'] = 'security'
        if field == 'encryption' and result.get('value') == 'AES-256': result['field'] = 'security'
        scalar = result.get('value')
        inferred = ('duration' if field == 'elapsedtime' and isinstance(scalar, str) and re.fullmatch(r'P[\dYMDTHS.]+', scalar, re.I)
                    else 'boolean' if type(scalar) is bool else 'number' if type(scalar) in (int, float)
                    else 'string' if isinstance(scalar, str) else None)
        if inferred and result.get('type') == inferred: result.pop('type')
        if field == 'datetime' and isinstance(scalar, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', scalar) and result.get('type') == 'dateTime':
            result.pop('type')
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
        if (entry.get('kind') == 'permission' and entry.get('actor') == 'Recipient'
                and entry['action'] == 'read' and entry.get('issuer') == 'Provider'): entry.pop('issuer')
        for field in ('requires', 'remedies'):
            for child in entry.get(field, []): rule(child)
    for entry in policy['rules']: rule(entry)
    return policy


def evaluate_classification(base, chunks, root):
    """Score every frozen PDF chunk, including no-rule definitions and negatives."""
    from xfed.evaluation import classification_metrics
    manifest, reference = load(base)
    cases = reference.get('classification')
    if not cases:
        raise ValueError('This older DCT revision has no frozen classification labels. Create a new revision.')
    expected = {case['chunk']['chunk_id']: case for case in cases}
    actual = {chunk['chunk_id']: chunk for chunk in chunks}
    if len(actual) != len(chunks) or set(actual) - set(expected):
        raise ValueError('Classification output has duplicate or unknown source chunks.')
    rows = []
    for identifier, case in expected.items():
        chunk = actual.get(identifier)
        if chunk and {k: v for k, v in chunk.items() if k != 'classification'} != case['chunk']:
            raise ValueError('Classification evidence changed: ' + identifier)
        prediction = (chunk or {}).get('classification', {})
        rows.append(dict(chunk_id=identifier, clause_id=case['chunk'].get('clause_id'), pages=case['chunk']['source']['pages'],
            text=case['chunk']['text'], gold=case['label'], gold_reason=case['rationale'], label=prediction.get('label'),
            reason=prediction.get('reason'), error='Missing prediction' if not prediction else None))
    metrics = classification_metrics(rows)
    return dict(task='classification', protocol='dct_classification_v1', contract_id=reference['contract_id'],
        reference_status='Authored DCT labels; not independently reviewed.', reference_sha256=manifest['reference_sha256'],
        pdf_sha256=manifest['pdf_sha256'], predictions_sha256=digest(rows), predictions=rows, metrics=metrics,
        mismatches=[row for row in rows if row['error'] or row['gold'] != row['label']],
        scorer_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        scope='Task 1.3 on every chunk of this generated DCT PDF; no claim of general contract accuracy.')


def evaluate(base, policy, chunks, root):
    manifest, reference = load(base)
    terms = dict(DEFAULTS, **manifest['terms'])
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
    clause_ids = list(dict.fromkeys(r['source'] for r in expected_rules))
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
        scenarios += [('3.3', 'Third party without consent', dict(recipient='third party', approval='absent', compliance='not required'), True),
                      ('3.3', 'Internal recipient', dict(recipient='Recipient', approval='absent', compliance='not required'), False)]
        scenarios.append(('3.3', 'Third party with consent', dict(recipient='third party', approval="Provider's prior written consent", compliance='not required'), not terms['consent_exception']))
        if terms['legal_disclosure']:
            scenarios += [('3.3', 'Legally required disclosure without consent', dict(recipient='third party', approval='absent', compliance='disclosure required by law'), False),
                          ('3.3', 'Legally required disclosure with consent', dict(recipient='third party', approval="Provider's prior written consent", compliance='disclosure required by law'), False)]
    if terms['storage_limit']:
        scenarios += [('4.4', 'Storage in ' + country, dict(spatial=country), False) for country in terms['storage_countries']]
        outside = next(c for c in STORAGE_COUNTRIES if c not in terms['storage_countries'])
        scenarios.append(('4.4', 'Storage outside permitted countries', dict(spatial=outside), True))
    for clause, label, state, expected_result in scenarios:
        bans = [r for r in actual['rules'] if actual_clauses[r['id']] == clause and r['kind'] == 'prohibition'
                and r['actor'] == 'Recipient' and r['asset'] == 'Data'
                and r['action'] == {'3.2': 'use', '3.3': 'share', '4.4': 'store'}[clause]]
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
            state = dict(elapsedTime=duration, event=event, recipient='Provider', compliance='not required')
            values = [constraint_truth([odrl_condition(c) for c in r.get('conditions', [])], state) for r in obligations]
            result = False if any(v is False for v in values) else None if not values or any(v is None for v in values) else True
            probes.append(dict(clause_id=clause, scenario=label + ' / ' + action, criterion='deadline satisfaction',
                               expected_satisfied=expected_result, actual_satisfied=result, passed=result is expected_result))
        if action == 'delete' and terms['legal_retention']:
            # Check the exception separately: an exempt duty is not a missed deadline.
            state = dict(elapsedTime='P0D', event='termination', compliance='retention required by law')
            values = [constraint_truth([odrl_condition(c) for c in r.get('conditions', [])], state) for r in obligations]
            result = True if any(v is True for v in values) else None if not values or any(v is None for v in values) else False
            probes.append(dict(clause_id=clause, scenario='Legally required retention', criterion='exception applicability',
                               expected_obligation_applies=False, actual_obligation_applies=result, passed=result is False))
    if terms['encryption']:
        encryption = [r for r in actual['rules'] if actual_clauses[r['id']] == '4.3' and r['kind'] == 'obligation'
                      and r['actor'] == 'Recipient' and r['asset'] == 'Data' and r['action'] == 'encrypt']
        for algorithm, expected_result in (('AES-256', True), ('plaintext', False)):
            values = [constraint_truth([odrl_condition(c) for c in r.get('conditions', [])], dict(security=algorithm)) for r in encryption]
            result = False if any(v is False for v in values) else None if not values or any(v is None for v in values) else True
            probes.append(dict(clause_id='4.3', scenario='Encryption / ' + algorithm, criterion='algorithm satisfaction',
                               expected_satisfied=expected_result, actual_satisfied=result, passed=result is expected_result))
    def check(clause, label, kind, actor, action, asset, state, expected_result, criterion='condition match'):
        candidates = [r for r in actual['rules'] if actual_clauses[r['id']] == clause and r['kind'] == kind
                      and r['actor'] == actor and r['action'] == action and r['asset'] == asset]
        values = [constraint_truth([odrl_condition(c) for c in r.get('conditions', [])], state) for r in candidates]
        result = True if any(v is True for v in values) else None if not values or any(v is None for v in values) else False
        probes.append(dict(clause_id=clause, scenario=label, criterion=criterion,
                           expected_condition_match=expected_result, actual_condition_match=result, passed=result is expected_result))
    if terms['competition_limit']:
        for purpose, expected_result in (('developing competing products', True), ('Permitted Purpose', False)):
            check('3.4', 'Competing-product use / '+purpose, 'prohibition', 'Recipient', 'use', 'Data', dict(purpose=purpose), expected_result)
    if terms['platform_access']:
        grant = dict(recipient='Recipient', discretion="Provider's sole discretion", security='Security Requirements')
        # New references retain the access mode; older frozen references and
        # their probes remain unchanged for historical comparisons.
        if any(r['source'] == '6.1' and any(c.get('field') == 'scope' and c.get('value') == 'direct electronic access'
                for c in r.get('conditions', [])) for r in expected_rules):
            grant['scope'] = 'direct electronic access'
        check('6.1', 'Provider-controlled secure grant', 'permission', 'Provider', 'provideAccess', 'Platform', grant, True)
        check('6.1', 'Grant with failed security', 'permission', 'Provider', 'provideAccess', 'Platform', dict(grant, security='not met'), False)
        check('6.1', 'Grant to wrong recipient', 'permission', 'Provider', 'provideAccess', 'Platform', dict(grant, recipient='other party'), False)
        if 'scope' in grant:
            check('6.1', 'Grant outside the stated access mode', 'permission', 'Provider', 'provideAccess', 'Platform', dict(grant, scope='other access mode'), False)
        for security, expected_result in (('Security Requirements', False), ('not met', True)):
            check('6.2', 'Platform denial / '+security, 'prohibition', 'Recipient', 'read', 'Platform', dict(security=security), expected_result)
    if terms['records_access']:
        scope = 'for all processing of '+terms['data']+' performed under this Agreement'
        check('7.1', 'Complete records in agreed scope', 'obligation', 'Recipient', 'maintain', 'Records', dict(completeness='complete', scope=scope), True, 'requirement satisfaction')
        check('7.1', 'Incomplete records', 'obligation', 'Recipient', 'maintain', 'Records', dict(completeness='incomplete', scope=scope), False, 'requirement satisfaction')
        access = dict(recipient='Provider', event='request', scope=scope)
        for label, state, expected_result in (('Requested records', access, True),
                ('No records request', dict(access, event='no request'), False),
                ('Records outside processing scope', dict(access, scope='unrelated records'), False)):
            check('7.2', label, 'obligation', 'Recipient', 'provideAccess', 'Records', state, expected_result, 'requirement satisfaction')
    if terms['background_access']:
        access = dict(dateTime=terms['project_start'], payAmount=0, scope='necessary for its own work', compliance='Work Plan')
        for label, state, expected_result in (('Project start boundary', access, True),
                ('Project end boundary', dict(access, dateTime=terms['project_end']), True),
                ('Before project start', dict(access, dateTime=(date.fromisoformat(terms['project_start'])-timedelta(days=1)).isoformat()), False),
                ('After project end', dict(access, dateTime=(date.fromisoformat(terms['project_end'])+timedelta(days=1)).isoformat()), False),
                ('Background access with a charge', dict(access, payAmount=1), False),
                ('Background unrelated to own work', dict(access, scope='unrelated work'), False),
                ('Background outside work plan', dict(access, compliance='other plan'), False)):
            check('8.1', label, 'permission', 'Recipient', 'read', 'Background', state, expected_result, 'time window' if 'project' in label.lower() else 'condition match')
        for label, purpose, approval, expected_result in (
                ('Allowed project purpose', 'Permitted Purpose', 'absent', False),
                ('Other purpose without consent', 'other purpose', 'absent', True),
                ('Other purpose with consent', 'other purpose', "Provider's prior written consent", False)):
            check('8.2', label, 'prohibition', 'Recipient', 'use', 'Background', dict(purpose=purpose, approval=approval), expected_result)
    if terms['publication']:
        for timing, expected_result in (('before publication', True), ('after publication', False)):
            check('9.1', 'Publication notice / '+timing, 'obligation', 'Recipient', 'notify', 'Results', dict(recipient='Provider', timing=timing), expected_result, 'deadline satisfaction')
        window, delay = terms['publication_notice_days'], terms['publication_delay_days']
        request = dict(elapsedTime='P'+str(window)+'D', event='publication notice', postponementDuration='P'+str(delay)+'D')
        for label, state, expected_result in (('Delay request at both bounds', request, True),
                ('Late delay request', dict(request, elapsedTime='P'+str(window+1)+'D'), False),
                ('Excessive requested delay', dict(request, postponementDuration='P'+str(delay+1)+'D'), False)):
            check('9.2', label, 'permission', 'Provider', 'requestDelay', 'Results', state, expected_result, 'time window')
        ordinary = dict(elapsedTime='P'+str(window)+'D', event='publication notice', approval='Provider confirmation of no objection', compliance='no Confidential Information disclosed')
        delayed = dict(elapsedTime='P'+str(delay)+'D', event="Provider's objection", approval="Provider's objections addressed", compliance='no Confidential Information disclosed')
        for clause, state, bound, approval in (('9.3', ordinary, window, 'unconfirmed'), ('9.4', delayed, delay, 'unresolved')):
            for label, inputs, expected_result in (('Publication at window boundary', state, True),
                    ('Publication before window opens', dict(state, elapsedTime='P'+str(bound-1)+'D'), False),
                    ('Missing publication clearance', dict(state, approval=approval), False),
                    ('Confidential publication', dict(state, compliance='Confidential Information disclosed'), False)):
                check(clause, label, 'permission', 'Recipient', 'publish', 'Results', inputs, expected_result, 'time window' if 'window' in label else 'condition match')
    if terms['authorized_access']:
        for recipient, expected_result in (('Authorized Users', False), ('unregistered researcher', True)):
            check('10.1', 'Research access / '+recipient, 'prohibition', 'Recipient', 'provideAccess', 'Data', dict(recipient=recipient), expected_result)
    if terms['no_reidentification']:
        check('10.2', 'Reidentification is prohibited', 'prohibition', 'Recipient', 'reidentify', 'Data', {}, True)
    if terms['attribution']:
        attribution = dict(recipient='Provider', timing='when publishing')
        check('10.3', 'Provider attribution on publication', 'obligation', 'Recipient', 'attribute', 'Results', attribution, True, 'requirement satisfaction')
        check('10.3', 'Attribution to wrong party', 'obligation', 'Recipient', 'attribute', 'Results', dict(attribution, recipient='other party'), False, 'requirement satisfaction')
    for enabled, clause, actor, action, asset, amount, prefix, suffix, event, recipient in (
            (terms['error_reporting'], '10.4', 'Recipient', 'notify', 'Error', terms['error_hours'], 'PT', 'H', 'error discovery', 'Provider'),
            (terms['deletion_certificate'], '10.5', 'Recipient', 'submit', 'Certificate', terms['certificate_days'], 'P', 'D', 'data deletion', 'Provider'),
            (terms['provider_delivery'], '10.6', 'Provider', 'share', 'Data', terms['delivery_days'], 'P', 'D', 'agreement signature', 'Recipient'),
            (terms['provider_delivery'], '10.7', 'Provider', 'share', 'Documentation', terms['delivery_days'], 'P', 'D', 'agreement signature', 'Recipient')):
        if not enabled: continue
        state = dict(event=event, recipient=recipient, elapsedTime=prefix+str(amount)+suffix)
        check(clause, 'At deadline / '+asset, 'obligation', actor, action, asset, state, True, 'deadline satisfaction')
        check(clause, 'Beyond deadline / '+asset, 'obligation', actor, action, asset, dict(state, elapsedTime=prefix+str(amount+1)+suffix), False, 'deadline satisfaction')
        check(clause, 'Wrong clock / '+asset, 'obligation', actor, action, asset, dict(state, event='unrelated event'), False, 'deadline satisfaction')
    alignments = [row for clause in comparisons for row in clause['alignment']]
    diagnostics = dict(missing_rules=sum(len(c['missing_reference_rule_ids']) for c in comparisons),
        extra_rules=sum(len(c['unmatched_prediction_rule_ids']) for c in comparisons),
        mismatched_rules=sum(not row['complete_agreement'] for row in alignments),
        wrong_parties=sum(not row['components']['actor'] or row['components'].get('issuer') is False for row in alignments),
        wrong_actions=sum(not row['components']['action'] for row in alignments),
        condition_mismatches=sum(not row['components']['conditions'] for row in alignments),
        failed_deadline_checks=sum(not c['passed'] for c in probes if c.get('criterion') in ('deadline satisfaction', 'time window')),
        failed_exception_checks=sum(not c['passed'] for c in probes if c.get('criterion') == 'exception applicability'
                                   or 'consent' in c['scenario'].lower() or 'legally required disclosure' in c['scenario'].lower()))
    return dict(protocol='dct_source_reference_v1', scope='DCT contracts only; template meanings and aliases, not arbitrary-contract legal accuracy.',
        complete=True, contract_id=reference['contract_id'], template_version=reference['template_version'],
        terms_sha256=manifest['terms_sha256'], pdf_sha256=manifest['pdf_sha256'], reference_sha256=manifest['reference_sha256'],
        policy_sha256=digest(policy),
        expected_rules=count, extracted_rules=predicted, matched_rules=matched, precision=precision, recall=recall,
        f1=2 * precision * recall / (precision + recall) if precision + recall else 0,
        complete_policy=matched == count == predicted, routed_operative_clauses=sum(c in routing for c in clause_ids),
        expected_operative_clauses=len(clause_ids), clauses=comparisons, behavior_checks=probes, diagnostics=diagnostics)
