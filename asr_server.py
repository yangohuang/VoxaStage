"""Bounded localhost WAV endpoint for local ASR; raw audio is never persisted."""
import argparse
import asyncio
import io
from pathlib import Path
import threading
import time
import wave

from fastapi import FastAPI, HTTPException, Request
import numpy as np


def read_audio(body):
    try:
        with wave.open(io.BytesIO(body), 'rb') as f:
            if (f.getframerate(), f.getnchannels(), f.getsampwidth()) != (16000, 1, 2):
                raise ValueError('Expected mono PCM16 WAV at 16000 Hz')
            count = f.getnframes()
            if not 0 < count <= 480000:
                raise ValueError('Expected 0 < duration <= 30 seconds')
            raw = f.readframes(count)
            if len(raw) != count * 2:
                raise ValueError('Truncated WAV')
        return np.frombuffer(raw, '<i2').astype(np.float32) / 32768
    except (wave.Error, EOFError, ValueError) as exc:
        raise HTTPException(400, str(exc) or 'Invalid WAV') from exc


def create_app(decode, model_name, metadata=None, *, supports_context=False):
    app = FastAPI()
    lock = threading.Lock()

    @app.get('/health')
    def health():
        return dict(ready=True, busy=lock.locked(), model=model_name, **(metadata or {}))

    def infer(audio, context=''):
        if not lock.acquire(blocking=False):
            raise HTTPException(409, 'ASR busy')
        try:
            started = time.perf_counter()
            # Reject digital silence without introducing a volume threshold
            # which could erase quiet but intelligible microphone speech.
            text = (decode(audio,context=context) if supports_context else decode(audio)) if np.any(audio) else ''
            return dict(text=text, audio_s=len(audio) / 16000,
                        elapsed_s=time.perf_counter() - started, model=model_name)
        finally:
            lock.release()

    @app.post('/transcribe')
    async def transcribe(request: Request):
        context=request.headers.get('x-asr-context','')
        if len(context)>500:
            raise HTTPException(400,'ASR context exceeds 500 characters')
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 2_000_000:
                raise HTTPException(413, 'WAV request exceeds 2 MB')
            body.extend(chunk)
        return await asyncio.to_thread(infer, read_audio(bytes(body)), context)

    return app


def load_qwen(model_path):
    import torch
    from qwen_asr import Qwen3ASRModel
    torch.set_num_threads(2)
    model = Qwen3ASRModel.from_pretrained(
        str(model_path), dtype=torch.bfloat16, device_map='cuda:0',
        attn_implementation='sdpa', max_inference_batch_size=1,
        max_new_tokens=512, local_files_only=True)

    def decode(audio, context=''):
        with torch.inference_mode():
            return model.transcribe(audio=(audio, 16000), language=None, context=context)[0].text
    return decode


def load_sensevoice(model_path):
    import sherpa_onnx
    model = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(model_path / 'model.int8.onnx'), tokens=str(model_path / 'tokens.txt'),
        provider='cpu', num_threads=2, language='zh', use_itn=True)

    def decode(audio):
        stream = model.create_stream()
        stream.accept_waveform(16000, audio)
        model.decode_stream(stream)
        return stream.result.text
    return decode


if __name__ == '__main__':
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument('--engine', choices=['qwen', 'sensevoice'], default='sensevoice')
    parser.add_argument('--model', type=Path)
    parser.add_argument('--model-name', default=None)
    parser.add_argument('--port', type=int, default=18315)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    if args.engine == 'qwen':
        decoder = load_qwen(args.model or root / 'models/qwen3-asr-0.6b')
        name, metadata = args.model_name or 'Qwen3-ASR-0.6B', {'device': 'cuda:0', 'dtype': 'bfloat16'}
    else:
        decoder = load_sensevoice(args.model or root / 'models/sensevoice')
        name, metadata = 'SenseVoiceSmall-int8', {'device': 'cpu', 'threads': 2}
    # Warm the kernel and feature extractor before exposing readiness.
    decoder(np.zeros(16000, dtype=np.float32))
    uvicorn.run(create_app(decoder, name, metadata, supports_context=args.engine=='qwen'),
                host='127.0.0.1', port=args.port, limit_concurrency=16)
