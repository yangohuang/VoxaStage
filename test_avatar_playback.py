import asyncio
import base64
import json
import unittest
from avatar_session import AvatarSession, AvatarSerializer
from playback_history import PlaybackHistory, UNHEARD_MARKER

class Socket:
    def __init__(self): self.events=[]
    async def send_json(self,event): self.events.append(event)
class Backend:
    async def stream(self,source):
        sample=0
        async for audio in source:
            yield {'type':'media','audio':base64.b64encode(audio).decode(),'start_sample':sample}
            sample+=len(audio)//2

class PlaybackIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_queued_marker_delivery_ack_and_persistence(self):
        saved=[];history=PlaybackHistory()
        async def persist(): saved.append(history.snapshot())
        session=AvatarSession(Socket(),Backend(),history=history,persist=persist)
        history.begin('你好',0);history.text('第一句。第二句。',0)
        session.start_clip();session.audio(bytes(960));session.mark_text('第一句。')
        async with asyncio.timeout(2):
            while not session.websocket.events:
                await asyncio.sleep(.01)
        await asyncio.sleep(.01)
        await session.playback_progress({'clip_id':'1','played_samples':480,'generation':0})
        await session.reset('interrupted')
        self.assertEqual(history.context()[-1]['content'],'第一句。\n'+UNHEARD_MARKER)
        self.assertTrue(saved)
        await session.close()
    async def test_serializer_accepts_only_bounded_progress(self):
        serializer=AvatarSerializer()
        valid={'type':'playback','state':'progress','generation':1,'clip_id':'12','played_samples':240}
        self.assertIsNotNone(await serializer.deserialize(json.dumps(valid)))
        for change in ({'played_samples':True},{'played_samples':-1},{'clip_id':'../x'},{'played_samples':720001}):
            self.assertIsNone(await serializer.deserialize(json.dumps(dict(valid,**change))))

    async def test_stale_text_and_end_never_reach_tts_or_new_history(self):
        from avatar_session import AvatarText, AvatarOutput, AvatarInput
        from pipecat.frames.frames import LLMTextFrame, LLMFullResponseEndFrame, ErrorFrame
        from pipecat.processors.frame_processor import FrameDirection
        session=AvatarSession(Socket(),Backend(),history=PlaybackHistory())
        await session.reset('new')
        forwarded=[]
        async def push(frame,direction=FrameDirection.DOWNSTREAM):forwarded.append(frame)
        text=AvatarText(session);text.push_frame=push
        end=AvatarOutput(session);end.push_frame=push
        stale=LLMTextFrame('旧结果');stale.avatar_generation=0
        finish=LLMFullResponseEndFrame();finish.avatar_generation=0
        await text.process_frame(stale,FrameDirection.DOWNSTREAM)
        await end.process_frame(finish,FrameDirection.DOWNSTREAM)
        source=AvatarInput(session);source.push_frame=push
        error=ErrorFrame(error='old synthesis failed');error.avatar_generation=0
        await source.process_frame(error,FrameDirection.UPSTREAM)
        self.assertFalse(forwarded)
        self.assertFalse(session.producer_finished)
        self.assertEqual([e['type'] for e in session.websocket.events],['reset'])
        await session.close()
