# Agent Setup Prompt — Contract / Policy / Workflow Prototype

Read `PLAN.md` and `AGENTS.md` before making changes.

Refactor the repository to implement:

```text
real contract
→ contract.txt
→ human ground-truth POLICY.md
→ algorithmic POLICY.md
→ LLM POLICY.md
→ policy evaluation

POLICY.md + REQUEST.md
→ algorithmic WORKFLOW.md
→ LLM WORKFLOW.md

POLICY.md + each WORKFLOW.md
→ deterministic compliance check
→ LLM review
→ comparison
```

There is no LLM fine-tuning in this version.

## Phase 0 — Cleanup

Inspect the repository first.

Remove obsolete files from older designs when unused:
- synthetic contract generators;
- generated synthetic contracts;
- fine-tuning scripts;
- training JSONL builders;
- training split logic;
- JSON-first policy pipelines;
- unused schemas;
- duplicate manifests;
- duplicate preprocessing/model scripts;
- stale experiment outputs;
- contradictory documentation.

Before deleting:
1. search references/imports;
2. confirm unused;
3. remove stale references;
4. run tests;
5. report deletions.

Never delete:
- real contract sources;
- `data/contract_sources/source.json`;
- real downloaded contracts;
- verified ground truth;
- current templates/results.

## Phase 1 — Real contracts

Use:
```text
data/contract_sources/source.json
```

Do not generate synthetic replacements.

Start with 3–5 contracts.

## Phase 2 — Preprocessing

Implement/reuse:
```text
contract.pdf
→ contract.txt
→ metadata.json
```

Preserve clause structure where practical.

## Phase 3 — Ground-truth policy

Use:
```text
templates/POLICY.template.md
```

Rules:
```text
allow
deny
require
```

Every rule must retain:
- rule ID;
- description;
- scope;
- source clause;
- source text.

The agent may draft ground truth but must not set human verification itself.

## Phase 4 — Algorithmic policy extraction

Create:
```text
src/policy/algorithmic/v1.py
```

V1:
```text
may / permitted / allowed → allow
shall not / must not / prohibited → deny
shall / must / required → require
```

Prepare V2 for:
- dependency parsing;
- actor/action/object extraction;
- negation;
- conditions;
- exceptions;
- deadlines;
- defined terms.

Output canonical `POLICY.md`.

## Phase 5 — LLM policy extraction

Input:
```text
contract.txt
POLICY.template.md
```

Output:
```text
POLICY.md
```

Use shared model adapters. No training.

Use Qwen and DeepSeek wrappers if supported by the repository/runtime.

## Phase 6 — Policy evaluation

Compare:
```text
algorithmic POLICY.md → ground_truth/POLICY.md
LLM POLICY.md         → ground_truth/POLICY.md
```

Measure:
- matches;
- missing rules;
- hallucinations;
- effect accuracy;
- source grounding;
- precision;
- recall;
- F1.

## Phase 7 — REQUEST.md

Use:
```text
templates/REQUEST.template.md
```

The same request must be supplied to both workflow generators.

## Phase 8 — Algorithmic workflow generation

Create:
```text
src/workflow/algorithmic/v1.py
```

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

Use simple deterministic mappings:
```text
request goal → Purpose
request dataset → Dataset
requested outputs → candidate Shared Outputs
deny raw export → raw/private data in Stays Private
require sandbox → host-sandbox execution in workflow
require approval → preserve approval requirement
```

## Phase 9 — LLM workflow generation

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

## Phase 10 — Deterministic workflow compliance

Create:
```text
src/review/deterministic_compliance.py
```

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

Do not score workflows mainly by text similarity.

## Phase 11 — LLM workflow review

Input:
```text
POLICY.md
WORKFLOW.md
```

Return:
- decision;
- satisfied rules;
- violated rules;
- reason;
- corrections.

Run it separately on algorithmic and LLM workflows.

## Phase 12 — Comparison

Policy comparison:
```text
algorithmic policy vs ground truth
LLM policy vs ground truth
```

Workflow comparison:
```text
algorithmic workflow policy compliance
LLM workflow policy compliance
```

Also compare LLM reviewer decisions against deterministic/human labels.

## Required templates

Use only:
```text
templates/POLICY.template.md
templates/REQUEST.template.md
templates/WORKFLOW.template.md
```

## Required per-contract structure

```text
data/contracts/<contract_id>/
├── source/
│   ├── original.*
│   ├── contract.pdf
│   ├── contract.txt
│   └── metadata.json
├── ground_truth/
│   ├── POLICY.md
│   └── review.json
├── policy_extractions/
│   ├── algorithmic/v1/
│   └── llm/<model>/
├── requests/
│   └── request_001/
│       ├── REQUEST.md
│       ├── workflows/
│       │   ├── algorithmic_v1/
│       │   └── <llm_name>/
│       └── reviews/
└── comparisons/
```

## Tests

Add tests for:
- source manifest integrity;
- preprocessing;
- policy parser/validator;
- request parser/validator;
- workflow parser/validator;
- algorithmic policy extraction;
- policy comparison;
- algorithmic workflow generation;
- prohibited output detection;
- required-private-data handling;
- compliance decisions;
- cleanup leaving no broken imports.

## Final deliverables

Report:
1. cleanup report;
2. repository tree;
3. contracts used initially;
4. preprocessing status;
5. ground-truth policy status;
6. algorithmic policy results;
7. LLM policy results;
8. policy comparison;
9. algorithmic workflows;
10. LLM workflows;
11. compliance results;
12. LLM reviews;
13. commands to run every stage;
14. test results;
15. genuine TODOs only.

Do not implement fine-tuning or expand beyond `PLAN.md`.
