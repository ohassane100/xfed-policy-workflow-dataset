"""Build final Markdown policy-extraction data from verified references only."""
import argparse
import json
from pathlib import Path
from src.dataset.splits import ensure_splits
from src.policy.verification import verified_policy
from src.utils.io import write_json
from src.utils.paths import ROOT,contract_path


def build(root: Path = ROOT) -> dict:
    counts,skipped={},{}
    output=root/'datasets';output.mkdir(parents=True,exist_ok=True)
    for split,ids in ensure_splits(root).items():
        records=[]
        for cid in ids:
            contract=contract_path(cid,root)
            try: policy=verified_policy(contract)
            except (ValueError,FileNotFoundError) as exc:
                skipped[cid]=str(exc);continue
            records.append(dict(contract_id=cid,input=(contract/'source/contract.txt').read_text(encoding='utf-8'),output=policy))
        (output/f'policy_extraction_{split}.jsonl').write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in records),encoding='utf-8')
        counts[split]=len(records)
    write_json(output/'policy_build_manifest.json',dict(counts=counts,skipped=skipped,human_verified_only=True))
    return counts


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,default=ROOT)
    print(build(parser.parse_args().root))
