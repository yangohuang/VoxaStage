import asyncio
import json
from pathlib import Path
import tempfile
import unittest
import wave

from prepare_avatar_cases import prepare
from benchmark_avatar import load_cases


class PreparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_five_categories_and_silence_are_distinct_from_speech(self):
        with tempfile.TemporaryDirectory() as tmp:
            async def synth(text):
                yield b'\x20\x00' * 4800
            out=Path(tmp)/'cases'
            await prepare(out,synth,voice_label='synthetic-test')
            cases=load_cases(out/'cases.json')
            self.assertEqual(len(cases),15)
            for category in ('zh','en','mixed','silence','pause'):
                self.assertEqual(sum(c.category==category for c in cases),3)
            self.assertTrue(all(not c.transcript_reviewed for c in cases))
            for c in cases:
                if c.category=='silence': self.assertFalse(any(c.pcm))
                elif c.category=='pause':
                    self.assertIn(bytes(9600),c.pcm)
                    self.assertTrue(any(c.pcm))
            report=json.loads((out/'provenance.json').read_text())
            self.assertEqual(report['status'],'passed')
            self.assertEqual(report['synthesized_cases'],9)

    async def test_failure_never_publishes_complete_manifest_and_existing_dir_is_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'cases'
            async def broken(text):
                yield b'\x01'
            with self.assertRaises(ValueError): await prepare(out,broken,voice_label='test')
            self.assertFalse((out/'cases.json').exists())
            self.assertEqual(json.loads((out/'provenance.json').read_text())['status'],'failed')
            with self.assertRaises(FileExistsError): await prepare(out,broken,voice_label='test')

if __name__=='__main__': unittest.main()
