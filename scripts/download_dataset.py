#!/usr/bin/env python3
"""Download a source dataset file into data/raw/."""

from __future__ import annotations

import argparse
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download dataset files into data/raw/")
    parser.add_argument("url", help="Source URL to download")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional output file path (default: data/raw/<filename>)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    default_name = Path(urlparse(args.url).path).name or "dataset.bin"
    output = args.output or Path("data/raw") / default_name
    output.parent.mkdir(parents=True, exist_ok=True)
    urllib.request.urlretrieve(args.url, output)
    print(f"Downloaded {args.url} -> {output}")


if __name__ == "__main__":
    main()
