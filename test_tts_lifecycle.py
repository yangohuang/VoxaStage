import unittest

from pipecat.frames.frames import (EndFrame, LLMFullResponseStartFrame, LLMFullResponseEndFrame,
                                   LLMTextFrame, TTSStartedFrame, TTSStoppedFrame, TTSAudioRawFrame)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker, PipelineParams
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.workers.runner import WorkerRunner
from services import LocalIndexTTS


class TTSLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_sentences_have_one_audio_context_and_one_final_stop(self):
        captured = []
        class Backend:
            async def synthesize(self, text):
                yield b'\x01\x00' * 2400
        class Capture(FrameProcessor):
            async def process_frame(self, frame, direction):
                await super().process_frame(frame, direction)
                if isinstance(frame, (TTSStartedFrame, TTSStoppedFrame, TTSAudioRawFrame)):
                    captured.append(frame)
                await self.push_frame(frame, direction)
        worker = PipelineWorker(Pipeline([LocalIndexTTS(Backend()), Capture()]),
                                params=PipelineParams(audio_out_sample_rate=24000), enable_rtvi=False)
        await worker.queue_frames([LLMFullResponseStartFrame(), LLMTextFrame('你好。'),
                                   LLMTextFrame('再见。'), LLMFullResponseEndFrame(), EndFrame()])
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)
        await runner.run()
        self.assertEqual(sum(isinstance(f, TTSStartedFrame) for f in captured), 1)
        self.assertEqual(sum(isinstance(f, TTSStoppedFrame) for f in captured), 1)
        self.assertIsInstance(captured[-1], TTSStoppedFrame)
        self.assertEqual(sum(len(f.audio) for f in captured if isinstance(f, TTSAudioRawFrame)), 9600)


if __name__ == '__main__':
    unittest.main()
