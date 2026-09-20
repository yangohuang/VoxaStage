"""Generation-scoped avatar output and Pipecat browser input controls."""
import asyncio
from contextlib import aclosing
import json

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame, BotStoppedSpeakingFrame, CancelFrame, EndFrame,
    InputAudioRawFrame, InputTransportMessageFrame, InterruptionFrame,
    LLMMessagesAppendFrame, LLMTextFrame, TranscriptionFrame,
    TTSAudioRawFrame, TTSStartedFrame, TTSStoppedFrame, TTSTextFrame, LLMFullResponseEndFrame, ErrorFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.serializers.base_serializer import FrameSerializer


class AvatarSerializer(FrameSerializer):
    async def serialize(self, frame):
        return None

    async def deserialize(self, data):
        if isinstance(data, bytes):
            if 0 < len(data) <= 32000 and len(data) % 2 == 0:
                return InputAudioRawFrame(audio=data, sample_rate=16000, num_channels=1)
            return None
        if len(data) > 360000:
            return None
        try:
            message = json.loads(data)
        except (ValueError, TypeError):
            return None
        if not isinstance(message, dict):
            return None
        kind = message.get('type')
        if kind == 'visual':
            return InputTransportMessageFrame(message=message)
        if len(data) > 4096:
            return None
        if kind == 'text':
            value = message.get('text')
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= 500:
                return None
            message = {'type': kind, 'text': value.strip()}
        elif kind == 'playback':
            if type(message.get('generation')) is not int or message.get('state') not in ('started', 'ended', 'progress'):
                return None
            if message['state'] == 'progress':
                clip = message.get('clip_id')
                samples = message.get('played_samples')
                if not isinstance(clip, str) or not clip.isascii() or not clip.isdigit() or len(clip) > 12 or type(samples) is not int or not 0 < samples <= 720000:
                    return None
        elif kind != 'interrupt':
            return None
        return InputTransportMessageFrame(message=message)


