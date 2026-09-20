"""Pipecat raw-audio processor for the optional end-to-end voice backend."""
import asyncio
import base64
import time
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams, VADState
from pipecat.frames.frames import CancelFrame, EndFrame, InputAudioRawFrame, InputTransportMessageFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from playback_history import PlaybackHistory
from visual_context import VisualContext


class OmniConversation:
    def __init__(self, session, backend, *, vad=None, visual_enabled=False, session_id=None):
        self.session, self.backend = session, backend
        self.vad = vad or SileroVADAnalyzer(sample_rate=16000, params=VADParams(stop_secs=.7))
        if vad is None:
            self.vad.set_sample_rate(16000)
        self.history = getattr(session, 'history', None) or PlaybackHistory('minicpm')
        self.visual = VisualContext(session_id, enabled=visual_enabled)
        self.task = None
        self.epoch = 0
        self.speaking = False
        self.audio_buffer = bytearray()
        self.preroll = bytearray()
        self.closed = False

    async def interrupt(self, reason):
        logger.info('OMNI_TURN interrupt generation={} reason={}',self.session.generation,reason)
        self.epoch += 1
        old, self.task = self.task, None
        self.history.interrupt()
        if old and not old.done():
            old.cancel()
        await self.session.reset(reason)
        if old:
            await asyncio.gather(old, return_exceptions=True)

    async def begin(self, user):
        if self.closed:
            return
        epoch, generation = self.epoch, self.session.generation
        logger.info('OMNI_TURN input generation={} kind={} audio_bytes={}',self.session.generation,
                    'audio' if 'audio' in user else 'text',len(user.get('audio',''))*3//4)
        user, binding = self.visual.bind(user)
        if self.visual.enabled:
            await self.session.send(binding)
        if self.closed or epoch != self.epoch:
            return
        messages = self.history.begin(user, generation)
        await self.session.persist_history()
        if self.closed or epoch != self.epoch:
            return
        self.task = asyncio.create_task(self.respond(messages, epoch, generation))

    async def respond(self, messages, epoch, generation):
        text, started = [], False
        block_text, block_audio = '', False
        began=time.monotonic();samples=0
        try:
            if self.closed or epoch != self.epoch:
                return
            await self.session.send({'type':'status','state':'thinking'},generation=generation)
            if self.closed or epoch != self.epoch:
                return
            async for kind, value in self.backend.generate(messages):
                if self.closed or epoch != self.epoch:
                    return
                if kind == 'text':
                    if block_audio and block_text:
                        self.session.mark_text(block_text)
                        block_text, block_audio = '', False
                    block_text += value
                    self.history.text(value, generation)
                    text.append(value)
                    await self.session.send({'type':'assistant_text','text':value},generation=generation)
                else:
                    if not started:
                        logger.info('OMNI_TURN first_audio generation={} seconds={:.3f}',generation,time.monotonic()-began)
                        self.session.start_clip()
                        started = True
                    self.session.audio(value)
                    samples+=len(value)//2
                    block_audio = True
            if epoch == self.epoch and not self.closed:
                if block_text and block_audio:
                    self.session.mark_text(block_text)
                if started:
                    self.session.end_clip()
                await self.session.finish_production()
                logger.info('OMNI_TURN generated generation={} samples={} seconds={:.3f}',generation,samples,time.monotonic()-began)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('MiniCPM conversation failed')
            if epoch == self.epoch and not self.closed:
                self.history.interrupt()
                await self.session.reset('omni_error')
                await self.session.send({'type':'error','message':'MiniCPM-o 生成失败，请检查模型服务后重新连接。'})

    async def control(self, message):
        if self.closed:
            return
        kind = message['type']
        if kind == 'visual':
            try:
                await self.session.send(self.visual.control(message))
            except ValueError as exc:
                await self.session.send({'type':'visual_error','sequence':message.get('sequence'),
                                         'message':str(exc)})
        elif kind in ('text','interrupt'):
            await self.interrupt(kind)
            self.audio_buffer.clear();self.preroll.clear();self.speaking=False
            if kind == 'text':
                await self.session.send({'type':'transcript','text':message['text']})
                await self.begin({'role':'user','text':message['text']})
        elif kind == 'playback' and message['generation'] == self.session.generation:
            if message['state'] == 'progress':
                await self.session.playback_progress(message)
            else:
                self.session.playing = message['state'] == 'started'

    async def audio(self, pcm):
        if self.closed:
            return
        for offset in range(0,len(pcm),640):
            packet=pcm[offset:offset+640]
            self.preroll.extend(packet)
            del self.preroll[:-9600]
            state=await self.vad.analyze_audio(packet)
            if self.closed:
                return
            if state == VADState.SPEAKING and not self.speaking:
                await self.interrupt('speech_started')
                self.speaking=True
                self.audio_buffer=bytearray(self.preroll)
                await self.session.send({'type':'status','state':'listening'})
            elif self.speaking:
                self.audio_buffer.extend(packet)
            if self.speaking and (state == VADState.QUIET or len(self.audio_buffer)>=896000):
                audio=bytes(self.audio_buffer)
                self.speaking=False;self.audio_buffer.clear();self.preroll.clear()
                # This label is deliberately not an ASR transcript.
                await self.session.send({'type':'transcript','text':'[语音输入]','source':'audio'})
                await self.begin({'role':'user','audio':base64.b64encode(audio).decode()})

    async def close(self):
        if self.closed:
            return
        self.closed=True
        self.visual.clear()
        self.epoch+=1
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)
        self.audio_buffer.clear();self.preroll.clear()
        self.history.interrupt()
        await self.session.persist_history()
        if hasattr(self.vad,'cleanup'):
            await self.vad.cleanup()


class OmniProcessor(FrameProcessor):
    def __init__(self, session, backend, *, visual_enabled=False, session_id=None):
        super().__init__()
        self.conversation=OmniConversation(session,backend,visual_enabled=visual_enabled,session_id=session_id)

    async def process_frame(self, frame, direction):
        await super().process_frame(frame,direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame,InputAudioRawFrame):
                await self.conversation.audio(frame.audio)
                return
            if isinstance(frame,InputTransportMessageFrame):
                await self.conversation.control(frame.message)
                return
            if isinstance(frame,(CancelFrame,EndFrame)):
                await self.conversation.close()
        await self.push_frame(frame,direction)

    async def cleanup(self):
        await self.conversation.close()
        await super().cleanup()
