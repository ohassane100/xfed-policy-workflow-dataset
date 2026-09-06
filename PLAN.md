# PLAN.md — Contract → POLICY.md → WORKFLOW.md Prototype

## Goal

Build a practical prototype using real data-sharing/data-use/data-transfer contracts.

No LLM fine-tuning.

The system compares:
1. human ground-truth `POLICY.md`;
2. algorithmic contract → `POLICY.md`;
3. LLM contract → `POLICY.md` using the same template;
4. algorithmic policy/request → `WORKFLOW.md`;
5. LLM policy/request → `WORKFLOW.md` using the same workflow template;
6. policy compliance of both workflows;
7. LLM review of both workflows.

## Core pipeline

```text
REAL CONTRACT
↓
contract.pdf
↓
contract.txt + metadata.json
↓
Human ground_truth/POLICY.md
↓
┌──────────────────────┬──────────────────────┐
│ Algorithm extractor  │ LLM extractor        │
│ → POLICY.md          │ → POLICY.md          │
└──────────────────────┴──────────────────────┘
↓
Compare both against human POLICY.md

POLICY.md + REQUEST.md + WORKFLOW.template.md
↓
┌──────────────────────┬──────────────────────┐
│ Algorithm workflow   │ LLM workflow         │
│ → WORKFLOW.md        │ → WORKFLOW.md        │
└──────────────────────┴──────────────────────┘
↓
Check both workflows against POLICY.md
↓
LLM reviewer:
APPROVE / DENY / REQUIRES_CHANGES
```

## Important: REQUEST.md

A policy does not uniquely tell us what task the companies want to run.

Therefore both workflow generators must receive the same:

```text
POLICY.md
REQUEST.md
WORKFLOW.template.md
```

This makes the workflow comparison fair.

## Real contracts

Use:

```text
data/contract_sources/source.json
```

This contains the 50 real public contract sources already collected.

Do not generate synthetic replacement contracts.

Start with 3–5 contracts. Expand toward 50 only after the complete pipeline works.

## Per-contract structure

```text
data/contracts/contract_001/
├── source/
│   ├── original.*
│   ├── contract.pdf
│   ├── contract.txt
│   └── metadata.json
├── ground_truth/
│   ├── POLICY.md
│   └── review.json
├── policy_extractions/
│   ├── algorithmic/
│   │   ├── v1/
│   │   │   ├── POLICY.md
│   │   │   ├── eval.json
│   │   │   └── run_config.json
│   │   └── v2/
│   └── llm/
│       ├── qwen/
│       └── deepseek/
├── requests/
│   └── request_001/
│       ├── REQUEST.md
│       ├── workflows/
│       │   ├── algorithmic_v1/
│       │   │   ├── WORKFLOW.md
│       │   │   ├── compliance.json
│       │   │   └── run_config.json
│       │   └── qwen/
│       │       ├── WORKFLOW.md
│       │       ├── compliance.json
│       │       └── run_config.json
│       └── reviews/
│           ├── algorithmic_workflow_llm_review.json
│           └── llm_workflow_llm_review.json
└── comparisons/
    ├── policy_comparison.json
    └── workflow_comparison.json
```

## POLICY.md

Canonical policy representation:

```text
allow
deny
require
```

Example:

```markdown
- `deny` **raw_data_export** — Raw data must not leave the host company.
- `allow` **approved_analysis** — The partner may analyze the dataset for the agreed purpose.
- `require` **output_review** — Outputs must be approved before release.
```

Each rule should also include:
- scope;
- source clause;
- exact source text.

## Ground truth

For each contract:

```text
contract.txt
↓
manual interpretation
↓
ground_truth/POLICY.md
```

This is the reference answer.

Agents may draft it, but only a human may mark it verified.

## Algorithmic policy extractor

### V1
Use deterministic modal/deontic patterns:

```text
may / permitted / allowed → allow
shall not / must not / prohibited → deny
shall / must / required → require
```

### V2
Add non-LLM NLP:
- clause/sentence segmentation;
- dependency parsing;
- subject/action/object extraction;
- negation;
- conditions;
- exceptions;
- deadlines;
- defined-term resolution.

Both versions must output the same `POLICY.md` template.

## LLM policy extractor

Input:

```text
contract.txt
POLICY.template.md
```

Output:

