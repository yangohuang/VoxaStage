import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from evaluation.asr_scoring import score


def row(case='a', reference='你好', text='你好', variant='base', reviewed=False, status='ok'):
    return dict(case_id=case, variant=variant, reference=reference,
                reference_kind='human_transcript' if reviewed else 'synthesis_prompt',
                transcript_reviewed=reviewed, status=status, text=text)


class AsrScoringTests(unittest.TestCase):
    def test_normalization_and_mixed_language(self):
        result = score([row(reference='你好，Ａｇｅｎｔ MiniCPM-o！', text='你好 agent minicpm O')])
        group = result['variants']['base']['reference_groups']['synthesis_prompt']
        self.assertEqual(group['edit_count'], 0)
        self.assertEqual(group['metric'], 'prompt_character_difference_rate')

    def test_micro_average_and_separate_human_reference(self):
        result = score([row(reference='a', text='b'), row('b', 'abcdefghi', 'abcdefghi'),
                        row('c', '中文', '中', reviewed=True)])
        groups = result['variants']['base']['reference_groups']
        self.assertEqual(groups['synthesis_prompt']['rate'], .1)
        self.assertEqual(groups['human_transcript']['rate'], .5)
        self.assertEqual(groups['human_transcript']['metric'], 'normalized_cer')

    def test_empty_recognition_is_deletion_not_failure(self):
        group = score([row(text='')])['variants']['base']['reference_groups']['synthesis_prompt']
        self.assertEqual(group['edit_count'], 2)
        self.assertEqual(group['rate'], 1)

    def test_silence_insertions_have_no_cer_denominator(self):
        rows = [row(reference='', text='幻觉'), row('b', '', '')]
        group = score(rows)['variants']['base']['reference_groups']['synthesis_prompt']
        self.assertIsNone(group['rate'])
        self.assertEqual(group['empty_reference_cases'], 2)
        self.assertEqual(group['empty_reference_nonempty_outputs'], 1)
        self.assertEqual(group['empty_reference_inserted_characters'], 2)

    def test_failure_retained_and_incomplete_score_identified(self):
        rows = [row(), row('b', status='error', text=None)]
        variant = score(rows)['variants']['base']
        self.assertEqual((variant['attempted'], variant['succeeded'], variant['failed']), (2, 1, 1))
        self.assertFalse(variant['complete'])
        group = variant['reference_groups']['synthesis_prompt']
        self.assertEqual(group['scored_cases'], 1)
        self.assertEqual(group['attempted_reference_characters'], 4)
        self.assertEqual(group['reference_character_coverage'], .5)
        self.assertEqual(group['rate_scope'], 'successful_nonempty_references_only')

    def test_all_failed_does_not_report_zero_error(self):
        group = score([row(status='error', text=None)])['variants']['base']['reference_groups']['synthesis_prompt']
        self.assertIsNone(group['rate'])
        self.assertEqual(group['reference_character_coverage'], 0)
        self.assertFalse(group['complete'])

    def test_duplicate_missing_and_mismatched_references_rejected(self):
        for rows in ([row(), row()],
                     [row(), row('b'), row(variant='other')],
                     [row(), row(reference='不同', variant='other')],
                     [row(), row(variant='other', reviewed=True)]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                score(rows)

    def test_invalid_records_and_false_review_claim_rejected(self):
        for field, value in [('transcript_reviewed', 'false'), ('reference_kind', 'unknown'),
                             ('reference_kind', 'human_transcript'), ('status', 'timeout'),
                             ('text', None), ('reference', None), ('variant', ''),
                             ('case_id', []), ('reference', 'x' * 4001)]:
            record = row()
            record[field] = value
            with self.subTest(field=field, value=str(value)[:40]), self.assertRaises(ValueError):
                score([record])
        with self.assertRaises(ValueError):
            score([])

    def test_order_does_not_change_aggregates(self):
        records = [row('b', '中英', '中文'), row()]
        before = copy.deepcopy(records)
        self.assertEqual(score(records), score(list(reversed(records))))
        self.assertEqual(records, before)

    def test_cli_reproduces_result_and_refuses_input_overwrite(self):
        script = Path(__file__).with_name('asr_scoring.py')
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'input.json'
            output = Path(directory) / 'output.json'
            records = [row()]
            source.write_text(json.dumps(records))
            result = subprocess.run([sys.executable, str(script), str(source), '--output', str(output)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(output.read_text()), score(records))
            raw = source.read_bytes()
            refused = subprocess.run([sys.executable, str(script), str(source), '--output', str(source)], capture_output=True)
            self.assertNotEqual(refused.returncode, 0)
            self.assertEqual(source.read_bytes(), raw)


if __name__ == '__main__':
    unittest.main()
