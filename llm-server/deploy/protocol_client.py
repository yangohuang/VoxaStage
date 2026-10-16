"""Inside-container streaming protocol collector; emits evidence as JSONL.

The host must persist stdout and acknowledge each request after checking daily
service occupancy. This client does not monitor or stop host services.
"""
import asyncio
import json
import os
import select
import sys
import time

import httpx


async def health(client):
    response = await client.get('/health', timeout=3)
    response.raise_for_status()
    return response.json()


async def wait_idle(client, timeout):
    async with asyncio.timeout(timeout):
        while True:
            state = await health(client)
            if state.get('ready') is True and state.get('busy') is False:
                return state
            await asyncio.sleep(.05)


async def collect(manifest, client, emit, admit):
    case = None
    limits = manifest['timeouts_s']
    try:
        # Retry connection failures only while the worker is starting. During
        # an active trial, failed health checks fail the trial immediately.
        async with asyncio.timeout(limits['startup']):
            while True:
                try:
                    state = await health(client)
                    if state.get('ready') is True and state.get('busy') is False:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(.05)
        emit({'event': 'ready', 'health': state})
        for row in manifest['requests']:
            case = row['case']
            emit({'event': 'request_ready', 'case': case, 'input': row})
            await admit(case)
            started = time.monotonic()
            texts = []
            first_elapsed = None
            busy_at_first = None
            async with asyncio.timeout(limits['request']):
                async with client.stream('POST', '/generate', json={
                    'messages': row['messages'], 'max_new_tokens': row['max_new_tokens'],
                }, timeout=limits['request']) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        elapsed = time.monotonic() - started
                        emit({'event': 'raw_line', 'case': case, 'elapsed_s': elapsed, 'line': line})
                        item = json.loads(line)
                        emit({'event': 'ndjson', 'case': case, 'elapsed_s': elapsed, 'data': item})
                        if 'error' in item:
                            raise RuntimeError(str(item['error']))
                        text = item.get('text')
                        if not isinstance(text, str):
                            raise RuntimeError('invalid_text_delta')
                        if not text:
                            continue
                        texts.append(text)
                        emit({'event': 'chunk', 'case': case, 'elapsed_s': elapsed, 'text': text})
                        if first_elapsed is None:
                            first_elapsed = elapsed
                            first_health = await health(client)
                            busy_at_first = first_health.get('busy') is True
                            emit({'event': 'first_health', 'case': case, 'health': first_health})
                            if (row['cancel_after_first_text'] or case == 'long') and not busy_at_first:
                                raise RuntimeError('not_busy_at_first_text')
                        if row['cancel_after_first_text']:
                            break
            stream_elapsed = time.monotonic() - started
            if not ''.join(texts).strip():
                raise RuntimeError('empty_response')
            if case == 'long' and (len(texts) < 2 or first_elapsed >= stream_elapsed):
                raise RuntimeError('long_reply_not_incremental')
            idle_started = time.monotonic()
            idle = await wait_idle(client, limits['recovery'])
            emit({'event': 'idle', 'case': case, 'health': idle,
                  'wait_s': time.monotonic() - idle_started})
            emit({'event': 'complete', 'case': case, 'text': ''.join(texts),
                  'chunks': len(texts), 'elapsed_s': stream_elapsed,
                  'first_text_s': first_elapsed, 'busy_at_first': busy_at_first,
                  'cancelled_by_client': row['cancel_after_first_text']})
        emit({'event': 'finished', 'requests': len(manifest['requests'])})
    except BaseException as error:
        emit({'event': 'failed', 'case': case, 'error_type': type(error).__name__, 'error': str(error)})
        raise


def emit_stdout(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


def read_ack(fd, case, timeout=20):
    deadline = time.monotonic() + timeout
    line = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise TimeoutError('host_admission_timeout')
        byte = os.read(fd, 1)
        if not byte:
            raise RuntimeError('host_admission_eof')
        if byte == b'\n':
            break
        line.extend(byte)
        if len(line) > 4096:
            raise RuntimeError('host_admission_too_long')
    acknowledgement = json.loads(line)
    if acknowledgement != {'continue': case}:
        raise RuntimeError('host_admission_refused')


async def admit_stdin(case):
    # No executor thread can keep Python alive after a lost host. The host
    # guard continues monitoring during this bounded, synchronous pipe read.
    read_ack(sys.stdin.fileno(), case)


async def main():
    manifest = json.loads(sys.argv[1])
    async with httpx.AsyncClient(base_url='http://127.0.0.1:18311', trust_env=False) as client:
        await collect(manifest, client, emit_stdout, admit_stdin)


if __name__ == '__main__':
    asyncio.run(main())
