"""Manage only this project's replacement ASR; never stop other model workers."""
import argparse
import fcntl
import json
import os
import signal
import subprocess
import time

from ctl import ROOT, STATE, OPENER, identity

PID = STATE / 'asr.pid.json'
URL = 'http://127.0.0.1:18315'


def health():
    try:
        with OPENER.open(URL + '/health', timeout=2) as r:
            return json.load(r)
    except Exception:
        return None


def owned():
    if PID.exists():
        record = json.loads(PID.read_text())
        if identity(record['pid']) == record['start_ticks']:
            return record


def stop():
    record = owned()
    if not record:
        print('No owned replacement ASR running')
        return
    os.killpg(record['pid'], signal.SIGTERM)
    for _ in range(100):
        if identity(record['pid']) != record['start_ticks']:
            PID.unlink(missing_ok=True)
            print('Replacement ASR stopped')
            return
        time.sleep(.1)
    raise TimeoutError('ASR did not stop; inspect runtime/asr.log')


def start(engine='qwen'):
    expected = 'Qwen3-ASR-0.6B' if engine == 'qwen' else 'SenseVoiceSmall-int8'
    if owned():
        status = health()
        if not status or not status.get('ready'):
            raise RuntimeError('Owned ASR is unhealthy; inspect runtime/asr.log')
        if status.get('model') != expected:
            raise RuntimeError('Another ASR engine is running; stop it before switching')
        print(json.dumps(status, ensure_ascii=False))
        return
    if health():
        raise RuntimeError('Port 18315 belongs to another process')
    if engine == 'qwen':
        free = int(subprocess.check_output(['nvidia-smi', '--query-gpu=memory.free',
            '--format=csv,noheader,nounits'], text=True).splitlines()[0])
        if free < 3500:
            raise RuntimeError(f'Qwen ASR requires 3500 MiB free at startup; available {free}. Stop only the old experimental ASR explicitly, or select CPU SenseVoice.')
    python = '.venv-qwen-asr/bin/python' if engine == 'qwen' else '.venv-asr/bin/python'
    command = [str(ROOT / python), str(ROOT / 'asr_server.py'), '--engine', engine]
    env = dict(os.environ, PYTHONUNBUFFERED='1', OMP_NUM_THREADS='2', NUMBA_NUM_THREADS='2',
               HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', TOKENIZERS_PARALLELISM='false',
               NUMBA_CACHE_DIR=str(STATE / 'qwen-numba'))
    with (STATE / 'asr.log').open('a') as log:
        process = subprocess.Popen(command, env=env, cwd=ROOT, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
    PID.write_text(json.dumps({'pid': process.pid, 'start_ticks': identity(process.pid), 'command': command}))
    try:
        for _ in range(60):
            if process.poll() is not None:
                raise RuntimeError('ASR exited; inspect runtime/asr.log')
            status = health()
            if status and status.get('ready'):
                print(json.dumps(status, ensure_ascii=False))
                return
            time.sleep(1)
        raise TimeoutError('ASR startup timeout')
    except BaseException:
        stop()
        raise


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=['start', 'stop', 'status'])
    p.add_argument('--engine', choices=['qwen', 'sensevoice'], default='qwen')
    args = p.parse_args()
    STATE.mkdir(exist_ok=True)
    with (STATE / 'asr-ctl.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.action == 'status':
            print(json.dumps({'process': owned(), 'health': health()}, ensure_ascii=False, indent=2))
        elif args.action == 'start':
            start(args.engine)
        else:
            stop()
