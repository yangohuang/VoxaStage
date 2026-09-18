import asyncio
from types import SimpleNamespace
import unittest

from pipecat.frames.frames import (
    BotStartedSpeakingFrame, InterruptionFrame, LLMContextFrame, LLMTextFrame,
    TranscriptionFrame, TTSAudioRawFrame, VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import FramePushed
from pipecat.processors.frame_processor import FrameDirection
from turn_strategy import LocalTurnStopStrategy
from pipecat.utils.asyncio.task_manager import TaskManager

from turn_settings import TurnSettings
from turn_observer import TurnObserver


class SettingsTests(unittest.TestCase):
    def test_rejects_invalid_timing_before_startup(self):
        for value in ['nan', 'inf', '-1', '0', 'abc', '5']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                TurnSettings.from_env({'PIPECAT_VAD_STOP_SECS': value})

    def test_explicit_baseline_can_be_restored(self):
        settings = TurnSettings.from_env({'PIPECAT_TURN_WAIT_SECS': '0.6'})
        self.assertAlmostEqual(settings.vad_stop_secs + settings.turn_wait_secs, 1.2)


class ObserverTests(unittest.IsolatedAsyncioTestCase):
    async def test_broadcast_does_not_double_count_vad(self):
        rows = []
        observer = TurnObserver('session', llm=object(), emit=rows.append)
        for direction in (FrameDirection.UPSTREAM, FrameDirection.DOWNSTREAM):
            await observer.on_push_frame(FramePushed(
                source=None, destination=None, frame=VADUserStoppedSpeakingFrame(stop_secs=.6),
                direction=direction, timestamp=1_000_000_000))
        self.assertEqual(len(rows), 1)

    async def test_first_stage_only_and_new_turn_resets(self):
        rows = []
        llm = object()
        observer = TurnObserver('session', llm=llm, emit=rows.append)

        async def push(frame, seconds, destination=None):
            await observer.on_push_frame(FramePushed(
                source=None, destination=destination, frame=frame,
                direction=FrameDirection.DOWNSTREAM, timestamp=int(seconds * 1e9)))

        await push(VADUserStoppedSpeakingFrame(stop_secs=.6), 1)
        context = LLMContextFrame(context=None)
        await push(context, 1.3, llm)
        await push(context, 1.31, llm)  # Same frame observed twice.
        await push(LLMTextFrame('答'), 1.5)
        await push(LLMTextFrame('案'), 1.6)
        await push(TTSAudioRawFrame(b'\0\0', 24000, 1), 1.8)
        await push(TTSAudioRawFrame(b'\0\0', 24000, 1), 1.9)
        await push(BotStartedSpeakingFrame(), 2)
        self.assertEqual(len([r for r in rows if r['event'] == 'llm_first_text']), 1)
        pcm = [r for r in rows if r['event'] == 'tts_first_pcm']
        self.assertEqual(len(pcm), 1)
        self.assertAlmostEqual(pcm[0]['since_commit_s'], .5)
        committed = [r for r in rows if r['event'] == 'user_input_committed'][0]
        self.assertAlmostEqual(committed['since_vad_stop_s'], .3)
        await push(InterruptionFrame(), 2.1)
        await push(VADUserStartedSpeakingFrame(), 3)
        await push(LLMContextFrame(context=None), 4, llm)
        await push(LLMTextFrame('新'), 4.2)
        self.assertEqual(rows[-1]['turn_id'], 2)
        self.assertNotIn('since_vad_stop_s', rows[-1])
        self.assertAlmostEqual(rows[-1]['since_commit_s'], .2)


class TurnPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_cancels_deadline_and_waits_for_tail_transcript(self):
        strategy = LocalTurnStopStrategy(user_speech_timeout=.05)
        await strategy.setup(SimpleNamespace(task_manager=TaskManager()))
        stopped = asyncio.Event()

        @strategy.event_handler('on_user_turn_stopped')
        async def on_stopped(*args):
            stopped.set()

        try:
            await strategy.process_frame(VADUserStartedSpeakingFrame())
            await strategy.process_frame(TranscriptionFrame('前半句', 'u', '', finalized=True))
            await strategy.process_frame(VADUserStoppedSpeakingFrame(stop_secs=.2))
            await asyncio.sleep(.01)
            await strategy.process_frame(VADUserStartedSpeakingFrame())
            await asyncio.sleep(.08)
            self.assertFalse(stopped.is_set(), 'Resumed speech must cancel the old deadline')
            # Same logical turn: previous text exists, but the tail is still pending.
            await strategy.process_frame(VADUserStoppedSpeakingFrame(stop_secs=.2))
            await asyncio.sleep(.08)
            self.assertFalse(stopped.is_set())
            await strategy.process_frame(TranscriptionFrame('后半句', 'u', '', finalized=True))
            await asyncio.wait_for(stopped.wait(), .5)
        finally:
            await strategy.cleanup()


if __name__ == '__main__':
    unittest.main()
