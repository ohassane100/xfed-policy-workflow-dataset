# AGENTS.md

## Mission

Implement only this prototype:

```text
real contract
→ POLICY.md
→ REQUEST.md
→ WORKFLOW.md
→ compliance review
```

No LLM fine-tuning.

## Non-negotiable rules

1. Use real contracts from `data/contract_sources/source.json`.
2. Do not generate replacement contracts.
3. `POLICY.md` is the canonical policy format.
4. `REQUEST.md` is the canonical task/request format.
5. `WORKFLOW.md` is the canonical workflow format.
6. Policy effects are `allow`, `deny`, and `require`.
7. Human `ground_truth/POLICY.md` is the policy reference.
8. Algorithmic and LLM policy extractors use the same policy template.
9. Algorithmic and LLM workflow generators use the same `POLICY.md`, `REQUEST.md`, and workflow template.
10. Workflow quality is judged primarily by policy compliance, not text similarity.
11. An LLM reviewer is secondary evaluation, not ground truth.
12. Do not implement LLM training/fine-tuning.
13. Remove obsolete files once confirmed unused.
14. Never delete verified ground truth or real contract sources.
15. Do not expand beyond `PLAN.md`.

## Agent 0 — Repository Cleanup

Before implementation:
- inspect the existing repository;
- identify obsolete synthetic-contract code;
- identify fine-tuning/training code;
- identify JSON-first policy code;
- identify duplicates and stale outputs;
- search references before deleting;
- remove stale imports/docs;
- run tests;
- produce a cleanup report.

Keep:
```text
data/contract_sources/source.json
real contracts
preprocessing
verified ground truth
current templates
current results
```

## Agent 1 — Real Contract Ingestion

Read `data/contract_sources/source.json`.

For selected contracts:
```text
official source
→ original.*
→ contract.pdf
```

Record source URL, original format, and metadata.

Never fabricate a replacement.

## Agent 2 — Preprocessing

Use one shared pipeline:

```text
contract.pdf
→ contract.txt
→ metadata.json
```

Preserve headings, clauses, lists, paragraphs, and defined terms where practical.

## Agent 3 — Ground-Truth Policy Support

Assist the human:

```text
contract.txt
→ ground_truth/POLICY.md
```

Rules:
```text
allow
deny
require
```

Every rule must include source grounding.

The agent may draft but may not mark the result human verified.

## Agent 4 — Algorithmic Policy Extractor

V1:
- modal/deontic patterns;
- allow/deny/require classification.

V2:
- dependency parsing;
- subject/action/object extraction;
- negation;
- conditions;
- exceptions;
- deadlines;
- defined-term resolution.

Output:
```text
policy_extractions/algorithmic/<version>/
├── POLICY.md
├── eval.json
└── run_config.json
```

## Agent 5 — LLM Policy Extractor

Input:
```text
contract.txt
POLICY.template.md
```

Output:
```text
POLICY.md
```

No training.

Record model, prompt, and run configuration.

## Agent 6 — Policy Evaluator

Compare each candidate against:

```text
ground_truth/POLICY.md
```

Measure:
- matched rules;
- missing rules;
- hallucinated rules;
- effect accuracy;
- source grounding;
- precision;
- recall;
- F1.

## Agent 7 — Request Builder

Create or accept:

```text
REQUEST.md
```

The same request must be used by both workflow generators.

## Agent 8 — Algorithmic Workflow Generator

Input:
```text
POLICY.md
REQUEST.md
WORKFLOW.template.md
```

Output:
```text
WORKFLOW.md
```

Use deterministic mappings.

Examples:
```text
request goal → Purpose
request dataset → Dataset
requested outputs → Shared Outputs candidates
deny raw-data export → raw data goes to Stays Private
require sandbox → workflow states host-sandbox execution
require approval → approval requirement preserved
```

## Agent 9 — LLM Workflow Generator

Input exactly:
```text
POLICY.md
REQUEST.md
WORKFLOW.template.md
```

Output:
```text
WORKFLOW.md
```

No training.

## Agent 10 — Deterministic Compliance Checker

Input:
```text
POLICY.md
WORKFLOW.md
```

Output:
```json
{
  "decision": "APPROVE|DENY|REQUIRES_CHANGES",
  "satisfied_rules": [],
  "violated_rules": [],
  "missing_requirements": []
}
```

Check explicit policy/workflow conflicts where possible.

## Agent 11 — LLM Workflow Reviewer

Review both algorithmic and LLM workflows against the same policy.

Return:
- decision;
- satisfied rule IDs;
- violated rule IDs;
- explanation;
- suggested corrections.

Do not silently edit the original workflow.

## Agent 12 — Comparison Agent

Policy:
```text
algorithmic POLICY.md vs ground truth
LLM POLICY.md         vs ground truth
```

Workflow:
```text
algorithmic WORKFLOW.md vs policy compliance
LLM WORKFLOW.md         vs policy compliance
```

Do not rank workflows primarily by textual similarity.

## Standard per-contract workflow

```text
1. ingest real contract
2. preprocess
3. human creates/verifies ground_truth/POLICY.md
4. run algorithmic policy extractor
5. run LLM policy extractor
6. evaluate both
7. create REQUEST.md
8. run algorithmic workflow generator
9. run LLM workflow generator
10. run deterministic compliance checker on both
11. run LLM reviewer on both
12. compare results
```

## Completion rule

A complete experiment has:

```text
source/contract.pdf
source/contract.txt
source/metadata.json
ground_truth/POLICY.md
algorithmic POLICY.md
LLM POLICY.md
REQUEST.md
algorithmic WORKFLOW.md
LLM WORKFLOW.md
compliance results
LLM review results
comparison results
```
