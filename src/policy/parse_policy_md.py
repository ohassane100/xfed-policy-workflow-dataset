"""Strict deterministic grammar for POLICY.md, including multiline quotes."""
from dataclasses import dataclass
import json
import re

META = ('Policy ID','Project ID','Company A','Company B','Drafted by','Source','Source Contract','Status')
RULE = re.compile(r'^- `([^`]+)` \*\*([A-Za-z0-9][A-Za-z0-9_-]*)\*\* — (.+)$')
SUBFIELDS = {'Applies to':'applies_to','Source clause':'source_clause','Source text':'source_text'}


@dataclass
class Policy:
    metadata: dict[str,str]
    rules: list[dict[str,str]]


def parse(text: str) -> Policy:
    lines = text.splitlines()
    if not lines or lines[0].strip() != '# POLICY.MD':
        raise ValueError('Expected # POLICY.MD')
    metadata, rules = {}, []
    in_rules = False
    current = None
    quote = None
    for raw in lines[1:]:
        line = raw
        if quote is not None:
            if not line.startswith('    '):
                raise ValueError('Quote continuation needs four spaces')
            quote += '\n' + line[4:]
            if quote.endswith('"'):
                current['source_text'] = quote[1:-1]
                quote = None
            continue
        if not line.strip():
            continue
        if line == '## Enforcement Rules' and not in_rules:
            in_rules = True
            continue
        if not in_rules:
            key, separator, value = line.partition(': ')
            if not separator or key not in META or key in metadata or not value.strip():
                raise ValueError(f'Invalid/duplicate policy metadata: {line}')
            metadata[key] = value.strip()
            continue
        match = RULE.fullmatch(line)
        if match:
            current = dict(effect=match[1],rule_id=match[2],description=match[3])
            rules.append(current)
            continue
        if current and line.startswith('  - '):
            key, separator, value = line[4:].partition(': ')
            if not separator or key not in SUBFIELDS or SUBFIELDS[key] in current:
                raise ValueError(f'Invalid/duplicate rule field: {line}')
            if key == 'Source text':
                if not value.startswith('"'):
                    raise ValueError('Source text must be quoted')
                if len(value)>1 and value.endswith('"'):
                    try:
                        current['source_text'] = json.loads(value)
                    except json.JSONDecodeError:
                        current['source_text'] = value[1:-1]  # literal quotes in legacy human drafts
                else:
                    quote = value
            else:
                current[SUBFIELDS[key]] = value
            continue
        raise ValueError(f'Unexpected policy syntax: {line}')
    if quote is not None or not in_rules:
        raise ValueError('Missing rules section or unterminated quote')
    return Policy(metadata,rules)


def render(policy: Policy) -> str:
    lines = ['# POLICY.MD','']+[f'{key}: {policy.metadata[key]}' for key in META]+['','## Enforcement Rules','']
    for rule in policy.rules:
        lines += [f"- `{rule['effect']}` **{rule['rule_id']}** — {rule['description']}",
                  f"  - Applies to: {rule['applies_to']}", f"  - Source clause: {rule['source_clause']}"]
        lines += ['  - Source text: '+json.dumps(rule['source_text'],ensure_ascii=False),'']
    return '\n'.join(lines)+'\n'
