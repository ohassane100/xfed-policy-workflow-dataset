# Run Pipeline

## Sources

https://gitlab.ux.uis.no/unix/gpu

## 1. UI - Gorina5

```bash
cd ~/git/xfed-policy-workflow-dataset
git pull --ff-only
"$HOME/.conda/envs/xfed/bin/python" -m pip install -e .
"$HOME/.conda/envs/xfed/bin/python" -m xfed.ui --workers-per-endpoint 2
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
Select the contract and one installed model. **Concurrent requests** controls
requests to that same model; workers do not mix models. Start with 2 workers.
For ablation, compare concurrency 1 versus 2 with batch size fixed at 1, then
batch size 1 versus 4 with concurrency fixed. Pass `--workers-per-endpoint 3`
to expose a third slot for a further concurrency comparison.
The shared Ollama service decides whether requests actually run concurrently;
a client worker is not a separate GPU replica.

Step 1.3 groups up to 4 clauses per request, with each clause's own context.
Groups are reduced when the input budget requires it; malformed, missing,
duplicated or truncated batch answers fall back to individual requests.
Set **Clauses per classification request** to 1 for the unbatched ablation.
Single-clause requests start with 512 output tokens and retry at the configured
policy limit if the response reaches the output cap. Batch requests scale that
initial budget by group size, up to the configured policy limit. Step 2 retains
its 4096-token default and existing policy logic. **Classification thinking (1.3)** can be
turned off only for models that support this; it leaves the policy thinking
setting unchanged. Context remains 32768 by default: reduce it only after
checking that clauses and supporting context fit.

The cache reuses schema-validated results across identical model replicas, but
keeps model digest, prompt, context/output limits, and thinking settings in the
key. Each saved stage's `output/.runs.json` entry records wall time, model
digest, per-clause worker assignments, and each worker's API timing/token totals.
For an uncached timing comparison, turn off **Reuse completed model requests**
and run step 1.3 from the same 1.2 input. Download the classification JSON
and save its run metadata before the next run, which replaces those outputs.
Compare classification labels as well as elapsed time. Request-time totals can exceed stage wall time when concurrent.

If an administrator later provides separate GPU-pinned replicas, pass their
endpoints with `--replica-urls http://127.0.0.1:11435,http://127.0.0.1:11436`
and `--workers-per-endpoint 1`, or set `execution.base_urls` in the model YAML.
The same selected model and digest must exist on every endpoint. With access
only to the shared API, the client cannot create replicas or change the
server's `OLLAMA_NUM_PARALLEL` setting.

## 5. GPU Monitor - Another Gorina5 Terminal

```bash
watch -n 1 nvidia-smi
```

Installed / loaded models:

```bash
curl -sS http://localhost:11434/api/tags
curl -sS http://localhost:11434/api/ps
```
