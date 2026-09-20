import asyncio
import unittest
from omni_processor import OmniConversation
from pipecat.audio.vad.vad_analyzer import VADState


class Session:
    def __init__(self):
        self.generation=0;self.events=[];self.audio_packets=[];self.playing=False
        from playback_history import PlaybackHistory
        self.history=PlaybackHistory('minicpm');self.sequence=0;self.samples=0
    async def send(self,event,*,generation=None):
        if generation is None or generation==self.generation:self.events.append(event)
    async def reset(self,reason):self.generation+=1;self.playing=False
    def start_clip(self):self.sequence+=1;self.samples=0
    def audio(self,pcm):
        self.audio_packets.append((self.generation,pcm));self.samples+=len(pcm)//2
        self.history.delivered(str(self.sequence),self.samples,self.generation)
    def end_clip(self):pass
    def mark_text(self,text):self.history.mark(str(self.sequence),self.samples,text,self.generation)
    async def persist_history(self):pass
    async def finish_production(self):self.history.finish(self.generation)
    async def playback_progress(self,m):self.history.acknowledge(m["clip_id"],m["played_samples"],m["generation"])


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
        self.assertEqual(len(c.history.context()),1)
        await c.control({'type':'playback','generation':s.generation,'state':'ended'})
        self.assertEqual(len(c.history.context()),1)
        await c.control({'type':'playback','generation':s.generation,'state':'progress','clip_id':'1','played_samples':480})
        self.assertEqual(c.history.context()[-1]['text'],'蓝鲸')
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
        self.assertIn('中断',c.history.context()[-1]['text'])
        await c.close()

class VisualConversationTests(unittest.IsolatedAsyncioTestCase):
    async def test_attachment_ack_bind_once_and_reject_stale_session(self):
        from test_visual_context import frame
        calls=[]
        class Backend:
            async def generate(self,messages):
                calls.append(messages);yield 'text','ok';yield 'audio',bytes(960)
        s=Session();c=OmniConversation(s,Backend(),vad=object(),visual_enabled=True,session_id='session-a')
        await c.control(frame())
        self.assertEqual(s.events[-1]['type'],'visual_ack')
        await c.control({'type':'text','text':'看图'});await c.task
        self.assertEqual(calls[-1][-1]['images'][0]['id'],'image-1')
        await c.control(dict(frame(2),session_id='old'))
        self.assertEqual(s.events[-1]['type'],'visual_error')
        await c.control({'type':'text','text':'然后呢'});await c.task
        self.assertNotIn('images',calls[-1][-1])
        bound=[e for e in s.events if e['type']=='visual_bound']
        self.assertEqual([e['turn_id'] for e in bound],[1,2])
        await c.close();self.assertIsNone(c.visual.pending)

    async def test_raw_voice_turn_binds_picture_and_close_releases_history(self):
        from test_visual_context import frame
        import base64
        captured=[]
        class VAD:
            def __init__(self):self.states=iter([VADState.SPEAKING,VADState.QUIET])
            async def analyze_audio(self,pcm):return next(self.states)
        class Backend:
            async def generate(self,messages):captured.extend(messages);yield 'text','ok';yield 'audio',bytes(960)
        c=OmniConversation(Session(),Backend(),vad=VAD(),visual_enabled=True,session_id='session-a')
        await c.control(frame());pcm=bytes(range(256))*5;await c.audio(pcm);await c.task
        self.assertEqual(base64.b64decode(captured[-1]['audio']),pcm)
        self.assertEqual(captured[-1]['images'][0]['id'],'image-1')
        self.assertEqual(len(c.history.context()),1)
        await c.close();self.assertEqual(len(c.history.context()),2)

    async def test_disconnect_during_visual_binding_cannot_restart_inference(self):
        entered,release=asyncio.Event(),asyncio.Event();calls=[]
        class BlockingSession(Session):
            async def send(self,event,**kwargs):
                if event['type']=='visual_bound':entered.set();await release.wait()
                await super().send(event,**kwargs)
        class Backend:
            async def generate(self,messages):calls.append(messages);yield 'text','late'
        c=OmniConversation(BlockingSession(),Backend(),vad=object(),visual_enabled=True,session_id='session-a')
        begin=asyncio.create_task(c.begin({'role':'user','text':'hi'}));await entered.wait()
        await c.close();release.set();await begin
        if c.task:await c.task
        self.assertFalse(calls);self.assertFalse(c.history.context())
