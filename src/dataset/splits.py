"""One immutable 35/5/10 assignment shared by both tasks; no contract leakage."""
import hashlib
from pathlib import Path
from src.ingestion.download_real_contracts import load_manifest
from src.utils.io import read_json,write_json


def split_ids(ids: list[str],seed: int = 42) -> dict[str,list[str]]:
    if len(ids)!=len(set(ids)):
        raise ValueError('Duplicate contract IDs')
    ordered=sorted(ids,key=lambda c:hashlib.sha256(f'{seed}:{c}'.encode()).hexdigest())
    n,m=int(.7*len(ids)),int(.1*len(ids))
    return dict(train=ordered[:n],validation=ordered[n:n+m],test=ordered[n+m:])


def ensure_splits(root: Path,seed: int = 42) -> dict[str,list[str]]:
    ids=[e['id'] for e in load_manifest(root/'data/contract_sources/source.json')]
    paths={name:root/'data/splits'/f'{name}.json' for name in ('train','validation','test')}
    if any(p.exists() for p in paths.values()):
        if not all(p.exists() for p in paths.values()):
            raise ValueError('Incomplete split manifests')
        splits={name:read_json(p) for name,p in paths.items()}
        flattened=[c for group in splits.values() for c in group]
        if len(flattened)!=len(set(flattened)) or set(flattened)!=set(ids):
            raise ValueError('Split leakage or manifest mismatch')
        return splits
    splits=split_ids(ids,seed)
    for name,path in paths.items():write_json(path,splits[name])
    return splits
