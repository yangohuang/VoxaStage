"""Validate the actual installed app and HTTP startup without model services.

Run as the image's default user with a fresh writable /output bind, tmpfs /tmp
and /opt/app/runtime, and --network none. Logs remain in /output.
"""
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.request

ROOT = Path('/opt/app')
OUTPUT = Path('/output')
report = dict(status='running', scope='Application container installation, regressions and no-model HTTP startup; not model inference.', checks=[])


def verify_source():
    raw = Path('/opt/source.manifest.json').read_bytes()
    rows = json.loads(raw)
    assert len({row['path'] for row in rows}) == len(rows)
    for row in rows:
        relative = Path(row['path'])
        assert not relative.is_absolute() and '..' not in relative.parts
        path = ROOT / relative
        assert not path.is_symlink() and path.resolve().is_relative_to(ROOT)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == row['sha256'], row['path']
    return dict(files=len(rows), manifest_sha256=hashlib.sha256(raw).hexdigest())


def save():
    temp = OUTPUT / 'result.tmp'
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    temp.replace(OUTPUT / 'result.json')


def run(name, args, *, env, timeout=300):
    started = time.monotonic()
    path = OUTPUT / (name + '.log')
    with path.open('x') as log:
        completed = subprocess.run(args, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
    data = path.read_bytes()
    row = dict(name=name, exit_code=completed.returncode, elapsed_s=time.monotonic()-started,
               log_sha256=hashlib.sha256(data).hexdigest())
    counts = re.findall(rb'Ran (\d+) tests?', data)
    if counts:
        row['tests'] = int(counts[-1])
    report['checks'].append(row)
    save()
    if completed.returncode:
        raise RuntimeError('Failed check: ' + name)


def main():
    save()
    env = {k: v for k, v in os.environ.items() if not k.startswith('PIPECAT_') and k not in ('PYTHONPATH', 'PYTHONHOME')}
    env.update(PIPECAT_ASR_URL='http://127.0.0.1:1', PIPECAT_LLM_URL='http://127.0.0.1:1',
               PIPECAT_TTS_URL='http://127.0.0.1:1', PIPECAT_AVATAR_URL='', PIPECAT_DINET_URL='',
               PIPECAT_FLASHHEAD_URL='', PIPECAT_MINICPM_URL='', PIPECAT_CONVERSATION_DIR='/tmp/conversations')
    server = None
    try:
        assert sys.version_info[:2] == (3, 11) and os.getuid() == 65532
        assert not (ROOT / '.env').exists() and not list((ROOT / 'runtime').iterdir())
        report['source_before'] = verify_source()
        expected = dict(line.split('==') for line in (ROOT / 'requirements.lock').read_text().splitlines() if line.strip())
        assert len(expected) == 100
        packages = {}
        for name, version in expected.items():
            dist = metadata.distribution(name)
            assert dist.version == version, name
            location = Path(dist.locate_file('')).resolve()
            assert location.is_relative_to('/opt/application'), name
            packages[name] = dict(version=dist.version, location=str(location))
        import nltk
        from pipecat.audio.vad.silero import SileroVADAnalyzer
        import av, soundfile, onnxruntime
        resource = str(nltk.data.find('tokenizers/punkt_tab/english'))
        assert resource.startswith('/opt/application/nltk_data/')
        vad = SileroVADAnalyzer(sample_rate=16000)
        assert vad is not None
        report['environment'] = dict(python=sys.version, uid=os.getuid(), packages=packages,
                                     nltk_resource=resource, silero_constructed=True,
                                     bootstrap_setuptools=metadata.version('setuptools'),
                                     source_lock_sha256=hashlib.sha256((ROOT / 'requirements.lock').read_bytes()).hexdigest())
        save()
        run('pip-check', [sys.executable, '-m', 'pip', 'check'], env=env)
        run('system-libraries', ['dpkg-query', '-W', 'libgomp1', 'libstdc++6', 'libc6'], env=env)
        run('application-tests', [sys.executable, '-m', 'unittest', 'discover', '-s', '.', '-p', 'test_*.py', '-v'], env=env)
        run('evaluation-tests', [sys.executable, '-m', 'unittest', 'discover', '-s', 'evaluation', '-p', 'test_*.py', '-v'], env=env)
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        port = listener.getsockname()[1]
        with (OUTPUT / 'server.log').open('x') as log:
            try:
                server = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'bot:app', '--fd', str(listener.fileno())],
                                          cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                                          pass_fds=(listener.fileno(),))
            finally:
                listener.close()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            deadline = time.monotonic() + 60
            while True:
                if server.poll() is not None:
                    raise RuntimeError('Server exited during startup')
                try:
                    with opener.open(f'http://127.0.0.1:{port}/avatar', timeout=1) as response:
                        assert response.status == 200 and b'<html' in response.read(1024*1024).lower()
                    break
                except (OSError, TimeoutError):
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(.2)
            routes = [{'route': '/avatar', 'status': 200}]
            for route in ['/avatar/static/app.mjs', '/avatar/static/style.css', '/avatar/static/visual.mjs', '/avatar/providers', '/health']:
                with opener.open(f'http://127.0.0.1:{port}'+route, timeout=5) as response:
                    data = response.read(2*1024*1024+1)
                    assert response.status == 200 and len(data) <= 2*1024*1024
                    routes.append(dict(route=route, status=response.status, bytes=len(data)))
                    if route == '/health':
                        assert json.loads(data)['ready'] is False
                    if route == '/avatar/providers':
                        assert not any(p['configured'] for p in json.loads(data)['providers'])
            report['startup'] = dict(routes=routes, models_configured=False, model_inference_tested=False)
        report['source_after'] = verify_source()
        assert report['source_before'] == report['source_after']
        report['status'] = 'passed'
    except BaseException as error:
        report.update(status='failed', error_type=type(error).__name__, error=str(error))
    finally:
        if server is not None:
            requested = server.poll() is None
            killed = False
            if server.poll() is None:
                server.terminate()
                try:
                    server.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    killed = True
                    server.kill()
                    server.wait(timeout=5)
            report['server_exit_code'] = server.returncode
            shutdown_log = (OUTPUT / 'server.log').read_text()
            graceful = ('Application shutdown complete.' in shutdown_log
                        and 'Finished server process' in shutdown_log)
            report['shutdown'] = dict(sigterm_requested=requested, kill_used=killed,
                                      graceful_shutdown_logged=graceful)
            # Uvicorn may re-raise its captured SIGTERM after graceful shutdown.
            if not (requested and not killed and graceful and server.returncode in (0, -signal.SIGTERM)):
                report['status'] = 'failed'
        save()
        print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
