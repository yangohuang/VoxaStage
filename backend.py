"""Protocols already used by the local Index-TTS voice-agent experiments."""
import asyncio
import io
import json
import os
import time
import uuid
import wave

import httpx
import numpy as np
import websockets
from loguru import logger
from api_backends import configured_api

LLM_URL = os.getenv('PIPECAT_LLM_URL', 'http://127.0.0.1:18311')
ASR_URL = os.getenv('PIPECAT_ASR_URL', 'http://127.0.0.1:18315')
LLM_MODEL = os.getenv('PIPECAT_LLM_MODEL', 'Qwen3-4B')
ASR_MODEL = os.getenv('PIPECAT_ASR_MODEL', 'Qwen3-ASR-0.6B')
TTS_URL = os.getenv('PIPECAT_TTS_URL', 'http://127.0.0.1:19006')


def bounded_messages(messages):
    """Fit the existing worker's 20-message/20000-character contract."""
    system = [dict(m) for m in messages if m.get('role') == 'system'][:1]
    history = [dict(m) for m in messages if m.get('role') in ('user', 'assistant')][-19:]
    while len(history) > 1 and (history[0]['role'] != 'user' or
                              len(json.dumps(system + history)) > 19000):
        history.pop(0)
    result = system + history
    if len(json.dumps(result)) > 20000:
        raise ValueError('Latest message exceeds the local LLM context limit')
    return result


async def pcm16_chunks(source):
    """WebSocket packet boundaries need not coincide with PCM sample boundaries."""
    pending = b''
    async for chunk in source:
        pending += chunk
        size = len(pending) // 2 * 2
        if size:
            yield pending[:size]
            pending = pending[size:]
    if pending:
        raise ValueError('Index-TTS returned truncated PCM16')


class LocalBackend:
    def __init__(self, client: httpx.AsyncClient, *, voice=None):
        self.client = client
        if voice not in (None,'male','female'):
            raise ValueError('Unknown voice')
        self.voice = voice
        self.asr_api=configured_api('asr')
        self.llm_api=configured_api('llm')

    async def health(self):
        async def check(name, url, method='GET'):
            api=self.asr_api if name=='asr' else self.llm_api if name=='llm' else None
            if api:
                return name, {'ready':True,'configured':True,'availability':'not_probed','mode':'api','model':api.model}
            try:
                r = await self.client.request(method, url, timeout=3)
                r.raise_for_status()
                data = r.json()
                ready = bool(data.get('speaker_ids')) if name == 'tts' else data.get('ready', False)
                return name, {'ready': ready, 'url': url, 'busy': data.get('busy', False),
                              'model': data.get('model'), 'device': data.get('device')}
            except Exception as exc:
                return name, {'ready': False, 'url': url, 'error': str(exc)}
        return dict(await asyncio.gather(check('llm', LLM_URL + '/health'),
                                         check('asr', ASR_URL + '/health'),
                                         check('tts', TTS_URL + '/api/tts/list', 'POST')))

    async def transcribe(self, wav):
        if self.asr_api:
            return await self.asr_api.transcribe(self.client,wav)
        started = time.monotonic()
        context=os.getenv('PIPECAT_ASR_CONTEXT','')
        if len(context)>500 or not context.isascii():
            raise ValueError('ASR context must be at most 500 ASCII characters')
        r = await self.client.post(ASR_URL + '/transcribe', content=wav,
                                   headers={'Content-Type': 'audio/wav','X-ASR-Context':context})
        r.raise_for_status()
        result = r.json()
        # Log the actual ASR text (before the LLM) and signal statistics. Do not
        # retain PCM, alter its gain, or treat a volume estimate as a diagnosis.
        try:
            with wave.open(io.BytesIO(wav), 'rb') as stream:
                rate, channels, width = stream.getframerate(), stream.getnchannels(), stream.getsampwidth()
                if width != 2:
                    raise ValueError('Expected PCM16 diagnostic input')
                samples = np.frombuffer(stream.readframes(stream.getnframes()), dtype='<i2').astype(np.float64)
                audio_s = stream.getnframes() / rate
            rms = float(np.sqrt(np.mean(samples ** 2))) if samples.size else 0
            peak = float(np.max(np.abs(samples))) if samples.size else 0
            event = {
                'model': result.get('model', ASR_MODEL),
                'text': result['text'], 'sample_rate': rate, 'channels': channels,
                'audio_s': round(audio_s, 3),
                'rms_dbfs': round(20 * np.log10(rms / 32768), 1) if rms else None,
                'peak_dbfs': round(20 * np.log10(peak / 32768), 1) if peak else None,
                'clipped_fraction': float(np.mean(np.abs(samples) >= 32767)) if samples.size else 0,
                'request_s': round(time.monotonic() - started, 3),
                'inference_s': result.get('elapsed_s'),
            }
            logger.info('ASR_DIAGNOSTIC {}', json.dumps(event, ensure_ascii=False, allow_nan=False))
        except Exception as exc:
            logger.warning('ASR diagnostic logging failed: {}', exc)
        return result['text']

    async def generate(self, messages):
        if self.llm_api:
            async for text in self.llm_api.generate(self.client,bounded_messages(messages)):
                yield text
            return
        async with self.client.stream('POST', LLM_URL + '/generate', json={
            'messages': bounded_messages(messages), 'max_new_tokens': 256,
        }) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if event.get('error'):
                    raise RuntimeError(event['error'])
                if event.get('text'):
                    yield event['text']

    async def synthesize(self, text):
        r = await self.client.post(TTS_URL + '/api/tts/list')
        r.raise_for_status()
        speakers = r.json()['speaker_ids']
        if self.voice:
            speaker = os.getenv('PIPECAT_' + self.voice.upper() + '_SPEAKER_ID',
                                'female11' if self.voice == 'female' else 'pipecat_male')
        else:
            speaker = os.getenv('PIPECAT_SPEAKER_ID') or next(iter(speakers), None)
        if speaker not in speakers:
            raise ValueError('No valid Index-TTS speaker configured')
        key = 'pipecat-' + uuid.uuid4().hex
        common = dict(session_id=key, transaction_id=key, audio_id=speaker, audio_sr=24000)
        url = TTS_URL.replace('http://', 'ws://', 1).replace('https://', 'wss://', 1) + '/ws/tts'
        async with websockets.connect(url, proxy=None, max_size=10_000_000,
                                      open_timeout=10, close_timeout=1) as ws:
            for data in [{'open_stream': 1}, {'is_start': 1}, {'text': text}, {'is_end': 1}]:
                await ws.send(json.dumps(dict(common, **data), ensure_ascii=False))

            async def receive():
                async with asyncio.timeout(90):
                    async for packet in ws:
                        if isinstance(packet, bytes):
                            if packet:
                                yield packet
                        else:
                            event = json.loads(packet)
                            if event.get('error') or event.get('code', 0) not in (0, 200, None):
                                raise RuntimeError(str(event))
                            if event.get('is_end'):
                                return
                    raise RuntimeError('Index-TTS closed before is_end')

            size = 0
            async for chunk in pcm16_chunks(receive()):
                size += len(chunk)
                yield chunk
            if not size:
                raise RuntimeError('Index-TTS returned no audio')
