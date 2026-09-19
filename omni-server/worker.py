"""Isolated MiniCPM-o worker: raw audio/text in, bounded text/PCM stream out."""
import argparse
import asyncio
import base64
import json
from pathlib import Path
import queue
import threading
import time
import traceback

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
import numpy as np
from visual_input import decode_images, prefill_chunks
from visual_grounding import POLICIES, build_system_prompt

SYSTEM = '你是中文语音助手。认真理解用户的中文和英文术语，依据上下文直接回答。简单问题简短回答，解释性问题给出关键原因和具体例子，通常不超过180字。用自然口语，不用Markdown。不确定时明确说明，听不清时请用户澄清，不编造事实。'


def parse_messages(payload, *, vision_enabled=False):
    messages = payload.get('messages') if isinstance(payload, dict) else None
    if not isinstance(messages, list) or not 1 <= len(messages) <= 13:
        raise ValueError('Expected 1–13 messages')
    result, audio_samples, text_chars = [], 0, 0
    image_ids = set()
    for message in messages:
        if not isinstance(message, dict) or message.get('role') not in ('user', 'assistant'):
            raise ValueError('Invalid role')
        text, audio = message.get('text'), message.get('audio')
        if (text is None) == (audio is None):
            raise ValueError('Expected either text or audio')
        if audio is not None:
            if message['role'] != 'user' or not isinstance(audio, str) or len(audio) > 1_280_000:
                raise ValueError('Invalid audio input')
            raw = base64.b64decode(audio, validate=True)
            if not raw or len(raw) % 2 or len(raw) > 960000:
                raise ValueError('Expected at most 30s PCM16 mono at 16k')
            audio_samples += len(raw) // 2
            content = [np.frombuffer(raw, '<i2').astype(np.float32) / 32768]
        else:
            if not isinstance(text, str) or not 1 <= len(text) <= 4000:
                raise ValueError('Invalid text input')
            text_chars += len(text)
            content = [text]
        if audio_samples > 960000 or text_chars > 12000:
            raise ValueError('History exceeds 60s audio or 12000 characters')
        if 'images' in message:
            if not vision_enabled or message['role'] != 'user':
                raise ValueError('Image input is not enabled for this message')
            content = decode_images(message['images'], image_ids) + content
        if message.get('images_omitted') is True:
            content = ['[此轮较早的附图已移出上下文，不能查看其内容。]'] + content
        result.append({'role': message['role'], 'content': content})
    if result[-1]['role'] != 'user':
        raise ValueError('Last message must be user input')
    return result


def create_app(engine):
    app = FastAPI()
    lock = threading.Lock()
    stats = {'requests': 0, 'cancelled': 0, 'failed': 0}

    @app.get('/health')
    def health():
        return dict(ready=True, busy=lock.locked(), model='MiniCPM-o-4.5-AWQ',
                    input_sample_rate=16000, output_sample_rate=24000,
                    mode='vad_segmented_end_to_end',
                    capabilities=dict(image_input=bool(getattr(engine, 'vision_enabled', False)),
                                      visual_grounding_policy=getattr(engine, 'visual_grounding_policy', 'baseline'),
                                      max_images=2, max_image_bytes=262144,
                                      max_image_dimension=1024, capture_clock='client_session_ms'), **stats)

    @app.post('/generate')
    async def generate(request: Request):
        body = bytearray()
        async for data in request.stream():
            if len(body) + len(data) > 4_000_000:
                raise HTTPException(413, 'Request too large')
            body.extend(data)
        try:
            payload=json.loads(body)
            messages = parse_messages(payload, vision_enabled=bool(getattr(engine, 'vision_enabled', False)))
            voice=payload.get('voice','female')
            if voice not in ('male','female'):
                raise ValueError('Unknown voice')
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(400, str(exc)) from exc
        if not lock.acquire(blocking=False):
            raise HTTPException(409, 'Model busy; previous inference is still cleaning up')
        stopped = threading.Event()
        finished = threading.Event()
        output = queue.Queue(maxsize=4)
        stats['requests'] += 1

        def emit(event):
            while not stopped.is_set():
                try:
                    output.put(event, timeout=.05)
                    return True
                except queue.Full:
                    pass
            return False

        def infer():
            total, text_count, error = 0, 0, None
            generator = None
            try:
                if hasattr(engine,'select_voice'):
                    engine.select_voice(voice)
                generator = engine.generate(messages, stopped)
                for text, audio in generator:
                    if stopped.is_set():
                        break
                    if text:
                        text_count += len(text)
                        if text_count > 4000:
                            raise ValueError('Model text exceeds output limit')
                        if not emit({'type': 'text', 'text': text}):
                            break
                    if audio is not None:
                        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
                        if not np.isfinite(samples).all() or total + len(samples) > 720000:
                            raise ValueError('Model audio exceeds 30s or is nonfinite')
                        total += len(samples)
                        pcm = (np.clip(samples, -1, 1) * 32767).astype('<i2').tobytes()
                        for i in range(0, len(pcm), 48000):
                            if not emit({'type': 'audio', 'audio': base64.b64encode(pcm[i:i+48000]).decode()}):
                                break
                if not stopped.is_set() and not total:
                    raise ValueError('Model generated no audio')
            except Exception:
                stats['failed'] += 1
                traceback.print_exc()
                error = {'type': 'error', 'message': 'MiniCPM generation failed; inspect worker log'}
            finally:
                try:
                    if generator is not None:
                        generator.close()
                finally:
                    lock.release()
                    if stopped.is_set():
                        stats['cancelled'] += 1
                    else:
                        emit(error or {'type': 'done', 'samples': total})
                    finished.set()

        thread = threading.Thread(target=infer, daemon=True, name='minicpm-inference')
        async def stream():
            try:
                yield json.dumps({'type':'metadata','sample_rate':24000,'protocol':1}) + '\n'
                while True:
                    try:
                        event = output.get_nowait()
                    except queue.Empty:
                        if finished.is_set():
                            raise RuntimeError('Worker ended without terminal event')
                        await asyncio.sleep(.01)
                        continue
                    yield json.dumps(event, ensure_ascii=False) + '\n'
                    if event['type'] in ('done', 'error'):
                        return
            finally:
                stopped.set()
                # The inference thread owns the lock until GPU work and
                # generator cleanup finish, even after HTTP disconnect.
        try:
            thread.start()
        except BaseException:
            lock.release()
            raise
        return StreamingResponse(stream(), media_type='application/x-ndjson')

    return app


