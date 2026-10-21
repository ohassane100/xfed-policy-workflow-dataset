# Run Pipeline

## Sources

https://gitlab.ux.uis.no/unix/gpu

## 1. UI - Gorina5

```bash
cd ~/git/xfed-policy-workflow-dataset
git pull --ff-only
"$HOME/.conda/envs/xfed/bin/python" -m pip install -e .
"$HOME/.conda/envs/xfed/bin/python" -m xfed.ui --workers-per-endpoint 1
```

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
workers share clauses in steps 1.3 and 2. Saved run metadata records the model and
digest used for each clause. Ollama controls concurrent model loading and GPU placement.

With the checkbox off, **Concurrent requests** controls requests to the primary
model. Use 1 worker on the current shared API: the measured Ollama 0.32.6 endpoint queues same-model requests.
For ablation, compare concurrency 1 versus 2 with batch size fixed at 1, then
batch size 1 versus 4 with concurrency fixed. Pass `--workers-per-endpoint 3`
to expose a third slot for a further concurrency comparison.
The shared Ollama service decides whether requests actually run concurrently;
a client worker is not a separate GPU replica.

Step 1.3 groups up to 4 clauses per request, with each clause's own context.
The default **Labels only** format deduplicates supporting text inside a request
and emits short labels keyed by local IDs. Source text and original IDs remain in
the saved output. Select **Labels and optional reasons** for the previous format.
Groups are reduced when the input budget requires it; invalid batches split into
smaller batches before individual fallback.
Set **Clauses per classification request** to 1 for the unbatched ablation.
Single-clause requests start with 128 output tokens and retry at the configured
policy limit if the response reaches the output cap. Batch requests scale that
initial budget by group size, up to the configured policy limit. Step 2 retains
its 4096-token default and existing policy logic. **Classification thinking (1.3)** can be
turned off only for models that support this; it leaves the policy thinking
setting unchanged. Classification context **0 (auto)** chooses one allocation for
the stage, starting at 8192 and growing to fit complete clause evidence up to the
maximum context setting (32768 by default). Requests explicitly forbid server
truncation and context shifting, and retry with the maximum context on overflow. Policy context and
generation logic retain their existing settings. **Prompt batch tokens** maps to
Ollama's `num_batch` for classification only; 0 preserves server automatic selection.

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

The cache reuses schema-validated results across identical model replicas, but
keeps model digest, prompt, context/output limits, and thinking settings in the
key. Each saved stage's `output/.runs.json` entry records wall time, model
digest, per-clause worker assignments, and each worker's API timing/token totals.
For an uncached timing comparison, turn off **Reuse completed model requests**
and run step 1.3 from the same 1.2 input. Download the classification JSON
and save its run metadata before the next run, which replaces those outputs.
Compare classification labels as well as elapsed time. Request-time totals can exceed stage wall time when concurrent.

If separate same-model endpoints are already available, pass their URLs with
`--replica-urls http://127.0.0.1:11435,http://127.0.0.1:11436`
and `--workers-per-endpoint 1`, or set `execution.base_urls` in the model YAML.
The same selected model and digest must exist on every endpoint. With access
only to the shared API, the client cannot create replicas or change the
server's `OLLAMA_NUM_PARALLEL` setting.

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
