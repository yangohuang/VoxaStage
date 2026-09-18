"""Pipecat services adapting existing localhost workers; no model is loaded here."""
from pipecat.audio.utils import pcm_to_wav
from pipecat.frames.frames import (
    ErrorFrame, LLMContextFrame, LLMFullResponseStartFrame, LLMFullResponseEndFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import LLMService
from pipecat.services.settings import LLMSettings, STTSettings, TTSSettings
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.services.tts_service import TTSService
from pipecat.transcriptions.language import Language
from pipecat.utils.time import time_now_iso8601
from backend import LLM_MODEL, ASR_MODEL


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

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if not isinstance(frame, LLMContextFrame):
            await self.push_frame(frame, direction)
            return
        await self.push_frame(LLMFullResponseStartFrame())
        try:
            async for text in self.backend.generate(frame.context.get_messages()):
                await self._push_llm_text(text)
        except Exception as exc:
            await self.push_error(f'Local Qwen: {exc}')
        finally:
            await self.push_frame(LLMFullResponseEndFrame())


class LocalIndexTTS(TTSService):
    def __init__(self, backend):
        super().__init__(sample_rate=24000, push_start_frame=True, push_stop_frames=True,
                         stop_frame_timeout_s=30,
                         settings=TTSSettings(model='Index-TTS', voice=None, language=Language.ZH))
        self.backend = backend

    async def run_tts(self, text, context_id):
        try:
            async for frame in self._stream_audio_frames_from_iterator(
                self.backend.synthesize(text), in_sample_rate=24000, context_id=context_id,
            ):
                yield frame
        except Exception as exc:
            yield ErrorFrame(error=f'Local Index-TTS: {exc}')
