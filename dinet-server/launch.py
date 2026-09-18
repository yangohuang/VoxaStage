"""Launch one audited existing DINet worker with deployment-configured pacing.

The installed native code and model assets remain external. Native proxy,
heartbeat, model cache and cancellation behavior are reused unchanged.
"""
import argparse
import hashlib
import inspect
import json
import os
from pathlib import Path
import sys
import textwrap

from pacing import load_policy, rewrite_consumer

SOURCES = {
    'digitalhumanServer/apps/video/worker.py': '95dbdd85b278176b4ad6d18aac629314c03b4f085bccbe15bbce0d1038dae504',
    'digitalhumanServer/apps/video/ws_service.py': '2f134a3b3b0d8026bfd8d355077fca793df68b0c60c823a8c7587c5118f48c88',
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-root', type=Path, required=True)
    parser.add_argument('--settings', type=Path, required=True)
    args = parser.parse_args()
    root, settings = args.native_root.resolve(), args.settings.resolve()
    load_policy(settings)  # Reject invalid deployment before native imports.
    for relative, expected in SOURCES.items():
        with (root / relative).open('rb') as source:
            raw = source.read(2_000_001)
        if len(raw) > 2_000_000 or hashlib.sha256(raw).hexdigest() != expected:
            parser.exit(1, 'Native source differs from the audited revision; worker not started.\n')
    sys.path.insert(0, str(root))
    from digitalhumanServer.apps.video import worker
    original = worker.VideoServer.audio_stream_consumer_realtime
    consumer = rewrite_consumer(textwrap.dedent(inspect.getsource(original)), worker.__dict__)

    async def configured_consumer(instance):
        policy = load_policy(settings)
        instance._voxastage_lookahead = policy.lookahead_seconds
        worker.logger.info('voxastage_pacing ' + json.dumps(dict(
            pid=os.getpid(), lookahead_seconds=policy.lookahead_seconds, config_sha256=policy.sha256)))
        return await consumer(instance)

    worker.VideoServer.audio_stream_consumer_realtime = configured_consumer
    from digitalhumanServer.apps.video import ws_service
    print(json.dumps(dict(event='voxastage_pacing_worker', pid=os.getpid(),
                          source_sha256=SOURCES)), flush=True)
    ws_service.start_ws_service()


if __name__ == '__main__':
    main()
