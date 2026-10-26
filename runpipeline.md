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
Select the contract and one model. Enable **Parallelize the selected model** and
set **Number of workers** (2–32). Workers share clauses in steps 1.3 and 2, using
the same model and settings; saved metadata records each clause's worker and digest.
**Auto** starts one private Ollama server with `OLLAMA_NUM_PARALLEL` equal to the
worker count. Ollama chooses placement, including spanning GPUs for large models.
**Different GPU per worker** starts one server per worker, each restricted using
`CUDA_VISIBLE_DEVICES` to the UUID resolved from its physical `nvidia-smi` index.
Vulkan and ROCm are disabled for these NVIDIA workers, so another backend cannot
bypass the CUDA selection. The UI offers only detected cards and limits this mode
to one worker per card, up to the application's cap of 32. Auto permits up to 32
request workers; usable concurrency depends on memory and model/context size.
Each worker loads its own copy of the model; the model and context must fit its GPU.
Auto uses one model runner with additional context memory for concurrent requests.
Check actual placement and available memory with `nvidia-smi`.
Worker rows say **requested** until an uncached response verifies placement.
Before accepting each response, the app matches that server's process descendants
against `nvidia-smi` GPU UUIDs and checks model GPU residency through `/api/ps`.
Wrong, missing or unverifiable placement fails the run instead of accepting the
response. Verification needs visibility of the worker processes through `ps` and
`nvidia-smi`. The worker tooltip includes its UUID, observed GPUs and any error.
Disable **Reuse completed model requests** when checking GPU activity so cached
answers do not bypass inference. Restart the updated UI process after syncing files;
parallelization stays disabled when the running backend does not advertise support.
The worker status distinguishes assigned, active, completed, and failed items,
as well as returned API responses. Zero completed items does not mean no request was sent.

Private services launch `/local/ollama/bin/ollama serve` as `ollama` through
noninteractive sudo, using isolated process groups and temporary loopback ports.
They reuse downloaded models, discovered from the
system service's `OLLAMA_MODELS`, falling back to the `ollama` user's default store.
If necessary, set `execution.service.models_dir` in `config/models.yaml` to the
existing absolute model-store path. The same section supports `binary`, `user`,
and `startup_timeout` (seconds). Startup fails if sudo cannot run noninteractively
or if the selected model/digest is unavailable in that store; models are never
downloaded automatically. Noninteractive sudo as `ollama` must allow both worker
startup and signals for cleanup throughout the run. Root systemctl access is unnecessary.
The example configuration also enables same-model workers for CLI pipeline/evaluation runs.

Services started by a run stop on completion, failure, cancellation, or normal UI
shutdown. The existing system service is left running. With parallelization off,
the UI sends one request at a time to that shared service.
Use **Reset contract** beside the run buttons to delete all generated JSON,
cached responses and run history for the selected contract, then return to step
1.1. Confirm the deletion in the dialog. Reset is unavailable while a job is active.
Private workers allocate additional model memory alongside any models still
loaded by the shared service.
The API provides schema constraints, generation controls and cancellation to the
same Ollama installation; invoking the Ollama CLI would also use its service API.

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
Worker rows show the worker, model, selected GPU, completed count and current activity.
Detailed request timings and token totals remain available in run metadata.
Policy validation rejects fragment identifiers and LegalTag names absent from the
supplied evidence.

Automated step 3 is disabled; its module, prompt and schema are retained for future
use. **Run through policy creation** finishes at step 2. Inspect generated clauses,
abstentions and rejections in the generation report.

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

**Cancel & unload** closes active inference connections and stops private workers.
For a run on the shared service, it requests unloading of the selected model with
Ollama's `keep_alive: 0`. The job remains
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

For classification and policy evaluation, use **Independent contract evaluation**
with Contract 05. Use the CLI development split (Contracts 01–03) to adjust prompts
and settings. Dataset, metric definitions and CLI usage are in
[evaluation/README.md](evaluation/README.md).

## 5. GPU Monitor - Another Gorina5 Terminal

```bash
watch -n 1 nvidia-smi
```

Installed / loaded models:

```bash
curl -sS http://localhost:11434/api/tags
curl -sS http://localhost:11434/api/ps
```
