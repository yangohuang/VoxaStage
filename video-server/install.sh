#!/usr/bin/env bash
# Install the pinned single-GPU worker into its own Python environment.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_DIR="${FLASHHEAD_ENV:-$ROOT/runtime/flashhead-standalone}"
PYTHON="${FLASHHEAD_PYTHON:-python3.10}"

command -v uv >/dev/null || { echo 'Install uv before running this script.' >&2; exit 1; }
"$PYTHON" -c 'import platform,sys; assert sys.version_info[:2] == (3,10), "Python 3.10 required"; assert sys.platform == "linux" and platform.machine() == "x86_64", "This lock targets Linux x86_64"'

if [[ -e "$ENV_DIR" ]]; then
  # Do not mutate the user's system environment or the inherited diagnostic env.
  [[ -f "$ENV_DIR/pyvenv.cfg" ]] || { echo 'Existing target is not a virtual environment.' >&2; exit 1; }
  "$ENV_DIR/bin/python" -c 'import pathlib,sys; p=pathlib.Path(sys.prefix)/"pyvenv.cfg"; assert sys.version_info[:2] == (3,10); assert "include-system-site-packages = false" in p.read_text(), "Refusing an environment that inherits system packages"'
else
  uv venv --python "$PYTHON" "$ENV_DIR"
fi

uv pip sync --python "$ENV_DIR/bin/python" --require-hashes \
  --extra-index-url https://download.pytorch.org/whl/cu128 \
  --index-strategy unsafe-best-match "$ROOT/video-server/requirements.lock"
uv pip check --python "$ENV_DIR/bin/python"
"$ENV_DIR/bin/python" -c 'import torch,torchvision,diffusers,transformers,soxr; print("FlashHead environment ready:", torch.__version__, torchvision.__version__, "CUDA", torch.version.cuda)'
echo "Environment: $ENV_DIR"
echo 'Next: prepare the pinned upstream source and weights as described in video-server/README.md.'
