from pathlib import Path
import sys

sys.dont_write_bytecode = True

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.preprocessing.pdf_to_text import pdf_to_text


def main() -> None:
    contract_id = sys.argv[1] if len(sys.argv) > 1 else "contract_001"
    source_dir = PROJECT_ROOT / "data" / "contracts" / contract_id / "source"
    pdf_to_text(
        source_dir / "contract.pdf",
        source_dir / "contract.txt",
    )
    print(f"Wrote data/contracts/{contract_id}/source/contract.txt")


if __name__ == "__main__":
    main()
