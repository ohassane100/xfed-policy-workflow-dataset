#!/usr/bin/env python3
"""Convert JSONL records into the XFed schema."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from xfed_policy_workflow_dataset.schema import normalize_record


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert input JSONL into schema-compliant JSONL")
    parser.add_argument("input", type=Path, help="Path to source JSONL file")
    parser.add_argument("output", type=Path, help="Path to write converted JSONL file")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)

    with args.input.open("r", encoding="utf-8") as infile, args.output.open("w", encoding="utf-8") as outfile:
        for line in infile:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            normalized = normalize_record(record)
            outfile.write(json.dumps(normalized, ensure_ascii=False) + "\n")

    print(f"Converted {args.input} -> {args.output}")


if __name__ == "__main__":
    main()
