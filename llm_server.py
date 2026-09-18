"""Offline localhost streaming LLM worker; Qwen3 and Qwen3.5 text generation."""
import argparse
import asyncio
import io
import json
import os
import threading
import time
import wave
from contextlib import asynccontextmanager

os.environ.setdefault('HF_HUB_OFFLINE', '1')
os.environ.setdefault('TRANSFORMERS_OFFLINE', '1')
os.environ.setdefault('OMP_NUM_THREADS', '4')
os.environ.setdefault('TOKENIZERS_PARALLELISM', 'false')

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field


def build_app(args):
    lock = threading.Lock()
    quantization = getattr(args, 'quantization', 'none')
    if quantization != 'none' and (args.mode != 'llm' or args.device != 'cuda'):
        raise ValueError('4-bit quantization is available only for CUDA LLM workers')
    state = {'ready': False, 'mode': args.mode, 'model': args.model, 'device': args.device,
             'quantization': quantization}
    model = tokenizer = None

    @asynccontextmanager
    async def lifespan(app):
        nonlocal model, tokenizer
        start = time.perf_counter()
        if args.mode == 'llm':
            import torch
            from transformers import AutoConfig, AutoModelForCausalLM, AutoTokenizer
            torch.set_num_threads(4)
            tokenizer = AutoTokenizer.from_pretrained(args.model, local_files_only=True)
            load_options = {}
            if quantization == '4bit':
                from transformers import BitsAndBytesConfig
                load_options['quantization_config'] = BitsAndBytesConfig(
                    load_in_4bit=True, bnb_4bit_quant_type='nf4',
                    bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=torch.bfloat16)
            config = AutoConfig.from_pretrained(args.model, local_files_only=True)
            loader = AutoModelForCausalLM
            if config.model_type == 'qwen3_5':
                from transformers import Qwen3_5ForCausalLM
                loader = Qwen3_5ForCausalLM
            model = loader.from_pretrained(
                args.model, local_files_only=True, dtype=torch.bfloat16,
                device_map=args.device, attn_implementation='sdpa', **load_options).eval()
        state.update(ready=True, load_s=time.perf_counter()-start)
        print(json.dumps(state), flush=True)
        yield

    app = FastAPI(title='Voice Agent Research Worker', lifespan=lifespan)

    @app.get('/health')
    def health():
        return dict(state, busy=lock.locked())

    class GenerateInput(BaseModel):
        messages: list[dict]
        max_new_tokens: int = Field(default=96, ge=1, le=256)

    @app.post('/generate')
    def generate(body: GenerateInput):
        if args.mode != 'llm':
            raise HTTPException(404, 'This is an ASR worker')
        if len(body.messages) > 20 or len(json.dumps(body.messages)) > 20000:
            raise HTTPException(413, 'Conversation too large for local trial')
        if not lock.acquire(blocking=False):
            raise HTTPException(409, 'Single worker busy')
        import torch
        from transformers import TextIteratorStreamer, StoppingCriteria, StoppingCriteriaList
        stop = threading.Event()
        errors = []

        class StopEvent(StoppingCriteria):
            def __call__(self, input_ids, scores, **kwargs):
                return stop.is_set()

        try:
            prompt = tokenizer.apply_chat_template(body.messages, tokenize=False,
                                                   add_generation_prompt=True, enable_thinking=False)
            inputs = tokenizer(prompt, return_tensors='pt').to(model.device)
            streamer = TextIteratorStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True, timeout=60)
        except BaseException:
            lock.release()
            raise

        def produce():
            try:
                with torch.inference_mode():
                    model.generate(**inputs, streamer=streamer, max_new_tokens=body.max_new_tokens,
                                   do_sample=False, stopping_criteria=StoppingCriteriaList([StopEvent()]))
            except Exception as exc:
                errors.append(str(exc))
                streamer.end()
            finally:
                lock.release()

        thread = threading.Thread(target=produce, daemon=True)
        thread.start()

        async def output():
            sentinel = object()
            iterator = iter(streamer)
            def advance():
                return next(iterator, sentinel)
            try:
                while True:
                    text = await asyncio.to_thread(advance)
                    if text is sentinel:
                        break
                    if text:
                        yield json.dumps({'text': text}, ensure_ascii=False) + '\n'
                if errors:
                    yield json.dumps({'error': errors[0]}, ensure_ascii=False) + '\n'
            finally:
                stop.set()
                # Admission is owned by the producer until GPU generation exits.

        return StreamingResponse(output(), media_type='application/x-ndjson')

    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.set_defaults(mode='llm')
    parser.add_argument('--model', required=True)
    parser.add_argument('--device', choices=['cuda', 'cpu'], default='cuda')
    parser.add_argument('--port', required=True, type=int)
    parser.add_argument('--quantization', choices=['none', '4bit'], default='none')
    args = parser.parse_args()
    import uvicorn
    uvicorn.run(build_app(args), host='127.0.0.1', port=args.port, log_level='info')
