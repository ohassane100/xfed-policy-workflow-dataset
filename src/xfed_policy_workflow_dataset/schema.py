"""Schema helpers for policy/workflow dataset records."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict

DATASET_FIELDS = [
    "id",
    "source_dataset",
    "source_document",
    "source_text",
    "policy_text",
    "policy_structured",
    "workflow_text",
    "workflow_structured",
    "label",
    "rationale",
    "evidence",
    "split",
]

ALLOWED_LABELS = {"COMPLIANT", "VIOLATION", "UNCLEAR"}
ALLOWED_SPLITS = {"train", "validation", "test"}


def make_empty_record() -> Dict[str, Any]:
    return {
        "id": "",
        "source_dataset": "",
        "source_document": "",
        "source_text": "",
        "policy_text": "",
        "policy_structured": {},
        "workflow_text": "",
        "workflow_structured": {},
        "label": "UNCLEAR",
        "rationale": "",
        "evidence": [],
        "split": "train",
    }


def normalize_record(record: Dict[str, Any]) -> Dict[str, Any]:
    normalized = make_empty_record()
    normalized.update(record)

    label = str(normalized.get("label", "UNCLEAR")).upper()
    normalized["label"] = label if label in ALLOWED_LABELS else "UNCLEAR"

    split = str(normalized.get("split", "train")).lower()
    normalized["split"] = split if split in ALLOWED_SPLITS else "train"

    if not isinstance(normalized.get("policy_structured"), dict):
        normalized["policy_structured"] = {"value": deepcopy(normalized["policy_structured"])}

    if not isinstance(normalized.get("workflow_structured"), dict):
        normalized["workflow_structured"] = {"value": deepcopy(normalized["workflow_structured"])}

    evidence = normalized.get("evidence", [])
    if isinstance(evidence, str):
        normalized["evidence"] = [evidence]
    elif isinstance(evidence, list):
        normalized["evidence"] = evidence
    else:
        normalized["evidence"] = [str(evidence)]

    for field in DATASET_FIELDS:
        normalized.setdefault(field, make_empty_record()[field])

    return {field: normalized[field] for field in DATASET_FIELDS}
