"""Acquire target Python 3.11 distributions without executing source packages.

Run inside the pinned base image with /input/requirements.lock and a writable
/output. The original lock pins versions only; this creates a supplementary
file-hash lock from official PyPI release metadata. No installed package copy.
"""
import concurrent.futures
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit
from urllib.request import urlopen

from pip._vendor.packaging.tags import sys_tags
from pip._vendor.packaging.utils import canonicalize_name, parse_wheel_filename
from pip._vendor.packaging.specifiers import SpecifierSet

NLTK_URL = 'https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/tokenizers/punkt_tab.zip'
NLTK_SHA256 = 'e57f64187974277726a3417ca6f181ec5403676c717672eef6a748a7b20e0106'


def select(name, version, ranks):
    url = f'https://pypi.org/pypi/{name}/{version}/json'
    with urlopen(url, timeout=45) as response:
        metadata = json.load(response)
    assert canonicalize_name(metadata['info']['name']) == name
    assert metadata['info']['version'] == version
    candidates = []
    for item in metadata['urls']:
        if item.get('yanked'):
            continue
        if item.get('requires_python') and not SpecifierSet(item['requires_python']).contains(sys.version.split()[0]):
            continue
        if item['packagetype'] == 'bdist_wheel':
            dist, wheel_version, _, tags = parse_wheel_filename(item['filename'])
            assert dist == name and str(wheel_version) == version
            scores = [ranks[t] for t in tags if t in ranks]
            if scores:
                candidates.append((min(scores), item))
        elif item['packagetype'] == 'sdist' and name == 'docopt':
            candidates.append((100000, item))
    assert candidates, 'No permitted compatible distribution: ' + name
    item = min(candidates, key=lambda pair: pair[0])[1]
    parsed = urlsplit(item['url'])
    assert parsed.scheme == 'https' and parsed.hostname == 'files.pythonhosted.org'
    assert Path(item['filename']).name == item['filename']
    assert re.fullmatch('[0-9a-f]{64}', item['digests']['sha256'])
    return dict(package=name, version=version, filename=item['filename'], kind=item['packagetype'],
                bytes=item['size'], sha256=item['digests']['sha256'], url=item['url'], metadata_url=url)


def download(row, directory):
    target = directory / row['filename']
    partial = target.with_name(target.name + '.part')
    started = time.monotonic()
    digest = hashlib.sha256()
    size = 0
    with urlopen(row['url'], timeout=90) as source, partial.open('xb') as output:
        while block := source.read(4 * 1024 * 1024):
            assert time.monotonic() - started < 600, 'distribution deadline'
            size += len(block)
            assert size <= row['bytes']
            digest.update(block)
            output.write(block)
    assert size == row['bytes'] and digest.hexdigest() == row['sha256'], row['filename']
    partial.replace(target)
    return dict(row, verified=True)


def main():
    assert sys.version_info[:2] == (3, 11)
    source = Path('/input/requirements.lock')
    output = Path('/output')
    pins = [re.fullmatch(r'([\w.-]+)==([^\s]+)', line) for line in source.read_text().splitlines() if line.strip()]
    assert len(pins) == 100 and all(pins)
    pins = [(canonicalize_name(m[1]), m[2]) for m in pins]
    assert len(dict(pins)) == 100 and 'setuptools' not in dict(pins)
    ranks = {tag: i for i, tag in enumerate(sys_tags())}
    state = dict(status='selecting', target_python=sys.version,
                 original_lock_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                 hash_origin='Official release file metadata, newly frozen; original lock contains versions only')
    def save():
        temp = output / 'state.tmp'
        temp.write_text(json.dumps(state, indent=2) + '\n')
        temp.replace(output / 'state.json')
    save()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            rows = list(pool.map(lambda pin: select(*pin, ranks), pins))
        bootstrap = select('setuptools', '81.0.0', ranks)
        state.update(status='acquiring', files=rows, bootstrap=bootstrap)
        save()
        directory = output / 'distributions'
        directory.mkdir(exist_ok=False)
        completed = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(download, row, directory) for row in rows + [bootstrap]]
            for future in concurrent.futures.as_completed(futures):
                row = future.result()
                completed.append(row)
                print(json.dumps({'package': row['package'], 'bytes': row['bytes']}), flush=True)
        state['files'] = sorted([row for row in completed if row['package'] != 'setuptools'], key=lambda row: row['package'])
        state['bootstrap'] = next(row for row in completed if row['package'] == 'setuptools')
        # NLTK's expected hash is inherited verbatim from install_demo.sh.
        with urlopen(NLTK_URL, timeout=90) as response:
            data = response.read(32 * 1024 * 1024 + 1)
        assert len(data) <= 32 * 1024 * 1024 and hashlib.sha256(data).hexdigest() == NLTK_SHA256
        (output / 'punkt_tab.zip').write_bytes(data)
        state['nltk'] = dict(url=NLTK_URL, sha256=NLTK_SHA256, bytes=len(data))
        def lock(rows):
            return ''.join(f"{row['package']}=={row['version']} --hash=sha256:{row['sha256']}\n" for row in rows)
        (output / 'requirements.container.lock').write_text(lock(state['files']))
        (output / 'build-bootstrap.lock').write_text(lock([bootstrap]))
        state['status'] = 'verified'
        save()
        (output / 'manifest.json').write_text(json.dumps(state, indent=2) + '\n')
    except BaseException as error:
        state.update(status='failed', error_type=type(error).__name__, error=str(error))
        save()
        raise


if __name__ == '__main__':
    main()
