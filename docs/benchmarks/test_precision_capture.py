import contextlib
import importlib.util
import io
import json
from pathlib import Path
import queue
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

PATH = Path(__file__).with_name('2026-10-16-run-precision-check.py')
spec = importlib.util.spec_from_file_location('precision', PATH)
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)
MANIFEST = PATH.with_name('2026-10-16-precision-input.json')


class PrecisionTests(unittest.TestCase):
    def test_precision_options_and_minimum_memory_are_distinct(self):
        manifest = json.loads(MANIFEST.read_text())
        torch = SimpleNamespace(bfloat16='BF16')
        calls = []
        def quantization(**kwargs):
            calls.append(kwargs)
            return kwargs
        nf4, memory = capture.load_configuration('nf4', manifest, torch, quantization)
        self.assertEqual(memory, 8 * 1024**3)
        self.assertEqual(nf4['quantization_config'], {
            'load_in_4bit': True, 'bnb_4bit_quant_type': 'nf4',
            'bnb_4bit_use_double_quant': True, 'bnb_4bit_compute_dtype': 'BF16'})
        bf16, memory = capture.load_configuration('bf16', manifest, torch, quantization)
        self.assertEqual(memory, 11 * 1024**3)
        self.assertEqual(bf16, {'dtype': 'BF16', 'device_map': 'cuda', 'attn_implementation': 'sdpa'})
        self.assertEqual(len(calls), 1)
        with self.assertRaises(ValueError):
            capture.load_configuration('cpu', manifest, torch, quantization)

    def test_matrix_keeps_new_questions_separate_from_historical_anchors(self):
        manifest = json.loads(MANIFEST.read_text())
        self.assertEqual(sum(c['group'] == 'new_diagnostic' for c in manifest['cases']), 12)
        self.assertEqual(sum(c['group'] == 'historical_anchor' for c in manifest['cases']), 2)
        rows = capture.request_matrix(manifest)
        self.assertEqual(len(rows), 28)
        self.assertEqual(len({(r['case']['id'], r['seed']) for r in rows}), 28)

    def run_fake(self, precision, free_gib=12, fail_formal=False):
        load_calls, quantization_calls, generation_calls = [], [], []
        memory_values = iter(free_gib) if isinstance(free_gib, list) else None
        def memory_info():
            free = next(memory_values) if memory_values is not None else free_gib
            return free * 1024**3, 24 * 1024**3
        class Tensor:
            def __init__(self, values): self.values = values
            @property
            def shape(self): return [1, len(self.values[0])]
            def tolist(self): return self.values
            def __getitem__(self, key):
                return Tensor(self.values[key[0]][key[1]]) if isinstance(key, tuple) else Tensor(self.values[key])
        class Inputs(dict):
            def to(self, device): return self
        class Tokenizer:
            chat_template = 'fake template'
            def apply_chat_template(self, messages, **kwargs):
                self.assertions = kwargs
                return 'fake prompt'
            def __call__(self, *args, **kwargs): return Inputs(input_ids=Tensor([[101, 102]]))
            def decode(self, ids, **kwargs): return ''.join(str(i) for i in ids if i != 9)
        class Streamer:
            def __init__(self, *args, **kwargs): self.queue, self.first = queue.Queue(), True
            def put(self, tensor):
                if self.first:
                    self.first = False
                elif tensor.tolist()[0] != 9:
                    self.queue.put(str(tensor.tolist()[0]))
            def end(self): self.queue.put(None)
            def __iter__(self): return self
            def __next__(self):
                value = self.queue.get(timeout=1)
                if value is None: raise StopIteration
                return value
        class Model:
            device = 'cuda'
            generation_config = SimpleNamespace(eos_token_id=[9], to_dict=lambda: {'eos_token_id': [9]})
            def eval(self): return self
            def generate(self, input_ids, streamer, **kwargs):
                generation_calls.append(kwargs)
                streamer.put(input_ids)
                streamer.put(Tensor([7]))
                if fail_formal and len(generation_calls) > 1:
                    raise ValueError('injected formal request failure')
                streamer.put(Tensor([9]))
                streamer.end()
                return Tensor([[101, 102, 7, 9]])
        def load(*args, **kwargs):
            load_calls.append(kwargs)
            return Model()
        def quantization(**kwargs):
            quantization_calls.append(kwargs)
            return kwargs
        cuda = SimpleNamespace(mem_get_info=memory_info,
            get_device_name=lambda: 'fake CPU harness', synchronize=lambda: None,
            reset_peak_memory_stats=lambda: None, max_memory_allocated=lambda: 12,
            max_memory_reserved=lambda: 16)
        torch = SimpleNamespace(cuda=cuda, set_num_threads=lambda n: None,
                                bfloat16='fake BF16', inference_mode=contextlib.nullcontext)
        transformers = SimpleNamespace(AutoConfig=SimpleNamespace(from_pretrained=lambda *a, **kw: SimpleNamespace(model_type='qwen3')),
            AutoModelForCausalLM=SimpleNamespace(from_pretrained=load),
            AutoTokenizer=SimpleNamespace(from_pretrained=lambda *a, **kw: Tokenizer()),
            BitsAndBytesConfig=quantization, TextIteratorStreamer=Streamer,
            StoppingCriteria=object, StoppingCriteriaList=list, set_seed=lambda s: None)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); model_dir = root / 'Qwen3-4B'; model_dir.mkdir()
            (model_dir / 'config.json').write_text('{}')
            output = root / 'result.json'
            error = None
            with patch.dict('sys.modules', {'torch': torch, 'transformers': transformers}), \
                    patch.object(capture, 'require_idle'), \
                    patch.object(capture, 'process_memory_bytes', return_value=20), \
                    patch.object(capture.importlib.metadata, 'version', return_value='fake'), \
                    contextlib.redirect_stdout(io.StringIO()):
                try:
                    capture.run(SimpleNamespace(model=model_dir, output=output, manifest=MANIFEST, precision=precision))
                except RuntimeError as exc:
                    error = str(exc)
            return json.loads(output.read_text()), load_calls, quantization_calls, generation_calls, error

    def test_actual_loader_receives_precision_and_same_sampling(self):
        results = {}
        for precision in ['nf4', 'bf16']:
            with self.subTest(precision=precision):
                result, loads, quantization, generations, error = self.run_fake(precision)
                self.assertIsNone(error)
                self.assertEqual(result['status'], 'complete')
                self.assertEqual(result['precision'], precision)
                self.assertEqual(result['minimum_free_bytes'], (8 if precision == 'nf4' else 11) * 1024**3)
                self.assertEqual(len(loads), 1)
                self.assertEqual('quantization_config' in loads[0], precision == 'nf4')
                self.assertEqual(len(quantization), int(precision == 'nf4'))
                self.assertEqual(loads[0]['device_map'], 'cuda')
                self.assertEqual(loads[0]['dtype'], 'fake BF16')
                self.assertEqual(len(generations), 29)  # one warmup +28 formal
                self.assertTrue(all(row['final_text'] == '7' for row in result['rows']))
                results[precision] = result
        self.assertEqual([r['generation_kwargs'] for r in results['nf4']['rows']],
                         [r['generation_kwargs'] for r in results['bf16']['rows']])

    def test_insufficient_memory_stops_before_loading_and_records_pending_matrix(self):
        for precision, free in [('nf4', 7), ('bf16', 10)]:
            with self.subTest(precision=precision):
                result, loads, _, generations, error = self.run_fake(precision, free)
                self.assertIn('Insufficient free memory', error)
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(loads, [])
                self.assertEqual(generations, [])
                self.assertEqual(sum(row['status'] == 'pending' for row in result['rows']), 28)

    def test_model_error_preserves_partial_tokens_and_unattempted_requests(self):
        result, _, _, _, error = self.run_fake('bf16', fail_formal=True)
        self.assertIn('Model generation failed', error)
        self.assertEqual(result['rows'][0]['token_ids'], [7])
        self.assertEqual(result['rows'][0]['finish_reason'], 'model_error')
        self.assertEqual(result['rows'][0]['status'], 'failed')
        self.assertEqual(sum(row['status'] == 'pending' for row in result['rows']), 27)

    def test_memory_is_rechecked_after_hashing_before_model_load(self):
        result, loads, _, generations, error = self.run_fake('bf16', free_gib=[12, 9])
        self.assertIsNotNone(error)
        self.assertIn('Insufficient free memory', error)
        self.assertEqual(loads, [])
        self.assertEqual(generations, [])
        self.assertEqual(result['device']['free_before_load_bytes'], 9 * 1024**3)


if __name__ == '__main__':
    unittest.main()
