# PLAN.md — Friday MVP, No spaCy Model Required

## Goal

Keep the MVP minimal:

```text
contract.pdf
↓
contract.txt
↓
ground_truth/POLICY.md
↓
algorithmic V1 → algo1/POLICY.md
↓
manual comparison
```

No workflow.
No LLM execution yet.
No fine-tuning.
No large dataset pipeline.

---

## Important environment constraint

Do **not** require:

```text
en_core_web_sm
```

The user cannot install it.

Algorithm V1 must therefore work without a downloaded spaCy model.

If spaCy itself is installed, `spacy.blank("en")` with a sentencizer may be used.

If spaCy is not installed, fall back to standard-library sentence splitting.

Do not require dependency parsing in V1.

Dependency parsing can be introduced later in V2 when the environment supports a language model.

---

## Minimal required source files

Recreate only:

```text
src/
├── __init__.py
├── preprocessing/
│   ├── __init__.py
│   └── pdf_to_text.py
└── policy/
    ├── __init__.py
    └── algorithmic/
        ├── __init__.py
        └── v1.py
```

Do not generate extra utility modules, schemas, configs, logging modules, or framework abstractions for this MVP.

---

## Preprocessing

`src/preprocessing/pdf_to_text.py` must:

```text
contract.pdf → contract.txt
```

Use `pypdf`.

Preserve page order and paragraph spacing as much as practical.

Do not:
- create metadata automatically;
- create caches/logs;
- OCR;
- create intermediate files.

`scripts/preprocess_contract.py` should be only a thin runner that imports the preprocessing function.

---

## Algorithmic policy V1

`src/policy/algorithmic/v1.py` must be simple and deterministic.

Pipeline:

```text
contract.txt
↓
numbered-clause reconstruction
↓
sentence splitting
↓
filter relevant legal/data sentences
↓
detect deontic phrase
↓
extract rough actor/action/object using word-pattern heuristics
↓
POLICY.md
```

Effects:

```text
may / permitted / entitled / have access
→ allow

shall not / must not / prohibited / may not
→ deny

shall / must / required / obligation
→ require
```

Use conservative heuristics.

If actor/action/object extraction is uncertain, keep the original sentence as the policy description instead of inventing meaning.

---

## Sentence segmentation without en_core_web_sm

Preferred:

```python
import spacy
nlp = spacy.blank("en")
nlp.add_pipe("sentencizer")
```

This does not require a downloaded model.

Fallback if spaCy is unavailable:

```text
regex split on punctuation / line boundaries
```

---

## run_policies.py

Keep one runner:

```python
VERSION = "v1"
CONTRACT_ID = "contract_001"
```

Mapping:

```text
v1
→ src/policy/algorithmic/v1.py
→ data/contracts/contract_001/policy_extractions/algo1/POLICY.md
```

Future:

```text
v2 → algo2/
v3 → algo3/
```

Create output folders only when the version is actually run.

---

## Cleanup rule

Do not recreate removed old architecture.

Do not generate:

```text
workflow/
REQUEST.md
WORKFLOW.md
training scripts
fine-tuning scripts
dataset builders
evaluation framework
JSON policy schema
extra config files
extra helper modules
debug outputs
logs
temporary files
```

Only recreate files required by this MVP.

---

## Commands

Install only:

```bash
pip install pypdf
```

spaCy is optional:

```bash
pip install spacy
```

No model download is required.

Run:

```bash
python scripts/preprocess_contract.py
python scripts/algorithmic/run_policies.py
```
