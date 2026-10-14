# XFed

Run the private pipeline dashboard on Gorina5 from the repository root:

```bash
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
The UI lists installed models; downloads and GPU selection remain server-admin
tasks. It supports stages 1.1-1.4 and 2, independent stage runs, output inspection,
cancellation between operations, and cached model requests for retry.
Rerunning a stage invalidates its downstream outputs.

Ollama is used for relevance classification (1.3) and policy creation (2); enrichment (1.4) resolves context deterministically.

Stage 2 uses Ollama JSON-schema structured outputs and saves an unreviewed
policy draft plus a generation report. Source text/IDs and rule traceability
are checked, but schema validity is not proof of contractual correctness.
Unresolved 1.4 clauses are excluded, and skipped clauses are reported.
Context overflow fails explicitly rather than silently truncating input.

CLI commands are in `run.md`; model configuration starts from
`config/models.example.yaml`. Run focused tests with:

```bash
python -m unittest discover -s "tests/1 preprocessing/1.3 relevance"
python -m unittest discover -s "tests/1 preprocessing/1.4 enrichment"
python -m unittest discover -s "tests/2 policy_generation"
python -m unittest discover -s tests/ui
```
