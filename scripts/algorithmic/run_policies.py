from pathlib import Path
import importlib
import re
import sys

sys.dont_write_bytecode = True

VERSION = "v1"
CONTRACT_ID = "contract_001"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> None:
    if not re.fullmatch(r"v[1-9][0-9]*", VERSION):
        raise ValueError("VERSION must be v1, v2, ...")
    source = (
        PROJECT_ROOT / "data" / "contracts" / CONTRACT_ID / "source" / "contract.txt"
    )

    if not source.exists():
        raise SystemExit(
            "contract.txt not found. Run: python scripts/preprocess_contract.py"
        )

    module = importlib.import_module(f"src.policy.algorithmic.{VERSION}")

    version_number = VERSION.removeprefix("v")
    output_dir = (
        PROJECT_ROOT
        / "data"
        / "contracts"
        / CONTRACT_ID
        / "policy_extractions"
        / f"algo{version_number}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    markdown = module.extract_policy_markdown(
        source.read_text(encoding="utf-8"),
        CONTRACT_ID,
    )

    output = output_dir / "POLICY.md"
    output.write_text(markdown, encoding="utf-8")
    print(f"Wrote {output.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
