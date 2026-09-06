"""Decision, violation and correction metrics against reviewed workflow outcomes."""
import argparse
from pathlib import Path
from src.policy.verification import verified_policy
from src.policy.validate_policy_md import validate
from src.utils.io import digest,read_json,write_json
from src.workflow.review_workflow import parse_review,validate_review


def evaluate(expected: dict,actual: dict,rule_ids: set[str]) -> dict:
    validate_review(expected,rule_ids);validate_review(actual,rule_ids)
    gold,pred=set(expected['violated_rules']),set(actual['violated_rules'])
    tp=len(gold&pred)
    satisfied_gold,satisfied_pred=set(expected['satisfied_rules']),set(actual['satisfied_rules'])
    cited=len(pred)+len(satisfied_pred)
    return dict(status='evaluated',decision_accuracy=int(expected['decision']==actual['decision']),
                violation_precision=tp/len(pred) if pred else float(not gold),
                violation_recall=tp/len(gold) if gold else 1.,
                false_approval=int(actual['decision']=='APPROVE' and expected['decision']!='APPROVE'),
                false_denial=int(actual['decision']=='DENY' and expected['decision']=='APPROVE'),
                rule_grounding_accuracy=(tp+len(satisfied_gold&satisfied_pred))/cited if cited else None,
                correction_exact_match=int(set(expected['required_changes'])==set(actual['required_changes'])),
                correction_quality='Semantic correction quality requires human assessment')


def evaluate_files(contract: Path,folder: Path,actual: dict) -> dict:
    try:
        policy=verified_policy(contract)
        expected=read_json(folder/'expected_review.json')
        if expected.get('human_verified') is not True or expected.get('status')!='verified':
            raise ValueError('Workflow gold outcome is not human verified')
        if expected.get('policy_sha256')!=digest(contract/'ground_truth/POLICY.md') or expected.get('workflow_sha256')!=digest(folder/'WORKFLOW.md'):
            raise ValueError('Stale workflow gold outcome')
    except (ValueError,FileNotFoundError) as exc:
        return dict(status='awaiting_human_verification',reason=str(exc))
    try:
        result=evaluate(expected,actual,{r['rule_id'] for r in validate(policy).rules})
    except ValueError as exc:
        return dict(status='invalid_review',error=str(exc))
    result.update(expected_sha256=digest(folder/'expected_review.json'))
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('contract',type=Path);parser.add_argument('workflow',type=Path);parser.add_argument('review',type=Path)
    args=parser.parse_args()
    result=evaluate_files(args.contract,args.workflow,parse_review(args.review.read_text(encoding='utf-8')))
    result['candidate_review_sha256']=digest(args.review)
    write_json(args.review.with_name('eval.json'),result)
