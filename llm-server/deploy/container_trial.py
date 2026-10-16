"""Run one guarded, isolated GPU protocol trial on the Docker host.

Supply loopback health URLs (SSH reverse forwarding is suitable), a verified
model directory, immutable image ID, frozen manifest, and new output directory.
All raw output is private by default; review it before publishing a report.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit
import uuid

from container_guard import OccupancyGuard, OwnedContainer, validate_idle_health


HEALTH_READER = '''
import json,sys,urllib.request
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
states=[]
for url in json.loads(sys.argv[1]):
 with opener.open(url,timeout=2) as response:
  body=response.read(65537)
  if len(body)>65536:raise RuntimeError('health_response_too_large')
  states.append(json.loads(body))
print(json.dumps(states))
'''


def probe_health(urls, timeout=5):
    if len(urls) != 2 or any(urlsplit(url).hostname not in
                            ('127.0.0.1', 'localhost', '::1') for url in urls):
        raise ValueError('two_loopback_health_urls_required')
    # A separate stdlib-only reader enforces a wall deadline even if a server
    # trickles bytes forever. run() kills and reaps the reader on timeout.
    result = subprocess.run([sys.executable, '-I', '-c', HEALTH_READER, json.dumps(urls)],
                            capture_output=True, text=True, check=True, timeout=timeout)
    validate_idle_health(*json.loads(result.stdout))


def command(args):
    result = subprocess.run(args, check=True, capture_output=True, text=True, timeout=15)
    return result.stdout + (result.stderr if args[1] == 'logs' else '')


class Controller:
    def __init__(self, image, model, manifest, provenance, output, health_urls, invoke=command):
        self.image, self.model = image, Path(model).resolve()
        self.manifest, self.provenance = manifest, provenance
        self.output, self.health_urls, self.invoke = Path(output), health_urls, invoke
        self.run_id = uuid.uuid4().hex
        self.name = 'voxastage-trial-' + self.run_id
        self.owned = self.child = self.guard = None
        self.create_attempted = False
        self.state = {'status': 'preparing', 'run_id': self.run_id, 'name': self.name,
                      'image_id': image, 'removed': False, 'cleanup_errors': [],
                      'health_probes': 0, 'max_health_probe_s': 0,
                      'source_sha256': {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                        for name in ('container_trial.py', 'container_guard.py', 'protocol_client.py')},
                      'manifest_sha256_canonical': hashlib.sha256(json.dumps(manifest, sort_keys=True,
                                                                            ensure_ascii=False).encode()).hexdigest()}
        OwnedContainer('0' * 64, image, self.run_id, invoke)

    def save(self):
        path = self.output / 'result.tmp'
        path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2) + '\n')
        path.replace(self.output / 'result.json')

    def probe(self):
        started = time.monotonic()
        try:
            probe_health(self.health_urls)
        finally:
            self.state['health_probes'] += 1
            self.state['max_health_probe_s'] = max(self.state['max_health_probe_s'], time.monotonic() - started)

    def gpu_admit(self):
        values = self.invoke(['nvidia-smi', '--query-gpu=memory.free', '--format=csv,noheader,nounits']).splitlines()
        if len(values) != 1 or int(values[0]) < 8192:
            raise RuntimeError('single_gpu_free_memory_below_8192MiB_or_ambiguous')
        self.state['admitted_free_mib'] = int(values[0])

    def verify_models(self):
        if self.provenance['revision'] != self.manifest['model_revision']:
            raise RuntimeError('model_revision_mismatch')
        rows = self.provenance['files']
        if len(rows) != 10 or len({row['name'] for row in rows}) != 10:
            raise RuntimeError('model_manifest_incomplete')
        verified = {}
        for row in rows:
            name = row['name']
            if Path(name).name != name:
                raise RuntimeError('invalid_model_filename')
            digest = hashlib.sha256()
            with (self.model / name).open('rb') as handle:
                for block in iter(lambda: handle.read(4 * 1024 * 1024), b''):
                    self.guard.raise_if_failed()
                    digest.update(block)
            if digest.hexdigest() != row['upstream_sha256']:
                raise RuntimeError('model_hash_mismatch:' + name)
            verified[name] = digest.hexdigest()
        self.state.setdefault('model_hash_checks', []).append(verified)
        self.save()

    def abort(self):
        if self.owned is not None:
            self.owned.stop()

    def exec_args(self):
        source = Path(__file__).with_name('protocol_client.py').read_text()
        if hashlib.sha256(source.encode()).hexdigest() != self.state['source_sha256']['protocol_client.py']:
            raise RuntimeError('client_source_changed')
        return ['docker', 'exec', '-i', self.owned.container_id, '/opt/worker/bin/python',
                '-I', '-u', '-c', source, json.dumps(self.manifest, ensure_ascii=False)]

    def protocol(self):
        expected = [r['case'] for r in self.manifest['requests']]
        admitted, completed, finished = [], [], False
        limits = self.manifest['timeouts_s']
        deadline = time.monotonic() + limits['startup'] + 10
        pending = b''
        with (self.output/'events.jsonl').open('wb') as raw, (self.output/'client-stderr.log').open('wb') as stderr:
            self.child = subprocess.Popen(self.exec_args(), stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE, stderr=stderr)
            try:
                while True:
                    self.guard.raise_if_failed()
                    if time.monotonic() >= deadline:
                        raise TimeoutError('client_phase_timeout')
                    ready, _, _ = select.select([self.child.stdout], [], [], .1)
                    if not ready:
                        continue
                    data = os.read(self.child.stdout.fileno(), 65536)
                    if not data:
                        break
                    raw.write(data); raw.flush()
                    pending += data
                    if len(pending) > 4 * 1024 * 1024:
                        raise RuntimeError('oversized_client_record')
                    while b'\n' in pending:
                        line, pending = pending.split(b'\n', 1)
                        event = json.loads(line)
                        kind = event['event']
                        if kind == 'request_ready':
                            case = event['case']
                            if len(admitted) >= len(expected) or case != expected[len(admitted)]:
                                raise RuntimeError('unexpected_request_order')
                            self.guard.check_now()
                            self.child.stdin.write((json.dumps({'continue': case}) + '\n').encode())
                            self.child.stdin.flush()
                            admitted.append(case)
                            deadline = time.monotonic() + limits['request'] + limits['recovery'] + 5
                        elif kind == 'complete':
                            if len(completed) >= len(admitted) or event['case'] != admitted[len(completed)]:
                                raise RuntimeError('unexpected_completion_order')
                            completed.append(event['case'])
                            deadline = time.monotonic() + 25
                        elif kind == 'finished':
                            if event['requests'] != len(expected) or completed != expected:
                                raise RuntimeError('incomplete_protocol')
                            finished = True
                        elif kind == 'failed':
                            raise RuntimeError('client_reported_failure')
                rc = self.child.wait(timeout=10)
                if rc or pending or not finished or completed != expected:
                    raise RuntimeError('incomplete_client_output')
                self.state['completed_cases'] = completed
            finally:
                # Preserve bytes already emitted even if guard/signal interrupted
                # JSON parsing. Stop the exec client; container cleanup is separate.
                if self.child.stdin:
                    self.child.stdin.close()
                    self.child.stdin = None
                if self.child.poll() is None:
                    self.child.terminate()
                try:
                    remainder, _ = self.child.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    self.child.kill()
                    remainder, _ = self.child.communicate(timeout=5)
                raw.write(remainder or b''); raw.flush()
                self.state['client_exit_code'] = self.child.returncode

    def discover_after_create_failure(self):
        # A timed-out Docker create may have completed without returning its ID.
        # Discovery uses our unique name; mutations still require full ID/image/label.
        deadline = time.monotonic() + 3
        while True:
            try:
                info = json.loads(self.invoke(['docker', 'container', 'inspect', self.name]))[0]
                self.owned = OwnedContainer(info['Id'], self.image, self.run_id, self.invoke)
                self.state['container_id'] = info['Id']
                return
            except subprocess.CalledProcessError as error:
                if 'No such' not in (error.stderr or ''):
                    raise
            if time.monotonic() >= deadline:
                raise RuntimeError('create_outcome_unresolved; inspect saved unique name before retry')
            time.sleep(.1)

    def cleanup(self):
        if self.guard is not None:
            try:
                self.guard.stop()
                try:
                    self.guard.raise_if_failed()
                except Exception as error:
                    self.state.update(status='failed', guard_failure=str(error))
                if self.guard.abort_error:
                    self.state['cleanup_errors'].append(self.guard.abort_error)
            except Exception as error:
                self.state['cleanup_errors'].append(str(error))
        if self.owned is None and self.create_attempted:
            try:
                self.discover_after_create_failure()
            except Exception as error:
                self.state['cleanup_errors'].append(str(error))
        if self.owned is not None:
            try:
                self.owned.stop()
                (self.output/'worker.log').write_text(self.invoke(['docker', 'logs', self.owned.container_id]))
                self.state['container_exit'] = json.loads(self.invoke(['docker', 'container', 'inspect', self.owned.container_id]))[0].get('State')
                self.owned.remove()
                self.state['removed'] = True
            except Exception as error:
                self.state['cleanup_errors'].append(str(error))
        if self.state['cleanup_errors']:
            self.state['status'] = 'failed'

    def run(self):
        self.output.mkdir(parents=True, exist_ok=False)
        self.save()
        self.guard = OccupancyGuard(self.probe, self.abort)
        try:
            self.guard.start()
            self.gpu_admit()
            self.verify_models()
            self.guard.check_now()
            self.gpu_admit()
            self.create_attempted = True
            cid = self.invoke(['docker', 'create', '--name', self.name,
                '--label', 'org.voxastage.validation-run=' + self.run_id,
                '--gpus', 'all', '--network', 'none', '--read-only',
                '--tmpfs', '/tmp:rw,nosuid,size=256m', '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges', '--mount',
                'type=bind,src=' + str(self.model) + ',dst=/model,readonly',
                self.image, '--model', '/model', '--port', '18311', '--quantization', '4bit']).strip()
            self.owned = OwnedContainer(cid, self.image, self.run_id, self.invoke)
            self.state.update(container_id=cid, status='running'); self.save()
            self.guard.check_now()
            self.gpu_admit()
            self.owned.start()
            self.protocol()
            self.guard.check_now()
            self.verify_models()
            self.state['status'] = 'passed'
        except BaseException as error:
            self.state.update(status='failed', error_type=type(error).__name__, error=str(error))
        finally:
            saved_handlers = {sig: signal.getsignal(sig) for sig in
                              (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
            try:
                for sig in saved_handlers:
                    signal.signal(sig, signal.SIG_IGN)
                self.cleanup()
                self.save()
            finally:
                for sig, handler in saved_handlers.items():
                    signal.signal(sig, handler)
        return self.state


def install_signal_handlers():
    def interrupted(signum, frame):
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, signal.SIG_IGN)
        raise KeyboardInterrupt('signal:' + str(signum))
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--provenance', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--application-health', required=True)
    parser.add_argument('--model-health', required=True)
    args = parser.parse_args()
    install_signal_handlers()
    result = Controller(args.image, args.model, json.loads(args.manifest.read_text()),
                        json.loads(args.provenance.read_text()), args.output,
                        [args.application_health, args.model_health]).run()
    print(json.dumps(result), flush=True)
    if result['status'] != 'passed':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
