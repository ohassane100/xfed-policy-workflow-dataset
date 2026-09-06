"""Controlled workflow variants from a reviewed contract-specific scenario specification."""
import argparse
import copy
import hashlib
from pathlib import Path
from src.policy.verification import verified_policy
from src.policy.validate_policy_md import validate as validate_policy
from src.workflow.parse_workflow_md import Workflow,render
from src.workflow.validate_workflow_md import validate
from src.workflow.review_workflow import review,validate_spec
from src.utils.io import digest,publish,read_json


def generate(contract: Path,spec_path: Path) -> list[Path]:
    policy=verified_policy(contract)
    spec=validate_spec(read_json(spec_path),policy)
    rules={r['rule_id'] for r in validate_policy(policy).rules}
    if {c['rule_id'] for c in spec['checks']}!=rules:
        raise ValueError('Example generation requires reviewed checks covering every policy rule')
    baseline=Workflow(spec['metadata'],spec['baseline_sections'])
    validate(render(baseline))
    if review(policy,render(baseline),spec)['decision']!='APPROVE':
        raise ValueError('Baseline fails the reviewed compliance checks')
    variants=[(baseline,dict(decision='APPROVE',violated_rules=[],satisfied_rules=sorted(rules),required_changes=[],reason='Baseline specified as compliant by the reviewed scenario design.'))]
    for check in spec['checks']:
        variant=copy.deepcopy(baseline)
        variant.sections[check['section']]=check['violating_section']
        validate(render(variant))
        expected=dict(decision=check['on_violation'],violated_rules=[check['rule_id']],
                      satisfied_rules=sorted(rules-{check['rule_id']}),required_changes=[check['correction']],
                      reason='Controlled mutation of '+check['section']+' for rule '+check['rule_id'])
        observed=review(policy,render(variant),spec)
        if observed['violated_rules']!=expected['violated_rules']:
            raise ValueError('Mutation must violate exactly its designated rule')
        variants.append((variant,expected))
    destinations=[contract/'workflows'/f'workflow_{i:03d}' for i in range(1,len(variants)+1)]
    if any(path.exists() for path in destinations):
        raise FileExistsError('Workflow examples already exist')
    for i,((workflow,expected),destination) in enumerate(zip(variants,destinations),1):
        workflow.metadata['Workflow ID']=f'workflow_{i:03d}'
        markdown=render(workflow)
        expected.update(human_verified=False,status='needs_review',policy_sha256=digest(contract/'ground_truth/POLICY.md'),
                        workflow_sha256=hashlib.sha256(markdown.encode()).hexdigest(),spec_sha256=digest(spec_path))
        publish(destination,{'WORKFLOW.md':markdown,'expected_review.json':expected})
    return destinations


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('contract',type=Path);parser.add_argument('--spec',type=Path,required=True)
    args=parser.parse_args();print('\n'.join(str(p) for p in generate(args.contract,args.spec)))
