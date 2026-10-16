"""Recompute paired checkpoint evidence and the preregistered advancement gate.

Run without -O. Requires two completed raw artifacts, two independent semantic
reviews, and the execution provenance file next to this script. No GPU needed.
This audits records; semantic correctness still depends on the disclosed review.
"""
from collections import Counter
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import statistics

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / '2026-10-16-instruct-input.json'
COLLECTOR = HERE / '2026-10-16-run-instruct-check.py'
SOURCES = HERE / '2026-10-16-instruct-model-sources.json'
spec = importlib.util.spec_from_file_location('instruct_capture', COLLECTOR)
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stats(values):
    values = sorted(v for v in values if v is not None)
    return {'n': len(values), 'median': statistics.median(values) if values else None,
            'p95_nearest_rank': values[math.ceil(.95 * len(values)) - 1] if values else None,
            'max': values[-1] if values else None}


def review_index(rows, manifest):
    cases = {case['id']: case for case in manifest['cases']}
    expected = {(item['case']['id'], item['condition']['id'], item['seed'])
                for item in capture.request_matrix(manifest)}
    actual = {}
    for row in rows:
        key = row['case'], row['condition'], row['seed']
        assert key in expected and key not in actual
        case = cases[row['case']]
        assert row['group'] == case['group']
        assert len(row['checks']) == len(case['rubric'])
        assert all(type(check) is bool for check in row['checks'])
        for field in ('notes', 'extra_factual_errors'):
            assert isinstance(row[field], list) and all(isinstance(v, str) for v in row[field])
        actual[key] = row
    assert set(actual) == expected
    return actual


def advance_gate(baseline, candidate, manifest):
    first, second = review_index(baseline, manifest), review_index(candidate, manifest)
    gate = manifest['advance_gate']
    def count(index):
        return sum(all(r['checks']) for r in index.values() if r['group'] == 'new_diagnostic')
    baseline_count, candidate_count = count(first), count(second)
    assert sum(r['group'] == 'new_diagnostic' for r in second.values()) == gate['new_case_answer_total']
    critical = [r for r in second.values() if r['case'] in gate['critical_new_cases_both_seeds_pass']]
    anchors = [r for r in second.values() if r['case'] in gate['anchors_both_seeds_pass']]
    assert len(critical) == 2 * len(gate['critical_new_cases_both_seeds_pass'])
    assert len(anchors) == 2 * len(gate['anchors_both_seeds_pass'])
    checks = {
        'minimum_new_pass': candidate_count >= gate['new_case_full_rubric_pass_minimum'],
        'minimum_gain': candidate_count - baseline_count >= gate['minimum_answer_gain_over_baseline'],
        'critical_cases': all(all(r['checks']) for r in critical),
        'critical_extra_errors': sum(len(r['extra_factual_errors']) for r in critical) <= gate['critical_new_case_extra_factual_errors'],
        'historical_anchors': all(all(r['checks']) for r in anchors)}
    return {'baseline_new_pass': baseline_count, 'candidate_new_pass': candidate_count,
            'new_answer_denominator': gate['new_case_answer_total'], 'checks': checks,
            'advance_to_agent_regression': all(checks.values()),
            'production_deployment_approved': False}


def validate(data, model, manifest):
    precision = manifest['execution_precision']
    assert data['model'] == model
    assert data['status'] == 'complete' and data['precision'] == precision
    assert data['manifest_sha256'] == digest(MANIFEST)
    assert data['collector_sha256'] == digest(COLLECTOR)
    profile = manifest['precision_profiles'][precision]
    assert data['quantization'] == profile['quantization']
    assert data['minimum_free_bytes'] == profile['minimum_free_gib'] * 2**30
    assert data['device']['free_before_load_bytes'] >= data['minimum_free_bytes']
    matrix = capture.request_matrix(manifest)
    assert len(matrix) == len(data['rows']) == 28
    for item, row in zip(matrix, data['rows']):
        assert (row['case'], row['condition'], row['seed']) == (
            item['case']['id'], item['condition']['id'], item['seed'])
        messages, thinking, kwargs = capture.request_configuration(
            manifest, item['case'], item['condition'])
        assert row['messages'] == messages and row['enable_thinking'] == thinking
        assert row['generation_kwargs'] == kwargs
    for row in [data['warmup']] + data['rows']:
        assert row['status'] == 'complete'
        assert row['generated_tokens'] == len(row['token_ids'])
        assert row['text'] == ''.join(chunk['text'] for chunk in row['chunks'])
        assert row['text'] == row['decoded_text']
        assert all(row[k] == v for k, v in capture.final_answer_view(
            row['chunks'], row['enable_thinking']).items())
        assert row['finish_reason'] in ('eos', 'length')
        assert row['finish_reason'] == capture.finish_reason(
            row['token_ids'], row['eos_token_ids'],
            row['generation_kwargs']['max_new_tokens'], None)
        times = [chunk['elapsed_s'] for chunk in row['chunks']]
        assert times == sorted(times) and all(0 <= t <= row['elapsed_s'] for t in times)
        assert row['first_text_s'] == (times[0] if times else None)
        assert row['process_memory_sample_errors'] == 0


