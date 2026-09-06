# Minimal policy extraction MVP

Run these commands with Python from the existing `.venv` environment:

```powershell
python scripts/preprocess_contract.py
python scripts/algorithmic/run_policies.py
```

The only required dependency is `pypdf` (`pip install -r requirements.txt`).
spaCy is optional: V1 uses a blank English sentencizer when installed and a
regex fallback otherwise. No downloaded language model is needed.

Preprocessing writes `data/contracts/contract_001/source/contract.txt`.
The version runner writes `data/contracts/contract_001/policy_extractions/algo1/POLICY.md`.
Set `VERSION` in that runner to select an implemented version; `v2` would map
to `algo2`, but only V1 is implemented. Re-running replaces that version's candidate.

V1 reconstructs numbered clauses, filters relevant sentences, classifies modal
phrases, and estimates actor/action/object using word patterns. Descriptions
retain the complete sentence, including conditions and exceptions. Source text
uses normalized whitespace, with the clause number retained. Unknown metadata
and actors remain unresolved. Mixed-effect sentences are skipped for manual
review; absence of a candidate rule does not establish permission.

This is a heuristic candidate for manual comparison, not verified ground truth.
PDF extraction can fragment words, and V1 does not resolve definitions,
cross-references, or complex legal scope. Review the original PDF when annotating.
Ground truth and review metadata are left untouched. No workflow, LLM execution,
training, or automated evaluation is included.

Policies contain a `Parties` section with one bullet per party, without a fixed
limit. V1 recognises the explicit registered-party list in this agreement and
retains each name, stated role and organisation number. Other list formats remain
unresolved for manual review. Rules separate `Actor`, `Counterparty/Recipient`,
and `Applies to` (the data, workflow or resource scope). Simple active sentences
use conservative word patterns; uncertain fields remain unresolved. A recipient
absent from a recognised pattern is marked `Not specified`. Descriptions retain
the original sentence and its qualifiers. Actor fields retain source wording;
collective terms such as “Parties” are not automatically expanded into individual
obligations.
