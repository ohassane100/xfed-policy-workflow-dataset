# XFed

Run the private pipeline dashboard on Gorina5 from the repository root:

```bash
git fetch origin
git checkout dev
git pull --ff-only origin dev
uenv miniconda3-py39
conda activate xfed
python -m pip install -e .
python -m xfed.ui
```

From your own computer, open an SSH tunnel (use your university SSH gateway):

```bash
ssh -J ohassane@ssh1.ux.uis.no -L 8875:127.0.0.1:8765 ohassane@gorina5
```

Open http://localhost:8875. The UI binds to loopback only; do not expose it
publicly. For a different Ollama endpoint, pass `--ollama-url URL` on Gorina5.
The UI lists installed models. Enable **Parallelize the selected model** to choose
concurrent workers with Auto placement or a different GPU for each worker.
Parallel runs launch private services as `ollama` using `/local/ollama/bin/ollama`;
see [runpipeline.md](runpipeline.md) for service settings and model-store discovery.
It supports stages 1.1-1.4 and 2, independent stage runs, output inspection,
cancellation between operations, and cached model requests for retry.
Rerunning a stage invalidates its downstream outputs.

Ollama is used for relevance classification (1.3) and policy creation (2); enrichment (1.4) resolves context deterministically.

Stage 2 extracts atomic rules with literal evidence, compiles ODRL locally, then
checks each clause for unsupported rules and omissions with a fresh model request.
One correction is allowed; rejected and partial clauses remain in the generation
report. The check uses the same model and is not independent human review.
Relevant and uncertain clauses reach generation; classification exclusions are
recorded. Missing issuers produce a draft ODRL Set rather than invented parties.
Context overflow fails explicitly. Schema validity and model checks do not prove
contractual correctness. Rerun through policy creation after updating to rebuild
extraction metadata and context.

CLI commands are in `run.md`; model configuration starts from
`config/models.example.yaml`. Evaluation defaults to the complete independent
Contract 05 (Equinor Volve data licence); Contracts 01–03 are development material.
See [evaluation/README.md](evaluation/README.md). Run focused tests with:

```bash
python -m unittest discover -s "tests/1 preprocessing/1.3 relevance"
python -m unittest discover -s "tests/1 preprocessing/1.4 enrichment"
python -m unittest discover -s "tests/2 policy_generation"
python -m unittest discover -s tests/ui
```
