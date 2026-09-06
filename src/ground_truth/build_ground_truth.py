"""Draft POLICY.md from real text. Existing ground truth is never changed."""
from pathlib import Path
import hashlib
from src.extractors.algorithmic.v1 import extract
from src.policy.validate_policy_md import validate
from src.utils.io import digest, publish
from src.utils.paths import ROOT


def build(contract: Path) -> Path:
    text_path = contract/'source/contract.txt'
    text = text_path.read_text(encoding='utf-8')
    draft = extract(text,contract.name,'ground_truth_draft_v1').replace('Status: candidate','Status: ground_truth')
    validate(draft,text,contract.name)
    review = dict(status='needs_review',human_verified=False,version=1,review_notes=[],
                  drafting_method='modal_baseline_v1; requires completeness and semantic review',
                  policy_sha256=hashlib.sha256(draft.encode()).hexdigest(),text_sha256=digest(text_path))
    return publish(contract/'ground_truth',{'POLICY.md':draft,'review.json':review})


def build_all(root: Path = ROOT) -> None:
    for contract in sorted((root/'data/contracts').glob('contract_*')):
        if (contract/'source/contract.txt').exists() and not (contract/'ground_truth').exists():
            print(build(contract))


if __name__ == '__main__':
    build_all()
