import importlib.util
import json
import queue
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

path = Path(__file__).with_name('2026-10-16-run-model-decoding.py')
spec = importlib.util.spec_from_file_location('capture', path)
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


class CaptureTests(unittest.TestCase):
    def test_matrix_is_complete_and_alternates_order(self):
        data = json.loads(path.with_name('2026-10-16-model-decoding-input.json').read_text())
        rows = capture.request_matrix(data)
        self.assertEqual(len(rows), 40)
        self.assertEqual(len({(r['case']['id'], r['condition']['id'], r['seed']) for r in rows}), 40)
        self.assertEqual(rows[0]['condition']['id'], 'greedy-256')
        self.assertEqual(rows[5]['condition']['id'], 'sample-256')
        self.assertEqual([r['seed'] for r in rows[5:8]], [29, 43, 17])

    def test_eos_at_budget_is_not_mislabeled_as_length(self):
        self.assertEqual(capture.finish_reason([3, 9], [9], 2, None), 'eos')
        self.assertEqual(capture.finish_reason([3, 4], [9], 2, None), 'length')
        self.assertEqual(capture.finish_reason([3], [9], 2, None), 'unexpected_stop')
        self.assertEqual(capture.finish_reason([9], [9], 2, 'application_active'), 'application_active')

    def test_existing_result_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'result.json'
            output.write_text('original')
            with self.assertRaises(FileExistsError):
                capture.Artifact(output, {'status': 'running'})
            self.assertEqual(output.read_text(), 'original')

    def test_partial_failure_remains_valid_and_saved(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'result.json'
            state = {'status': 'running', 'rows': [{'status': 'running', 'chunks': ['部分']} ]}
            artifact = capture.Artifact(output, state)
            state['status'] = 'failed'
            state['rows'][0].update(status='failed', error_type='TimeoutError')
            artifact.save()
            self.assertEqual(json.loads(output.read_text()), state)

    def test_preregistered_matrix_marks_unattempted_rows(self):
        data = json.loads(path.with_name('2026-10-16-model-decoding-input.json').read_text())
        rows = capture.pending_rows(data)
        self.assertEqual(len(rows), 40)
        self.assertTrue(all(row['status'] == 'pending' for row in rows))
        self.assertTrue(all(row['token_ids'] == [] for row in rows))

    def test_stream_tokens_and_elapsed_survive_producer_error_or_cancel(self):
        class Values:
            def __init__(self, values): self.values = values
            def tolist(self): return self.values
        class Delegate:
            def put(self, values): pass
            def end(self): pass
        for reason in ['model_error', 'cancelled']:
            row = {'chunks': [{'text': '片段', 'elapsed_s': .5}], 'text': '片段'}
            tokens = capture.TokenRecorder(Delegate())
            tokens.put(Values([[101, 102]]))  # prompt must be excluded
            tokens.put(Values([7])); tokens.put(Values([8]))
            try:
                raise RuntimeError('injected after two tokens')
            except RuntimeError:
                capture.finalize_capture(row, tokens.ids, [9], 20, reason, 1.25)
            self.assertEqual(row['token_ids'], [7, 8])
            self.assertEqual(row['generated_tokens'], 2)
            self.assertEqual(row['finish_reason'], reason)
            self.assertEqual(row['elapsed_s'], 1.25)
            self.assertEqual(row['first_text_s'], .5)

    def test_actual_runner_preserves_partial_model_error_and_pending_rows(self):
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
            def apply_chat_template(self, *args, **kwargs): return 'fake prompt'
            def __call__(self, *args, **kwargs): return Inputs(input_ids=Tensor([[101, 102]]))
            def decode(self, ids, **kwargs): return ''.join(str(i) for i in ids if i != 9)
        class Streamer:
            def __init__(self, *args, **kwargs): self.queue, self.first = queue.Queue(), True
            def put(self, tensor):
                if self.first: self.first = False
                else: self.queue.put(str(tensor.tolist()[0]))
            def end(self): self.queue.put(None)
            def __iter__(self): return self
            def __next__(self):
                value = self.queue.get(timeout=1)
                if value is None: raise StopIteration
                return value
        class Model:
            device = 'cpu'
            generation_config = SimpleNamespace(eos_token_id=[9], to_dict=lambda: {'eos_token_id': [9]})
            calls = 0
            def eval(self): return self
            def generate(self, input_ids, streamer, **kwargs):
                self.calls += 1
                streamer.put(input_ids)
                for token in ([7, 9] if self.calls == 1 else [7, 8]):
                    streamer.put(Tensor([token]))
                if self.calls > 1: raise ValueError('injected producer error')
                streamer.end()
                return Tensor([[101, 102, 7, 9]])
        from contextlib import nullcontext
        model = Model()
        cuda = SimpleNamespace(mem_get_info=lambda: (16 * 1024**3, 24 * 1024**3),
            get_device_name=lambda: 'fake CPU harness', synchronize=lambda: None,
            reset_peak_memory_stats=lambda: None, max_memory_allocated=lambda: 12,
            max_memory_reserved=lambda: 16)
        torch = SimpleNamespace(cuda=cuda, set_num_threads=lambda n: None,
                                bfloat16='fake', inference_mode=nullcontext)
        loader = SimpleNamespace(from_pretrained=lambda *args, **kwargs: model)
        transformers = SimpleNamespace(AutoConfig=SimpleNamespace(from_pretrained=lambda *a, **kw: SimpleNamespace(model_type='qwen3')),
            AutoModelForCausalLM=loader, AutoTokenizer=SimpleNamespace(from_pretrained=lambda *a, **kw: Tokenizer()),
            BitsAndBytesConfig=lambda **kw: kw, TextIteratorStreamer=Streamer,
            StoppingCriteria=object, StoppingCriteriaList=list, set_seed=lambda s: None)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); model_dir = root / 'Qwen3-4B'; model_dir.mkdir()
            (model_dir / 'config.json').write_text('{}')
            output = root / 'result.json'
            with patch.dict('sys.modules', {'torch': torch, 'transformers': transformers}), \
                    patch.object(capture, 'require_idle'), \
                    patch.object(capture, 'process_memory_bytes', return_value=20), \
                    patch.object(capture.importlib.metadata, 'version', return_value='fake'):
                with self.assertRaisesRegex(RuntimeError, 'Model generation failed'):
                    capture.run(SimpleNamespace(model=model_dir, output=output))
            result = json.loads(output.read_text())
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['warmup']['finish_reason'], 'eos')
            failed = result['rows'][0]
            self.assertEqual(failed['token_ids'], [7, 8])
            self.assertEqual(failed['finish_reason'], 'model_error')
            self.assertEqual(failed['model_error_type'], 'ValueError')
            self.assertEqual(failed['text'], '78')
            self.assertIsNotNone(failed['elapsed_s'])
            self.assertEqual(failed['sampled_process_peak_bytes'], 20)
            self.assertEqual(sum(row['status'] == 'pending' for row in result['rows']), 39)


if __name__ == '__main__':
    unittest.main()
