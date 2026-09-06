from pathlib import Path
import sys

sys.dont_write_bytecode = True

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.preprocessing.pdf_to_text import pdf_to_text


def main() -> None:
    source_dir = PROJECT_ROOT / "data" / "contracts" / "contract_001" / "source"
    pdf_to_text(
        source_dir / "contract.pdf",
        source_dir / "contract.txt",
    )
    print("Wrote data/contracts/contract_001/source/contract.txt")


if __name__ == "__main__":
    main()
