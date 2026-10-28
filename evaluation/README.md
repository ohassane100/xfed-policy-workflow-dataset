# Contract evaluation

**Contract 05 is the default test agreement for relevance and policy extraction.**
Contracts 01–03 are development material. Contract 04 is synthetic and excluded.
The evaluation covers the complete document, rather than sampling clauses from
the development contracts. Its predictions have now been inspected; use Contract
05 as an exposed diagnostic set and reserve new agreements for final validation.

## Source and coverage

Contract 05 is Equinor's published [Volve data licence](https://cdn.equinor.com/files/h61q9gi9/global/de6532f6134b9a953f6c41bac47a0c055a3712d3.pdf?equinor-hrs-terms-and-conditions-for-licence-to-data-volve.pdf=),
downloaded on 2026-10-26 from Equinor's CDN. The [Volve data-sharing page](https://www.equinor.com/energy/volve-data-sharing)
provides the dataset's context. This is a real published licence, not a signed
bilateral agreement. It governs petroleum data use, derivatives, sharing,
attribution, resale restrictions, moral rights and licence termination.

The original ten-page PDF is saved as `contracts/contract_05/input/contract.pdf`.
`reference.json` records the source URLs, PDF and segmentation hashes, exact
extracted text, pages, context, labels, rule slots and annotation rationales.
All 76 stage 1.2 chunks appear once in **each** task, in document order. Nothing
is balanced, deduplicated or selected based on predictions. The non-binding
summary on page 1, operative terms on pages 2–4 and technical exhibit on pages
5–10 are retained. The summary cannot independently create policy rules.

| Split | Contracts | Relevance cases | Policy cases |
|---|---|---:|---:|
| Development | 01–03 | 300 | 60 |
| Default test | 05 only | 76 | 76 |

The reference labels are 38 RELEVANT and 38 NOT_RELEVANT; there are no
natural NEEDS_CONTEXT examples. The policy reference has seven operative chunks
with 18 canonical rules and 69 chunks expecting no policy. Relevant definitions,
asset descriptions and lifecycle declarations do not necessarily create a rule.
Some extracted quotes/bullets contain replacement glyphs; text is preserved
verbatim and operative pages were also inspected visually.

## Reference review and independence

Review source text and supplied evidence before viewing model predictions.
Record corrections in `gold`, the reviewer in `gold.reviewer`, and completion
in `gold.review_status`. Use the existing relevance and policy scope. Resolve `requires_adjudication` cases, particularly waiver,
lifecycle and notice wording. The sharing clause has a predeclared alternative
for whether its “should” notices impose duties. Do not change aliases or reference
rules merely to fit predictions. Freeze the reviewed JSON and its reported hash.

Fix prompts, code, settings and model choices on Contracts 01–03. Then compare
those fixed configurations on Contract 05 and retain every report. If Contract
05 results guide further tuning, disclose that exposure; it is no longer an
untouched final test. One agreement cannot establish generalisation to new
contract populations.

## Run

Restart the updated dashboard, open **Independent contract evaluation**, select
relevance, policy extraction or both, and click **Evaluate Contract 05**. The
evaluation uses the selected model and generation settings. To compare models,
run each fixed configuration separately and download its JSON report. Parallel
mode uses concurrent workers of the selected model.

```bash
python -m xfed.evaluation describe
python -m xfed.evaluation run --split dev --task both --model llama3.1:70b --output /tmp/xfed-dev.json
python -m xfed.evaluation run --task both --model llama3.1:70b --output /tmp/xfed-contract05.json
python -m xfed.evaluation run --task both --policy-mode gated --model llama3.1:70b --output /tmp/xfed-gated.json
python -m xfed.evaluation score --predictions /tmp/xfed-contract05.json --output /tmp/xfed-rescored.json
```

The CLI defaults to Contract 05; `--split dev` selects Contracts 01–03. Add
`--require-reviewed` to select only completed reference reviews. `--config` uses existing
model settings. Mixed-model `--worker-models` assignments depend on completion
order and should not be reported as the score of a single model.

