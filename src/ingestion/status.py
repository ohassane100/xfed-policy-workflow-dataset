"""Rebuild a per-source provenance/status report covering all 50 manifest entries."""
from src.ingestion.download_real_contracts import load_manifest
from src.utils.paths import ROOT,contract_path
from src.utils.io import read_json,write_json


def report() -> list[dict]:
    rows=[]
    for entry in load_manifest():
        contract=contract_path(entry['id']);path=contract/'source/metadata.json'
        meta=read_json(path) if path.exists() else {'ingestion_status':'not_attempted'}
        row=dict(entry,**{k:v for k,v in meta.items() if k not in entry})
        row['drafted']=(contract/'ground_truth/POLICY.md').exists()
        review=contract/'ground_truth/review.json'
        row['human_verified']=read_json(review).get('human_verified') is True if review.exists() else False
        rows.append(row)
    write_json(ROOT/'results/ingestion_status.json',rows)
    lines=['# Real-source ingestion status','','| Contract | Original | Ingestion | Preprocessing | Draft |','|---|---|---|---|---|']
    for r in rows:lines.append(f"| {r['id']} | {r.get('original_format',r['native_format'])} | {r['ingestion_status']} | {r.get('preprocessing_status','not_ready')} | {r['drafted']} |")
    (ROOT/'results/ingestion_status.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return rows


if __name__=='__main__':
    from collections import Counter
    print(Counter(row['ingestion_status'] for row in report()))
