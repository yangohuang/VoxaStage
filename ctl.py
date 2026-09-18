"""Manage only this localhost Pipecat process, preserving all model workers."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'runtime'
PID = STATE / 'pid.json'
PORT = 18314
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def backend_environment(environ=None):
    """Persist server settings across ctl restarts; explicit environment wins."""
    path = STATE / 'backend-env.json'
    settings = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(settings, dict) or any(
        not key.startswith('PIPECAT_') or not isinstance(value, str)
        for key, value in settings.items()
    ):
        raise ValueError('backend-env.json must contain only PIPECAT_ string settings')
    return {**settings, **(os.environ if environ is None else environ)}


def identity(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return None if fields[0] == 'Z' else fields[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def health():
    try:
        with OPENER.open(f'http://127.0.0.1:{PORT}/health', timeout=12) as response:
            return json.load(response)
    except Exception:
        return None


def owned():
    if not PID.exists():
        return None
    record = json.loads(PID.read_text())
    return record if identity(record['pid']) == record['start_ticks'] else None


def start():
    if owned():
        print(json.dumps({'already_running': True, 'health': health()}, ensure_ascii=False))
        return
    if health():
        raise RuntimeError(f'Port {PORT} belongs to another process')
    env = backend_environment()
    if env.get('PIPECAT_ASR_MODE', 'local') == 'local' and env.get('PIPECAT_ASR_URL', 'http://127.0.0.1:18315') == 'http://127.0.0.1:18315':
        subprocess.run(['python3', str(ROOT / 'asr_ctl.py'), 'start', '--engine',
                        env.get('PIPECAT_ASR_ENGINE', 'qwen')], check=True, env=env)
    command = [str(ROOT / '.venv/bin/python'), str(ROOT / 'bot.py'),
               '--host', '127.0.0.1', '--port', str(PORT), '-t', 'webrtc']
    env = dict(env, PYTHONUNBUFFERED='1', OMP_NUM_THREADS='2', NUMBA_NUM_THREADS='2',
               NUMBA_CACHE_DIR=str(STATE / 'numba-cache'), NLTK_DATA=str(ROOT / '.venv/nltk_data'))
    with (STATE / 'server.log').open('a') as log:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
    PID.write_text(json.dumps({'pid': process.pid, 'start_ticks': identity(process.pid), 'command': command}))
    for _ in range(60):
        if process.poll() is not None:
            raise RuntimeError(f'Pipecat exited: see {STATE / "server.log"}')
        status = health()
        if status:
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return
        time.sleep(1)
    stop()
    raise TimeoutError('Pipecat startup timed out')


def stop():
    record = owned()
    if not record:
        print('No owned Pipecat process running')
        return
    try:
        request = urllib.request.Request(f'http://127.0.0.1:{PORT}/admin/cancel-sessions',
                                         data=b'', method='POST')
        with OPENER.open(request, timeout=10) as response:
            response.read()
    except Exception as exc:
        print(f'Session cleanup endpoint unavailable: {exc}')
    os.killpg(record['pid'], signal.SIGTERM)
    for _ in range(100):
        if identity(record['pid']) != record['start_ticks']:
            PID.unlink(missing_ok=True)
            print('Pipecat stopped')
            return
        time.sleep(.1)
    raise TimeoutError('Pipecat has not exited; inspect runtime/server.log')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['start', 'stop', 'status'])
    args = parser.parse_args()
    STATE.mkdir(exist_ok=True)
    with (STATE / 'ctl.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action == 'status':
            print(json.dumps({'process': owned(), 'health': health()}, ensure_ascii=False, indent=2))
        else:
            globals()[args.action]()