Both tasks use the existing inference code with frozen supporting source context.
Parents, definitions and references come from the existing context index; explicit
grant limitations/lifecycle evidence and the summary disclaimer are also attached
where needed. **Policy extraction is scored independently of relevance**, including
all non-rule controls. These are stage scores, not an end-to-end pipeline score.
Error patterns from Contract 05 have informed subsequent improvements. Its scores
must not be presented as untouched holdout performance.

Policy inference now includes a source-backed rule plan, deterministic ODRL
compilation and a fresh same-model evidence/coverage check with one correction.
Its plan and check are saved in each prediction's generation report. Strict rule
scores are unchanged; mismatches list missing and unexpected canonical rules.
These checks are part of inference, not the gold judge. Compare new runs separately
from earlier direct-generation runs; do not combine their scores or timings.
Evidence in new model plans contains source IDs; the compiler copies exact text
locally. Older plans with quotes are still checked, allowing PDF whitespace
differences while rejecting changed words or source IDs. Notification recipients,
qualitative timing and clear reversed prohibition predicates receive compiler
checks in addition to model review.
Clause-level plans emit source terms as ODRL Sets and do not infer policy issuers.
Explicit legacy issuer metadata still compiles, but deployment authority needs
separate verification. Access permissions must retain their own security
prerequisites and any restriction to assets necessary for the stated task.

`--policy-mode gated` routes RELEVANT and NEEDS_CONTEXT clauses to policy
extraction and scores exclusions and upstream failures against the full policy
reference. It requires both tasks. The dashboard defaults to this mode when both
tasks are selected. `isolated` preserves stage comparisons. Neither mode reruns
PDF extraction or context retrieval; the report states this boundary.
Policy inputs absent from the relevance benchmark receive extra unscored
classification decisions, saved with each policy prediction. They do not change
the relevance benchmark denominator. Scored reports also record the scorer hash.
`reference_output_compatibility` flags actor/asset reference labels unavailable
under literal-source compilation, allowing supplied context, declared aliases and
reference alternatives. It diagnoses reference/schema mismatches without changing
gold or forgiving model errors; strict F1 alone is not a semantic quality grade.

Predictions and `live_metrics` are checkpointed after each completed request, so
the dashboard and downloaded JSON expose early results and cancellation retains
completed work. Interim metrics cover completed cases only; `progress` retains
the full selected-case denominator. Final `metrics` still count every case,
including missing predictions and failures. Keep each run's code/prompts frozen;
use a separate local snapshot while editing the next version.
Connection failures stop the run and retain completed predictions with an
infrastructure-error marker. Restore the endpoint before starting another run;
an interrupted run is not a completed model-quality comparison.

For an existing approved model service forwarded to the local computer, no remote
scripts, installations or private model services are needed:

```bash
python -m xfed.evaluation run --split dev --task both --policy-mode gated --model qwen3.5:122b-a10b --base-url http://127.0.0.1:11435 --workers 3 --output tmp/xfed-dev.json
```

`--workers` limits concurrent requests to that endpoint. Optional OSDU fields are
left out of the model decoder when the source does not mention them, avoiding a
server grammar incompatibility without weakening saved-policy validation.

## Evaluation method and blind review