def summarize():
    manifest = json.loads(MANIFEST.read_text())
    sources = json.loads(SOURCES.read_text())['models']
    execution_path = HERE / '2026-10-16-instruct-execution.json'
    execution = json.loads(execution_path.read_text())
    assert execution['status'] == 'complete'
    assert execution['manifest_sha256'] == digest(MANIFEST)
    assert execution['collector_sha256'] == digest(COLLECTOR)
    assert [r['model'] for r in execution['runs']] == manifest['models']
    assert all(r['exit_code'] == 0 for r in execution['runs'])
    batches, reviews = {}, {}
    result = {'scope': '28 requests per model, one excluded warmup each; same-host BF16 text diagnostic, not physical voice latency or deployment acceptance',
              'manifest_sha256': digest(MANIFEST), 'collector_sha256': digest(COLLECTOR),
              'execution_sha256': digest(execution_path), 'models': {}}
    for label, model in zip(('baseline', 'candidate'), manifest['models']):
        path = HERE / f'2026-10-16-instruct-{label}.json'
        data = json.loads(path.read_text())
        validate(data, model, manifest)
        provenance = execution['models'][model]
        assert provenance['revision'] == sources[model]['revision'] == manifest['model_revisions'][model]
        assert set(provenance['files']) == set(sources[model]['files'])
        for name, expected in sources[model]['files'].items():
            observed = provenance['files'][name]
            assert observed['bytes'] == expected['bytes'] and observed['official_match'] is True
            if 'sha256' in expected:
                assert observed['sha256'] == expected['sha256']
        expected_capture = {name for name in provenance['files']
                            if Path(name).suffix in {'.json', '.jinja', '.safetensors'}}
        assert set(data['model_files']) == expected_capture
        for name, observed in data['model_files'].items():
            assert observed == {k: provenance['files'][name][k] for k in ('bytes', 'sha256')}
        batches[label] = data
        review_path = HERE / f'2026-10-16-instruct-{label}-review.json'
        review = json.loads(review_path.read_text())
        assert review['raw_sha256'] == digest(path) and review['manifest_sha256'] == digest(MANIFEST)
        assert review['model'] == model
        index = review_index(review['rows'], manifest)
        for row in data['rows']:
            if not row['final_text']:
                assert not any(index[(row['case'], row['condition'], row['seed'])]['checks'])
        reviews[label] = review['rows']
        rows = data['rows']
        result['models'][model] = {
            'raw_sha256': digest(path), 'review_sha256': digest(review_path),
            'requests': len(rows), 'finish_reasons': dict(Counter(r['finish_reason'] for r in rows)),
            'load_s': data['load_s'],
            'all_rubric_pass': sum(all(r['checks']) for r in review['rows']),
            'additional_error_answers': sum(bool(r['extra_factual_errors']) for r in review['rows']),
            **{key: stats([r[key] for r in rows]) for key in (
                'first_final_text_s', 'elapsed_s', 'generated_tokens',
                'torch_peak_allocated_bytes', 'torch_peak_reserved_bytes', 'sampled_process_peak_bytes')}}
    a, b = batches['baseline'], batches['candidate']
    for key in ('environment', 'precision', 'quantization'):
        assert a[key] == b[key], key
    for key in ('name', 'total_bytes'):
        assert a['device'][key] == b['device'][key], key
    for x, y in zip(a['rows'], b['rows']):
        for key in ('case', 'condition', 'seed', 'messages', 'generation_kwargs', 'enable_thinking'):
            assert x[key] == y[key], (x['case'], key)
    result['paired_messages_verified'] = 28
    result['gate'] = advance_gate(reviews['baseline'], reviews['candidate'], manifest)
    return result


if __name__ == '__main__':
    print(json.dumps(summarize(), ensure_ascii=False, indent=2))
