"""Rank only current verified-reference evaluations; agreement is descriptive."""
import argparse
from itertools import combinations
from pathlib import Path
from src.policy.validate_policy_md import validate
from src.policy.verification import verified_policy
from src.utils.io import digest,read_json,write_json
from src.utils.paths import ROOT


def compare(contract: Path) -> dict:
    methods=[];signatures={};workflow_methods=[]
    try:
        verified_policy(contract)
        reference_current=True
    except (FileNotFoundError,ValueError):
        reference_current=False
    for path in sorted((contract/'extractions').glob('*/*/eval.json')):
        result=read_json(path);config=read_json(path.with_name('run_config.json'))
        name='/'.join(path.parent.parts[-2:])
        stale=config['input_sha256']!=digest(contract/'source/contract.txt') or result.get('candidate_sha256')!=digest(path.with_name('POLICY.md'))
        if result.get('status')=='evaluated':
            stale=stale or not reference_current or result.get('ground_truth_sha256')!=digest(contract/'ground_truth/POLICY.md') or result.get('review_sha256')!=digest(contract/'ground_truth/review.json')
        methods.append(dict(result,method=name,stale=stale,offline_fixture=config.get('execution_mode')=='offline_fixture'))
        if not stale:
            signatures[name]={(r['effect'],' '.join(r['description'].casefold().split())) for r in validate(path.with_name('POLICY.md').read_text(encoding='utf-8')).rules}
    ranked=sorted((m for m in methods if m['status']=='evaluated' and not m['stale'] and not m['offline_fixture']),key=lambda m:(-m['f1'],m['method']))
    agreement=[dict(method_a=a,method_b=b,agreed=len(signatures[a]&signatures[b]),only_a=sorted(signatures[a]-signatures[b]),only_b=sorted(signatures[b]-signatures[a])) for a,b in combinations(sorted(signatures),2)]
    policy=dict(contract_id=contract.name,ranking=[m['method'] for m in ranked],methods=methods,agreement=agreement,agreement_is_ground_truth=False)
    for path in sorted((contract/'workflows').glob('*/reviews/*/eval.json')):
        result=read_json(path);config=read_json(path.with_name('run_config.json'))
        folder=path.parents[2]
        stale=not reference_current or config.get('policy_sha256')!=digest(contract/'ground_truth/POLICY.md') or config.get('workflow_sha256')!=digest(folder/'WORKFLOW.md') or result.get('candidate_review_sha256')!=digest(path.with_name('review.md'))
        if result.get('status')=='evaluated':stale=stale or result.get('expected_sha256')!=digest(folder/'expected_review.json')
        workflow_methods.append(dict(result,method=path.parent.name,workflow=folder.name,stale=stale,offline_fixture=config.get('execution_mode')=='offline_fixture'))
    workflow=dict(contract_id=contract.name,methods=workflow_methods)
    write_json(contract/'comparisons/policy_extraction_summary.json',policy)
    write_json(contract/'comparisons/workflow_review_summary.json',workflow)
    return dict(policy=policy,workflow=workflow)


def aggregate(root: Path = ROOT) -> dict:
    contracts=[compare(c) for c in sorted((root/'data/contracts').glob('contract_*'))]
    groups={};workflows={}
    for contract in contracts:
        for task,group in (('policy',groups),('workflow',workflows)):
            for method in contract[task]['methods']:
                if method['status']=='evaluated' and not method['stale'] and not method['offline_fixture']:
                    group.setdefault(method['method'],[]).append(method)
    result=dict(policy_methods={name:dict(count=len(runs),mean_f1=sum(r['f1'] for r in runs)/len(runs)) for name,runs in groups.items()},
                workflow_methods={name:dict(count=len(runs),decision_accuracy=sum(r['decision_accuracy'] for r in runs)/len(runs),false_approvals=sum(r['false_approval'] for r in runs)) for name,runs in workflows.items()},
                note='Only current verified-reference evaluations are ranked; compare coverage before interpreting averages.')
    write_json(root/'results/aggregate/summary.json',result);return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('contract',nargs='?',type=Path);parser.add_argument('--all',action='store_true')
    args=parser.parse_args()
    if args.all:print(aggregate())
    elif args.contract:print(compare(args.contract))
    else:parser.error('Supply a contract or --all')
