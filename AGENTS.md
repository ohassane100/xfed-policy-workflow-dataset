# AGENTS.md — Minimal MVP Rules

## Objective

Recreate only the source files necessary for:

```text
contract.pdf
→ contract.txt
→ algorithmic POLICY.md
```

Do not rebuild the previous large architecture.

---

## Required source files

Create only if missing:

```text
src/__init__.py
src/preprocessing/__init__.py
src/preprocessing/pdf_to_text.py
src/policy/__init__.py
src/policy/algorithmic/__init__.py
src/policy/algorithmic/v1.py
```

Do not create additional source modules unless absolutely necessary.

---

## Preprocessing Agent

Implement:

```text
src/preprocessing/pdf_to_text.py
```

Function:

```python
pdf_to_text(pdf_path: Path, output_path: Path) -> None
```

Requirements:

- use `pypdf`;
- extract pages in order;
- preserve readable spacing;
- fail clearly if PDF is missing;
- fail clearly if no text is extracted;
- create only `contract.txt`.

No OCR.
No metadata.
No logging files.
No cache files.

`scripts/preprocess_contract.py` must stay a thin runner.

---

## Algorithmic Policy Agent

Implement:

```text
src/policy/algorithmic/v1.py
```

Do not require `en_core_web_sm`.

Use:

```python
spacy.blank("en")
nlp.add_pipe("sentencizer")
```

only if spaCy exists.

If spaCy is unavailable, use regex sentence splitting.

V1 must:

1. reconstruct numbered contract clauses;
2. split clauses into sentences;
3. filter relevant sentences;
4. detect allow/deny/require;
5. use lightweight pattern-based actor/action/object extraction;
6. fall back to the original sentence when uncertain;
7. output Markdown rules with source clause numbers.

No LLM.
No dependency parser.
No embeddings.
No external NLP models.

---

## Runner

`scripts/algorithmic/run_policies.py` is the only version selector.

It imports:

```text
src/policy/algorithmic/v1.py
```

for:

```python
VERSION = "v1"
```

and writes only:

```text
data/contracts/contract_001/policy_extractions/algo1/POLICY.md
```

Create `algo1/` only when running V1.

---

## Ground truth

Do not modify:

```text
data/contracts/contract_001/ground_truth/POLICY.md
data/contracts/contract_001/ground_truth/review.json
```

The review file remains:

```json
{
  "human_verified": false,
  "status": "not_verified"
}
```

---

## LLM folders

Keep:

```text
llm1_deepseek/
llm2_qwen/
```

empty.

Do not create LLM code yet.

---

## Cleanup

Remove unused old implementation files only after checking references.

Do not recreate them afterward.

Especially avoid:

```text
workflow code
training code
fine-tuning code
large evaluation framework
synthetic contract code
old schemas
duplicate scripts
extra utils
extra configs
```

The goal is the smallest working Friday demo.
