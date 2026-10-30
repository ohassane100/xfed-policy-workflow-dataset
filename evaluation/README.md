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

## Reference policy set

`policy_reference.json` contains the reference policy set: a provisional pilot
prepared using ChatGPT/Codex, covering 12 previously inspected clauses from
Contracts 01–03. Ten cases are scored; an unfilled form
note and the publication/objection clause await review and are excluded. The
reference was authored from source before the current Qwen comparison. It is
neither independently verified legal ground truth nor an unseen-agreement test.

```powershell
python -m xfed.policy_evaluation describe
python -m xfed.policy_evaluation run --workers 3 --output tmp/policy-pilot.json
python -m xfed.policy_evaluation score --predictions tmp/policy-pilot.json --output tmp/policy-pilot-scores.json
```

This local runner uses the forwarded API, with no remote commands or private
services. Its defaults enable reasoning with 12,288 output tokens. Excluded cases
are skipped unless `--include-unreviewed` is supplied; their scores remain excluded.
It tests isolated extraction using frozen context, including definitions
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

The report also separates actor, modality, action, asset and condition agreement,
using bounded one-to-one rule alignment. A separate logical-rule score recognizes
Boolean equivalence over identical literal predicates, including membership lists
and their complements. It does not infer paraphrase equivalence. Missing material
conditions still fail the complete-rule measure even when other components match.
Mutation regressions check swapped parties, removed exceptions, changed operators,
AND/OR changes and duplicate rules; these are evaluator checks, not model accuracy.

`policy_validation.json` freezes eight additional source-first clause cases from
the same agreements, with eighteen expected atomic rules. It includes a compound
confidentiality clause and two no-rule cases. It is AI-authored and provisional;
it is not an unseen-agreement benchmark. Run with
`--reference evaluation/policy_validation.json`. Keep results separate from the
original development pilot; do not revise these answers to fit model predictions.

Freeze reference revisions before new comparisons; never rewrite expected answers
to match predictions. Keep uncertain cases excluded until reviewed. For this small
project, combine this comparison with source inspection of important mismatches
and focused regression tests for polarity, exceptions, clocks and binding scope.
Do not build another model-jury system merely to obtain a higher score.

DCT authoring keeps selected terms and a hashed reference outside the input
PDF. After the normal stages 1.1 through 2, `guided_evaluation.json` compares
complete rules, operative-clause routing and consent/deadline scenarios. Explicit
template role/name aliases and Boolean equivalence are allowed; missing conditions
and extra rules still fail. This measures the supported generated contracts,
not arbitrary-contract accuracy. Changing terms creates a new contract revision.
Diagnostics separate missing/extra rules from aligned rule mismatches and failed
exception/deadline checks. Encryption, country and legal-exception probes are
enabled only when those DCT terms are selected.

Generation also gives its same-model reviewer small computed examples of purpose
restrictions. Missing state, unresolved values and unsupported comparisons stay
unknown. These examples check the encoded logic, not whether it matches the law
or source; a false prohibition condition never grants a permission.
