# Architecture

```text
Contract
├── Human → ground_truth/POLICY.md
├── Algorithm → POLICY.md
└── LLM + POLICY template → POLICY.md

POLICY.md + REQUEST.md
├── Algorithm + WORKFLOW template → WORKFLOW.md
└── LLM + WORKFLOW template → WORKFLOW.md

POLICY.md + WORKFLOW.md
├── Deterministic compliance checker
└── LLM reviewer
```

Primary comparisons:

- policy extraction against human ground truth;
- workflow compliance against policy;
- LLM reviewer accuracy against deterministic/human labels.
