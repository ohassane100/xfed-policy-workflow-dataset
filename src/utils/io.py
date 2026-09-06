"""Small shared I/O and immutable-run helpers; Markdown is canonical."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8-sig'))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def publish(destination: Path, files: dict[str, str | dict]) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    lock = destination.with_name(destination.name + '.lock')
    with lock.open('x'):
        pass
    staging = None
    try:
        if destination.exists():
            raise FileExistsError(f'Existing artifact is immutable: {destination}; use a fresh run name')
        staging = Path(tempfile.mkdtemp(prefix='.pending-', dir=destination.parent))
        for name, content in files.items():
            if Path(name).name != name:
                raise ValueError('Artifact filename must be a basename')
            if isinstance(content, str):
                (staging/name).write_text(content, encoding='utf-8', newline='\n')
            else:
                write_json(staging/name, content)
        for attempt in range(6):
            try:
                os.rename(staging, destination)
                break
            except PermissionError:
                if attempt == 5 or destination.exists():
                    raise
                time.sleep(.1 * (attempt+1))
        return destination
    finally:
        if staging and staging.exists():
            shutil.rmtree(staging)
        lock.unlink()