class AvatarSession:
    def __init__(self, websocket, backend, *, history=None, persist=None):
        self.websocket, self.backend = websocket, backend
        self.history, self._persist = history, persist
        self.cancel_agent = None
        self.producer_finished = False
        self.pending_clips = set()
        self.last_boundary = None
        self.generation = 0
        self.playing = False
        self.lock = asyncio.Lock()
        self.clips = asyncio.Queue(maxsize=8)
        self.current = None
        self.task = None
        self.tasks = set()
        self.sequence = 0
        self.queued_bytes = 0
        self.current_bytes = 0
        self.max_clip_bytes = 30 * 24000 * 2
        self.closed = False

    async def send(self, event, *, generation=None):
        async with self.lock:
            if self.closed or generation is not None and generation != self.generation:
                return
            if generation is not None:
                event = dict(event, generation=generation)
            await asyncio.wait_for(self.websocket.send_json(event), 3)

    async def persist_history(self):
        if self._persist is not None:
            await self._persist()

    def mark_text(self, text):
        if self.history is not None and self.last_boundary:
            clip, sample = self.last_boundary
            self.history.mark(clip, sample, text, self.generation)

    async def playback_progress(self, message):
        if self.history is not None and self.history.acknowledge(
                message["clip_id"], message["played_samples"], message["generation"]):
            # Save at complete phrase boundaries, not on every PCM packet.
            snapshot = self.history.snapshot()
            if snapshot != getattr(self, "_last_saved_snapshot", None):
                await self.persist_history()
                self._last_saved_snapshot = snapshot

    async def finish_production(self):
        self.producer_finished = True
        await self._finish_history()

    async def _finish_history(self):
        if self.history is not None and self.producer_finished and not self.pending_clips:
            self.history.finish(self.generation)
            await self.persist_history()

    def start_clip(self):
        self.end_clip()
        self.sequence += 1
        clip = (str(self.sequence), asyncio.Queue())
        try:
            self.clips.put_nowait(clip)
        except asyncio.QueueFull:
            raise ValueError('Avatar clip backlog exceeded')
        self.pending_clips.add(clip[0])
        self.current = clip[1]
        self.current_bytes = 0
        if self.task is None or self.task.done():
            previous = tuple(self.tasks)
            self.task = asyncio.create_task(self._run(self.generation, self.clips, previous))
            self.tasks.add(self.task)
            self.task.add_done_callback(self.tasks.discard)

    def audio(self, pcm):
        if self.current is None:
            raise ValueError('Avatar audio without a started clip')
        if self.queued_bytes + len(pcm) > 1_440_000:
            raise ValueError('Avatar audio backlog exceeded')
        if len(pcm) % 2:
            raise ValueError('Expected complete PCM16 samples')
        self.queued_bytes += len(pcm)
        offset = 0
        while offset < len(pcm):
            if self.current_bytes == self.max_clip_bytes:
                self.start_clip()
            count = min(len(pcm) - offset, self.max_clip_bytes - self.current_bytes)
            self.current.put_nowait(pcm[offset:offset + count])
            self.current_bytes += count
            self.last_boundary = (str(self.sequence), self.current_bytes // 2)
            offset += count

    def end_clip(self):
        if self.current is not None:
            self.current.put_nowait(None)
            self.current = None
            self.current_bytes = 0

    async def _run(self, generation, clips, previous):
        try:
            playback_tail = 0.0
            if previous:
                await asyncio.shield(asyncio.gather(*previous, return_exceptions=True))
            while True:
                clip_id, queue = await clips.get()
                delivered_samples = 0

                async def source():
                    while True:
                        item = await queue.get()
                        if item is None:
                            return
                        if generation == self.generation:
                            self.queued_bytes -= len(item)
                        yield item

                async with aclosing(self.backend.stream(source())) as stream:
                    async for event in stream:
                        if event.get('type') == 'media':
                            # Keep at most one second of lead across clips.
                            # Underruns must not accumulate future send credit.
                            now = asyncio.get_running_loop().time()
                            playback_tail = max(playback_tail, now)
                            delay = playback_tail - 1 - now
                            if delay > 0:
                                await asyncio.sleep(delay)
                            audio = event['audio']
                            samples = (len(audio) * 3 // 4 - audio.count('=')) // 2
                            playback_tail = max(playback_tail, asyncio.get_running_loop().time()) + samples / 24000
                        await self.send(dict(event, clip_id=clip_id), generation=generation)
                        if event.get("type") == "media" and generation == self.generation and self.history is not None:
                            delivered_samples += samples
                            self.history.delivered(clip_id, delivered_samples, generation)
                if generation == self.generation:
                    self.pending_clips.discard(clip_id)
                    await self._finish_history()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception('Avatar stream failed')
            await self.reset('backend_error')
            await self.send({'type': 'error', 'message': f'数字人生成失败：{type(exc).__name__}，可重试。'})

    async def reset(self, reason):
        # Change generation under the same lock as sending: no old media can
        # cross the reset message even if the backend finishes concurrently.
        async with self.lock:
            if self.cancel_agent is not None:
                self.cancel_agent()
            if self.history is not None:
                self.history.interrupt()
            self.producer_finished = False
            self.pending_clips.clear()
            self.last_boundary = None
            self.generation += 1
            self.playing = False
            self.task = None
            previous = tuple(task for task in self.tasks if task is not asyncio.current_task())
            self.current = None
            self.current_bytes = 0
            self.clips = asyncio.Queue(maxsize=8)
            self.queued_bytes = 0
            for task in previous:
                if not task.cancelling():
                    task.cancel()
            if not self.closed:
                await asyncio.wait_for(self.websocket.send_json(
                    {'type': 'reset', 'generation': self.generation, 'reason': reason}), 3)
        if previous:
            await asyncio.shield(asyncio.gather(*previous, return_exceptions=True))
        await self.persist_history()

    async def close(self):
        self.closed = True
        await self.reset('closed')


class AvatarInput(FrameProcessor):
    def __init__(self, session):
        super().__init__()
        self.session = session

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if getattr(frame, 'avatar_generation', self.session.generation) != self.session.generation:
            return
        if isinstance(frame, ErrorFrame):
            await self.session.reset('pipeline_error')
            await self.broadcast_frame(BotStoppedSpeakingFrame)
            await self.session.send({'type': 'error', 'message': '语音处理失败，请重新连接后重试。'})
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, TranscriptionFrame) and frame.text:
                await self.session.send({'type': 'transcript', 'text': frame.text})
            if isinstance(frame, InputTransportMessageFrame):
                message = frame.message
                kind = message['type']
                if kind == 'visual':
                    await self.session.send({'type':'visual_error','sequence':message.get('sequence'),
                                             'message':'当前对话后端不支持画面输入。'})
                elif kind in ('text', 'interrupt'):
                    await self.broadcast_interruption()
                    await self.broadcast_frame(BotStoppedSpeakingFrame)
                    if kind == 'text':
                        await self.session.send({'type': 'transcript', 'text': message['text']})
                        await self.push_frame(LLMMessagesAppendFrame(
                            messages=[{'role': 'user', 'content': message['text']}], run_llm=True))
                elif kind == 'playback' and message['generation'] == self.session.generation:
                    if message['state'] == 'progress':
                        await self.session.playback_progress(message)
                        return
                    playing = message['state'] == 'started'
                    if playing != self.session.playing:
                        self.session.playing = playing
                        await self.broadcast_frame(BotStartedSpeakingFrame if playing else BotStoppedSpeakingFrame)
                return
        await self.push_frame(frame, direction)


class AvatarText(FrameProcessor):
    def __init__(self, session):
        super().__init__()
        self.session = session

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if getattr(frame, 'avatar_generation', self.session.generation) != self.session.generation:
            return
        if direction == FrameDirection.DOWNSTREAM and isinstance(frame, LLMTextFrame):
            await self.session.send({'type': 'assistant_text', 'text': frame.text}, generation=getattr(frame, 'avatar_generation', self.session.generation))
        await self.push_frame(frame, direction)


class AvatarOutput(FrameProcessor):
    def __init__(self, session):
        super().__init__()
        self.session = session

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if getattr(frame, 'avatar_generation', self.session.generation) != self.session.generation:
            return
        if direction == FrameDirection.DOWNSTREAM:
            try:
                if isinstance(frame, InterruptionFrame):
                    # Configured cascade LLM owns reset before starting the next
                    # inference; a delayed TTS interruption must not cancel it.
                    if self.session.cancel_agent is None:
                        await self.session.reset('interrupted')
                    await self.broadcast_frame(BotStoppedSpeakingFrame)
                elif isinstance(frame, (EndFrame, CancelFrame)):
                    await self.session.close()
                elif isinstance(frame, TTSStartedFrame):
                    self.session.start_clip()
                    return
                elif isinstance(frame, TTSAudioRawFrame):
                    self.session.audio(frame.audio)
                    return
                elif isinstance(frame, TTSTextFrame):
                    self.session.mark_text(frame.text)
                elif isinstance(frame, LLMFullResponseEndFrame):
                    await self.session.finish_production()
                elif isinstance(frame, TTSStoppedFrame):
                    self.session.end_clip()
                    return
                elif isinstance(frame, ErrorFrame):
                    await self.session.reset('pipeline_error')
                    await self.session.send({'type': 'error', 'message': '语音生成失败，请重试。'})
            except ValueError as exc:
                await self.session.reset('buffer_limit')
                await self.session.send({'type': 'error', 'message': str(exc)})
                return
        await self.push_frame(frame, direction)

    async def cleanup(self):
        await self.session.close()
        await super().cleanup()
