"""Fetch pinned official Lite weights with resumable ranges and SHA256 checks.

No credentials are needed for these public artifacts. Model files remain local.
"""
import argparse
import fcntl
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import threading
import time
import urllib.request


SOURCES = (
    ('Soul-AILab/SoulX-FlashHead-1_3B', 'flashhead-lite',
     ('Model_Lite/', 'VAE_LTX/', 'README.md')),
    ('facebook/wav2vec2-base-960h', 'wav2vec2-base-960h',
     ('config.json', 'preprocessor_config.json', 'model.safetensors', 'README.md')),
)


def digest(path):
    sha = hashlib.sha256()
    with path.open('rb') as source:
        while block := source.read(4 * 1024 * 1024):
            sha.update(block)
    return sha.hexdigest()


def fetch(root, repo, revision, entry, workers):
    path = root / entry['path']
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + '.download.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _fetch(root, repo, revision, entry, workers)


def _fetch(root, repo, revision, entry, workers):
    path = root / entry['path']
    path.parent.mkdir(parents=True, exist_ok=True)
    size, sha = entry['size'], entry.get('lfs', {}).get('oid')
    if path.exists() and path.stat().st_size == size and (not sha or digest(path) == sha):
        print('verified existing', entry['path'], flush=True)
        return
    partial = path.with_suffix(path.suffix + '.partial')
    progress = path.with_suffix(path.suffix + '.progress.json')
    if progress.exists():
        state = json.loads(progress.read_text())
        if state['revision'] != revision or state['size'] != size:
            raise ValueError('Partial download belongs to another revision')
    else:
        state = dict(revision=revision, size=size,
                     seed_size=partial.stat().st_size if partial.exists() else 0, done=[])
    if state['seed_size'] > size:
        raise ValueError('Partial file larger than expected model')
    lock = threading.Lock()

    def checkpoint():
        temp = progress.with_suffix('.tmp')
        temp.write_text(json.dumps(state))
        temp.replace(progress)

    checkpoint()  # Record contiguous old partial bytes before preallocating.
    fd = os.open(partial, os.O_RDWR | os.O_CREAT, 0o600)
    os.ftruncate(fd, size)
    block_size = 32 * 1024 * 1024
    ranges = [(start, min(start + block_size, size) - 1)
              for start in range(state['seed_size'], size, block_size)
              if start not in state['done']]
    print('fetch', entry['path'], 'remaining parts', len(ranges), flush=True)

    def part(bounds):
        start, end = bounds
        for attempt in range(5):
            try:
                url = f'https://huggingface.co/{repo}/resolve/{revision}/{entry["path"]}?download=true&part={start}'
                request = urllib.request.Request(url, headers={'Range': f'bytes={start}-{end}'})
                with urllib.request.urlopen(request, timeout=120) as response:
                    full_file = response.status == 200 and start == 0 and end == size - 1
                    valid_range = response.status == 206 and response.headers.get('Content-Range') == f'bytes {start}-{end}/{size}'
                    if not (full_file or valid_range):
                        raise ValueError('Server did not honor the requested byte range')
                    offset = start
                    while block := response.read(min(1024 * 1024, end - offset + 1)):
                        view = memoryview(block)
                        while view:
                            written = os.pwrite(fd, view, offset)
                            if written <= 0:
                                raise OSError('Unable to write model data')
                            offset += written
                            view = view[written:]
                        if offset > end:
                            break
                    if offset != end + 1:
                        raise ValueError('Incomplete model byte range')
                with lock:
                    state['done'].append(start)
                    checkpoint()
                    if len(state['done']) % 8 == 0:
                        print(entry['path'], 'completed parts', len(state['done']), flush=True)
                return
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(min(2 ** attempt, 10))

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(part, ranges))
        os.fsync(fd)
    finally:
        os.close(fd)
    actual = digest(partial)
    if sha and actual != sha:
        raise ValueError(f'SHA256 mismatch: {entry["path"]}; keep partial for diagnosis')
    partial.replace(path)
    progress.unlink()
    print('verified', entry['path'], actual, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1] / 'models')
    parser.add_argument('--workers', type=int, default=8, choices=range(1, 17))
    args = parser.parse_args()
    for repo, directory, patterns in SOURCES:
        root = args.output / directory
        root.mkdir(parents=True, exist_ok=True)
        manifest = root / 'source.json'
        if manifest.exists():
            info = json.loads(manifest.read_text())
            if info['repo'] != repo:
                raise ValueError('Unexpected model repository')
        else:
            with urllib.request.urlopen('https://huggingface.co/api/models/' + repo, timeout=60) as response:
                revision = json.load(response)['sha']
            with urllib.request.urlopen(f'https://huggingface.co/api/models/{repo}/tree/{revision}?recursive=true', timeout=60) as response:
                entries = json.load(response)
            info = dict(repo=repo, revision=revision, files=[e for e in entries if e['type'] == 'file'
                        and any(e['path'].startswith(p) if p.endswith('/') else e['path'] == p for p in patterns)])
            manifest.write_text(json.dumps(info, indent=2))
        for entry in info['files']:
            fetch(root, repo, info['revision'], entry, args.workers)


if __name__ == '__main__':
    main()
