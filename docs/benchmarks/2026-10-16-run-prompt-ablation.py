"""Replay the predeclared text-only experiment against an idle local worker.

Usage: python this_file.py --output /path/to/new-results.json
No model reconfiguration, service restart, or production prompt changes.
"""
import argparse
import hashlib
import json
import time
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=Path(__file__).with_name('2026-10-16-prompt-ablation-input.json'))
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--llm', default='http://127.0.0.1:18311')
    parser.add_argument('--app', default='http://127.0.0.1:18314')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive reservation: do not silently replace an earlier attempt.
    with args.output.open('x') as f:
        f.write('{}\n')
    raw = args.manifest.read_bytes()
    spec = json.loads(raw)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    result = dict(status='running', manifest_sha256=hashlib.sha256(raw).hexdigest(),
                  generation=spec['generation'], warmup=None, rows=[], inflight=None,
                  finish_reason_available=False, measurement=spec['measurement'])

    def save():
        temp = args.output.with_suffix(args.output.suffix + '.tmp')
        temp.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        temp.replace(args.output)

    def get(url):
        with opener.open(url, timeout=10) as response:
            return json.load(response)

    def idle():
        app = get(args.app.rstrip('/') + '/health')
        model = get(args.llm.rstrip('/') + '/health')
        if app.get('active_sessions') != 0 or not model.get('ready') or model.get('busy'):
            raise RuntimeError('Service unavailable or occupied; stopped without reconfiguration')
        return {k: (Path(str(model[k])).name if k == 'model' else model[k])
                for k in ('model', 'device', 'quantization', 'mode') if k in model}

    def generate(messages, budget):
        payload = dict(messages=messages, max_new_tokens=budget)
        request = urllib.request.Request(args.llm.rstrip('/') + '/generate',
                    data=json.dumps(payload, ensure_ascii=False).encode(),
                    headers={'Content-Type': 'application/json'})
        row = dict(request_sha256=hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                   text='', chunks=[], first_text_s=None, elapsed_s=None, transport_complete=False,
                   finish_reason=None, error=None)
        start = time.monotonic()
        try:
            with opener.open(request, timeout=120) as response:
                for line in response:
                    if time.monotonic() - start > 180:
                        raise TimeoutError('Request exceeded experiment budget')
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if event.get('error'):
                        # Do not publish backend exception strings or machine paths.
                        raise RuntimeError('Worker reported generation error')
                    text = event.get('text', '')
                    if text:
                        elapsed = time.monotonic() - start
                        if row['first_text_s'] is None:
                            row['first_text_s'] = elapsed
                        row['chunks'].append(dict(text=text, elapsed_s=elapsed))
                        row['text'] += text
                row['transport_complete'] = True
        except Exception as exc:
            row['error'] = type(exc).__name__
        row['elapsed_s'] = time.monotonic() - start
        return row

    try:
        result['model'] = idle()
        result['warmup'] = generate([{'role': 'user', 'content': '只回复：准备好了。'}], 32)
        save()
        if result['warmup']['error']:
            raise RuntimeError('Warmup failed')
        conditions = spec['conditions']
        for index, case in enumerate(spec['cases']):
            offset = index % len(conditions)
            for condition in conditions[offset:] + conditions[:offset]:
                model = idle()
                if model != result['model']:
                    raise RuntimeError('Model health identity changed')
                result['inflight'] = dict(case=case['id'], condition=condition['id'])
                save()
                messages = [{'role': 'system', 'content': condition['system_prompt']}] + case['messages']
                row = dict(**result['inflight'], family=case['family'], order=len(result['rows']),
                           **generate(messages, spec['generation']['max_new_tokens']))
                result['rows'].append(row)
                result['inflight'] = None
                save()
                print(json.dumps({k: row[k] for k in ('case', 'condition', 'elapsed_s', 'error')}, ensure_ascii=False), flush=True)
                if row['error']:
                    raise RuntimeError('Trial failed; attempt preserved')
        result['model_after'] = idle()
        result['status'] = 'complete'
    except BaseException as exc:
        result['status'] = 'stopped'
        result['stop_reason'] = type(exc).__name__
        raise
    finally:
        save()


if __name__ == '__main__':
    main()
