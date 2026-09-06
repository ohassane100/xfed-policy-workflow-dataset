"""One-to-one evidence matching; final metrics require verified ground truth."""
import argparse
from pathlib import Path
from src.policy.validate_policy_md import validate
from src.policy.verification import verified_policy
from src.utils.io import digest, read_json, write_json


def norm(value: str) -> str:
    return ' '.join(value.casefold().split())


def evaluate(truth: str, candidate: str, source: str) -> dict:
    expected = validate(truth).rules
    actual = validate(candidate).rules
    available = set(range(len(actual)))
    pairs = []
    for i, rule in enumerate(expected):
        matches = [j for j in available if norm(rule['source_text']) == norm(actual[j]['source_text'])]
        if matches:
            j = min(matches, key=lambda j:(actual[j]['effect'] != rule['effect'],j))
            pairs.append((i,j)); available.remove(j)
    correct = sum(expected[i]['effect']==actual[j]['effect'] and norm(expected[i]['description'])==norm(actual[j]['description']) for i,j in pairs)
    precision = correct/len(actual) if actual else float(not expected)
    recall = correct/len(expected) if expected else 1.0
    return dict(status='evaluated',matcher='exact_evidence_description_effect_v1',
                correct_rules=correct,missing_rules=len(expected)-correct,hallucinated_rules=len(actual)-correct,
                precision=precision,recall=recall,f1=2*precision*recall/(precision+recall) if precision+recall else 0.,
                effect_accuracy=sum(expected[i]['effect']==actual[j]['effect'] for i,j in pairs)/len(pairs) if pairs else None,
                rule_id_accuracy=sum(expected[i]['rule_id']==actual[j]['rule_id'] for i,j in pairs)/len(pairs) if pairs else None,
                source_clause_accuracy=sum(expected[i]['source_clause']==actual[j]['source_clause'] for i,j in pairs)/len(pairs) if pairs else None,
                scope_accuracy=sum(norm(expected[i]['applies_to'])==norm(actual[j]['applies_to']) for i,j in pairs)/len(pairs) if pairs else None,
                source_grounding=sum(bool(r['source_text']) and r['source_text'] in source for r in actual)/len(actual) if actual else None,
                matched_evidence_pairs=len(pairs),format_valid=True)


def evaluate_contract(contract: Path, candidate: str) -> dict:
    try:
        validate(candidate,contract_id=contract.name)
    except ValueError as exc:
        return dict(status='invalid_candidate',format_valid=False,error=str(exc))
    try:
        truth = verified_policy(contract)
    except (ValueError,FileNotFoundError) as exc:
        return dict(status='awaiting_human_verification',reason=str(exc),format_valid=True,
                    precision=None,recall=None,f1=None)
    result = evaluate(truth,candidate,(contract/'source/contract.txt').read_text(encoding='utf-8'))
    result.update(ground_truth_sha256=digest(contract/'ground_truth/POLICY.md'),
                  review_sha256=digest(contract/'ground_truth/review.json'))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('contract',type=Path)
    parser.add_argument('candidate',type=Path)
    args = parser.parse_args()
    result = evaluate_contract(args.contract,args.candidate.read_text(encoding='utf-8'))
    result['candidate_sha256'] = digest(args.candidate)
    write_json(args.candidate.with_name('eval.json'),result)
    print(result['status'])


if __name__ == '__main__':
    main()
