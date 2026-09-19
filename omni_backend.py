"""Bounded MiniCPM stream client and explicit heard/interrupted history."""
import asyncio
import base64
import json
from copy import deepcopy


class ConversationHistory:
    def __init__(self):
        self.turns = []
        self.pending = None
        self.trimmed = False

    def interrupt(self):
        if self.pending:
            self.turns.append([self.pending['user'], {'role':'assistant','text':'[上轮回复被用户打断，未确认完整播放。]'}])
            self.pending = None

    def begin(self, user):
        self.interrupt()
        user = deepcopy(user)
        self.pending = {'user':user, 'text':None}
        def audio_size(message):
            return len(message.get('audio','')) * 3 // 4
        while self.turns and (len(self.turns) > 6 or
                sum(audio_size(m) for turn in self.turns for m in turn) + audio_size(user) > 1_920_000 or
                sum(len(m.get('text','')) for turn in self.turns for m in turn) + len(user.get('text','')) > 12000):
            self.turns.pop(0)
            self.trimmed = True
        messages = [m for turn in self.turns for m in turn] + [user]
        image_count = sum(len(m.get('images', [])) for m in messages)
        for message in messages:
            if image_count <= 2:
                break
            images = message.get('images', [])
            remove = min(len(images), image_count - 2)
            if remove:
                message['images'] = images[remove:]
                if not message['images']:
                    del message['images']
                message['images_omitted'] = True
                image_count -= remove
                self.trimmed = True
        return deepcopy(messages)

    def generated(self, text):
        if self.pending is not None:
            self.pending['text'] = text or '[语音回复，没有文字输出。]'

    def heard(self):
        if self.pending and self.pending['text'] is not None:
            self.turns.append([self.pending['user'], {'role':'assistant','text':self.pending['text']}])
            self.pending = None


class OmniBackend:
    def __init__(self, client, url, *, voice='female'):
        if voice not in ('male','female'):
            raise ValueError('Unknown voice')
        self.voice = voice
        self.client, self.url = client, url.rstrip('/')

    async def generate(self, messages):
        deadline = asyncio.get_running_loop().time() + 30
        while True:
            async with self.client.stream('POST', self.url + '/generate', json={'messages':messages,'voice':self.voice}, timeout=120) as response:
                if response.status_code == 409:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise RuntimeError('MiniCPM is still cleaning up the previous turn')
                else:
                    response.raise_for_status()
                    buffer, samples, text_chars = bytearray(), 0, 0
                    metadata = done = False
                    async for data in response.aiter_bytes(chunk_size=16384):
                        buffer.extend(data)
                        while b'\n' in buffer:
                            line, _, tail = buffer.partition(b'\n')
                            buffer = bytearray(tail)
                            if not line or len(line) > 100000:
                                raise ValueError('Invalid MiniCPM event size')
                            event = json.loads(line)
                            if not isinstance(event,dict) or done:
                                raise ValueError('Invalid MiniCPM event sequence')
                            kind = event.get('type')
                            if not metadata:
                                if kind != 'metadata' or event.get('protocol') != 1 or event.get('sample_rate') != 24000:
                                    raise ValueError('Invalid MiniCPM metadata')
                                metadata = True
                            elif kind == 'text':
                                text = event.get('text')
                                if not isinstance(text,str) or text_chars + len(text) > 4000:
                                    raise ValueError('Invalid MiniCPM text')
                                text_chars += len(text)
                                yield 'text', text
                            elif kind == 'audio':
                                pcm = base64.b64decode(event.get('audio',''),validate=True)
                                if not pcm or len(pcm)%2 or samples + len(pcm)//2 > 720000:
                                    raise ValueError('Invalid MiniCPM PCM')
                                samples += len(pcm)//2
                                yield 'audio', pcm
                            elif kind == 'done':
                                if not samples or type(event.get('samples')) is not int or event['samples'] != samples:
                                    raise ValueError('MiniCPM sample count mismatch')
                                done = True
                            elif kind == 'error':
                                raise RuntimeError('MiniCPM worker generation failed')
                            else:
                                raise ValueError('Unexpected MiniCPM event')
                        if len(buffer)>100000:
                            raise ValueError('MiniCPM event exceeds bound')
                    if buffer or not done:
                        raise ValueError('Truncated MiniCPM stream')
                    return
            await asyncio.sleep(.1)
