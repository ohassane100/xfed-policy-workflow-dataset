# Independent Contract 05 evaluation

**Contract 05 is the default test agreement for relevance and policy extraction.**
Contracts 01–03 are development material. Contract 04 is synthetic and excluded.
The test covers the complete independent document, rather than sampling clauses
from the development contracts.

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

The draft relevance labels are 38 RELEVANT and 38 NOT_RELEVANT; there are no
natural NEEDS_CONTEXT examples. The policy draft has seven operative chunks
with 18 canonical rules and 69 chunks expecting no policy. Relevant definitions,
asset descriptions and lifecycle declarations do not necessarily create a rule.
Some extracted quotes/bullets contain replacement glyphs; text is preserved
verbatim and operative pages were also inspected visually.

## Reference review and independence

**Every annotation is an assistant-authored draft, with no independent human
review.** No real model predictions on Contract 05 were run or inspected when
these annotations were prepared. Public availability means overlap with model
training data is unknown; source separation is not proof of training independence.

Have an independent reviewer read the source and supplied evidence **before
viewing model predictions**, correct `gold`, enter their name in `gold.reviewer`,
and set `gold.review_status` to `reviewed`. Follow the existing relevance and
policy prompts. Resolve `requires_adjudication` cases, particularly waiver,
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
python -m xfed.evaluation score --predictions /tmp/xfed-contract05.json --output /tmp/xfed-rescored.json
```

The CLI defaults to Contract 05; `--split dev` selects Contracts 01–03. Add
`--require-reviewed` to refuse unreviewed annotations. `--config` uses existing
model settings. Mixed-model `--worker-models` assignments depend on completion
order and should not be reported as the score of a single model.

Both tasks use the existing inference code with frozen supporting source context.
Parents, definitions and references come from the existing context index; explicit
grant limitations/lifecycle evidence and the summary disclaimer are also attached
where needed. **Policy extraction is scored independently of relevance**, including
all non-rule controls. These are stage scores, not an end-to-end pipeline score.
Production prompts and pipeline stages have not been tuned on Contract 05.

Policy inference now includes a source-backed rule plan, deterministic ODRL
compilation and a fresh same-model evidence/coverage check with one correction.
Its plan and check are saved in each prediction's generation report. Strict rule
scores are unchanged; mismatches list missing and unexpected canonical rules.
These checks are part of inference, not the gold judge. Compare new runs separately
from earlier direct-generation runs; do not combine their scores or timings.

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
paraphrases can need blind adjudication; these scores do not establish legal
correctness or grade OSDU metadata. Schema and traceability validation are reported
separately. Missing/failed predictions remain in denominators, and rejected
generations never count as correct abstentions.

Reports include 95% Wilson intervals and 500-replicate page-cluster bootstrap
intervals. Bootstrap macro labels remain fixed to the original selected gold
classes. Contract 05 has only ten pages, with operative terms on three pages;
intervals are exploratory and do not estimate variation across contracts. There
is no evidence here for NEEDS_CONTEXT performance or small model differences.