Keep complete-rule precision/recall/F1 as correctness measures, with separate
operative-clause accuracy, no-rule controls, failures and the all-abstain baseline.
New aligned field diagnostics report modality, action, assignee, target and
conditions using maximum-weight one-to-one assignment. They explain partial
matches without forgiving a wrong complete rule, reusing one reference rule for
duplicates, or treating a similarity score as legal equivalence. This follows
[ExtractBench's field-specific evaluation and array alignment](https://arxiv.org/abs/2602.12247).
Fixed-length `elapsedTime` durations compare quantitatively (for example, `P3D`
equals `PT72H`); calendar months/years, trigger events and comparison direction
remain distinct. Only a consistent redundant duration unit is ignored.

Report precision together with recall and operative generation coverage. The
[selective classification literature](https://arxiv.org/abs/1705.08500) makes the
accuracy/coverage trade-off explicit. Here the reference targets are at least
99% rule precision and 99% rule recall. Targets cannot be verified using draft
labels. Wilson lower bounds are also reported: a perfect point estimate on a
small sample is not evidence of 99% population precision. Rules within a clause
are dependent, so the rule-level interval is descriptive only.

Create a blind annotation packet with neither model outputs nor draft answers:

```bash
python -m xfed.evaluation review-export --split test --output /tmp/xfed-review.json
python -m xfed.evaluation review-apply --annotations /tmp/xfed-review.json --output /tmp/xfed-reviewed-reference.json
python -m xfed.evaluation run --reference /tmp/xfed-reviewed-reference.json --require-reviewed --task both --policy-mode gated --model llama3.1:70b --output /tmp/xfed-reviewed-run.json
```

An independent human fills each packet entry's gold label/rules, source-based
rationale and reviewer, then marks it reviewed. Apply completed entries only;
unfinished entries stay draft. Source text and IDs are checked against the
packet's frozen reference hash. The original reference is preserved. Resolve
disagreements before freezing; allow predeclared equivalent representations,
never aliases added simply to fit a tested model. Separate model families may
assist review, but the generator's same-model evidence check is not gold truth.
Compare fixed models/settings on the same reviewed contract splits; inspect
recall, failure types and per-contract results alongside precision.
`contract_macro_f1` gives each contract with supported gold rules equal weight
instead of letting a long agreement dominate the model comparison.

The extraction response schema now constrains supported operators and atomic
actions before decoding, and the compiler requires literal actor/asset/issuer
labels in cited spans. This complements [Ollama's schema-constrained output](https://docs.ollama.com/capabilities/structured-outputs);
valid JSON still needs semantic review. Partial outputs retain only fully
entailed parent/duty groups: dropping an unsupported prerequisite must never
create an unconditional permission. No restricted contract format is required;
clear definitions and one actor/action/asset/condition per sentence help, but
changing the input format must be evaluated as a separate cohort.

Runs are uncached and preserve saved pipeline outputs. UI reports are saved under
ignored `evaluation/runs/` and restored after restart. Each report contains source,
reference, input, code/schema and prompt hashes; case IDs, gold, predictions,
mismatches, run times, model digests/settings and assignments. Changed source
PDFs, missing full-document cases, changed frozen chunks and changed reference
content are rejected. Changes during inference stop the run. Earlier reports from
Contracts 02–03 remain identifiable by their source manifest/reference hash.

## Read the scores

The dashboard shows correct relevance labels, expected rules recovered (recall),
generated rules matching the reference (precision), fully matched operative
clauses, correct no-rule decisions and failed evaluations. Results include counts
and plain-language explanations. Zero-denominator scores say why they are not
applicable rather than displaying an unexplained dash. Detailed scores, mismatches,
confusion matrices and uncertainty remain available underneath and in the download.
No-rule success is separate from rule extraction: abstaining on every chunk gets
69/76 cases right while recovering none of the rules.

Relevance reports accuracy, precision/recall/F1, weighted F1, balanced accuracy
and a three-label confusion matrix with an ERROR column. Macro scores average
the fixed labels supported by the selected gold cases; an absent class is
unmeasured, not a zero. Incorrect NEEDS_CONTEXT predictions still count as errors
against their actual class. Undefined individual metrics are `null`.

Policies report generate/abstain accuracy, canonical rule and constraint
precision/recall/F1, and exact-case accuracy. Canonical matching checks rule type,
atomic action, assignee, target and constraints. It ignores arbitrary UIDs,
rule/constraint order and issuer/assigner; it retains duplicates as false positives.
Only frozen aliases and explicit alternative rule sets are accepted. Semantic
paraphrases are handled through adjudication. OSDU metadata is outside rule
matching; schema and traceability validation are reported separately. Missing/failed predictions remain in denominators, and rejected
generations never count as correct abstentions.

Reports include 95% Wilson intervals and 500-replicate page-cluster bootstrap
intervals. Bootstrap macro labels remain fixed to the original selected gold
classes. Contract 05 has only ten pages, with operative terms on three pages;
intervals are exploratory and do not estimate variation across contracts. There
is no evidence here for NEEDS_CONTEXT performance or small model differences.
