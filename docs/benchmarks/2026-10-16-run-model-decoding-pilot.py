"""Fixed-input local text experiment; never changes application/model services."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import urllib.request

os.environ.setdefault('HF_HUB_OFFLINE', '1')
os.environ.setdefault('TRANSFORMERS_OFFLINE', '1')
os.environ.setdefault('OMP_NUM_THREADS', '4')
os.environ.setdefault('TOKENIZERS_PARALLELISM', 'false')


class Artifact:
    def __init__(self, output, state):
        self.output, self.state = Path(output), state
        self.output.parent.mkdir(parents=True, exist_ok=True)
        with self.output.open('x') as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
        self.save()

    def save(self):
        with tempfile.NamedTemporaryFile(mode='w', dir=self.output.parent, delete=False) as handle:
            json.dump(self.state, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
            temporary = Path(handle.name)
        try:
            temporary.replace(self.output)
        finally:
            temporary.unlink(missing_ok=True)


def sha256(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def request_matrix(manifest):
    rows = []
    for index, case in enumerate(manifest['cases']):
        conditions = manifest['conditions'][::(-1 if index % 2 else 1)]
        for condition in conditions:
            seeds = condition['seeds']
            offset = index % len(seeds)
            for seed in seeds[offset:] + seeds[:offset]:
                rows.append({'case': case, 'condition': condition, 'seed': seed})
    return rows


def finish_reason(token_ids, eos_ids, budget, stop_reason):
    if stop_reason:
        return stop_reason
    if token_ids and token_ids[-1] in eos_ids:
        return 'eos'
    if len(token_ids) >= budget:
        return 'length'
    return 'unexpected_stop'


def require_idle():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for port in (18314, 18311):
        with opener.open(f'http://127.0.0.1:{port}/health', timeout=2) as response:
            health = json.load(response)
        if not health.get('ready'):
            raise RuntimeError('service_not_ready')
        if port == 18314 and health.get('active_sessions') != 0:
            raise RuntimeError('application_active')
        if port == 18311 and health.get('busy'):
            raise RuntimeError('production_model_busy')


def run(args):
    manifest_path = Path(__file__).with_name('2026-10-16-model-decoding-input.json')
    manifest = json.loads(manifest_path.read_text())
    model_path = args.model.resolve()
    if model_path.name not in manifest['models']:
        raise ValueError('Model must match a predeclared local model name')
    state = {'status': 'initializing', 'model': model_path.name, 'scope': manifest['scope'],
             'started_utc': datetime.now(timezone.utc).isoformat(),
             'manifest_sha256': sha256(manifest_path), 'collector_sha256': sha256(__file__),
             'rows': [], 'warmup': None}
    artifact = Artifact(args.output, state)
    try:
        require_idle()
        import torch
        from transformers import (AutoConfig, AutoModelForCausalLM, AutoTokenizer,
                                  BitsAndBytesConfig, TextIteratorStreamer,
                                  StoppingCriteria, StoppingCriteriaList, set_seed)
        torch.set_num_threads(4)
        free, total = torch.cuda.mem_get_info()
        state['device'] = {'name': torch.cuda.get_device_name(), 'total_bytes': total, 'free_before_load_bytes': free}
        if free < 8 * 1024**3:
            raise RuntimeError('Insufficient free memory for isolated experiment')
        state['environment'] = {package: importlib.metadata.version(package)
                                for package in ('torch', 'transformers', 'bitsandbytes', 'accelerate')}
        files = sorted(p for p in model_path.iterdir() if p.is_file() and p.suffix in {'.json', '.jinja', '.safetensors'})
        state['model_files'] = {p.name: {'bytes': p.stat().st_size, 'sha256': sha256(p)} for p in files}
        artifact.save()
        require_idle()
        tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
        config = AutoConfig.from_pretrained(model_path, local_files_only=True)
        loader = AutoModelForCausalLM
        if config.model_type == 'qwen3_5':
            from transformers import Qwen3_5ForCausalLM
            loader = Qwen3_5ForCausalLM
        start = time.monotonic()
        model = loader.from_pretrained(model_path, local_files_only=True, dtype=torch.bfloat16,
            device_map='cuda', attn_implementation='sdpa', quantization_config=BitsAndBytesConfig(
                load_in_4bit=True, bnb_4bit_quant_type='nf4', bnb_4bit_use_double_quant=True,
                bnb_4bit_compute_dtype=torch.bfloat16)).eval()
        state['load_s'] = time.monotonic() - start
        state['generation_config'] = model.generation_config.to_dict()
        # Never export local model directories embedded in generation config.
        state['generation_config'].pop('_from_model_config', None)
        state['chat_template'] = tokenizer.chat_template
        state['quantization'] = manifest['quantization']
        if config.model_type == 'qwen3_5':
            from transformers.models.qwen3_5 import modeling_qwen3_5
            state['qwen35_fast_path'] = getattr(modeling_qwen3_5, 'is_fast_path_available', None)
        state['status'] = 'running'
        artifact.save()

        def generate(case, condition, seed):
            require_idle()
            row = {'case': case['id'], 'condition': condition['id'], 'seed': seed,
                   'status': 'running', 'chunks': [], 'text': '', 'token_ids': None}
            state['warmup'] = row if case['id'] == 'warmup' else state['warmup']
            if case['id'] != 'warmup':
                state['rows'].append(row)
            artifact.save()
            messages = [{'role': 'system', 'content': manifest['system_prompt']}] + case['messages']
            prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True,
                                                    enable_thinking=manifest['enable_thinking'])
            inputs = tokenizer(prompt, return_tensors='pt').to(model.device)
            row.update(messages=messages, prompt=prompt, input_token_ids=inputs['input_ids'][0].tolist())
            kwargs = {k: v for k, v in condition.items() if k not in {'id', 'seeds'}}
            row['generation_kwargs'] = kwargs
            set_seed(seed)
            torch.cuda.synchronize()
            torch.cuda.reset_peak_memory_stats()
            started = time.monotonic()
            stop, monitor_done = threading.Event(), threading.Event()
            stopped = []
            produced, errors = [], []
            streamer = TextIteratorStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True, timeout=60)

            class Stop(StoppingCriteria):
                def __call__(self, input_ids, scores, **unused):
                    if time.monotonic() - started > 90 and not stopped:
                        stopped.append('time_limit')
                        stop.set()
                    return stop.is_set()

            def monitor():
                while not monitor_done.wait(1):
                    try:
                        require_idle()
                    except Exception as exc:
                        stopped.append(str(exc) if isinstance(exc, RuntimeError) else 'health_check_failed')
                        stop.set()
                        return

            def produce():
                try:
                    with torch.inference_mode():
                        output = model.generate(**inputs, **kwargs, streamer=streamer,
                            stopping_criteria=StoppingCriteriaList([Stop()]))
                    produced.append(output[0, inputs['input_ids'].shape[1]:].tolist())
                except BaseException as exc:
                    errors.append(type(exc).__name__)
                    streamer.end()

            monitor_thread = threading.Thread(target=monitor, daemon=True)
            worker = threading.Thread(target=produce, daemon=True)
            monitor_thread.start(); worker.start()
            try:
                for text in streamer:
                    if text:
                        row['chunks'].append({'text': text, 'elapsed_s': time.monotonic() - started})
                        row['text'] += text
                worker.join(timeout=10)
                if worker.is_alive():
                    raise TimeoutError('Generation did not terminate')
                if errors:
                    row['model_error_type'] = errors[0]
                    raise RuntimeError('Model generation failed')
                torch.cuda.synchronize()
                row['elapsed_s'] = time.monotonic() - started
                row['first_text_s'] = row['chunks'][0]['elapsed_s'] if row['chunks'] else None
                row['token_ids'] = produced[0]
                row['generated_tokens'] = len(produced[0])
                row['decoded_text'] = tokenizer.decode(produced[0], skip_special_tokens=True)
                eos = model.generation_config.eos_token_id
                eos = [eos] if isinstance(eos, int) else eos or []
                row['eos_token_ids'] = eos
                row['finish_reason'] = finish_reason(produced[0], eos, kwargs['max_new_tokens'], stopped[0] if stopped else None)
                row['peak_allocated_bytes'] = torch.cuda.max_memory_allocated()
                row['peak_reserved_bytes'] = torch.cuda.max_memory_reserved()
                row['status'] = 'complete' if row['finish_reason'] in {'eos', 'length'} else 'stopped'
                if row['status'] != 'complete':
                    raise RuntimeError('Experiment stopped by runtime guard')
            except BaseException as exc:
                row.update(status='failed', error_type=type(exc).__name__)
                raise
            finally:
                stop.set(); monitor_done.set()
                worker.join(timeout=10); monitor_thread.join(timeout=3)
                artifact.save()
            print(json.dumps({k: row[k] for k in ('case', 'condition', 'seed', 'finish_reason', 'generated_tokens', 'elapsed_s', 'text')}, ensure_ascii=False), flush=True)

        generate({'id': 'warmup', 'messages': [{'role': 'user', 'content': '请说你好。'}]},
                 {'id': 'warmup', 'max_new_tokens': 16, 'do_sample': False}, 17)
        for item in request_matrix(manifest):
            generate(**item)
        require_idle()
        state['status'] = 'complete'
    except BaseException as exc:
        state.update(status='failed', error_type=type(exc).__name__)
        raise
    finally:
        state['finished_utc'] = datetime.now(timezone.utc).isoformat()
        artifact.save()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    run(parser.parse_args())
