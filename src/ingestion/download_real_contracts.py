"""Download the immutable official-source manifest; failures never fabricate content."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
import io
from pathlib import Path
import re
from urllib.parse import urljoin, urlparse, unquote
from urllib.request import Request, urlopen
import zipfile
from src.utils.io import digest, now, read_json, write_json
from src.utils.paths import ROOT, contract_path

MANIFEST = ROOT / 'data/contract_sources/source.json'


def load_manifest(path: Path = MANIFEST) -> list[dict]:
    entries = read_json(path)
    if not isinstance(entries, list) or len(entries) != 50:
        raise ValueError('Manifest must contain exactly 50 entries')
    ids = [e['id'] for e in entries]
    if set(ids) != {f'contract_{i:03d}' for i in range(1,51)}:
        raise ValueError('Manifest needs 50 unique contract_001..contract_050 IDs')
    for entry in entries:
        if urlparse(entry['source_url']).scheme not in {'https','http'}:
            raise ValueError('Source must be an HTTP(S) URL')
        for key in ('organization','title','agreement_type','native_format'):
            if not entry.get(key):
                raise ValueError(f'Missing {key}')
    return entries


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.href = None
        self.label = ''

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.href = dict(attrs).get('href')
            self.label = ''

    def handle_data(self, data):
        if self.href:
            self.label += data

    def handle_endtag(self, tag):
        if tag == 'a' and self.href:
            self.links.append((self.href, ' '.join(self.label.split())))
            self.href = None


def fetch(url: str) -> tuple[bytes, str, str, str]:
    request = Request(url, headers={'User-Agent': 'UniversityContractResearch/2.0', 'Accept': '*/*'})
    with urlopen(request, timeout=30) as response:
        body = response.read(40 * 1024 * 1024 + 1)
        if len(body) > 40 * 1024 * 1024:
            raise ValueError('Source exceeds 40 MiB limit')
        return body, response.geturl(), response.headers.get_content_type(), response.headers.get('Content-Disposition','')


def detect_format(body: bytes) -> str:
    if body.lstrip().startswith(b'%PDF-'):
        return 'pdf'
    if body.startswith(b'PK'):
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                if 'word/document.xml' in archive.namelist():
                    return 'docx'
        except zipfile.BadZipFile:
            pass
    if body.startswith(bytes.fromhex('D0CF11E0A1B11AE1')):
        return 'doc'
    if re.search(br'<(?:!doctype\s+html|html|body)\b', body[:10000], re.I):
        return 'html'
    raise ValueError('Response is not a recognized PDF, DOC, DOCX or HTML document')


def resolve_links(html: str, base: str, title: str) -> list[dict]:
    parser = Links(); parser.feed(html)
    target = set(re.findall(r'[a-z]{3,}', title.lower())) - {'template','sample','agreement','data','sharing','use','and','for','the'}
    candidates = {}
    for href, label in parser.links:
        url = urljoin(base, href)
        if urlparse(url).scheme not in {'http','https'}:
            continue
        if not re.search(r'\.(pdf|docx?)(?:[?#/]|$)', url, re.I):
            continue
        words = (label + ' ' + unquote(url)).lower()
        if not any(w in words for w in ('agreement','dua','dta','dsfc','dtua','dsa')):
            continue
        score = sum(w in words for w in target)
        candidates[url] = dict(url=url, label=label, score=score)
    return sorted(candidates.values(), key=lambda c: (-c['score'], c['url']))


def download(entry: dict, root: Path = ROOT, resolution: dict | None = None) -> dict:
    source = contract_path(entry['id'], root) / 'source'
    source.mkdir(parents=True, exist_ok=True)
    status_path = source / 'metadata.json'
    if status_path.exists():
        old = read_json(status_path)
        originals = list(source.glob('original.*'))
        if originals:
            if old.get('source_url') != entry['source_url'] or len(originals) != 1 or old.get('original_sha256') != digest(originals[0]):
                raise ValueError('Existing source provenance mismatch; refusing overwrite')
            return old
    meta = dict(contract_id=entry['id'], title=entry['title'], organization=entry['organization'],
                agreement_type=entry['agreement_type'], source_url=entry['source_url'],
                original_format=entry['native_format'], document_type='data_collaboration',
                schema_version='2.0', source='real_public_agreement', retrieved_at=now(),
                ingestion_status='pending', preprocessing_status='not_ready')
    try:
        url = resolution.get('download_url', entry['source_url']) if resolution else entry['source_url']
        body, final_url, content_type, disposition = fetch(url)
        kind = detect_format(body)
        if kind == 'html' and not (resolution and resolution.get('agreement_html')):
            # Preserve discovery evidence separately; a landing page is not a contract.
            (source/'landing_page.html').write_bytes(body)
            candidates = resolve_links(body.decode('utf-8', errors='replace'), final_url, entry['title'])
            meta['download_candidates'] = candidates
            if len(candidates) == 1 or (len(candidates) > 1 and candidates[0]['score'] >= 2 and candidates[0]['score'] > candidates[1]['score']):
                url = candidates[0]['url']
                body, final_url, content_type, disposition = fetch(url)
                kind = detect_format(body)
            else:
                raise ValueError('Landing page: no unambiguous official agreement download; human resolution required')
        if kind == 'html' and not (resolution and resolution.get('agreement_html')):
            raise ValueError('Download returned HTML instead of an agreement document')
        original = source / f'original.{kind}'
        with original.open('xb') as stream:
            stream.write(body)
        meta.update(ingestion_status='downloaded', resolved_url=final_url, download_url=url,
                    original_format=kind.upper(), original_sha256=digest(original),
                    original_filename=unquote(Path(urlparse(final_url).path).name),
                    content_type=content_type, content_disposition=disposition,
                    resolution=resolution or {'method': 'direct_or_unique_official_link'})
    except Exception as exc:
        meta.update(ingestion_status='manual_download_required', error=f'{type(exc).__name__}: {exc}')
    write_json(status_path, meta)
    return meta


def import_local(entry: dict, local: Path, root: Path, download_url: str) -> dict:
    """Register a manually acquired official original, never replacing existing evidence."""
    if urlparse(download_url).scheme not in {'http','https'}:
        raise ValueError('Record the actual official acquisition URL')
    body=local.read_bytes();kind=detect_format(body)
    source=contract_path(entry['id'],root)/'source';source.mkdir(parents=True,exist_ok=True)
    if list(source.glob('original.*')):
        raise FileExistsError('A preserved original already exists')
    original=source/f'original.{kind}'
    with original.open('xb') as stream:stream.write(body)
    meta=dict(contract_id=entry['id'],title=entry['title'],organization=entry['organization'],
              agreement_type=entry['agreement_type'],source_url=entry['source_url'],original_format=kind.upper(),
              original_sha256=digest(original),original_filename=local.name,download_url=download_url,
              resolved_url=download_url,source='real_public_agreement',document_type='data_collaboration',
              schema_version='2.0',ingestion_status='downloaded',preprocessing_status='not_ready',
              retrieved_at=now(),resolution={'method':'manual_import; acquisition URL supplied by operator'})
    write_json(source/'metadata.json',meta);return meta


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=MANIFEST)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--contract')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--local-file',type=Path)
    parser.add_argument('--download-url')
    args = parser.parse_args()
    entries = load_manifest(args.manifest)
    resolutions_path = args.manifest.with_name('resolutions.json')
    resolutions = read_json(resolutions_path) if resolutions_path.exists() else {}
    selected = [e for e in entries if not args.contract or e['id'] == args.contract]
    if not selected:
        parser.error('Unknown contract ID')
    if args.local_file:
        if not args.contract or not args.download_url:
            parser.error('--local-file requires --contract and --download-url')
        print(import_local(selected[0],args.local_file,args.root,args.download_url)['ingestion_status'])
        return
    with ThreadPoolExecutor(max_workers=max(1,min(args.workers,8))) as pool:
        for result in pool.map(lambda e: download(e,args.root,resolutions.get(e['id'])), selected):
            print(result['contract_id'], result['ingestion_status'], flush=True)


if __name__ == '__main__':
    main()
