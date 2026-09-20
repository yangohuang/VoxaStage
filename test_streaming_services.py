import asyncio
import json
import unittest
from unittest.mock import patch

from pipecat.clocks.system_clock import SystemClock
from pipecat.frames.frames import (
    CancelFrame, EndFrame, ErrorFrame, InterruptionFrame, LLMContextFrame, LLMFullResponseEndFrame,
    LLMFullResponseStartFrame, LLMTextFrame, TTSAudioRawFrame, TTSStartedFrame,
    TTSStoppedFrame, TTSTextFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker, PipelineParams
from pipecat.workers.runner import WorkerRunner
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor, FrameProcessorSetup
from pipecat.utils.asyncio.task_manager import TaskManager

from avatar_session import AvatarOutput
from playback_history import PlaybackHistory, UNHEARD_MARKER
from services import LocalIndexTTS, LocalQwenLLM


def tool_call(name='calculate', arguments=None):
    return '<tool_call>' + json.dumps({'name': name, 'arguments': arguments if arguments is not None else {'expression': '6 * 7'}}) + '</tool_call>'


class Backend:
    def __init__(self, turns):
        self.turns = list(turns)
        self.messages = []

    async def generate(self, messages):
        self.messages.append([dict(m) for m in messages])
        for text in self.turns.pop(0):
            yield text


class Session:
    def __init__(self, generation=7):
        self.generation = generation
        self.events = []
        self.persisted = 0
        self.reasons = []

    async def reset(self, reason):
        self.reasons.append(reason)
        if getattr(self, 'cancel_agent', None) is not None:
            self.cancel_agent()
        self.generation += 1

    async def persist_history(self):
        self.persisted += 1

    async def send(self, event, *, generation=None):
        if generation == self.generation:
            self.events.append(dict(event, generation=generation))


class CaptureLLM(LocalQwenLLM):
    def __init__(self, backend):
        super().__init__(backend)
        self.frames = []
        self.errors = []

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        self.frames.append(frame)

    async def push_error(self, error_msg, **kwargs):
        self.errors.append(error_msg)


class StreamingServiceTests(unittest.IsolatedAsyncioTestCase):
    async def service(self, backend):
        service = CaptureLLM(backend)
        await service.setup(FrameProcessorSetup(clock=SystemClock(), task_manager=TaskManager(), pipeline_worker=None))
        self.addAsyncCleanup(service.cleanup)
        return service

    async def respond(self, service, messages=None):
        context = LLMContext(messages or [{'role': 'user', 'content': '计算六乘七'}])
        await service.process_frame(LLMContextFrame(context), FrameDirection.DOWNSTREAM)

    async def test_non_avatar_tools_execute_and_only_answer_reaches_tts(self):
        backend = Backend([[c for c in tool_call()], ['答案是', '42。']])
        service = await self.service(backend)
        await self.respond(service)
        self.assertEqual(''.join(f.text for f in service.frames if isinstance(f, LLMTextFrame)), '答案是42。')
        self.assertIsInstance(service.frames[0], LLMFullResponseStartFrame)
        self.assertIsInstance(service.frames[-1], LLMFullResponseEndFrame)
        self.assertEqual(len(backend.messages), 2)
        self.assertIn('42', backend.messages[1][-1]['content'])

    async def test_avatar_maps_task_generation_and_tracks_only_visible_answer(self):
        backend = Backend([[tool_call()], ['42。']])
        service = await self.service(backend)
        session, history = Session(), PlaybackHistory()
        service.configure_agent(session, history)
        await self.respond(service)
        self.assertEqual([e['state'] for e in session.events], ['running', 'completed'])
        self.assertTrue(all(e['type'] == 'agent_task' and e['generation'] == 7 for e in session.events))
        self.assertTrue(all(f.avatar_generation == 7 for f in service.frames if isinstance(f, LLMTextFrame)))
        self.assertEqual(history.transcript()[-1]['generated_text'], '42。')
        self.assertEqual(history.transcript()[-1]['heard_text'], '')
        self.assertNotIn('<tool_call>', json.dumps(history.snapshot(), ensure_ascii=False))

    async def test_heard_history_replaces_unconfirmed_aggregator_history(self):
        history = PlaybackHistory(history=[{'role': 'user', 'content': '上一个问题'},
                                           {'role': 'assistant', 'content': '已听到。\n' + UNHEARD_MARKER}])
        backend = Backend([['下一句。']])
        service = await self.service(backend)
        service.configure_agent(Session(), history)
        await self.respond(service, [{'role': 'system', 'content': '人物设定：康辉'},
                                     {'role': 'user', 'content': '旧问题'},
                                     {'role': 'assistant', 'content': '未播放的错误内容'},
                                     {'role': 'user', 'content': '继续'}])
        request = json.dumps(backend.messages[0], ensure_ascii=False)
        self.assertIn('人物设定：康辉', request)
        self.assertIn('已听到。', request)
        self.assertIn(UNHEARD_MARKER, request)
        self.assertNotIn('未播放的错误内容', request)
        self.assertEqual(backend.messages[0][-1]['content'], '继续')

    async def test_new_user_turn_is_persisted_before_model_request(self):
        session = Session()
        history = PlaybackHistory()
        observed = []
        class PersistAwareBackend:
            async def generate(self, messages):
                observed.append((session.persisted, history.snapshot()['history'][0]['content']))
                yield '回复'
        service = await self.service(PersistAwareBackend())
        service.configure_agent(session, history)
        await self.respond(service)
        self.assertEqual(observed, [(1, '计算六乘七')])

    async def test_service_status_removes_urls_paths_errors_and_unknown_fields(self):
        backend = Backend([[tool_call('service_status', {})], ['服务已就绪。']])
        async def health():
            return {'llm': {'ready': True, 'busy': False, 'model': 'Qwen3-4B', 'device': 'cuda:0',
                            'url': 'http://internal-secret:9001', 'error': '/private/secret/path',
                            'token': 'private-token', 'nested': {'ready': True}},
                    'asr': {'ready': False, 'model': '/private/models/secret',
                            'device': 'http://secret', 'configured': True},
                    '/private/unexpected': {'ready': True}}
        backend.health = health
        service = await self.service(backend)
        session = Session()
        service.configure_agent(session, PlaybackHistory())
        await self.respond(service)
        result = session.events[-1]['result']
        self.assertEqual(result['llm'], {'ready': True, 'busy': False, 'model': 'Qwen3-4B', 'device': 'cuda:0'})
        self.assertEqual(result['asr'], {'ready': False, 'configured': True})
        self.assertNotIn('secret', json.dumps(result))
        self.assertEqual(set(result), {'llm', 'asr'})

    async def test_status_reader_exception_does_not_leak_private_error(self):
        backend = Backend([[tool_call('service_status', {})], ['暂时无法读取状态。']])
        async def health():
            raise RuntimeError('http://private-host /private/path password=secret')
        backend.health = health
        service = await self.service(backend)
        session = Session()
        service.configure_agent(session, PlaybackHistory())
        await self.respond(service)
        self.assertEqual(session.events[-1]['state'], 'failed')
        self.assertEqual(session.events[-1]['error'], 'Service status unavailable')
        self.assertNotIn('private', json.dumps(backend.messages))

    async def test_asr_turn_without_interruption_advances_session_before_history_begin(self):
        class ResetSession(Session):
            def __init__(self):
                super().__init__()
                self.reasons = []
            async def reset(self, reason):
                self.reasons.append(reason)
                self.cancel_agent()
                self.generation += 1
        service = await self.service(Backend([['第一句'], ['第二句']]))
        session, history = ResetSession(), PlaybackHistory()
        service.configure_agent(session, history)
        await self.respond(service)
        await self.respond(service, [{'role': 'user', 'content': '下一轮'}])
        self.assertEqual(session.reasons, ['new_turn'])
        self.assertEqual(session.generation, 8)
        self.assertEqual([f.avatar_generation for f in service.frames if isinstance(f, LLMTextFrame)], [7, 8])
        self.assertEqual(history.context()[-1]['content'], '下一轮')
        self.assertEqual(history.transcript()[-1]['generated_text'], '第二句')
        self.assertEqual(service.errors, [])

    async def test_plain_text_reaches_tts_before_model_finishes(self):
        release = asyncio.Event()
        class StreamingBackend:
            async def generate(self, messages):
                yield '先说一句。'
                await release.wait()
                yield '再说一句。'
        service = await self.service(StreamingBackend())
        task = asyncio.create_task(self.respond(service))
        try:
            for _ in range(100):
                if any(isinstance(f, LLMTextFrame) for f in service.frames):
                    break
                await asyncio.sleep(.001)
            self.assertTrue(any(isinstance(f, LLMTextFrame) for f in service.frames))
            self.assertFalse(task.done())
        finally:
            release.set()
            await task

    async def test_interrupt_and_cancel_frames_discard_old_model_text(self):
        for frame_type in (InterruptionFrame, CancelFrame):
            with self.subTest(frame=frame_type.__name__):
                entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
                class LateBackend:
                    async def generate(self, messages):
                        entered.set()
                        try:
                            await release.wait()
                        except asyncio.CancelledError:
                            await release.wait()
                        finished.set()
                        yield '过期回答'
                service = await self.service(LateBackend())
                task = asyncio.create_task(self.respond(service))
                await asyncio.wait_for(entered.wait(), 1)
                await service.process_frame(frame_type(), FrameDirection.DOWNSTREAM)
                await asyncio.wait_for(task, 1)
                release.set()
                await asyncio.wait_for(finished.wait(), 1)
                self.assertFalse(any(isinstance(f, LLMTextFrame) for f in service.frames))
                self.assertIsInstance(service.frames[-1], frame_type)
                self.assertEqual(service.errors, [])

    async def test_concurrent_context_waits_for_interruption_reset_before_begin(self):
        reset_started, release = asyncio.Event(), asyncio.Event()
        class SlowResetSession(Session):
            async def reset(self, reason):
                reset_started.set()
                await release.wait()
                await super().reset(reason)
        session, history = SlowResetSession(), PlaybackHistory()
        backend = Backend([['新回答']])
        service = await self.service(backend)
        service.configure_agent(session, history)
        interrupt = asyncio.create_task(service.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM))
        response = None
        try:
            await asyncio.wait_for(reset_started.wait(), .5)
            response = asyncio.create_task(self.respond(service))
            await asyncio.sleep(.01)
            self.assertEqual(backend.messages, [])
            self.assertEqual(history.context(), [], 'New history must not start while reset is pending')
            self.assertEqual(service.frames, [], 'Interruption must not be forwarded before reset completes')
            release.set()
            await asyncio.wait_for(asyncio.gather(interrupt, response), 1)
            self.assertEqual(session.reasons, ['interrupted'])
            self.assertEqual(session.generation, 8)
            self.assertEqual([f.text for f in service.frames if isinstance(f, LLMTextFrame)], ['新回答'])
            self.assertEqual(service.frames[-2].avatar_generation, 8)
        finally:
            release.set()
            await asyncio.gather(interrupt, *([response] if response else []), return_exceptions=True)

    async def test_late_output_interruption_does_not_cancel_new_context_during_save(self):
        save_started, release = asyncio.Event(), asyncio.Event()
        class SlowSaveSession(Session):
            async def persist_history(self):
                save_started.set()
                await release.wait()
                await super().persist_history()
        class CaptureOutput(AvatarOutput):
            async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
                pass
            async def broadcast_frame(self, frame_type, **kwargs):
                pass
        session, history = SlowSaveSession(), PlaybackHistory()
        service = await self.service(Backend([['这一轮正常回复']]))
        service.configure_agent(session, history)
        output = CaptureOutput(session)
        await output.setup(FrameProcessorSetup(clock=SystemClock(), task_manager=TaskManager(), pipeline_worker=None))
        self.addAsyncCleanup(FrameProcessor.cleanup, output)
        interruption = InterruptionFrame()
        await service.process_frame(interruption, FrameDirection.DOWNSTREAM)
        response = asyncio.create_task(self.respond(service))
        try:
            await asyncio.wait_for(save_started.wait(), .5)
            # TTS can forward this earlier system frame after the new input has
            # reached the LLM and begun an asynchronous persistence operation.
            await output.process_frame(interruption, FrameDirection.DOWNSTREAM)
            self.assertEqual(session.reasons, ['interrupted'], 'Only the LLM entry may reset the cascade turn')
            self.assertEqual(session.generation, 8)
            release.set()
            await asyncio.wait_for(response, 1)
            self.assertEqual([f.text for f in service.frames if isinstance(f, LLMTextFrame)], ['这一轮正常回复'])
            self.assertEqual(history.transcript()[-1]['generated_text'], '这一轮正常回复')
            self.assertEqual(service.errors, [])
        finally:
            release.set()
            await asyncio.gather(response, return_exceptions=True)

    async def test_session_generation_change_discards_tool_completion_and_answer(self):
        entered, release = asyncio.Event(), asyncio.Event()
        backend = Backend([[tool_call('service_status', {})], ['旧服务状态']])
        async def health():
            entered.set()
            await release.wait()
            return {'llm': {'ready': True}}
        backend.health = health
        service = await self.service(backend)
        session, history = Session(), PlaybackHistory()
        service.configure_agent(session, history)
        task = asyncio.create_task(self.respond(service))
        await asyncio.wait_for(entered.wait(), 1)
        session.generation += 1
        release.set()
        await asyncio.wait_for(task, 1)
        self.assertEqual([e['state'] for e in session.events], ['running'])
        self.assertFalse(any(isinstance(f, LLMTextFrame) for f in service.frames))
        self.assertEqual(len(backend.messages), 1)

    async def test_cancel_agent_without_session_generation_change_stops_old_events(self):
        entered, release = asyncio.Event(), asyncio.Event()
        backend = Backend([[tool_call('service_status', {})], ['新回答']])
        async def health():
            entered.set()
            await release.wait()
            return {'llm': {'ready': True}}
        backend.health = health
        service = await self.service(backend)
        session, history = Session(), PlaybackHistory()
        service.configure_agent(session, history)
        task = asyncio.create_task(self.respond(service))
        await asyncio.wait_for(entered.wait(), 1)
        service.cancel_agent()
        await asyncio.wait_for(task, 1)
        self.assertEqual([e['state'] for e in session.events], ['running'])
        self.assertFalse(any(isinstance(f, LLMTextFrame) for f in service.frames))
        history.interrupt()
        session.generation += 1
        await self.respond(service)
        self.assertEqual(service.frames[-2].text, '新回答')


class TTSGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def tts(self):
        service = LocalIndexTTS(None)
        await service.setup(FrameProcessorSetup(clock=SystemClock(), task_manager=TaskManager(), pipeline_worker=None))
        self.addAsyncCleanup(service.cleanup)
        return service

    async def begin(self, service, generation):
        if service._turn_context_id is not None:
            await service.process_frame(LLMFullResponseEndFrame(), FrameDirection.DOWNSTREAM)
        frame = LLMFullResponseStartFrame()
        frame.avatar_generation = generation
        await service.process_frame(frame, FrameDirection.DOWNSTREAM)
        return service._turn_context_id

    async def test_old_context_audio_text_and_stop_stay_old_after_new_turn(self):
        service = await self.tts()
        old_context = await self.begin(service, 7)
        new_context = await self.begin(service, 8)
        captured = []
        async def capture(processor, frame, direction=FrameDirection.DOWNSTREAM):
            captured.append(frame)
        old_frames = [TTSStartedFrame(context_id=old_context),
                      TTSAudioRawFrame(b'\x01\0', 24000, 1, context_id=old_context),
                      TTSTextFrame('old', aggregated_by='sentence', context_id=old_context),
                      TTSStoppedFrame(context_id=old_context)]
        new_frames = [TTSStartedFrame(context_id=new_context),
                      TTSAudioRawFrame(b'\x02\0', 24000, 1, context_id=new_context),
                      TTSTextFrame('new', aggregated_by='sentence', context_id=new_context),
                      TTSStoppedFrame(context_id=new_context)]
        with patch.object(FrameProcessor, 'push_frame', new=capture):
            for frame in old_frames + new_frames:
                await service.push_frame(frame)
        self.assertEqual([getattr(f, 'avatar_generation', None) for f in captured], [7] * 4 + [8] * 4)

    async def test_avatar_output_discards_old_context_and_accepts_new_context_after_reset(self):
        class MediaSession(Session):
            def __init__(self):
                super().__init__(generation=8)
                self.media = []
                self.current = False
            def start_clip(self):
                self.current = True
                self.media.append('start')
            def audio(self, audio):
                if not self.current:
                    raise ValueError('Audio without start')
                self.media.append(audio)
            def mark_text(self, text):
                self.media.append(text)
            def end_clip(self):
                self.current = False
                self.media.append('stop')
        class CaptureOutput(AvatarOutput):
            async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
                pass
        service = await self.tts()
        old_context = await self.begin(service, 7)
        new_context = await self.begin(service, 8)
        session = MediaSession()
        output = CaptureOutput(session)
        await output.setup(FrameProcessorSetup(clock=SystemClock(), task_manager=TaskManager(), pipeline_worker=None))
        self.addAsyncCleanup(FrameProcessor.cleanup, output)
        async def forward(processor, frame, direction=FrameDirection.DOWNSTREAM):
            await output.process_frame(frame, direction)
        with patch.object(FrameProcessor, 'push_frame', new=forward):
            for context, label in ((old_context, 'old'), (new_context, 'new')):
                await service.push_frame(TTSStartedFrame(context_id=context))
                await service.push_frame(TTSAudioRawFrame(label.encode(), 24000, 1, context_id=context))
                await service.push_frame(TTSTextFrame(label, aggregated_by='sentence', context_id=context))
                await service.push_frame(TTSStoppedFrame(context_id=context))
        self.assertEqual(session.media, ['start', b'new', 'new', 'stop'])
        self.assertEqual(session.reasons, [], 'Stale audio must not cause audio-without-start resets')

    async def test_evicted_or_unknown_context_is_never_stamped_with_current_generation(self):
        service = await self.tts()
        oldest = await self.begin(service, 0)
        for generation in range(1, 70):
            await self.begin(service, generation)
        frames = [TTSAudioRawFrame(b'\0\0', 24000, 1, context_id=oldest),
                  TTSTextFrame('unknown', aggregated_by='sentence', context_id='missing')]
        async def discard(processor, frame, direction=FrameDirection.DOWNSTREAM):
            pass
        with patch.object(FrameProcessor, 'push_frame', new=discard):
            for frame in frames:
                await service.push_frame(frame)
        self.assertTrue(all(getattr(f, 'avatar_generation', None) == -1 for f in frames))
        self.assertLessEqual(len(service._avatar_context_generations), 64)

    async def test_late_backend_error_keeps_its_old_audio_context_generation(self):
        class Backend:
            async def synthesize(self, text):
                raise RuntimeError('old request failed')
                yield b''
        service = await self.tts()
        service.backend = Backend()
        old_context = await self.begin(service, 7)
        await self.begin(service, 8)
        frames = [frame async for frame in service.run_tts('old', old_context)]
        self.assertEqual(len(frames), 1)
        self.assertIsInstance(frames[0], ErrorFrame)
        self.assertEqual(getattr(frames[0], 'avatar_generation', None), 7)

    async def test_real_sdk_queue_preserves_generations_for_two_audio_contexts(self):
        captured = []
        class Backend:
            async def synthesize(self, text):
                yield b'\x01\0' * 2400
        class Capture(FrameProcessor):
            async def process_frame(self, frame, direction):
                await super().process_frame(frame, direction)
                if isinstance(frame, (TTSStartedFrame, TTSStoppedFrame, TTSAudioRawFrame, TTSTextFrame)):
                    captured.append(frame)
                await self.push_frame(frame, direction)
        frames = []
        for generation, text in ((7, '第一句。'), (8, '第二句。')):
            start, end = LLMFullResponseStartFrame(), LLMFullResponseEndFrame()
            start.avatar_generation = end.avatar_generation = generation
            frames.extend([start, LLMTextFrame(text), end])
        frames.append(EndFrame())
        worker = PipelineWorker(Pipeline([LocalIndexTTS(Backend()), Capture()]),
                                params=PipelineParams(audio_out_sample_rate=24000), enable_rtvi=False)
        await worker.queue_frames(frames)
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)
        await asyncio.wait_for(runner.run(), 30)
        self.assertEqual([getattr(f, 'avatar_generation', None) for f in captured if isinstance(f, TTSStartedFrame)], [7, 8])
        self.assertEqual([getattr(f, 'avatar_generation', None) for f in captured if isinstance(f, TTSStoppedFrame)], [7, 8])
        contexts = {f.context_id: f.avatar_generation for f in captured if isinstance(f, TTSStartedFrame)}
        self.assertEqual(len(contexts), 2)
        self.assertTrue(all(f.avatar_generation == contexts[f.context_id] for f in captured))
        self.assertEqual(sum(len(f.audio) for f in captured if isinstance(f, TTSAudioRawFrame)), 9600)


if __name__ == '__main__':
    unittest.main()
