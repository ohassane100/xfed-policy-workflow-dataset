Run from the repository root:

```sh
python -m pip install -e .
python -m xfed contract_01 --through 1.1
python -m xfed all --through 1.2
python -m unittest discover -s tests -v
```

Tests mirror stages 1.1-1.4 under `tests/1 preprocessing/`. To run one stage:
`python -m unittest discover -s "tests/1 preprocessing/1.1 extraction" -v`.

For stages 1.3-1.4, copy `config/models.example.yaml` to `config/models.yaml`,
set `relevance_classifier.base_url` and `model`, then run
`python -m xfed all --through 1.4`. Unresolved decisions and missing-reference
details remain in `classified_chunks.json`; only confirmed relevant clauses enter
`enriched_relevance.json`. Rerunning an earlier stage removes stale downstream
preprocessing outputs. Policy generation and review are not implemented.
