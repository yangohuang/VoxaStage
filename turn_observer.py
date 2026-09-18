"""Bounded, read-only server-side timing; never claims device playback timing."""
from collections import deque
import json

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame, BotStoppedSpeakingFrame, InterruptionFrame,
    LLMContextFrame, LLMTextFrame, TranscriptionFrame, TTSAudioRawFrame,
    VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame,
)
from pipecat.observers.base_observer import BaseObserver
from pipecat.processors.frame_processor import FrameDirection


class TurnObserver(BaseObserver):
    def __init__(self, session_id, *, llm, emit=None):
        super().__init__()
        self.session_id = session_id
        self.llm = llm
        self.emit = emit or self._log
        self.turn = 0
        self.commit_at = None
        self.vad_stop_at = None
        self.response_vad_stop_at = None
        self.first = set()
        self.seen = set()
        self.recent = deque()

    @staticmethod
    def _log(row):
        logger.info('TURN_EVENT {}', json.dumps(row, ensure_ascii=False))

    async def on_push_frame(self, data):
        # Broadcasts have separate upstream/downstream frame IDs. Observe one
        # direction so the same VAD or interruption event isn't logged twice.
        if data.direction != FrameDirection.DOWNSTREAM:
            return
        frame = data.frame
        if isinstance(frame, LLMContextFrame):
            if data.destination is not self.llm or data.direction != FrameDirection.DOWNSTREAM:
                return
            event = 'user_input_committed'
        else:
            event = next((name for kind, name in (
                (VADUserStartedSpeakingFrame, 'vad_speech_started'),
                (VADUserStoppedSpeakingFrame, 'vad_speech_stopped'),
                (TranscriptionFrame, 'transcript_ready'),
                (LLMTextFrame, 'llm_first_text'),
                (TTSAudioRawFrame, 'tts_first_pcm'),
                (BotStartedSpeakingFrame, 'server_output_started'),
                (BotStoppedSpeakingFrame, 'server_output_stopped'),
                (InterruptionFrame, 'interruption_requested'),
            ) if isinstance(frame, kind)), None)
        if event is None:
            return
        first_only = event in ('llm_first_text', 'tts_first_pcm', 'server_output_started')
        if first_only and (self.commit_at is None or event in self.first):
            return
        if frame.id in self.seen:
            return
        if len(self.recent) == 256:
            self.seen.remove(self.recent.popleft())
        self.recent.append(frame.id)
        self.seen.add(frame.id)
        now = data.timestamp / 1e9
        input_event = event in ('vad_speech_started', 'vad_speech_stopped', 'transcript_ready')
        if event == 'vad_speech_started':
            self.vad_stop_at = None
        elif event == 'vad_speech_stopped':
            self.vad_stop_at = now
        elif event == 'user_input_committed':
            self.turn += 1
            self.commit_at = now
            self.response_vad_stop_at = self.vad_stop_at
            self.first.clear()
        if first_only:
            self.first.add(event)
        row = dict(session_id=self.session_id, turn_id=None if input_event else self.turn,
                   event=event, pipeline_s=round(now, 6), scope='server_pipeline')
        if not input_event and self.commit_at is not None:
            row['since_commit_s'] = round(now - self.commit_at, 6)
            if self.response_vad_stop_at is not None:
                row['since_vad_stop_s'] = round(now - self.response_vad_stop_at, 6)
        self.emit(row)