```text
POLICY.md
```

Use zero-shot or few-shot prompting only. No training.

Start with Qwen and DeepSeek if available.

## Policy evaluation

Compare:

```text
algorithmic POLICY.md vs ground_truth/POLICY.md
LLM POLICY.md         vs ground_truth/POLICY.md
```

Measure:
- matched rules;
- missing rules;
- hallucinated rules;
- allow/deny/require correctness;
- source grounding;
- precision;
- recall;
- F1.

## REQUEST.md

Example:

```markdown
## Goal
Detect abnormal valve behaviour.

## Requested Dataset
Anti-surge valve telemetry.

## Requested Computation
Run anomaly detection inside the host environment.

## Requested Outputs
- anomaly counts
- feature statistics
- sanitized event patterns
```

## Algorithmic workflow generator

Input:

```text
POLICY.md
REQUEST.md
WORKFLOW.template.md
```

Simple deterministic mapping:

```text
request goal → Purpose
request dataset → Dataset
requested outputs → candidate Shared Outputs

deny raw data export
→ raw data goes to Stays Private

require sandbox execution
→ Purpose specifies execution in host sandbox

deny an output
→ remove/block it from Shared Outputs

require approval
→ preserve approval requirement
```

This is a transparent baseline, not a perfect planner.

## LLM workflow generator

Input exactly the same:

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

## Workflow evaluation

Do not compare workflows mainly by text similarity.

Different workflows can both be correct.

Evaluate each workflow against the policy:

```text
POLICY.md + WORKFLOW.md → compliance
```

Check:
- prohibited data in Shared Outputs;
- required private data in Stays Private;
- allowed purpose;
- allowed dataset;
- sandbox/local execution requirements;
- approval requirements;
- other explicit policy rules.

Output:

```json
{
  "decision": "APPROVE",
  "satisfied_rules": [],
  "violated_rules": [],
  "missing_requirements": []
}
```

## LLM workflow reviewer

Input:

```text
POLICY.md
WORKFLOW.md
```

Output:
- `APPROVE`, `DENY`, or `REQUIRES_CHANGES`;
- satisfied rule IDs;
- violated rule IDs;
- explanation;
- suggested corrections.

Run the reviewer separately on the algorithmic workflow and the LLM-generated workflow.

## Comparison

Policy:
```text
Algorithm policy → ground truth
LLM policy       → ground truth
```

Workflow:
```text
Algorithm workflow → policy compliance
LLM workflow       → policy compliance
```

Also compare the LLM reviewer's decisions against deterministic/human labels where available.

## Cleanup

Before implementation, remove files that belong only to old project versions.

Remove if unused:
- synthetic contract generators;
- generated synthetic contracts;
- fine-tuning scripts;
- training JSONL builders;
- train/validation/test logic used only for fine-tuning;
- JSON-first policy pipelines;
- unused policy JSON schemas;
- duplicate preprocessing/model scripts;
- stale outputs;
- old source manifests;
- documentation contradicting this plan.

Keep:
- `data/contract_sources/source.json`;
- real contracts;
- preprocessing;
- `POLICY.md`, `REQUEST.md`, `WORKFLOW.md` templates;
- verified ground truth;
- current experiment results.

Before deletion:
1. search references;
2. confirm unused;
3. remove stale imports/references;
4. run tests;
5. report what was deleted.

## Recommended build order

1. Clean repository.
2. Select 3–5 real contracts.
3. Download and preprocess them.
4. Manually create ground-truth `POLICY.md`.
5. Implement algorithmic policy V1.
6. Run one LLM policy extractor.
7. Evaluate both against ground truth.
8. Create `REQUEST.md`.
9. Implement algorithmic workflow generator.
10. Implement LLM workflow generator.
11. Implement deterministic workflow compliance checker.
12. Implement LLM workflow reviewer.
13. Compare results.
14. Expand toward 50 contracts only after this works.

## Short meeting explanation

> I use real data-sharing agreements and manually create a reference POLICY.md using a fixed template. A deterministic extractor and an LLM independently produce the same policy format and are evaluated against the reference. For workflow generation, both methods receive the same policy, task request, and workflow template. Their workflows are evaluated primarily by policy compliance rather than textual similarity. An LLM is also used as a reviewer to identify violations and required changes. I am not fine-tuning the LLMs.
