"""Shared human-verification gate for final evaluation and datasets."""
from pathlib import Path
from src.policy.validate_policy_md import validate
from src.utils.io import digest, read_json


def verified_policy(contract: Path) -> str:
    path = contract/'ground_truth/POLICY.md'
    review = read_json(contract/'ground_truth/review.json')
    if review.get('status') != 'verified' or review.get('human_verified') is not True:
        raise ValueError('awaiting_human_verification')
    source = contract/'source/contract.txt'
    if review.get('policy_sha256') != digest(path) or review.get('text_sha256') != digest(source):
        raise ValueError('stale_human_verification')
    text = path.read_bytes().decode('utf-8')
    validate(text,source.read_text(encoding='utf-8'),contract.name)
    return text