class MiniCPMEngine:
    def __init__(self, model_path, *, male_reference=None, female_reference=None, vision_enabled=False,
                 visual_grounding_policy='baseline'):
        if visual_grounding_policy not in POLICIES:
            raise ValueError('Unknown visual grounding policy')
        self.visual_grounding_policy = visual_grounding_policy
        import torch
        from transformers import AutoModel
        torch.set_num_threads(4)
        self.torch = torch
        self.vision_enabled = bool(vision_enabled)
        self.model = AutoModel.from_pretrained(str(model_path), trust_remote_code=True,
            attn_implementation='sdpa', torch_dtype=torch.float16,
            init_vision=self.vision_enabled, init_audio=True, init_tts=True, local_files_only=True)
        self.model.eval().cuda()
        self.model.init_tts(streaming=True, model_dir=str(model_path / 'assets/token2wav'))
        import librosa
        self.references={}
        for voice,path in [('male',male_reference),('female',female_reference or model_path/'assets/system_ref_audio.wav')]:
            if path:
                reference, _ = librosa.load(str(path), sr=16000, mono=True, duration=10)
                self.references[voice]=reference
        self.voice=None
        self.select_voice('female')
        print('MiniCPM loaded', {'allocated_mib': round(torch.cuda.memory_allocated()/1048576)}, flush=True)

    def select_voice(self, voice):
        if voice not in self.references:
            raise ValueError('Reference voice is not configured')
        if voice != self.voice:
            self.model.init_token2wav_cache(prompt_speech_16k=self.references[voice])
            self.voice=voice

    def generate(self, messages, stop):
        with self.torch.inference_mode():
            self.model.reset_session(reset_token2wav_cache=False)
            sid = 'pipecat-' + str(time.monotonic_ns())
            system_prompt = build_system_prompt(SYSTEM, messages,
                                                getattr(self, 'visual_grounding_policy', 'baseline'))
            system_content=[system_prompt]
            if getattr(self,'references',None):
                system_content=['模仿音频样本的音色并生成新的内容。',self.references[self.voice],system_prompt]
            self.model.streaming_prefill(session_id=sid,msgs=[{'role':'system','content':system_content}],omni_mode=False)
            for message in messages:
                content = message['content']
                if message['role'] == 'user':
                    # Replaying history does not call streaming_generate between
                    # turns, so explicitly reset the audio encoder's chunk state.
                    self.model.init_streaming_processor()
                elif message['role'] == 'assistant':
                    # This pinned streaming API templates only the first message
                    # and new user turns; subsequent assistant content is raw.
                    content = ['<|im_end|>\n<|im_start|>assistant\n' + content[0] + '<|im_end|>\n']
                chunks = prefill_chunks(content)
                for i, chunk in enumerate(chunks):
                    if stop.is_set():
                        return
                    self.model.streaming_prefill(session_id=sid,
                        msgs=[{'role':message['role'],'content':chunk}], omni_mode=False,
                        is_last_chunk=i == len(chunks)-1)
            generator = self.model.streaming_generate(session_id=sid, generate_audio=True,
                use_tts_template=True, do_sample=True, max_new_tokens=256)
            try:
                for audio, text in generator:
                    if stop.is_set():
                        return
                    yield text, None if audio is None else audio.squeeze().float().cpu().numpy()
            finally:
                generator.close()


if __name__ == '__main__':
    import uvicorn
    parser=argparse.ArgumentParser()
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18201)
    parser.add_argument('--male-reference',type=Path)
    parser.add_argument('--female-reference',type=Path)
    parser.add_argument('--vision',action='store_true',help='Load the vision encoder and enable bounded still-image input')
    parser.add_argument('--visual-grounding', choices=POLICIES, default='baseline',
                        help='Server-selected visual evidence prompt; baseline preserves the original prompt')
    args=parser.parse_args()
    engine=MiniCPMEngine(args.model,male_reference=args.male_reference,female_reference=args.female_reference,
                         vision_enabled=args.vision, visual_grounding_policy=args.visual_grounding)
    uvicorn.run(create_app(engine), host='127.0.0.1', port=args.port, limit_concurrency=16)
