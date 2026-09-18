"""Pipecat raw-audio processor for the optional end-to-end voice backend."""
import asyncio
import base64
import time
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams, VADState
from pipecat.frames.frames import CancelFrame, EndFrame, InputAudioRawFrame, InputTransportMessageFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from omni_backend import ConversationHistory


class OmniConversation:
    def __init__(self, session, backend, *, vad=None):
        self.session, self.backend = session, backend
        self.vad = vad or SileroVADAnalyzer(sample_rate=16000, params=VADParams(stop_secs=.7))
        if vad is None:
            self.vad.set_sample_rate(16000)
        self.history = ConversationHistory()
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
        logger.info('OMNI_TURN input generation={} kind={} audio_bytes={}',self.session.generation,
                    'audio' if 'audio' in user else 'text',len(user.get('audio',''))*3//4)
        messages = self.history.begin(user)
        if self.history.trimmed:
            await self.session.send({'type':'context_trimmed','message':'较早的对话已移出模型上下文。'})
            self.history.trimmed = False
        epoch, generation = self.epoch, self.session.generation
        self.task = asyncio.create_task(self.respond(messages, epoch, generation))

    async def respond(self, messages, epoch, generation):
        text, started = [], False
        began=time.monotonic();samples=0
        try:
            await self.session.send({'type':'status','state':'thinking'},generation=generation)
            async for kind, value in self.backend.generate(messages):
                if self.closed or epoch != self.epoch:
                    return
                if kind == 'text':
                    text.append(value)
                    await self.session.send({'type':'assistant_text','text':value},generation=generation)
                else:
                    if not started:
                        logger.info('OMNI_TURN first_audio generation={} seconds={:.3f}',generation,time.monotonic()-began)
                        self.session.start_clip()
                        started = True
                    self.session.audio(value)
                    samples+=len(value)//2
            if epoch == self.epoch and not self.closed:
                self.history.generated(''.join(text))
                if started:
                    self.session.end_clip()
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
        kind = message['type']
        if kind in ('text','interrupt'):
            await self.interrupt(kind)
            self.audio_buffer.clear();self.preroll.clear();self.speaking=False
            if kind == 'text':
                await self.session.send({'type':'transcript','text':message['text']})
                await self.begin({'role':'user','text':message['text']})
        elif kind == 'playback' and message['generation'] == self.session.generation:
            self.session.playing = message['state'] == 'started'
            if message['state'] == 'ended':
                self.history.heard()

    async def audio(self, pcm):
        for offset in range(0,len(pcm),640):
            packet=pcm[offset:offset+640]
            self.preroll.extend(packet)
            del self.preroll[:-9600]
            state=await self.vad.analyze_audio(packet)
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
        self.epoch+=1
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)
        self.audio_buffer.clear();self.preroll.clear()
        if hasattr(self.vad,'cleanup'):
            await self.vad.cleanup()


class OmniProcessor(FrameProcessor):
    def __init__(self, session, backend):
        super().__init__()
        self.conversation=OmniConversation(session,backend)

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
