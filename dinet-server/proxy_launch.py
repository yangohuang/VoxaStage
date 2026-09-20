"""Launch the audited proxy with bounded backpressure and disconnect cleanup."""
import argparse
import json
from pathlib import Path
import sys

from deployment import PROXY_SHA256, rewrite_proxy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-root', type=Path, required=True)
    parser.add_argument('--port', type=int, required=True)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port must be between 1 and 65535')
    root = args.native_root.resolve()
    path = root / 'digitalhumanServer/apps/video/app.py'
    with path.open('rb') as source:
        raw = source.read(2_000_001)
    if len(raw) > 2_000_000:
        parser.error('native proxy source exceeds size limit')
    source = rewrite_proxy(raw)
    sys.path.insert(0, str(root))
    sys.argv = [str(path), '--port', str(args.port)]
    print(json.dumps(dict(event='avatar_proxy', bounded_backpressure=True,
                          source_sha256=PROXY_SHA256)), flush=True)
    exec(compile(source, str(path), 'exec'), {'__name__': '__main__', '__file__': str(path)})


if __name__ == '__main__':
    main()
