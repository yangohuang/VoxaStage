"""Real loopback HTTP/WebSocket + stdin/stdout CLI drill, without model claims."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import wave

from aiohttp import web

from test_session_client import Socket


class WireTests(unittest.IsolatedAsyncioTestCase):
    async def drill(self, *, fail=False):
        histories = {}
        sockets = []
        audio_packets = []

        async def connect(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            prior = histories.get('fixture-conversation', []) if 'conversation' in request.query else []
            sock = Socket('error' if fail else 'normal', prior)
            sockets.append(sock)
            acknowledged = {}

            async def outgoing():
                while True:
                    await ws.send_str(await sock.recv())

            sender = asyncio.create_task(outgoing())
            try:
                async for message in ws:
                    if message.type == web.WSMsgType.BINARY:
                        audio_packets.append(bytes(message.data))
                        await sock.send(bytes(message.data))
                    elif message.type == web.WSMsgType.TEXT:
                        event = json.loads(message.data)
                        await sock.send(message.data)
                        if event.get('state') == 'progress':
                            acknowledged[(event['generation'], event['clip_id'])] = event['played_samples']
                    rows = sock.history()
                    if acknowledged.get((sock.generation, '2')) != 800:
                        rows[-1]['status'] = 'generated'
                    histories['fixture-conversation'] = rows
            finally:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
            return ws

        async def history(request):
            return web.json_response({'transcript': histories.get('fixture-conversation', [])})

        app = web.Application()
        app.router.add_get('/avatar/ws', connect)
        app.router.add_get('/avatar/conversations/{id}', history)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        child = None
        try:
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                wav_path = root / 'fixture.wav'
                with wave.open(str(wav_path), 'wb') as wav:
                    wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                    wav.writeframes(b'\x01\x00' * 1280)
                cases = [dict(case='text', kind='text', text='你好', timeout_s=4)]
                if not fail:
                    cases += [dict(case='restore', kind='text', text='继续', restore_case='text', timeout_s=4),
                              dict(case='audio', kind='wav', file=wav_path.name,
                                   sha256=hashlib.sha256(wav_path.read_bytes()).hexdigest(), timeout_s=5)]
                manifest = root / 'cases.json'
                manifest.write_text(json.dumps({'cases': cases}))
                child = await asyncio.create_subprocess_exec(
                    sys.executable, '-u', str(Path(__file__).with_name('session_client.py')),
                    '--manifest', str(manifest), '--inputs', directory, '--url', f'http://127.0.0.1:{port}',
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                events = []
                async with asyncio.timeout(15):
                    while line := await child.stdout.readline():
                        event = json.loads(line)
                        events.append(event)
                        if event['event'] == 'request_ready':
                            child.stdin.write((json.dumps({'continue': event['case']}) + '\n').encode())
                            await child.stdin.drain()
                    await child.wait()
                stderr = (await child.stderr.read()).decode()
                return child.returncode, events, stderr, audio_packets, sockets
        finally:
            if child is not None and child.returncode is None:
                child.kill()
                await child.communicate()
            await runner.cleanup()

    async def test_cli_text_restore_and_paced_wav(self):
        rc, events, stderr, packets, sockets = await self.drill()
        self.assertEqual(rc, 0, stderr)
        self.assertEqual(events[-1], {'event': 'finished', 'requests': 3})
        results = next(e['report']['cases'] for e in events if e['event'] == 'capture')
        self.assertTrue(results[1]['restored_history_matches'])
        self.assertEqual(len(packets), 64)
        self.assertTrue(all(len(p) == 640 for p in packets))
        self.assertEqual(b''.join(packets[:4]), b'\x01\x00' * 1280)
        self.assertEqual(b''.join(packets[4:]), bytes(640 * 60))
        self.assertEqual([r['status'] for r in results], ['passed'] * 3)

    async def test_cli_failure_exits_nonzero_with_partial_evidence(self):
        rc, events, stderr, packets, sockets = await self.drill(fail=True)
        self.assertNotEqual(rc, 0)
        self.assertEqual(events[-1]['event'], 'failed')
        report = [e['report'] for e in events if e['event'] == 'checkpoint'][-1]
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['evidence']['clips']['1:1']['samples'], 800)
        self.assertFalse(any(e['event'] == 'complete' for e in events))


if __name__ == '__main__':
    unittest.main()
