"""Publish immutable Markdown extractor runs and evaluation status together."""
from pathlib import Path
from src.evaluation.evaluate_policy_extraction import evaluate_contract
from src.policy.validate_policy_md import validate
from src.utils.io import digest, now, publish
from src.utils.paths import extraction_path
import hashlib


def save_run(contract: Path, family: str, name: str, output: str, config: dict) -> Path:
    source = contract/'source/contract.txt'
    validate(output,source.read_text(encoding='utf-8'),contract.name)
    evaluation = evaluate_contract(contract,output)
    evaluation['candidate_sha256'] = hashlib.sha256(output.encode()).hexdigest()
    config = dict(config, schema_version='2.0',created_at=now(),contract_id=contract.name,input_sha256=digest(source))
    return publish(extraction_path(contract,family,name),{'POLICY.md':output,'run_config.json':config,'eval.json':evaluation})
