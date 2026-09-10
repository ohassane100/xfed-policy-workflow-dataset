from pathlib import Path
import argparse
import importlib
import re
import sys

sys.dont_write_bytecode = True

VERSION = "v1"
CONTRACT_ID = "contract_001"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main(contract_id: str = CONTRACT_ID) -> None:
    if not re.fullmatch(r"v[1-9][0-9]*", VERSION):
        raise ValueError("VERSION must be v1, v2, ...")
    if not re.fullmatch(r"contract_[0-9]+", contract_id):
        raise ValueError("contract_id must look like contract_001")
    source = PROJECT_ROOT / "data" / "contracts" / contract_id / "source" / "contract.txt"

    if not source.exists():
        raise SystemExit(
            "contract.txt not found."
        )

    module = importlib.import_module(f"src.algorithmic.{VERSION}")

    version_number = VERSION.removeprefix("v")
    output_dir = (
        PROJECT_ROOT
        / "data"
        / "contracts"
        / contract_id
        / "policy_extractions"
        / f"algo{version_number}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    markdown = module.extract_policy_markdown(
        source.read_text(encoding="utf-8"),
        contract_id,
    )

    output = output_dir / "POLICY.md"
    output.write_text(markdown, encoding="utf-8")
    print(f"Wrote {output.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract a policy with algorithmic_v1.")
    parser.add_argument("contract_id", nargs="?", default=CONTRACT_ID)
    main(parser.parse_args().contract_id)
