# XFed Policy & Workflow Dataset

This repository contains scaffolding for an MSc research dataset focused on evaluating and fine-tuning local LLMs for:

1. extracting machine-readable policies from contracts and regulations;
2. reviewing policies;
3. checking whether proposed workflows comply with those policies.

## Repository layout

- `data/raw/` - unmodified source datasets
- `data/processed/` - normalized/processed records
- `data/samples/` - small example records in target schema
- `scripts/` - CLI utilities for downloading/importing and conversion
- `src/` - reusable Python code for schema and processing utilities

## Initial dataset schema

Each record contains:

- `id`
- `source_dataset`
- `source_document`
- `source_text`
- `policy_text`
- `policy_structured`
- `workflow_text`
- `workflow_structured`
- `label` (`COMPLIANT`, `VIOLATION`, `UNCLEAR`)
- `rationale`
- `evidence`
- `split` (`train`, `validation`, `test`)

## Quick start

```bash
python scripts/download_dataset.py <url>
python scripts/convert_to_schema.py data/samples/sample_records.jsonl data/processed/sample_records.jsonl
```

The reusable schema helpers are in `src/xfed_policy_workflow_dataset/schema.py`.
