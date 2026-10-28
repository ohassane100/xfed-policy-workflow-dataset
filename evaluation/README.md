# Classification evaluation (task 1.3)

The Evaluation tab measures RELEVANT, NOT_RELEVANT and NEEDS_CONTEXT classification.
The frozen reference contains 300 development cases from Contracts 01–03 and all
76 chunks from Contract 05. Classification labels are unchanged. The reference
labels are provisional; Contract 05 has already been inspected during development.
It is a diagnostic test set, not an unseen-agreement validation. NEEDS_CONTEXT has
no examples in Contract 05, so its recall is unmeasured there.

Results include accuracy, macro precision/recall/F1, per-class scores, confusion
matrices, uncertainty intervals, failures and mismatches. Failed or missing answers
remain in the denominators. Interim scores cover completed cases only. Source,
input and code hashes prevent silently rescoring changed evidence. The benchmark
uses frozen clause context and does not test PDF extraction or context retrieval.

```powershell
python -m xfed.evaluation describe
python -m xfed.evaluation run --split dev --model qwen3.5:122b-a10b --base-url http://127.0.0.1:11435 --output tmp/classification-dev.json
python -m xfed.evaluation run --model qwen3.5:122b-a10b --base-url http://127.0.0.1:11435 --output tmp/classification-test.json
```

Use review-export and review-apply for source-based classification annotation.
The dashboard saves reports in ignored evaluation/runs/. Historical local reports
are preserved, but retired task reports are not loaded into the classification UI.
Policy generation checks and advisory node review are separate application features.
