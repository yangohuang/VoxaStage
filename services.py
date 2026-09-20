"""Pipecat services adapting existing localhost workers; no model is loaded here."""
import asyncio
from contextlib import aclosing
from pathlib import Path
import re

from pipecat.audio.utils import pcm_to_wav
from pipecat.frames.frames import (
    CancelFrame, ErrorFrame, InterruptionFrame, LLMContextFrame, LLMFullResponseStartFrame,
    LLMFullResponseEndFrame, LLMTextFrame, TranscriptionFrame, TTSAudioRawFrame,
    TTSStartedFrame, TTSStoppedFrame, TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import LLMService
from pipecat.services.settings import LLMSettings, STTSettings, TTSSettings
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.services.tts_service import TTSService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601
from backend import LLM_MODEL, ASR_MODEL
from agent_runtime import AgentRuntime
from agent_tools import ToolRegistry


class LocalASRSTT(SegmentedSTTService):
    def __init__(self, backend):
        super().__init__(sample_rate=16000, settings=STTSettings(model=ASR_MODEL, language=Language.ZH))
        self.backend = backend
        self._pending_vad_segments = 0

    async def _handle_vad_user_started_speaking(self, frame):
        # Count before awaiting anything: a previous ASR request may finish
        # before this new segment has reached the audio queue.
        self._pending_vad_segments += 1
        await super()._handle_vad_user_started_speaking(frame)

    async def process_audio_frame(self, frame, direction):
        await super().process_audio_frame(frame, direction)
        # Include VAD preroll and padding in the existing ASR's 30-second limit.
        limit = self.sample_rate * 2 * 28
        while self._user_speaking and len(self._audio_buffer) >= limit:
            pcm = bytes(self._audio_buffer[:limit]) + self._trailing_silence()
            del self._audio_buffer[:limit]
            await self._segment_queue.put((pcm_to_wav(pcm, self.sample_rate), False))

    async def _segment_task_handler(self):
        # Only the final VAD segment may finalize a turn. Publishing a finalized
        # intermediate transcript would let the turn timer race the tail ASR.
        texts = []
        failed = False
        while True:
            item = await self._segment_queue.get()
            if item is None:
                return
            audio, final = item if isinstance(item, tuple) else (item, True)
            async for frame in self.run_stt(audio):
                if isinstance(frame, TranscriptionFrame):
                    texts.append(frame.text)
                else:
                    failed = True
                    await self.push_frame(frame)
            if final:
                self._pending_vad_segments = max(0, self._pending_vad_segments - 1)
            if final and not self._pending_vad_segments and not self._user_speaking:
                if not failed:
                    # Empty tail still signals completion of resumed speech.
                    # Hold older text until all VAD segments finish rather than
                    # sending finalized=False (the SDK forces that flag true).
                    await self.push_frame(TranscriptionFrame(
                        ''.join(texts), self._user_id, time_now_iso8601(),
                        language=Language.ZH, finalized=True,
                    ))
                texts.clear()
                failed = False

    async def run_stt(self, audio):
        try:
            text = await self.backend.transcribe(audio)
            if text:
                yield TranscriptionFrame(text, self._user_id, time_now_iso8601(),
                                         language=Language.ZH, finalized=True)
        except Exception as exc:
            yield ErrorFrame(error=f'Local ASR: {exc}')


class LocalQwenLLM(LLMService):
    def __init__(self, backend):
        super().__init__(settings=LLMSettings(
            model=LLM_MODEL, system_instruction=None, temperature=None, max_tokens=256,
            top_p=None, top_k=None, frequency_penalty=None, presence_penalty=None,
            seed=None, filter_incomplete_user_turns=False, user_turn_completion_config=None,
        ))
        self.backend = backend
        self.agent_runtime = AgentRuntime(backend, ToolRegistry(
            Path(__file__).resolve().parent, status_reader=self._agent_status))
        self._agent_session = None
        self._agent_history = None
        self._agent_epoch = 0
        self._agent_begin_generation = None
        self._agent_transition_lock = asyncio.Lock()

    def configure_agent(self, session, history):
        """Bind optional avatar playback history; tools also work without avatars."""
        self.cancel_agent()
        self._agent_session, self._agent_history = session, history
        self._agent_begin_generation = None
        if session is not None:
            session.cancel_agent = self.cancel_agent

    def cancel_agent(self):
        self._agent_epoch += 1
        self.agent_runtime.cancel()

    async def _agent_status(self):
        # Health endpoints are deployment-owned. Do not pass endpoint URLs,
        # traceback text, credentials, or local model paths to the model/UI.
        try:
            health = await self.backend.health()
        except Exception as exc:
            raise ValueError('Service status unavailable') from exc
        if not isinstance(health, dict):
            raise ValueError('Invalid service status response')
        result = {}
        for name in ('llm', 'asr', 'tts'):
            value = health.get(name)
            if not isinstance(value, dict):
                continue
            clean = {key: value[key] for key in ('ready', 'busy', 'configured')
                     if type(value.get(key)) is bool}
            for key in ('model', 'device'):
                item = value.get(key)
                pattern = r'[A-Za-z0-9][A-Za-z0-9_. -]{0,95}' if key == 'model' else r'[A-Za-z0-9][A-Za-z0-9_. :()-]{0,95}'
                if isinstance(item, str) and re.fullmatch(pattern, item):
                    clean[key] = item
            result[name] = clean
        return result

    async def process_frame(self, frame, direction):
        # Invalidate before Pipecat awaits cancellation of its processing task.
        if isinstance(frame, InterruptionFrame):
            self.cancel_agent()
            # System frames can run concurrently with the newly-created process
            # task. Complete the reset here before the next context captures its
            # generation, rather than letting delayed TTS output reset that turn.
            async with self._agent_transition_lock:
                await super().process_frame(frame, direction)
                if self._agent_session is not None:
                    await self._agent_session.reset('interrupted')
                await self.push_frame(frame, direction)
            return
        if isinstance(frame, CancelFrame):
            self.cancel_agent()
        await super().process_frame(frame, direction)
        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return

        async with self._agent_transition_lock:
            self.cancel_agent()
            session, history = self._agent_session, self._agent_history
            messages = frame.context.get_messages()
            user = next((m.get('content') for m in reversed(messages)
                         if isinstance(m, dict) and m.get('role') == 'user'
                         and isinstance(m.get('content'), str)), None)
            # ASR may begin another completed turn without an interruption frame.
            # Reset first because reset also invokes cancel_agent and changes epoch.
            if (session is not None and history is not None and user is not None
                    and self._agent_begin_generation == session.generation):
                await session.reset('new_turn')
            epoch = self._agent_epoch
            generation = session.generation if session is not None else None

        def current():
            return (epoch == self._agent_epoch and
                    (session is None or generation == session.generation))

        started = False
        try:
            if history is not None:
                systems = [dict(m) for m in messages if isinstance(m, dict) and m.get('role') == 'system']
                if user is not None:
                    messages = systems + history.begin(user, generation)
                    self._agent_begin_generation = generation
                    if session is not None:
                        await session.persist_history()
                        if not current():
                            return
                else:
                    messages = systems + history.context()
            response_start = LLMFullResponseStartFrame()
            if generation is not None:
                response_start.avatar_generation = generation
            await self.push_frame(response_start)
            started = True
            if not current():
                return
            async with aclosing(self.agent_runtime.stream(messages)) as events:
                async for event in events:
                    if not current():
                        break
                    if event['type'] == 'text':
                        # This service disables speculative turn-text filtering.
                        # Keep the SDK's first-text metrics, then gate again after
                        # that await before recording or forwarding text.
                        if self.reports_ttfat:
                            await self.stop_ttfat_metrics()
                        if not current():
                            break
                        if history is not None and not history.text(event['text'], generation):
                            break
                        text_frame = LLMTextFrame(event['text'])
                        if generation is not None:
                            text_frame.avatar_generation = generation
                        await self.push_frame(text_frame)
                    elif event['type'] == 'task':
                        if session is not None:
                            payload = dict(event, type='agent_task', generation=generation)
                            await session.send(payload, generation=generation)
                        elif event.get('state') == 'failed' and event.get('label') == 'agent':
                            await self.push_error('Local Qwen: ' + event.get('error', 'Agent failed'))
        except Exception as exc:
            if current():
                await self.push_error(f'Local Qwen: {exc}')
        finally:
            # An old end frame must not finish a newer answer after interruption.
            if started and current():
                response_end = LLMFullResponseEndFrame()
                if generation is not None:
                    response_end.avatar_generation = generation
                await self.push_frame(response_end)


class LocalIndexTTS(TTSService):
    def __init__(self, backend):
        super().__init__(sample_rate=24000, push_start_frame=True, push_stop_frames=True,
                         stop_frame_timeout_s=30,
                         settings=TTSSettings(model='Index-TTS', voice=None, language=Language.ZH))
        self.backend = backend
        self._avatar_next_generation = None
        self._avatar_generation_enabled = False
        self._avatar_context_generations = {}

    async def process_frame(self, frame, direction):
        if isinstance(frame, LLMFullResponseStartFrame):
            generation = getattr(frame, 'avatar_generation', None)
            self._avatar_next_generation = generation if type(generation) is int and generation >= 0 else None
            self._avatar_generation_enabled |= self._avatar_next_generation is not None
        await super().process_frame(frame, direction)

    async def on_turn_context_created(self, context_id):
        # SDK audio queues may drain after a newer LLM turn has started. Bind
        # their immutable context ID now; never infer generation at output time.
        self._avatar_context_generations.setdefault(context_id, self._avatar_next_generation)
        while len(self._avatar_context_generations) > 64:
            self._avatar_context_generations.pop(next(iter(self._avatar_context_generations)))
        await super().on_turn_context_created(context_id)

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        if self._avatar_generation_enabled and isinstance(
                frame, (TTSStartedFrame, TTSStoppedFrame, TTSAudioRawFrame, TTSTextFrame)):
            generation = self._avatar_context_generations.get(frame.context_id)
            # Unknown/evicted contexts are explicitly stale, not current. Keep
            # old mappings through stop delivery and late provider callbacks.
            frame.avatar_generation = generation if generation is not None else -1
        await super().push_frame(frame, direction)


    async def run_tts(self, text, context_id):
        try:
            async for frame in self._stream_audio_frames_from_iterator(
                self.backend.synthesize(text), in_sample_rate=24000, context_id=context_id,
            ):
                yield frame
        except Exception as exc:
            frame = ErrorFrame(error=f'Local Index-TTS: {exc}')
            if self._avatar_generation_enabled:
                generation = self._avatar_context_generations.get(context_id)
                frame.avatar_generation = generation if generation is not None else -1
            yield frame
