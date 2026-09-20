"""Exercise archived proxy methods with fake sockets; never imports/starts native services."""
import argparse
import ast
import asyncio
import hashlib
import json
import logging
from pathlib import Path
import traceback
from types import SimpleNamespace

from deployment import rewrite_proxy


class Disconnect(Exception):
    code = 1000
    reason = 'fake client disconnected'


class Closed(Exception):
    code = 1000
    reason = 'fake connection closed'


async def exercise(source, error_type):
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'WorkerProxy')
    cls.body = [n for n in cls.body if isinstance(n, ast.AsyncFunctionDef)
                and n.name in ('forward_websocket', '_receive_worker', '_send_client')]
    for node in ast.walk(cls):
        if isinstance(node, ast.arg):
            node.annotation = None
    tracked = []
    connect_options = {}
    context_exit_pending = []
    real_create_task = asyncio.create_task

    def track(coro):
        task = real_create_task(coro)
        tracked.append(task)
        return task

    class WorkerSocket:
        async def recv(self):
            return b'x' * 100

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            context_exit_pending.extend(t.get_coro().__qualname__ for t in tracked if not t.done())

    def connect(*args, **kwargs):
        connect_options.update(kwargs)
        return WorkerSocket()

    async_api = SimpleNamespace(**{name: getattr(asyncio, name) for name in dir(asyncio)})
    async_api.create_task = track
    namespace = dict(asyncio=async_api, logger=logging.getLogger('proxy-cleanup-check'),
                     traceback=traceback, WebSocketDisconnect=Disconnect,
                     ConnectionClosed=Closed, ConnectionClosedError=Closed,
                     ConnectionClosedOK=Closed, websockets=SimpleNamespace(connect=connect))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])),
                 '<archived-proxy-methods>', 'exec'), namespace)
    proxy = namespace['WorkerProxy']()
    proxy.lock = asyncio.Lock()
    proxy.active_connections = 0
    proxy.get_worker_info = lambda _: dict(pid=1, port=1234)

    send_started = asyncio.Event()

    async def blocked_client_input(*args):
        await send_started.wait()
        await asyncio.sleep(0)
        raise error_type()

    proxy._forward_client_to_worker = blocked_client_input

    class ClientSocket:
        async def send_bytes(self, message):
            # Receiver has run first, filled its original 20-slot queue, and is
            # blocked putting the 21st/22nd message. Simulate stalled output
            # while the other forwarding direction reports disconnection.
            send_started.set()
            await asyncio.Future()

        async def send_text(self, message):
            send_started.set()
            await asyncio.Future()

    observed_error = None
    try:
        await asyncio.wait_for(proxy.forward_websocket(ClientSocket(), 'id', 'demo'), 1)
    except error_type:
        observed_error = error_type.__name__
    await asyncio.sleep(0)
    leftovers = []
    for task in tracked:
        if task.done():
            continue
        row = dict(coroutine=task.get_coro().__qualname__)
        if '_receive_worker' in row['coroutine']:
            local = task.get_coro().cr_frame.f_locals
            row.update(queue_size=local['rec_queue'].qsize(), stop_set=local['stop_event'].is_set())
        leftovers.append(row)
    # Clean only tasks created by this in-process test.
    for task in tracked:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tracked, return_exceptions=True)
    return dict(error=observed_error, active_connections=proxy.active_connections,
                pending_before_socket_context_exit=context_exit_pending,
                pending_after_parent_return=leftovers, connect_options=connect_options)


async def main(source, output):
    with source.open('rb') as stream:
        raw = stream.read(2_000_001)
    patched = rewrite_proxy(raw)  # Exact fingerprint checked before executing methods.
    backpressure = raw.decode().replace('rec_queue.put_nowait(message)', 'await rec_queue.put(message)')
    result = dict(scope='Offline archived proxy methods; no network, model, or service operations',
                  native_sha256=hashlib.sha256(raw).hexdigest(),
                  script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  patched_source_sha256=hashlib.sha256(patched.encode()).hexdigest(), cases=[])
    for error in (Disconnect, Closed, asyncio.TimeoutError):
        before = await exercise(backpressure, error)
        after = await exercise(patched, error)
        assert any(x.get('queue_size') == 20 and x['stop_set'] for x in before['pending_after_parent_return'])
        assert after['error'] == error.__name__
        assert after['active_connections'] == 0
        assert not after['pending_before_socket_context_exit']
        assert not after['pending_after_parent_return']
        assert after['connect_options']['close_timeout'] == .5
        result['cases'].append(dict(trigger=error.__name__, before=before, after=after))
    result['status'] = 'passed'
    result['limitation'] = 'Checks configured close_timeout, not real network close timing.'
    output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(dict(status=result['status'], cases=len(result['cases']), output=str(output))))


if __name__ == '__main__':
    logging.basicConfig(level=logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--native-source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.native_source, args.output))
