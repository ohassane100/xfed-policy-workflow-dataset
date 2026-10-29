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
Policy generation checks are separate; the Policy review tab remains a static demo.

## Provisional policy reference

`policy_reference.json` is a ChatGPT/Codex-authored pilot covering 12 previously
inspected clauses from Contracts 01–03. Ten cases are scored; an unfilled form
note and the publication/objection clause await review and are excluded. The
reference was authored from source before the current Qwen comparison. It is
neither independently verified legal ground truth nor an unseen-agreement test.

```powershell
python -m xfed.policy_evaluation describe
python -m xfed.policy_evaluation run --workers 3 --output tmp/policy-pilot.json
python -m xfed.policy_evaluation score --predictions tmp/policy-pilot.json --output tmp/policy-pilot-scores.json
```

This local runner uses the forwarded API, with no remote commands or private
services. It tests isolated extraction using frozen context, including definitions
and no-rule cases; it does not measure relevance routing or PDF extraction.
It records reference, code, input, PDF and model identities. Results are saved
after each case; interim reports are labelled incomplete and missing cases count
as failures. Rejected and missing positive cases retain their reference rules in
recall. Duplicate/unknown cases and changed reference/input hashes are rejected.

Report normalized exact **reference agreement**, with per-clause mismatches.
Normalization handles whitespace, typographic apostrophes, ordering of AND/OR
conditions and fixed day/hour durations. It does not silently equate different
exceptions, actors, operators or wording. Core agreement ignores conditions and
is diagnostic only. Review mismatches as possible representation differences or
real errors before drawing conclusions about fidelity. Do not call these scores
legal accuracy. A missing/failed no-rule case never earns a correct-negative pass.

Freeze reference revisions before new comparisons; never rewrite expected answers
to match predictions. Keep uncertain cases excluded until reviewed. For this small
project, combine this comparison with source inspection of important mismatches
and focused regression tests for polarity, exceptions, clocks and binding scope.
Do not build another model-jury system merely to obtain a higher score.
