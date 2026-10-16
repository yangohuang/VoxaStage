"""Protocol evidence checks independent of model inference or device playback."""
import base64
import struct
import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location('session_evidence', Path(__file__).with_name('session_client.py'))


def media(generation=1, clip='1', start=0, pcm=b'\0\0'*800):
    return dict(type='media', generation=generation, clip_id=clip, start_sample=start,
                audio=base64.b64encode(pcm).decode(), frame_index=start//800, pts=start/24000,
                vertices=base64.b64encode(struct.pack('<9f',0,0,0,1,0,0,0,1,0)).decode())


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.module=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(self.module)
        self.e=self.module.Evidence({'generation':0}, now=0.)
        self.e.accept({'type':'reset','generation':1,'reason':'text'},now=.1)
        self.e.accept({'type':'avatar_meta','generation':1,'clip_id':'1','vertex_count':3,'fps':30,
                       'sample_rate':24000,'faces':[[0,1,2]]},now=.2)

    def test_multiple_clips_require_own_offsets_and_all_clips_to_end(self):
        event=self.e.accept(media(),now=.3)
        self.assertNotIn('audio',event);self.assertNotIn('vertices',event)
        self.assertEqual(event['audio_samples'],800)
        self.e.accept({'type':'clip_end','generation':1,'clip_id':'1','total_samples':800},now=.4)
        self.assertTrue(self.e.media_complete(1))
        self.e.accept({'type':'avatar_meta','generation':1,'clip_id':'2','vertex_count':3,'fps':30,
                       'sample_rate':24000,'faces':[[0,1,2]]},now=.5)
        self.e.accept(media(clip='2'),now=.6)
        self.assertFalse(self.e.media_complete(1))
        self.e.accept({'type':'clip_end','generation':1,'clip_id':'2','total_samples':800},now=.7)
        self.assertTrue(self.e.media_complete(1))

    def test_reset_rejects_late_old_generation_media(self):
        self.e.accept(media(),now=.3)
        self.e.accept({'type':'reset','generation':2,'reason':'interrupted'},now=.4)
        with self.assertRaisesRegex(ValueError,'generation'):
            self.e.accept(media(start=800),now=.5)

    def test_clip_end_must_equal_pcm_samples(self):
        self.e.accept(media(),now=.3)
        with self.assertRaisesRegex(ValueError,'sample'):
            self.e.accept({'type':'clip_end','generation':1,'clip_id':'1','total_samples':801},now=.4)

    def test_duplicate_offset_truncated_pcm_and_nonfinite_mesh_rejected(self):
        self.e.accept(media(),now=.3)
        for event in [media(),media(start=800,pcm=b'x'),dict(media(start=800),vertices=base64.b64encode(struct.pack('<9f',*([float('nan'),0,0]*3))).decode())]:
            with self.assertRaises(ValueError):self.e.accept(event,now=.4)

    def test_report_survives_error_and_never_marks_partial_clip_complete(self):
        self.e.accept(media(),now=.3)
        with self.assertRaisesRegex(RuntimeError,'server_error'):
            self.e.accept({'type':'error','message':'backend failed'},now=.4)
        self.assertFalse(self.e.media_complete(1))
        self.assertEqual(self.e.snapshot()['clips']['1:1']['samples'],800)

if __name__=='__main__':unittest.main()
