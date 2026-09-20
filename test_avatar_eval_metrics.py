import base64
import json
import unittest

from avatar_eval_metrics import MediaMeasurement, percentile, summarize


def metadata(kind='2d', sample_rate=24000):
    result = dict(type='avatar_meta', fps=25, sample_rate=sample_rate)
    if kind == '2d':
        result.update(kind='2d', width=16, height=16, codec='jpeg')
    else:
        result.update(vertex_count=3, faces=[[0, 1, 2]])
    return result


def media(pcm, index=0, start=0, sample_rate=24000, **changes):
    result = dict(type='media', frame_index=index, start_sample=start,
                  pts=start / sample_rate, audio=base64.b64encode(pcm).decode(),
                  image='not-retained')
    result.update(changes)
    return result


class MediaMeasurementTests(unittest.TestCase):
    pcm = b'\x01\x00\x02\x00\x03\x00'

    def started(self):
        result = MediaMeasurement(self.pcm)
        result.add(metadata(), 0.1)
        return result

    def completed(self):
        result = self.started()
        result.add(media(self.pcm), 0.2)
        result.add(dict(type='clip_end', total_samples=3), 0.3)
        return result

    def test_complete_2d_and_legacy_3d_with_short_tail(self):
        for kind in ('2d', '3d'):
            with self.subTest(kind=kind):
                result = MediaMeasurement(self.pcm)
                result.add(metadata(kind), 0.1)
                result.add(media(self.pcm[:4]), 0.2)
                result.add(media(self.pcm[4:], index=1, start=2), 0.5)
                result.add(dict(type='clip_end', total_samples=3), 0.6)
                metrics = result.finish(0.7)
                self.assertEqual(metrics['frames'], 2)
                self.assertEqual(metrics['received_samples'], 3)
                self.assertEqual(metrics['input_samples'], 3)
                self.assertEqual(metrics['audio_duration_s'], 3 / 24000)
                self.assertEqual(metrics['first_media_s'], 0.2)
                self.assertEqual(metrics['wall_s'], 0.7)
                self.assertEqual(metrics['wall_audio_ratio'], 0.7 / (3 / 24000))
                self.assertAlmostEqual(metrics['max_receive_gap_s'], 0.3)
                self.assertEqual(metrics['max_pts_error_s'], 0)
                for name in ('pcm_exact', 'prefix_exact', 'completed'):
                    self.assertIs(metrics[name], True)
                for name in ('playback_measured', 'lip_sync_measured'):
                    self.assertIs(metrics[name], False)
                json.dumps(metrics, allow_nan=False)

    def test_single_frame_has_no_receive_gap(self):
        self.assertIsNone(self.completed().finish(0.3)['max_receive_gap_s'])

    def test_metadata_does_not_require_visual_payloads(self):
        result = MediaMeasurement(self.pcm)
        result.add(dict(type='avatar_meta', sample_rate=24000, fps=25), 0)
        result.add(media(self.pcm), 0.1)
        result.add(dict(type='clip_end', total_samples=3), 0.2)
        self.assertTrue(result.finish(0.3)['completed'])

    def test_interrupt_measures_only_verified_prefix(self):
        result = self.started()
        result.add(media(self.pcm[:2]), 0.2)
        metrics = result.finish(0.3, interrupted=True)
        self.assertEqual(metrics['received_samples'], 1)
        self.assertIsNone(metrics['pcm_exact'])
        self.assertIs(metrics['prefix_exact'], True)
        self.assertIs(metrics['completed'], False)

    def test_custom_sample_rate_controls_pts_and_duration(self):
        result = MediaMeasurement(self.pcm, sample_rate=16000)
        result.add(metadata(sample_rate=16000), 0)
        result.add(media(self.pcm[:2], sample_rate=16000), 0)
        result.add(media(self.pcm[2:], index=1, start=1, sample_rate=16000), 0)
        result.add(dict(type='clip_end', total_samples=3), 0)
        self.assertEqual(result.finish(0)['audio_duration_s'], 3 / 16000)

    def test_rejects_invalid_input_pcm_and_rate(self):
        for pcm in (b'', b'a', 'ab', bytearray(b'ab'), None):
            with self.subTest(pcm=pcm), self.assertRaises(ValueError):
                MediaMeasurement(pcm)
        for rate in (0, -1, True, 24000.5, float('inf')):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                MediaMeasurement(self.pcm, sample_rate=rate)

    def test_metadata_must_precede_media_and_occur_once(self):
        with self.assertRaises(ValueError):
            MediaMeasurement(self.pcm).add(media(self.pcm), 0)
        with self.assertRaises(ValueError):
            self.started().add(metadata(), 0.2)
        with self.assertRaises(ValueError):
            self.started().add(dict(type='clip_end', total_samples=3), 0.2)

    def test_rejects_invalid_metadata(self):
        invalid = [dict(metadata(), fps=0), dict(metadata(), fps=float('nan')),
                   dict(metadata(), fps=True), dict(metadata(), sample_rate=16000),
                   dict(metadata(), sample_rate=True),
                   dict(type='avatar_meta', sample_rate=24000)]
        for event in invalid:
            with self.subTest(event=event), self.assertRaises(ValueError):
                MediaMeasurement(self.pcm).add(event, 0)

    def test_rejects_malformed_and_out_of_order_media(self):
        invalid = [media(self.pcm, index=1), media(self.pcm, index=False),
                   media(self.pcm, start=1), media(self.pcm, start=False),
                   media(self.pcm, pts=0.01), media(self.pcm, pts=float('nan')),
                   media(self.pcm, pts=True), media(self.pcm, audio='not base64!'),
                   media(self.pcm, audio=None), media(b''), media(b'a'),
                   media(b'\xff\xff'), media(self.pcm + b'\x00\x00')]
        for event in invalid:
            with self.subTest(event=event), self.assertRaises(ValueError):
                self.started().add(event, 0.2)

    def test_pts_tolerance_is_absolute(self):
        result = self.started()
        result.add(media(self.pcm, pts=0.0000005), 0.2)
        self.assertEqual(result.finish(0.3, interrupted=True)['max_pts_error_s'], 0.0000005)

    def test_rejects_incomplete_or_incorrect_completion(self):
        result = self.started()
        result.add(media(self.pcm[:2]), 0.2)
        with self.assertRaises(ValueError):
            result.add(dict(type='clip_end', total_samples=3), 0.3)
        with self.assertRaises(ValueError):
            result.finish(0.3)
        for total in (2, 4, True, 3.0):
            result = self.started()
            result.add(media(self.pcm), 0.2)
            with self.subTest(total=total), self.assertRaises(ValueError):
                result.add(dict(type='clip_end', total_samples=total), 0.3)

    def test_rejects_events_after_end_and_unknown_types(self):
        for event in (media(self.pcm), dict(type='clip_end', total_samples=3), metadata()):
            with self.subTest(event=event), self.assertRaises(ValueError):
                self.completed().add(event, 0.4)
        for event in ({'type': 'progress'}, {}, None, []):
            with self.subTest(event=event), self.assertRaises(ValueError):
                self.started().add(event, 0.2)

    def test_arrival_and_finish_times_must_be_finite_nonnegative_monotonic(self):
        for elapsed in (-1, float('nan'), float('inf'), True, '0.2', 0.05):
            with self.subTest(elapsed=elapsed), self.assertRaises(ValueError):
                self.started().add(media(self.pcm), elapsed)
        for elapsed in (-1, float('nan'), float('inf'), True, '0.2', 0.25):
            with self.subTest(elapsed=elapsed), self.assertRaises(ValueError):
                self.completed().finish(elapsed)

    def test_finish_without_media_is_never_a_success(self):
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted), self.assertRaises(ValueError):
                self.started().finish(0.2, interrupted=interrupted)


