#!/usr/bin/env bash
# Portable client/pipeline environment. Model services are installed separately.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
export UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/pipecat-demo-uv-cache}"
export UV_LINK_MODE=copy
uv venv --python "${PIPECAT_PYTHON:-3.11}" .venv
uv pip sync --python .venv/bin/python requirements.lock
archive_path=$(mktemp /tmp/pipecat-demo-punkt-XXXXXX.zip)
trap 'rm -f "$archive_path"' EXIT
curl -fsSL --retry 2 --max-time 120 \
  https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/tokenizers/punkt_tab.zip \
  -o "$archive_path"
.venv/bin/python - "$archive_path" <<'PY'
import hashlib
from pathlib import Path
import sys
import zipfile
archive = Path(sys.argv[1])
if hashlib.sha256(archive.read_bytes()).hexdigest() != 'e57f64187974277726a3417ca6f181ec5403676c717672eef6a748a7b20e0106':
    raise SystemExit('NLTK checksum mismatch; inspect upstream before installing')
target = Path('.venv/nltk_data/tokenizers')
target.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(archive) as resource:
    resource.extractall(target)
PY
uv pip check --python .venv/bin/python
