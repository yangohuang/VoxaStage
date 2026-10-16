"""Audit the complete paired precision artifacts; print descriptive metrics.

Run with Python (without -O). No GPU or model download is required.
Semantic rubric reviews are separate from this structural audit.
"""
from collections import Counter
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import statistics

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / '2026-10-16-precision-input.json'
COLLECTOR = HERE / '2026-10-16-run-precision-check.py'
spec = importlib.util.spec_from_file_location('precision_capture', COLLECTOR)
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stats(values):
    values = sorted(values)
    return {'n': len(values), 'median': statistics.median(values),
            'p95_nearest_rank': values[math.ceil(.95 * len(values)) - 1],
            'max': values[-1]}


def validate(data, precision, manifest):
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
    batches = {}
    result = {'scope': '28 requests per precision; one excluded warmup each; descriptive same-host metrics, not acoustic latency',
              'manifest_sha256': digest(MANIFEST), 'collector_sha256': digest(COLLECTOR),
              'precisions': {}}
    for precision in ('nf4', 'bf16'):
        path = HERE / f'2026-10-16-precision-{precision}.json'
        data = json.loads(path.read_text())
        validate(data, precision, manifest)
        batches[precision] = data
        rows = data['rows']
        result['precisions'][precision] = {
            'raw_sha256': digest(path), 'requests': len(rows),
            'finish_reasons': dict(Counter(r['finish_reason'] for r in rows)),
            'load_s': data['load_s'],
            **{key: stats([r[key] for r in rows]) for key in (
                'first_final_text_s', 'elapsed_s', 'generated_tokens',
                'torch_peak_allocated_bytes', 'torch_peak_reserved_bytes',
                'sampled_process_peak_bytes')}}
        review_path = HERE / f'2026-10-16-precision-{precision}-review.json'
        review = json.loads(review_path.read_text())
        assert review['raw_sha256'] == review['source_sha256'] == digest(path)
        assert review['manifest_sha256'] == digest(MANIFEST)
        assert review['precision'] == precision and len(review['rows']) == len(rows)
        cases = {case['id']: case for case in manifest['cases']}
        groups = {}
        for row, scored in zip(rows, review['rows']):
            for key in ('case', 'condition', 'seed'):
                assert scored[key] == row[key]
            case = cases[row['case']]
            assert scored['group'] == case['group']
            assert scored['ambiguous'] == (row['case'] in ('tail-choice', 'median-choice'))
            assert len(scored['checks']) == len(case['rubric'])
            assert all(type(check) is bool for check in scored['checks'])
            group = 'ambiguous_choice' if scored['ambiguous'] else scored['group']
            groups.setdefault(group, {'answers': 0, 'all_rubric_checks_passed': 0})
            groups[group]['answers'] += 1
            groups[group]['all_rubric_checks_passed'] += int(all(scored['checks']))
        result['precisions'][precision]['semantic_review'] = {
            'sha256': digest(review_path), 'scope': review['reviewer'], 'groups': groups}
    a, b = batches['nf4'], batches['bf16']
    for key in ('model', 'model_files', 'environment', 'generation_config', 'chat_template'):
        assert a[key] == b[key], key
    for x, y in zip(a['rows'], b['rows']):
        for key in ('case', 'condition', 'seed', 'messages', 'prompt', 'input_token_ids',
                    'generation_kwargs', 'enable_thinking'):
            assert x[key] == y[key], (x['case'], key)
    result['paired_inputs_verified'] = 28
    return result


if __name__ == '__main__':
    print(json.dumps(summarize(), ensure_ascii=False, indent=2))
