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

Stage 2 extracts atomic rules with source IDs, copies original evidence locally, compiles ODRL internally, then
checks each clause for unsupported rules and omissions with a fresh model request.
One correction is allowed; rejected and partial clauses remain in the generation
report. The check uses the same model.
The model-facing grammar separates modalities, uses a small condition vocabulary,
and lets explicit ONLY restrictions state allowed alternatives; code computes their
forbidden complement. Source checks require explicit deadlines, time spans and
resolved targets. They cover known patterns, not every legal construction.
The saved `policy.json` uses the readable v2 schema: one `rules` list and one
`sources` catalogue, including exact quotations and PDF locations. **Export ODRL**
provides the compatibility representation. Old policy files remain readable.
Optional OSDU metadata, unresolved parameters/parties, prerequisites and remedies
are preserved. Schema simplification alone does not demonstrate better extraction.
Actor/asset labels must appear in cited source clauses. Unsupported rule groups are
withheld together with their prerequisites; supported groups can remain as an
explicit partial draft.
Policy thinking Auto enables supported Qwen3.5 reasoning; classification Auto stays
off when supported. UI and policy-pilot defaults reserve 12,288 output tokens in a 32,768-token
context. Existing model configuration and explicit run settings take precedence.
Evaluation saves predictions and provisional scores after each completed request;
unfinished cases stay visible until final scoring.
Relevant and uncertain clauses reach generation; classification exclusions are
recorded. Rules with missing issuers use an ODRL Set. Context overflow fails
explicitly. Rerun through policy creation after updating to rebuild extraction
metadata and context.

The **Evaluation** tab measures task 1.3 classification only. **Policy review**
is a static visual demo with labelled sample content and inert buttons. It has
no node connections, review actions, downloads or amendment logic.

**Guided authoring** creates a contract PDF from selected data-sharing terms.
The PDF passes through the same stages 1.1–2. Terms and the frozen reference stay
outside the extraction input and are used only for post-run comparison. Results
appear in Guided authoring after selecting the saved contract. They measure
agreement with the supported template, not arbitrary-contract accuracy. Changes
create a new contract revision; editing its PDF/reference invalidates the score.

To use the university model through the existing tunnel while keeping scripts and
reports local, run locally (no server changes or remote commands):

```powershell
.venv/Scripts/python.exe -m xfed.ui --ollama-url http://127.0.0.1:11435
```

Model configuration starts from
`config/models.example.yaml`. Classification evaluation uses the complete exposed
Contract 05 (Equinor Volve data licence); Contracts 01–03 are development material.
See [evaluation/README.md](evaluation/README.md). Run regression tests with:

```bash
python -m unittest discover -s tests -p "test_*.py"
```
