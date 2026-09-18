"""Isolated, opt-in experiment for one pinned custom DINet deployment.

Uses installed native code; does not redistribute it or modify its source files.
Start a new process, never import this entry point into a production worker.
"""
import argparse
import asyncio
import hashlib
import inspect
import json
import os
from pathlib import Path
import sys
import textwrap
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--native-root', type=Path, required=True)
parser.add_argument('--host', default='127.0.0.1')
parser.add_argument('--port', type=int, default=18427)
args = parser.parse_args()
if not 1024 <= args.port <= 65535:
    parser.error('port must be 1024..65535')
ROOT = args.native_root.resolve()
sys.path.insert(0, str(ROOT))
SOURCE = ROOT / 'digitalhumanServer/apps/video/worker.py'
EXPECTED = '95dbdd85b278176b4ad6d18aac629314c03b4f085bccbe15bbce0d1038dae504'
if hashlib.sha256(SOURCE.read_bytes()).hexdigest() != EXPECTED:
    raise SystemExit('Native source differs from the audited revision; no experiment started.')

from digitalhumanServer.apps.video import worker

class NoHeartbeat:
    worker_id = 'isolated-pacing-experiment'
    def start(self): pass
    def stop(self): pass
    def update_ws_clients(self, count): pass
    def update_ws_transaction_id(self, value): pass
    def send_event(self): pass

worker.get_worker_heartbeat = lambda *a, **k: NoHeartbeat()
from digitalhumanServer.apps.video import ws_service
import uvicorn

rows = []
active = 0

def measured_delay(instance, started, blocks):
    now = time.perf_counter()
    delay = started + blocks * .2 - instance._lab_lead - now
    instance._lab_row['blocks'].append(dict(block=blocks, elapsed_s=now-started,
        computed_delay_s=delay, input_queue=instance.audio_queue.qsize()))
    return delay

original = textwrap.dedent(inspect.getsource(worker.VideoServer.audio_stream_consumer_realtime))
needle = 'start_time+audio_num*0.2-time.perf_counter()'
assert original.count(needle) == 1
changed = original.replace(needle, '_measured_delay(self, start_time, audio_num)')
namespace = dict(worker.__dict__, _measured_delay=measured_delay)
exec(compile(changed, '<isolated-pacing-consumer>', 'exec'), namespace)
consumer = namespace['audio_stream_consumer_realtime']

async def instrumented(instance):
    global active
    lead = float(instance.websocket.query_params.get('lead', '0'))
    if lead not in (0., .4, .8): raise ValueError('invalid experiment lead')
    row = dict(lead_s=lead, blocks=[], completed=False)
    rows.append(row)
    instance._lab_lead, instance._lab_row = lead, row
    active += 1
    try:
        await consumer(instance)
        row['completed'] = True
    finally:
        active -= 1
        row['consumer_finished'] = True

worker.VideoServer.audio_stream_consumer_realtime = instrumented

@ws_service.app.get('/experiment/status')
async def status():
    return dict(pid=os.getpid(), active=active, connections=len(ws_service.manager.active_connections),
                source_sha256=EXPECTED, script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                trials=rows)

uvicorn.run(ws_service.app, host=args.host, port=args.port, access_log=False)
