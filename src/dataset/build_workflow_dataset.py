"""Reuse policy splits; validation contracts never enter workflow train or test."""
import argparse
import json
from pathlib import Path
from src.dataset.splits import ensure_splits
from src.policy.verification import verified_policy
from src.policy.validate_policy_md import validate as validate_policy
from src.workflow.validate_workflow_md import validate
from src.workflow.review_workflow import validate_review
from src.utils.io import digest,read_json,write_json
from src.utils.paths import ROOT,contract_path


def build(root: Path = ROOT) -> dict:
    counts={};skipped={}
    output=root/'datasets';output.mkdir(parents=True,exist_ok=True)
    splits=ensure_splits(root)
    for split in ('train','test'):
        records=[]
        for cid in splits[split]:
            contract=contract_path(cid,root)
            try:policy=verified_policy(contract)
            except (ValueError,FileNotFoundError) as exc:
                skipped[cid]=str(exc);continue
            for folder in sorted((contract/'workflows').glob('workflow_*')):
                expected=read_json(folder/'expected_review.json')
                if expected.get('human_verified') is not True or expected.get('status')!='verified':continue
                if expected.get('policy_sha256')!=digest(contract/'ground_truth/POLICY.md') or expected.get('workflow_sha256')!=digest(folder/'WORKFLOW.md'):
                    raise ValueError('Stale workflow review: '+str(folder))
                workflow=(folder/'WORKFLOW.md').read_text(encoding='utf-8');validate(workflow)
                validate_review(expected,{r['rule_id'] for r in validate_policy(policy).rules})
                records.append(dict(contract_id=cid,policy=policy,workflow=workflow,decision=expected['decision'],
                                    violated_rules=expected['violated_rules'],satisfied_rules=expected['satisfied_rules'],
                                    reason=expected['reason'],required_changes=expected['required_changes']))
        (output/f'workflow_review_{split}.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in records),encoding='utf-8')
        counts[split]=len(records)
    write_json(output/'workflow_build_manifest.json',dict(counts=counts,skipped=skipped,validation_contracts_reserved=True))
    return counts


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,default=ROOT)
    print(build(parser.parse_args().root))
