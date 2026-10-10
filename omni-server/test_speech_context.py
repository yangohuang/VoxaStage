import unittest
from contextlib import nullcontext
from types import SimpleNamespace

from worker import MiniCPMEngine, SYSTEM


class SpeechContextTests(unittest.TestCase):
    def prefill(self, context):
        calls = []
        def generate(**kwargs):
            yield None, '回答'
        engine = MiniCPMEngine.__new__(MiniCPMEngine)
        engine.torch = SimpleNamespace(inference_mode=nullcontext)
        engine.model = SimpleNamespace(reset_session=lambda **kw: None,
            init_streaming_processor=lambda: None,
            streaming_prefill=lambda **kw: calls.append(kw['msgs'][0]),
            streaming_generate=generate)
        engine.references = {'male': object()}
        engine.voice = 'male'
        engine.speech_context = context
        engine.visual_grounding_policy = 'baseline'
        list(engine.generate([{'role': 'user', 'content': ['你好']}],
                             SimpleNamespace(is_set=lambda: False)))
        return calls[0]['content'], engine.references['male']

    def test_configured_terms_reach_model_without_replacing_voice(self):
        content, reference = self.prefill('VAD, ASR, WebRTC')
        self.assertIn('VAD, ASR, WebRTC', content[2])
        self.assertTrue(content[2].startswith(SYSTEM))
        self.assertIs(content[1], reference)

    def test_empty_context_preserves_prompt(self):
        content, _ = self.prefill('')
        self.assertEqual(content[2], SYSTEM)

    def test_invalid_context_rejected_before_model_loading(self):
        for context in (None, 123, 'x' * 1025):
            with self.subTest(context_type=type(context).__name__):
                with self.assertRaises(ValueError):
                    MiniCPMEngine('/missing/model', speech_context=context)
