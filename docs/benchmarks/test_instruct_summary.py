import copy
import importlib.util
import json
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('summary', HERE / '2026-10-16-summarize-instruct.py')
summary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(summary)
MANIFEST = json.loads((HERE / '2026-10-16-instruct-input.json').read_text())


def review():
    return [{'case': c['id'], 'condition': 'sample-direct', 'seed': seed,
             'group': c['group'], 'checks': [True, True, True],
             'extra_factual_errors': [], 'notes': []}
            for c in MANIFEST['cases'] for seed in (17, 43)]


class GateTests(unittest.TestCase):
    def test_threshold_gain_and_critical_requirements_all_needed(self):
        baseline, candidate = review(), review()
        # Four noncritical failures give candidate exactly20/24 and gain4.
        for rows, cases in [(candidate, ['weights-bytes', 'current-facts']),
                            (baseline, ['weights-bytes', 'current-facts', 'heard-context', 'actual-input-debug'])]:
            for row in rows:
                if row['case'] in cases:
                    row['checks'][0] = False
        result = summary.advance_gate(baseline, candidate, MANIFEST)
        self.assertTrue(result['advance_to_agent_regression'])
        self.assertEqual(result['baseline_new_pass'], 16)
        self.assertEqual(result['candidate_new_pass'], 20)
        self.assertFalse(result['production_deployment_approved'])

    def test_high_score_without_gain_is_not_enough(self):
        result = summary.advance_gate(review(), review(), MANIFEST)
        self.assertFalse(result['advance_to_agent_regression'])
        self.assertFalse(result['checks']['minimum_gain'])

    def test_one_critical_seed_failure_blocks_advancement(self):
        baseline, candidate = review(), review()
        for row in baseline: row['checks'][0] = False
        next(r for r in candidate if r['case'] == 'cancelled-lookup')['checks'][0] = False
        result = summary.advance_gate(baseline, candidate, MANIFEST)
        self.assertEqual(result['candidate_new_pass'], 23)
        self.assertFalse(result['checks']['critical_cases'])
        self.assertFalse(result['advance_to_agent_regression'])

    def test_extra_error_blocks_even_if_rubric_passes(self):
        baseline, candidate = review(), review()
        for row in baseline: row['checks'][0] = False
        next(r for r in candidate if r['case'] == 'document-version')['extra_factual_errors'] = ['Invented fact']
        result = summary.advance_gate(baseline, candidate, MANIFEST)
        self.assertFalse(result['checks']['critical_extra_errors'])
        self.assertFalse(result['advance_to_agent_regression'])

    def test_anchor_failure_blocks_advancement(self):
        baseline, candidate = review(), review()
        for row in baseline: row['checks'][0] = False
        next(r for r in candidate if r['case'] == 'anchor-nf4-size')['checks'][0] = False
        result = summary.advance_gate(baseline, candidate, MANIFEST)
        self.assertFalse(result['checks']['historical_anchors'])

    def test_missing_duplicate_and_unreviewed_fields_are_rejected(self):
        original = review()
        for mutation in ['missing', 'duplicate', 'missing_errors', 'not_boolean']:
            bad = copy.deepcopy(original)
            if mutation == 'missing': bad.pop()
            elif mutation == 'duplicate': bad[-1] = copy.deepcopy(bad[0])
            elif mutation == 'missing_errors': del bad[0]['extra_factual_errors']
            else: bad[0]['checks'][0] = 'true'
            with self.subTest(mutation=mutation), self.assertRaises((AssertionError, KeyError)):
                summary.advance_gate(original, bad, MANIFEST)


if __name__ == '__main__':
    unittest.main()
