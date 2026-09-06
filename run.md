# Generate POLICY.md (Git Bash)

Run from the repository root:

```bash
python scripts/algorithmic/run_policies.py
```

Expected output:

```text
Wrote data\contracts\contract_001\policy_extractions\algo1\POLICY.md
```

To use the virtual environment directly:

```bash
./.venv/Scripts/python.exe scripts/algorithmic/run_policies.py
```

If `contract.txt` is missing, first run:

```bash
./.venv/Scripts/python.exe scripts/preprocess_contract.py
```
