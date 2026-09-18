import asyncio
import base64
import json
import unittest

from avatar_session import AvatarSession, AvatarSerializer, AvatarInput, AvatarOutput
from pipecat.frames.frames import ErrorFrame, BotStoppedSpeakingFrame, TTSStartedFrame, TTSStoppedFrame
from pipecat.processors.frame_processor import FrameDirection


class Socket:
    def __init__(self):
        self.events = []

    async def send_json(self, event):
        self.events.append(event)


class Backend:
    def __init__(self):
        self.closed = False

    async def stream(self, source):
        try:
            async for audio in source:
                yield {'type': 'media', 'audio': audio.hex()}
        finally:
            self.closed = True


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_renderer_stall_does_not_accumulate_browser_send_credit(self):
        times = []
        class TimedSocket(Socket):
            async def send_json(self, event):
                if event.get('type') == 'media':
                    times.append(asyncio.get_running_loop().time())
                await super().send_json(event)
        class StalledBackend:
            async def stream(self, source):
                async for _ in source:
                    pass
                event = {'type':'media','audio':base64.b64encode(bytes(4800)).decode()}
                yield event
                await asyncio.sleep(1.25)
                for _ in range(20):
                    yield event
        session = AvatarSession(TimedSocket(), StalledBackend())
        session.start_clip(); session.audio(bytes(2)); session.end_clip()
        try:
            async with asyncio.timeout(4):
                while len(times) < 21:
                    await asyncio.sleep(.02)
            self.assertGreater(times[-1] - times[1], .75)
        finally:
            await session.close()

    async def test_reset_during_pacing_waits_for_backend_cleanup(self):
        offered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        packet = base64.b64encode(bytes(48000)).decode()
        class PacedBackend:
            async def stream(self, source):
                try:
                    async for _ in source:
                        pass
                    for i in range(3):
                        if i == 2:
                            offered.set()
                        yield {'type': 'media', 'audio': packet}
                finally:
                    cleaning.set()
                    await release.wait()
        session = AvatarSession(Socket(), PacedBackend())
        session.start_clip()
        session.audio(bytes(2))
        session.end_clip()
        await asyncio.wait_for(offered.wait(), 1)
        await asyncio.sleep(.01)
        reset = asyncio.create_task(session.reset('paced_interrupt'))
        try:
            await asyncio.wait_for(cleaning.wait(), 1)
            await asyncio.sleep(.01)
            self.assertFalse(reset.done(), 'reset must own renderer cleanup during pacing')
        finally:
            release.set()
            await reset
            await session.close()

    async def test_long_audio_splits_at_bound_without_loss_and_reset_clears_count(self):
        socket = Socket()
        session = AvatarSession(socket, Backend())
        session.max_clip_bytes = 8
        session.start_clip()
        session.audio(bytes(range(6)))
        session.audio(bytes(range(6, 20)))
        session.end_clip()
        await asyncio.sleep(.02)
        media = [event for event in socket.events if event['type'] == 'media']
        groups = {}
        for event in media:
            groups.setdefault(event['clip_id'], bytearray()).extend(bytes.fromhex(event['audio']))
        self.assertEqual([len(value) for value in groups.values()], [8, 8, 4])
        self.assertEqual(b''.join(groups.values()), bytes(range(20)))
        await session.reset('test')
        session.start_clip()
        session.audio(bytes(8))
        self.assertEqual(session.current_bytes, 8)
        session.end_clip()
        await session.close()

    async def test_upstream_model_error_resets_media_and_notifies_browser(self):
        stopped = []
        class CaptureInput(AvatarInput):
            async def broadcast_frame(self, kind, **kwargs):
                stopped.append(kind)
            async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
                pass
        socket = Socket()
        session = AvatarSession(socket, Backend())
        await CaptureInput(session).process_frame(ErrorFrame(error='model failure'), FrameDirection.UPSTREAM)
        self.assertEqual([e['type'] for e in socket.events], ['reset', 'error'])
        self.assertEqual(stopped, [BotStoppedSpeakingFrame])
        await session.close()

    async def test_generation_end_does_not_tell_transport_browser_stopped_playing(self):
        forwarded = []
        class CaptureOutput(AvatarOutput):
            async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
                forwarded.append(frame)
        session = AvatarSession(Socket(), Backend())
        output = CaptureOutput(session)
        await output.process_frame(TTSStartedFrame(), FrameDirection.DOWNSTREAM)
        await output.process_frame(TTSStoppedFrame(), FrameDirection.DOWNSTREAM)
        self.assertEqual(forwarded, [], 'Only browser playback acknowledgements drive speaking state')
        await session.close()

    async def test_reset_cancels_backend_and_rejects_stale_output(self):
        socket, backend = Socket(), Backend()
        session = AvatarSession(socket, backend)
        session.start_clip()
        session.audio(b'\x01\x00')
        await asyncio.sleep(.01)
        await session.reset('test')
        self.assertTrue(backend.closed)
        self.assertEqual(session.generation, 1)
        await session.send({'type': 'media'}, generation=0)
        self.assertEqual(socket.events[-1]['type'], 'reset')
        session.start_clip()
        session.audio(b'\x02\x00')
        session.end_clip()
        await asyncio.sleep(.01)
        self.assertEqual(socket.events[-1]['generation'], 1)
        await session.close()

    async def test_audio_backlog_is_bounded(self):
        session = AvatarSession(Socket(), Backend())
        session.start_clip()
        with self.assertRaisesRegex(ValueError, 'backlog'):
            session.audio(bytes(1_440_002))
        await session.close()

    async def test_new_clip_survives_slow_previous_backend_cancellation(self):
        cancelling, release = asyncio.Event(), asyncio.Event()
        class SlowBackend(Backend):
            async def stream(self, source):
                try:
                    async for audio in source:
                        yield {'type': 'media', 'audio': audio.hex()}
                finally:
                    cancelling.set()
                    await release.wait()
        socket = Socket()
        session = AvatarSession(socket, SlowBackend())
        session.start_clip()
        session.audio(b'\x01\x00')
        await asyncio.sleep(.01)
        resetting = asyncio.create_task(session.reset('test'))
        await cancelling.wait()
        session.start_clip()
        session.audio(b'\x02\x00')
        release.set()
        await resetting
        session.audio(b'\x03\x00')
        session.end_clip()
        await asyncio.sleep(.01)
        self.assertEqual([e['audio'] for e in socket.events if e.get('generation') == 1 and 'audio' in e], ['0200', '0300'])
        self.assertEqual(session.queued_bytes, 0)
        await session.close()

    async def test_serializer_rejects_malformed_or_oversized_input(self):
        serializer = AvatarSerializer()
        for data in ['{', '[]', json.dumps({'type': 'text', 'text': 'x'*501}), bytes(32002), b'x']:
            self.assertIsNone(await serializer.deserialize(data))
        self.assertEqual((await serializer.deserialize(bytes(640))).sample_rate, 16000)
        self.assertEqual((await serializer.deserialize('{"type":"interrupt"}')).message['type'], 'interrupt')

    async def test_repeated_reset_preserves_all_pending_backend_cleanup(self):
        cancelling, release = asyncio.Event(), asyncio.Event()
        entered = []

        class SlowBackend(Backend):
            async def stream(self, source):
                entered.append(True)
                try:
                    async for audio in source:
                        yield {'type': 'media', 'audio': audio.hex()}
                finally:
                    cancelling.set()
                    await release.wait()

        socket = Socket()
        session = AvatarSession(socket, SlowBackend())
        resets = []
        try:
            session.start_clip()
            session.audio(b'\x01\x00')
            await asyncio.sleep(.01)
            resets.append(asyncio.create_task(session.reset('first')))
            await asyncio.wait_for(cancelling.wait(), 1)
            session.start_clip()
            session.audio(b'\x02\x00')
            await asyncio.sleep(.01)
            resets.append(asyncio.create_task(session.reset('second')))
            await asyncio.sleep(.01)
            session.start_clip()
            session.audio(b'\x03\x00')
            session.end_clip()
            await asyncio.sleep(.01)
            self.assertEqual(len(entered), 1, 'Newest generation must wait for the original backend cleanup')
            self.assertFalse(resets[-1].done(), 'Repeated reset must still await original cleanup')
            release.set()
            await asyncio.wait_for(asyncio.gather(*resets), 1)
            await asyncio.sleep(.01)
            self.assertEqual(len(entered), 2)
            self.assertEqual([e['audio'] for e in socket.events if e.get('generation') == 2 and 'audio' in e], ['0300'])
            self.assertEqual(session.queued_bytes, 0)
        finally:
            release.set()
            await asyncio.gather(*resets, return_exceptions=True)
            await session.close()
        self.assertFalse(session.tasks, 'Session close must join every generation task')


if __name__ == '__main__':
    unittest.main()