class SummaryTests(unittest.TestCase):
    def row(self, **changes):
        result = dict(provider='flashhead', case_id='case-a', input_sha256='a' * 64,
                      mode='burst', phase='measured', status='passed',
                      metrics=dict(first_media_s=1.0, wall_s=2.0, wall_audio_ratio=0.5))
        result.update(changes)
        return result

    def test_percentile_interpolates_and_handles_empty_and_single_sample(self):
        self.assertIsNone(percentile([], 50))
        self.assertEqual(percentile([7], 95), 7)
        self.assertEqual(percentile([10, 0], 50), 5)
        self.assertEqual(percentile([10, 0], 95), 9.5)
        self.assertEqual(percentile([10, 0], 0), 0)
        self.assertEqual(percentile([10, 0], 100), 10)

    def test_percentile_rejects_invalid_values(self):
        for values in ([float('nan')], [float('inf')], ['1'], [True]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                percentile(values, 50)
        for p in (-1, 101, float('nan'), float('inf'), True, '50'):
            with self.subTest(p=p), self.assertRaises(ValueError):
                percentile([], p)

    def test_summary_counts_failures_without_including_failure_latency(self):
        rows = [self.row(), self.row(status='failed', metrics={'wall_s': 999}),
                self.row(metrics=dict(first_media_s=3, wall_s=6, wall_audio_ratio=1.5))]
        result = summarize(rows)[0]
        self.assertEqual((result['attempted'], result['passed'], result['failed']), (3, 2, 1))
        for name, p50, p95 in (('first_media_s', 2.0, 2.9), ('wall_s', 4.0, 5.8),
                               ('wall_audio_ratio', 1.0, 1.45)):
            with self.subTest(metric=name):
                self.assertEqual(result['metrics'][name]['n'], 2)
                self.assertAlmostEqual(result['metrics'][name]['p50'], p50)
                self.assertAlmostEqual(result['metrics'][name]['p95'], p95)

    def test_failed_only_group_and_single_sample_group(self):
        result = summarize([self.row(status='failed', metrics=None)])[0]
        self.assertEqual(result['metrics']['first_media_s'], dict(n=0, p50=None, p95=None))
        self.assertEqual(summarize([self.row()])[0]['metrics']['wall_s'], dict(n=1, p50=2.0, p95=2.0))

    def test_nonmeasured_phases_excluded_and_groups_separated_sorted(self):
        rows = [self.row(provider='z'), self.row(mode='realtime'),
                self.row(input_sha256='b' * 64), self.row(case_id='case-b'), self.row()]
        rows += [self.row(phase=phase, metrics=None) for phase in ('warmup', 'interrupt', 'recovery')]
        result = summarize(rows)
        keys = [(r['provider'], r['case_id'], r['input_sha256'], r['mode']) for r in result]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(len(result), 5)
        self.assertTrue(all(r['attempted'] == 1 for r in result))
        self.assertEqual(summarize([]), [])

    def test_malformed_measured_rows_are_rejected(self):
        invalid = [self.row(provider=''), self.row(case_id=None), self.row(input_sha256=''),
                   self.row(mode='other'), self.row(status='maybe'), self.row(metrics=None),
                   self.row(metrics={})]
        for name in ('first_media_s', 'wall_s', 'wall_audio_ratio'):
            for value in (None, -1, float('nan'), float('inf'), True, '1'):
                metrics = self.row()['metrics']
                metrics[name] = value
                invalid.append(self.row(metrics=metrics))
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(ValueError):
                summarize([row])


if __name__ == '__main__':
    unittest.main()
