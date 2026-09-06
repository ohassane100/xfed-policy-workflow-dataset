"""Validate syntax and optionally exact source grounding."""
import argparse
from pathlib import Path
from src.policy.parse_policy_md import META, Policy, parse


def validate(text: str, source: str | None = None, contract_id: str | None = None) -> Policy:
    result = parse(text)
    if set(result.metadata) != set(META):
        raise ValueError('Missing policy metadata')
    if contract_id and result.metadata['Source Contract'] != contract_id:
        raise ValueError('Source Contract mismatch')
    if result.metadata['Status'] not in {'ground_truth','candidate','reviewed'}:
        raise ValueError('Unknown policy status')
    ids = set()
    for rule in result.rules:
        if rule['effect'] not in {'allow','deny','require'}:
            raise ValueError('Allowed effects: allow, deny, require')
        if rule['rule_id'] in ids:
            raise ValueError('Duplicate rule ID')
        ids.add(rule['rule_id'])
        for key in ('description','applies_to','source_clause','source_text'):
            if not rule.get(key, '').strip():
                raise ValueError('Missing rule field: '+key)
        if source is not None and rule['source_text'] not in source:
            raise ValueError('Ungrounded source text: '+rule['rule_id'])
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('policy', type=Path)
    parser.add_argument('--source',type=Path)
    args = parser.parse_args()
    result = validate(args.policy.read_text(encoding='utf-8'),args.source.read_text(encoding='utf-8') if args.source else None)
    print(f'Valid POLICY.md: {len(result.rules)} rules')
