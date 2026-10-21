# Run Pipeline

## Sources

https://gitlab.ux.uis.no/unix/gpu

## 1. UI - Gorina5

```bash
cd ~/git/xfed-policy-workflow-dataset
git fetch origin
git checkout dev
git pull --ff-only origin dev
"$HOME/.conda/envs/xfed/bin/python" -m pip install -e .
"$HOME/.conda/envs/xfed/bin/python" -m xfed.ui
```

Use `dev` for development runs. Update `main` through pull requests from `dev`.

## 2. Tunnel - ssh1

In a separate terminal on your computer:

```bash
ssh ohassane@ssh1.ux.uis.no
```

Then, on ssh1:

```bash
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:8875:127.0.0.1:8765 gorina5
```

## 3. Tunnel - Windows PowerShell

```powershell
ssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:8875:127.0.0.1:8875 ohassane@ssh1.ux.uis.no
```

Keep the UI and both tunnels running. Blank tunnel terminals are normal.

## 4. Browser

Open [http://127.0.0.1:8875](http://127.0.0.1:8875).
Select the contract and primary model. Enable **Parallelize with different models**,
set **Number of models**, and choose the additional models in the dropdowns.
Each selected model gets one concurrent request worker through the existing API;
workers share clauses in steps 1.3, 2 and 3. Saved run metadata records the model and
digest used for each clause. Ollama controls concurrent model loading and GPU placement.
For separate cards on the reported Ollama 0.32.6 server, set **GPU index for model 1**
to `0` and **GPU index for model 2** to `1` for the Qwen3.5 9B/35B pair.
These indices are sent as each model's `options.main_gpu`; they refer to GPUs visible
to Ollama. Leave the field blank (**Auto**) for a large model such as DeepSeek 671B
that needs several GPUs. Each explicitly selected card must have enough free VRAM
for its model and context. Placement still depends on the server version and backend;
check actual activity with `nvidia-smi`. Do not infer placement from the worker count.
Disable **Reuse completed model requests** when checking GPU activity so cached
answers do not bypass inference. Restart the updated UI process after syncing files;
GPU selection stays disabled when the running backend does not advertise support.
The worker status distinguishes assigned, active, completed, and failed items,
as well as returned API responses. Zero completed items does not mean no request was sent.

With the checkbox off, the UI sends one request at a time to the primary model.
The shared Ollama service decides whether different models actually run concurrently;
a client worker is not a separate GPU replica.

Step 1.3 defaults to one clause per request with **Labels and optional reasons**,
matching the 14 October request format. Batching and **Labels only** remain optional;
the latter deduplicates supporting text and returns short labels keyed by local IDs.
Source text and original IDs remain in the saved output. Invalid batch responses
can split into smaller groups, but output-token exhaustion stops immediately
instead of repeatedly regenerating and splitting the same request.
**Classification output tokens (1.3)** is a per-request limit, independent of
**Policy output tokens (2)**; both default to 4096. Increasing this limit does not
force a short answer to use the entire allowance.
**Classification thinking (1.3)** defaults to **Auto (off when supported)**.
It uses the server's thinking-control metadata when available and Qwen3.5's documented
on/off control on older Ollama versions; other models retain their default.
Choose **Use model/run default** or **On** to evaluate reasoning explicitly.
**Policy thinking (2)** also defaults to **Auto (off when supported)**. Select
**Model default** or **On** when measuring reasoning explicitly. A response that
exhausts its output budget on reasoning without an answer is not automatically
repeated with a larger budget. The default run context is 16384 tokens,
as in the earlier UI. Classification context **0 (run setting)** uses that same
allocation; smaller allocations are optional. Complete evidence is retained:
requests forbid truncation and context shifting, and can retry with the run's
maximum context on overflow. **Prompt batch tokens** maps to Ollama's `num_batch`
for classification only; 0 preserves server automatic selection.
Worker rows show the model, requested GPU, completed count and current activity.
Detailed request timings and token totals remain available in run metadata.
Policy validation rejects fragment identifiers and LegalTag names absent from the
supplied evidence. Schema and evidence checks still do not establish semantic correctness.

**Run step 3 / Policy review** checks every primary clause, including classifier
negatives and generator abstentions, for missing rules and classification disagreements.
It reviews rule modality, parties, assets and conditions against source evidence,
records model/digest provenance, and flags exact duplicate/conflicting rule content.
Select a different qualified model before running step 3 for stronger review;
same-model reviews are recorded explicitly. **Run through review** uses the same
selected pool throughout and does not guarantee a different reviewer per clause.
Review can take longer because it audits all clauses. Failed/oversized requests
remain incomplete. No review result constitutes human approval or an accuracy score.
Use the frozen **Development** split to qualify models separately before combining
them; keep the held-out test split for final comparisons. Generation reports
separate generated clauses, abstentions needing review, and rejected responses per model.

After restarting the updated UI, open **Generation settings → Compare 1.3 settings**
at the existing browser URL. It uses the primary model and existing Ollama API, displays a comparison
table, and offers a JSON report download. No additional tunnel is needed. The same
comparison is available through `POST /api/benchmark` with `contract` and `model`,
and its results through `GET /api/benchmark`.

Alternatively, measure from a Gorina5 terminal without replacing pipeline outputs:

```bash
"$HOME/.conda/envs/xfed/bin/python" -m xfed.benchmark contract_04 \
  --model llama3.1:70b --limit 24 --output /tmp/xfed-benchmark.json
```

The benchmark uses one model/digest and complete context from the full contract.
It compares single-clause requests, 4-clause batches, compact answers, context allocation, output cap,
8-clause batches, and 4096 prompt batch tokens in successive steps. Warmup is excluded;
API load/prompt/generation times and token counts are included in the report.
Label agreement with the baseline is a regression measure, not ground-truth
accuracy. Use `--limit 0` for all clauses; repeat comparisons before selecting
settings. Optional variants include `batch16`, `prefill512`, `prefill2048`, and
`thinking_off`; unsupported settings are reported
as failures rather than silently ignored.

**Cancel & unload** closes active inference connections, then requests unloading of
the models selected for that run with Ollama's `keep_alive: 0`. The job remains
**Cancelling** until cleanup finishes; unloading failures are reported in the log.
Use **Unload selected models** while idle to release a model left in memory,
including DeepSeek after switching models. Downloaded model files are retained.
Unloading affects the shared server's model; another client's request can keep it
loaded or load it again. Sync the updated files and restart the UI to enable this.

The cache reuses schema-validated results for matching model digest, prompt,
context/output limits, and thinking settings. Each saved stage's `output/.runs.json` entry records wall time, model
digest, per-clause worker assignments, and each worker's API timing/token totals.
For an uncached timing comparison, turn off **Reuse completed model requests**
and run step 1.3 from the same 1.2 input. Download the classification JSON
and save its run metadata before the next run, which replaces those outputs.
Compare classification labels as well as elapsed time. Request-time totals can exceed stage wall time when concurrent.

For ground-truth comparisons of classification and policy generation, use
**Evaluation against reference set**. Start with Development; independent review
is needed before publication. Dataset, splits, metric definitions and CLI usage
are in [evaluation/README.md](evaluation/README.md).

## 5. GPU Monitor - Another Gorina5 Terminal

```bash
watch -n 1 nvidia-smi
```

Installed / loaded models:

```bash
curl -sS http://localhost:11434/api/tags
curl -sS http://localhost:11434/api/ps
```
