import asyncio
import unittest
from omni_processor import OmniConversation
from pipecat.audio.vad.vad_analyzer import VADState


class Session:
    def __init__(self):
        self.generation=0;self.events=[];self.audio_packets=[];self.playing=False
    async def send(self,event,*,generation=None):
        if generation is None or generation==self.generation:self.events.append(event)
    async def reset(self,reason):self.generation+=1;self.playing=False
    def start_clip(self):pass
    def audio(self,pcm):self.audio_packets.append((self.generation,pcm))
    def end_clip(self):pass


class OmniConversationTests(unittest.IsolatedAsyncioTestCase):
    async def test_vad_passes_raw_audio_to_model_without_transcription(self):
        import base64
        captured=[]
        class VAD:
            def __init__(self):self.states=iter([VADState.SPEAKING,VADState.QUIET])
            async def analyze_audio(self,pcm):return next(self.states)
        class Backend:
            async def generate(self,messages):
                captured.extend(messages)
                yield 'text','回复';yield 'audio',bytes(960)
        s=Session();c=OmniConversation(s,Backend(),vad=VAD())
        pcm=bytes(range(256))*5
        await c.audio(pcm)
        await c.task
        self.assertEqual(base64.b64decode(captured[-1]['audio']),pcm)
        self.assertNotIn('text',captured[-1])
        self.assertTrue(any(e.get('source')=='audio' for e in s.events))
        await c.close()

    async def test_text_turn_commits_only_after_playback(self):
        class Backend:
            async def generate(self,messages):
                yield 'text','蓝鲸';yield 'audio',bytes(960)
        s=Session();c=OmniConversation(s,Backend(),vad=object())
        await c.control({'type':'text','text':'暗号是蓝鲸'})
        await c.task
        self.assertFalse(c.history.turns)
        await c.control({'type':'playback','generation':s.generation,'state':'ended'})
        self.assertEqual(c.history.turns[-1][-1]['text'],'蓝鲸')
        await c.close()

    async def test_late_result_after_cancel_is_discarded(self):
        entered=asyncio.Event()
        class Backend:
            async def generate(self,messages):
                entered.set()
                try:await asyncio.sleep(20)
                except asyncio.CancelledError:pass
                yield 'text','stale';yield 'audio',bytes(960)
        s=Session();c=OmniConversation(s,Backend(),vad=object())
        await c.control({'type':'text','text':'hi'})
        await entered.wait();await c.control({'type':'interrupt'})
        self.assertFalse(s.audio_packets)
        self.assertFalse(any(e.get('text')=='stale' for e in s.events))
        self.assertIn('打断',c.history.turns[-1][-1]['text'])
        await c.close()
