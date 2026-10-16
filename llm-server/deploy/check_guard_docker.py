"""Opt-in CPU-only Docker fault drill. Uses an already-present pinned image.

Run on the Docker host with --image sha256:... --output NEW_DIRECTORY.
Creates only labelled, network-isolated sleep containers; never uses a GPU.
"""
import argparse
import json
from pathlib import Path
import subprocess
import threading
import time
import uuid

from container_guard import OccupancyGuard, OccupancyViolation, OwnedContainer


def invoke(command):
    return subprocess.run(command, check=True, capture_output=True, text=True,
                          timeout=20).stdout


def drill(image, output, failure):
    run_id = uuid.uuid4().hex
    record = {'fault': failure, 'run_id': run_id, 'gpu_used': False, 'events': []}
    container_id = invoke([
        'docker', 'create', '--label', 'org.voxastage.validation-run=' + run_id,
        '--network', 'none', '--read-only', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges', '--user', '65532:65532',
        '--entrypoint', 'python', image, '-u', '-c',
        'import time; print("partial-evidence", flush=True); time.sleep(60)',
    ]).strip()
    owned = OwnedContainer(container_id, image, run_id, invoke)
    injected = threading.Event()
    def probe():
        if injected.is_set():
            if failure == 'health_timeout':
                raise TimeoutError('injected_health_timeout')
            raise OccupancyViolation('injected_application_busy')
    def abort():
        record['events'].append('abort')
        owned.stop()
    guard = OccupancyGuard(probe, abort, interval=.2)
    record['container_id'] = container_id
    try:
        guard.start()
        guard.check_now()
        invoke(['docker', 'start', container_id])
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            logs = invoke(['docker', 'logs', container_id])
            if 'partial-evidence' in logs:
                break
            time.sleep(.1)
        else:
            raise RuntimeError('child_start_timeout')
        (output / (failure + '.partial.log')).write_text(logs)
        # A caller with a different label must not stop this real container.
        impostor = OwnedContainer(container_id, image, 'foreign-run', invoke)
        try:
            impostor.stop()
        except RuntimeError as error:
            if str(error) != 'container_identity_mismatch':
                raise
            record['foreign_cleanup_rejected'] = True
        else:
            raise AssertionError('foreign_cleanup_succeeded')
        info = json.loads(invoke(['docker', 'container', 'inspect', container_id]))[0]
        assert info['State']['Running'], 'foreign_cleanup_stopped_child'
        injected.set()
        deadline = time.monotonic() + 15
        while guard.running and time.monotonic() < deadline:
            time.sleep(.05)
        guard.stop()
        assert record['events'] == ['abort'], record
        assert guard.abort_error is None, guard.abort_error
        info = json.loads(invoke(['docker', 'container', 'inspect', container_id]))[0]
        assert not info['State']['Running'], 'child_still_running'
        record['exit_state'] = info['State']
        injected.clear()
        try:
            guard.check_now()
        except OccupancyViolation:
            record['failure_sticky'] = True
        else:
            raise AssertionError('failed_trial_resumed')
        record['passed'] = True
    finally:
        guard.stop()
        try:
            owned.stop()
            (output / (failure + '.log')).write_text(invoke(['docker', 'logs', container_id]))
            owned.remove()
            record['removed'] = True
        finally:
            (output / (failure + '.json')).write_text(json.dumps(record, indent=2) + '\n')
    return record


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    # Validate identity shape before any Docker mutation.
    OwnedContainer('0' * 64, args.image, 'preflight', invoke)
    args.output.mkdir(parents=True, exist_ok=False)
    for failure in ('application_busy', 'health_timeout'):
        print(json.dumps(drill(args.image, args.output, failure)), flush=True)
