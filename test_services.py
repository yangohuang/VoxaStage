import asyncio
import io
import unittest
import wave

from pipecat.frames.frames import InputAudioRawFrame, TranscriptionFrame, VADUserStartedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection
from services import LocalASRSTT


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_tail_completes_and_failure_does_not_poison_next_segment(self):
        received = []

        class Backend:
            async def transcribe(self, audio):
                if audio == b'bad':
                    raise RuntimeError('test failure')
                return '' if audio == b'empty' else '下一句'

        class CaptureSTT(LocalASRSTT):
            async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
                received.append(frame)

        stt = CaptureSTT(Backend())
        for audio in (b'empty', b'bad', b'good', None):
            await stt._segment_queue.put(audio)
        await stt._segment_task_handler()
        transcripts = [f for f in received if isinstance(f, TranscriptionFrame)]
        self.assertEqual([f.text for f in transcripts], ['', '下一句'])
        self.assertTrue(all(f.finalized for f in transcripts))
        self.assertEqual(len(received), 3, 'Backend failure must still emit an error')

    async def test_older_segment_waits_for_resumed_speech_and_empty_tail(self):
        received, first_done = [], asyncio.Event()
        release_tail = asyncio.Event()

        class Backend:
            async def transcribe(self, audio):
                if audio == b'tail':
                    await release_tail.wait()
                    return ''
                first_done.set()
                return '前半句'

        class CaptureSTT(LocalASRSTT):
            async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
                received.append(frame)

        stt = CaptureSTT(Backend())
        await stt._handle_vad_user_started_speaking(VADUserStartedSpeakingFrame())
        await stt._handle_vad_user_started_speaking(VADUserStartedSpeakingFrame())
        stt._user_speaking = False
        await stt._segment_queue.put(b'first')
        task = asyncio.create_task(stt._segment_task_handler())
        try:
            await asyncio.wait_for(first_done.wait(), 1)
            await asyncio.sleep(.01)
            # The second VAD segment has started but its audio is not queued yet.
            self.assertEqual(received, [], 'Old completion must not finalize the newer VAD segment')
            await stt._segment_queue.put(b'tail')
            release_tail.set()
            await stt._segment_queue.put(None)
            await asyncio.wait_for(task, 1)
            self.assertEqual([f.text for f in received], ['前半句'])
            self.assertTrue(received[0].finalized)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_continuous_speech_is_split_before_backend_limit(self):
        stt = LocalASRSTT(None)
        stt._sample_rate = 16000
        stt._audio_buffer_size_1s = 32000
        stt._user_speaking = True
        for _ in range(300):
            await stt.process_audio_frame(InputAudioRawFrame(b'\0' * 3200, 16000, 1),
                                          FrameDirection.DOWNSTREAM)
        self.assertGreater(stt._segment_queue.qsize(), 0, 'Long speech must produce bounded segments')
        segment, final = stt._segment_queue.get_nowait()
        self.assertFalse(final)
        with wave.open(io.BytesIO(segment)) as wav:
            self.assertLessEqual(wav.getnframes(), 480000)
        self.assertLess(len(stt._audio_buffer), 480000 * 2)
        self.assertTrue(stt._user_speaking)

    async def test_partial_segments_wait_for_delayed_final_transcript(self):
        first_done = asyncio.Event()
        release_tail = asyncio.Event()
        received = []

        class Backend:
            async def transcribe(self, audio):
                if audio == b'tail':
                    await release_tail.wait()
                    return '后半句'
                first_done.set()
                return '前半句'

        class CaptureSTT(LocalASRSTT):
            async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
                received.append(frame)

        stt = CaptureSTT(Backend())
        await stt._segment_queue.put((b'first', False))
        await stt._segment_queue.put(b'tail')
        task = asyncio.create_task(stt._segment_task_handler())
        try:
            await asyncio.wait_for(first_done.wait(), 1)
            await asyncio.sleep(.7)
            self.assertEqual(received, [], 'Intermediate segments must not finalize the user turn')
            release_tail.set()
            await stt._segment_queue.put(None)
            await asyncio.wait_for(task, 1)
            self.assertEqual(len(received), 1)
            self.assertIsInstance(received[0], TranscriptionFrame)
            self.assertEqual(received[0].text, '前半句后半句')
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
