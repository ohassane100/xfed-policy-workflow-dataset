"""Canonical, traversal-safe repository paths."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]


def safe_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", value):
        raise ValueError(f"Unsafe identifier: {value!r}")
    return value


def contract_path(contract_id: str, root: Path = ROOT) -> Path:
    return root / "data" / "contracts" / safe_name(contract_id)


def extraction_path(contract: Path, family: str, run: str) -> Path:
    if family not in {"algorithmic", "llm"}:
        raise ValueError("Unknown extractor family")
    return contract / "extractions" / family / safe_name(run)
