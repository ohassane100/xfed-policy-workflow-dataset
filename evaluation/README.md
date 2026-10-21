# Compact XFed evaluation

`reference.json` freezes source text, source pages, attached evidence, draft labels,
rule slots, rationales and PDF hashes. Previous pipeline predictions were not used
as ground truth. All annotations are **assistant-authored drafts**; none has been
independently reviewed. Initial scores are therefore provisional.

## Contents and split

| Cases | Development: contract 1 | Real test: contracts 2–3 | Synthetic test: contract 4 |
|---|---:|---:|---:|
| Classification coverage | 100 | 200 | 100 |
| Classification context challenge | 14 | 10 | 16 |
| Policy generation | 20 | 40 | 20 |

There are 440 classification cases and 80 policy cases, including 40 operative
policy cases, 40 abstention controls and 62 draft reference rules. The 40 context
challenge cases are 20 paired inputs with and without supporting evidence; they
are not 40 independent clauses. Contract 4 declares itself fictional and adapts
concepts from the other contracts, so it is not an independent real-world test.

Classification coverage uses a fixed seed and three clause-length strata, with
duplicate primary texts removed within each contract. Sampling details are in
the JSON. Categories cover use/purpose, disclosure, security, personal data,
retention, intellectual property, remedies, reporting, business negatives and
non-operative text. These are coverage-oriented benchmark scores, not estimates
of real-world class prevalence. Natural NEEDS_CONTEXT support is small; challenge
results must be reported separately.

## Annotation and review

Follow `prompts/relevance_classifier.md`: material data rights/obligations are
RELEVANT; unrelated business terms and standalone headings/commentary are
NOT_RELEVANT. NEEDS_CONTEXT requires genuinely necessary missing evidence, not
merely a missing detail. Ordinary invoice/signature administration is negative;
substantive technical/research reporting and explicit personal-data handling are
relevant. Do not let related context turn an unrelated primary into a positive.

For policies, judge only rules supported by the primary and supplied context.
Preserve the actor, asset, modality, purposes, deadlines, conditions and exceptions.
A relevant definition can correctly produce no policy. A right to request is
not a right to receive. Context cannot independently introduce another rule.

Have an independent reviewer inspect the text and evidence **before viewing
model predictions**, correct `gold`, record their name in `gold.reviewer`, and
set that annotation's `gold.review_status` to `reviewed`. Resolve disagreements,
especially `requires_adjudication` cases. For stronger evidence, obtain two blind
annotations and adjudicate differences; do not invent inter-annotator agreement.
Tune on development only, then freeze the reviewed JSON and its reported SHA-256
before running test. Do not tune references or accepted aliases to test outputs.

## Run

After syncing and restarting the dashboard, open **Evaluation against reference
set**. Choose task, development/test split and real/synthetic sources, then
**Evaluate reference set**. It uses the model selections, parallelization checkbox
and generation settings. Saved pipeline outputs and caches are not changed.
Results include per-cohort tables, per-class metrics, confusion matrices,
uncertainty, failures, model/digest assignments and a downloadable prediction report.

From the repository root:

```bash
python -m xfed.evaluation describe
python -m xfed.evaluation run --task both --split dev --model llama3.1:70b --output /tmp/xfed-dev.json
python -m xfed.evaluation run --task both --split test --source real --model llama3.1:70b --require-reviewed --output /tmp/xfed-test.json
python -m xfed.evaluation score --predictions /tmp/xfed-test.json --require-reviewed --output /tmp/xfed-rescored.json
```

Use `--worker-models llama3.1:70b gemma3:27b` for multiple models, or `--config`
for existing model settings. Use the same reference cases, prompt hashes and
settings when comparing runs. Mixed-model assignments depend on completion order;
report assignments and repeat runs rather than attributing their score to one model.
Runs are uncached, include loading time, and keep task timings and API token totals.

## Metrics and limits

Classification reports accuracy, per-class precision/recall/F1/support, macro and
weighted F1, balanced accuracy and a confusion matrix with an ERROR column.
Macro scores use all three labels with undefined contributions set to zero;
undefined individual precision/recall/F1 values are `null`. Missing/failed
predictions remain in the denominator. Low-support labels are identified.

Policies report generate/abstain accuracy, canonical rule precision/recall/F1,
constraint precision/recall/F1 and exact-case accuracy. Rule matching uses
multisets, retaining duplicate rules as false positives. It checks rule type,
atomic action, assignee, target and constraints, ignoring arbitrary UIDs,
rule/constraint order and issuer/assigner. Set-valued operands ignore order;
other arrays preserve order. Numeric punctuation and comparison signs are preserved.
Only the frozen explicit aliases and approved alternative
reference rule sets are accepted. It does **not** infer logical equivalence or
grade OSDU metadata. Correct paraphrases may need blinded semantic adjudication;
report canonical scores under that name, not as complete legal correctness.
Accepted-output schema and source-traceability validation are reported separately.
Rejected/failed generations never count as correct abstentions.
The `all_curated_cases` policy rows combine operative and abstention controls
within each source type, so rules invented on negative controls reduce aggregate
precision. These balanced curated scores are still not population estimates.

Accuracy and per-class precision/recall have 95% Wilson intervals. Macro F1
(classification), canonical rule F1 and exact-case accuracy (policy) also have
500-replicate page-cluster bootstrap intervals,
resampling pages within the fixed source contracts. These intervals describe
uncertainty within this benchmark and do not establish generalization to new
contract populations. Roughly, 200 independent test clauses have a worst-case
95% margin near ±7 percentage points; 40 policy cases near ±15 points. Correlation
can make uncertainty greater. This compact set supports initial comparisons;
small improvements and rare-class claims need more independently reviewed cases
and more real contracts.
An interval is `null` when a denominator is absent or too many bootstrap
replicates have undefined scores.

Metric definitions follow [scikit-learn's classification evaluation reference](https://scikit-learn.org/stable/modules/model_evaluation.html).
Wilson intervals follow the [NIST confidence-interval reference](https://www.itl.nist.gov/div898/handbook/prc/section2/prc241.htm).
