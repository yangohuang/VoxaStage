import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from pacing import load_policy, paced_delay, rewrite_consumer

SOURCE = '''async def consumer(self):
    start_time = self.now
    audio_num = 0
    for _ in range(10):
        self.now += 0.01
        audio_num += 1
        self.consumed.append(self.now)
        delay = start_time+audio_num*0.2-time.perf_counter()
        if delay > 0 and audio_num >= 5:
            await self.sleep(delay)
'''


class PacingTests(unittest.TestCase):
    def test_zero_preserves_original_and_lookahead_subtracts(self):
        self.assertAlmostEqual(paced_delay(10, 5, 10.05, 0), .95)
        self.assertAlmostEqual(paced_delay(10, 5, 10.05, .4), .55)
        self.assertLess(paced_delay(10, 5, 11.1, .4), 0)

    def test_startup_burst_then_bounded_ahead_cadence(self):
        from types import SimpleNamespace
        async def run(lead):
            obj = SimpleNamespace(now=0., consumed=[], _voxastage_lookahead=lead)
            async def sleep(delay): obj.now += delay
            obj.sleep = sleep
            fn = rewrite_consumer(SOURCE, dict(time=SimpleNamespace(perf_counter=lambda: obj.now)))
            await fn(obj)
            return obj.consumed
        original = asyncio.run(run(0))
        improved = asyncio.run(run(.4))
        self.assertEqual(original[:5], improved[:5])
        for a, b in zip(original[5:], improved[5:]): self.assertAlmostEqual(a-b, .4)
        for a, b in zip(improved[5:], improved[6:]): self.assertAlmostEqual(b-a, .2)

    def test_unexpected_native_expression_rejected(self):
        for source in (SOURCE.replace('audio_num*0.2', 'audio_num*0.3'), SOURCE + '\n# start_time+audio_num*0.2-time.perf_counter()'):
            with self.assertRaises(ValueError): rewrite_consumer(source, {})

    def test_policy_is_explicit_bounded_and_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'settings.json'
            path.write_text('{"version":1,"lookahead_seconds":0.4}')
            old = load_policy(path)
            path.write_text('{"version":1,"lookahead_seconds":0}')
            new = load_policy(path)
            self.assertEqual(old.lookahead_seconds, .4)
            self.assertEqual(new.lookahead_seconds, 0)
            self.assertNotEqual(old.sha256, new.sha256)
            for value in (-.01, .81, float('nan'), float('inf'), True, '0.4', None):
                path.write_text(json.dumps(dict(version=1, lookahead_seconds=value)))
                with self.subTest(value=value), self.assertRaises(ValueError): load_policy(path)
            for text in ('{}', '{"version":2,"lookahead_seconds":0}', '{"version":true,"lookahead_seconds":0}',
                         '{"version":1,"lookahead_seconds":0,"other":1}', '{', ' '*1025):
                path.write_text(text)
                with self.subTest(text=text[:30]), self.assertRaises(ValueError): load_policy(path)
            path.unlink()
            with self.assertRaises(FileNotFoundError): load_policy(path)


if __name__ == '__main__': unittest.main()
