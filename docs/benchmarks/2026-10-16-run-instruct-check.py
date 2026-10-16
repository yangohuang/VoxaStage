"""Frozen instruction-checkpoint comparison derived from the reviewed precision collector.

The earlier collector remains unchanged so published source digests stay valid.
This copy changes the default manifest and provides Python3.10-compatible hashing.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
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
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(8 * 1024**2), b''):
            digest.update(chunk)
    return digest.hexdigest()


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


def pending_rows(manifest):
    return [{'case': item['case']['id'], 'condition': item['condition']['id'], 'seed': item['seed'],
             'status': 'pending', 'chunks': [], 'text': '', 'token_ids': []}
            for item in request_matrix(manifest)]


def request_configuration(manifest, case, condition):
    system = manifest['system_prompt']
    if 'system_variant' in condition:
        system = manifest['system_prompts'][condition['system_variant']]
    thinking = condition.get('enable_thinking', manifest['enable_thinking'])
    metadata = {'id', 'seeds', 'system_variant', 'enable_thinking'}
    kwargs = {key: value for key, value in condition.items() if key not in metadata}
    return [{'role': 'system', 'content': system}] + case['messages'], thinking, kwargs


def final_answer_view(chunks, thinking):
    """Separate user-visible answers from reasoning, retaining stream timestamps.

    Thinking mode requires an explicit closing marker, including templates that
    put the opening marker in the prompt. Missing closure is never an answer.
    """
    text = ''.join(chunk['text'] for chunk in chunks)
    unexpected = not thinking and ('<think>' in text or '</think>' in text)
    reasoning = thinking or unexpected
    close = text.find('</think>') if reasoning else -1
    closed = close >= 0 if reasoning else None
    start = close + len('</think>') if closed else (len(text) if reasoning else 0)
    while start < len(text) and text[start].isspace():
        start += 1
    first = None
    offset = 0
    for chunk in chunks:
        offset += len(chunk['text'])
        if start < offset:
            first = chunk['elapsed_s']
            break
    return {'final_text': text[start:].strip(), 'first_final_text_s': first,
            'thinking_closed': closed, 'unexpected_thinking': unexpected}


class TokenRecorder:
    """Keep every generated token even if generation raises before returning."""
    def __init__(self, delegate):
        self.delegate, self.ids, self.prompt = delegate, [], True

    def put(self, value):
        values = value.tolist()
        if self.prompt:
            self.prompt = False
        else:
            if values and isinstance(values[0], list):
                if len(values) != 1:
                    raise ValueError('Collector supports one sequence only')
                values = values[0]
            self.ids.extend(values)
        self.delegate.put(value)

    def end(self):
        self.delegate.end()


def finalize_capture(row, ids, eos_ids, budget, stopped, elapsed):
    row.update(token_ids=list(ids), generated_tokens=len(ids), elapsed_s=elapsed,
               first_text_s=row['chunks'][0]['elapsed_s'] if row['chunks'] else None,
               eos_token_ids=eos_ids, finish_reason=finish_reason(ids, eos_ids, budget, stopped))


def process_memory_bytes():
    output = subprocess.check_output(['nvidia-smi', '--query-compute-apps=pid,used_memory',
                                      '--format=csv,noheader,nounits'], text=True, timeout=3)
    values = [int(memory.strip()) * 1024**2 for line in output.splitlines()
              for pid, memory in [line.split(',', 1)] if int(pid.strip()) == os.getpid()]
    return sum(values) if values else None


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


def load_configuration(precision, manifest, torch, quantization_class):
    if precision not in {'nf4', 'bf16'}:
        raise ValueError('Precision must be nf4 or bf16')
    options = {'dtype': torch.bfloat16, 'device_map': 'cuda', 'attn_implementation': 'sdpa'}
    if precision == 'nf4':
        options['quantization_config'] = quantization_class(
            load_in_4bit=True, bnb_4bit_quant_type='nf4',
            bnb_4bit_use_double_quant=True, bnb_4bit_compute_dtype=torch.bfloat16)
    minimum = manifest['precision_profiles'][precision]['minimum_free_gib'] * 1024**3
    return options, minimum


def run(args):
    manifest_path = getattr(args, 'manifest', None) or Path(__file__).with_name('2026-10-16-instruct-input.json')
    manifest = json.loads(manifest_path.read_text())
    model_path = args.model.resolve()
    if model_path.name not in manifest['models']:
        raise ValueError('Model must match a predeclared local model name')
    state = {'status': 'initializing', 'model': model_path.name, 'scope': manifest['scope'],
             'precision': args.precision,
             'started_utc': datetime.now(timezone.utc).isoformat(),
             'manifest_sha256': sha256(manifest_path), 'collector_sha256': sha256(__file__),
             'rows': pending_rows(manifest), 'warmup': None}
    artifact = Artifact(args.output, state)
    try:
        require_idle()
        import torch
        from transformers import (AutoConfig, AutoModelForCausalLM, AutoTokenizer,
                                  BitsAndBytesConfig, TextIteratorStreamer,
                                  StoppingCriteria, StoppingCriteriaList, set_seed)
        torch.set_num_threads(4)
        load_options, minimum_free = load_configuration(args.precision, manifest, torch, BitsAndBytesConfig)
        state['minimum_free_bytes'] = minimum_free
        state['quantization'] = manifest['precision_profiles'][args.precision]['quantization']
        free, total = torch.cuda.mem_get_info()
        state['device'] = {'name': torch.cuda.get_device_name(), 'total_bytes': total,
                           'free_at_admission_bytes': free, 'free_before_load_bytes': None}
        if free < minimum_free:
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
        # Hashing/tokenizer setup can take time while other GPU jobs start.
        require_idle()
        free, _ = torch.cuda.mem_get_info()
        state['device']['free_before_load_bytes'] = free
        if free < minimum_free:
            raise RuntimeError('Insufficient free memory immediately before model load')
        start = time.monotonic()
        model = loader.from_pretrained(model_path, local_files_only=True, **load_options).eval()
        state['load_s'] = time.monotonic() - start
        state['generation_config'] = model.generation_config.to_dict()
        # Never export local model directories embedded in generation config.
        state['generation_config'].pop('_from_model_config', None)
        state['chat_template'] = tokenizer.chat_template
        if config.model_type == 'qwen3_5':
            from transformers.models.qwen3_5 import modeling_qwen3_5
            state['qwen35_fast_path'] = getattr(modeling_qwen3_5, 'is_fast_path_available', None)
        state['status'] = 'running'
        artifact.save()

        def generate(case, condition, seed, row):
            row['status'] = 'running'
            row['phase'] = 'admission'
            artifact.save()
            attempt_started = time.monotonic()
            started = None
            stop, monitor_done = threading.Event(), threading.Event()
            stopped, produced, errors, memory_samples = [], [], [], []
            tokens = worker = monitor_thread = None
            kwargs = {'max_new_tokens': condition['max_new_tokens']}
            thinking = condition.get('enable_thinking', manifest['enable_thinking'])
            eos = model.generation_config.eos_token_id
            eos = [eos] if isinstance(eos, int) else eos or []
            row['process_memory_sample_interval_s'] = 1
            row['process_memory_sample_errors'] = 0

            def sample_memory():
                try:
                    value = process_memory_bytes()
                    if value is not None:
                        memory_samples.append(value)
                    else:
                        row['process_memory_sample_errors'] += 1
                except Exception:
                    row['process_memory_sample_errors'] += 1

            def monitor():
                while not monitor_done.wait(1):
                    try:
                        require_idle()
                    except Exception as exc:
                        stopped.append(str(exc) if isinstance(exc, RuntimeError) else 'health_check_failed')
                        stop.set()
                        return
                    sample_memory()

            class Stop(StoppingCriteria):
                def __call__(self, input_ids, scores, **unused):
                    if time.monotonic() - started > 90 and not stopped:
                        stopped.append('time_limit')
                        stop.set()
                    return stop.is_set()

            try:
                require_idle()
                row['phase'] = 'configuration'
                messages, thinking, kwargs = request_configuration(manifest, case, condition)
                row.update(generation_kwargs=kwargs, enable_thinking=thinking,
                           system_variant=condition.get('system_variant', 'default'))
                row['phase'] = 'tokenization'
                prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True,
                                                        enable_thinking=thinking)
                inputs = tokenizer(prompt, return_tensors='pt').to(model.device)
                row.update(messages=messages, prompt=prompt, input_token_ids=inputs['input_ids'][0].tolist())
                set_seed(seed)
                torch.cuda.synchronize()
                torch.cuda.reset_peak_memory_stats()
                sample_memory()
                started = time.monotonic()
                streamer = TextIteratorStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True, timeout=60)
                tokens = TokenRecorder(streamer)
                row['phase'] = 'generation'

                def produce():
                    try:
                        with torch.inference_mode():
                            output = model.generate(**inputs, **kwargs, streamer=tokens,
                                stopping_criteria=StoppingCriteriaList([Stop()]))
                        produced.append(output[0, inputs['input_ids'].shape[1]:].tolist())
                    except BaseException as exc:
                        errors.append(type(exc).__name__)
                        streamer.end()

                monitor_thread = threading.Thread(target=monitor, daemon=True)
                worker = threading.Thread(target=produce, daemon=True)
                monitor_thread.start(); worker.start()
                for text in streamer:
                    if text:
                        row['chunks'].append({'text': text, 'elapsed_s': time.monotonic() - started})
                        row['text'] += text
                worker.join(timeout=10)
                if worker.is_alive():
                    stopped.append('worker_timeout')
                    raise TimeoutError('Generation did not terminate')
                if errors:
                    row['model_error_type'] = errors[0]
                    stopped.append('model_error')
                    raise RuntimeError('Model generation failed')
                if not produced or produced[0] != tokens.ids:
                    stopped.append('token_capture_mismatch')
                    raise RuntimeError('Returned tokens do not match stream capture')
                if stopped:
                    raise RuntimeError('Experiment stopped by runtime guard')
                row['status'] = 'complete'
            except BaseException as exc:
                if not stopped:
                    stopped.append('cancelled' if isinstance(exc, KeyboardInterrupt) else row['phase'] + '_error')
                row.update(status='failed', error_type=type(exc).__name__)
                raise
            finally:
                generation_observed_end = time.monotonic()
                stop.set(); monitor_done.set()
                if worker is not None:
                    worker.join(timeout=10)
                if monitor_thread is not None:
                    monitor_thread.join(timeout=8)
                elapsed = generation_observed_end - started if started is not None else None
                finalize_capture(row, tokens.ids if tokens is not None else [], eos,
                                 kwargs['max_new_tokens'], stopped[0] if stopped else None, elapsed)
                row['attempt_elapsed_s'] = time.monotonic() - attempt_started
                row['decoded_text'] = tokenizer.decode(row['token_ids'], skip_special_tokens=True)
                row.update(final_answer_view(row['chunks'], thinking))
                row['torch_peak_allocated_bytes'] = torch.cuda.max_memory_allocated() if started is not None else None
                row['torch_peak_reserved_bytes'] = torch.cuda.max_memory_reserved() if started is not None else None
                sample_memory()
                row['sampled_process_peak_bytes'] = max(memory_samples) if memory_samples else None
                row['process_memory_samples'] = len(memory_samples)
                artifact.save()
            if row['finish_reason'] not in {'eos', 'length'}:
                row['status'] = 'failed'
                artifact.save()
                raise RuntimeError('Unexpected model termination')
            print(json.dumps({k: row[k] for k in ('case', 'condition', 'seed', 'finish_reason', 'generated_tokens', 'elapsed_s', 'text')}, ensure_ascii=False), flush=True)

        warmup = {'case': 'warmup', 'condition': 'warmup', 'seed': 17,
                  'status': 'pending', 'chunks': [], 'text': '', 'token_ids': []}
        state['warmup'] = warmup
        generate({'id': 'warmup', 'messages': [{'role': 'user', 'content': '请说你好。'}]},
                 {'id': 'warmup', 'max_new_tokens': 16, 'do_sample': False}, 17, warmup)
        for item, row in zip(request_matrix(manifest), state['rows']):
            generate(**item, row=row)
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
    parser.add_argument('--manifest', type=Path, help='Predeclared experiment input; defaults to the instruction-checkpoint diagnostic')
    parser.add_argument('--precision', required=True, choices=['nf4', 'bf16'])
    run(parser.parse_args())
