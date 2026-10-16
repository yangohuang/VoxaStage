"""Validate this predeclared experiment, including its documented split execution.

Usage: python docs/benchmarks/2026-10-16-summarize-thinking-context.py RESULTS_JSON
       [--completion COMPLETION_JSON]
Run at the experiment source revision so collector digests remain verifiable.
"""
import argparse
from collections import Counter
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parents[2]
COLLECTOR = ROOT / 'docs/benchmarks/2026-10-16-run-model-decoding.py'
MANIFEST = ROOT / 'docs/benchmarks/2026-10-16-thinking-context-input.json'
COMPLETION = MANIFEST.with_name('2026-10-16-thinking-context-completion-input.json')
spec = importlib.util.spec_from_file_location('collector', COLLECTOR)
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def identity(row):
    return row['case'], row['condition'], row['seed']


def validate_batch(path, manifest_path):
    data, manifest = json.loads(path.read_text()), json.loads(manifest_path.read_text())
    assert data['manifest_sha256'] == digest(manifest_path)
    assert data['collector_sha256'] == digest(COLLECTOR)
    matrix = capture.request_matrix(manifest)
    assert len(matrix) == len(data['rows'])
    for item, row in zip(matrix, data['rows']):
        assert identity(row) == (item['case']['id'], item['condition']['id'], item['seed'])
        if row['status'] == 'pending':
            continue
        messages, thinking, kwargs = capture.request_configuration(manifest, item['case'], item['condition'])
        assert row['messages'] == messages and row['enable_thinking'] == thinking
        assert row['generation_kwargs'] == kwargs
    for row in [data['warmup']] + data['rows']:
        if row['status'] == 'pending':
            continue
        assert row['generated_tokens'] == len(row['token_ids'])
        assert row['text'] == ''.join(chunk['text'] for chunk in row['chunks'])
        assert all(row[key] == value for key, value in capture.final_answer_view(row['chunks'], row['enable_thinking']).items())
        if row['status'] != 'complete':
            assert row['status'] == 'failed' and row['finish_reason'] == 'production_model_busy'
            continue  # failed streams can retain unflushed tokens; never score them
        assert row['text'] == row['decoded_text']
        assert row['finish_reason'] in ('eos', 'length')
        assert row['finish_reason'] == capture.finish_reason(row['token_ids'], row['eos_token_ids'], row['generation_kwargs']['max_new_tokens'], None)
        times = [chunk['elapsed_s'] for chunk in row['chunks']]
        assert times == sorted(times) and all(0 <= value <= row['elapsed_s'] for value in times)
        assert row['first_text_s'] == (times[0] if times else None)
        assert row['process_memory_sample_errors'] == 0
    return data


def stats(values):
    values = sorted(value for value in values if value is not None)
    return {'n': len(values), 'median': statistics.median(values) if values else None,
            'p95_nearest_rank': values[math.ceil(.95 * len(values)) - 1] if values else None}


def summarize(source, completion=None):
    manifest = json.loads(MANIFEST.read_text())
    data = validate_batch(source, MANIFEST)
    sources = {'primary': {'sha256': digest(source), 'status': data['status'],
                          'row_status_counts': dict(Counter(row['status'] for row in data['rows']))}}
    selected = [(row, 'primary', index) for index, row in enumerate(data['rows'])]
    overlaps = None
    if completion:
        child = json.loads(COMPLETION.read_text())
        assert child['parent_manifest_sha256'] == digest(MANIFEST)
        assert child['cases'] == manifest['cases'][6:]
        for key in ('conditions', 'system_prompt', 'system_prompts', 'quantization', 'models', 'enable_thinking'):
            assert child[key] == manifest[key]
        tail = validate_batch(completion, COMPLETION)
        assert data['status'] == 'failed' and tail['status'] == 'complete'
        assert [row['status'] for row in data['rows']] == ['complete'] * 54 + ['failed'] + ['pending'] * 9
        for key in ('model', 'model_files', 'environment', 'generation_config', 'chat_template', 'quantization'):
            assert data[key] == tail[key]
        sources['completion'] = {'sha256': digest(completion), 'status': tail['status'],
                                 'row_status_counts': dict(Counter(row['status'] for row in tail['rows']))}
        selected = selected[:48] + [(row, 'completion', index) for index, row in enumerate(tail['rows'])]
        pairs = list(zip(data['rows'][48:54], tail['rows'][:6]))
        overlaps = {'completed_duplicates_excluded': len(pairs),
                    'identical_output_tokens': sum(a['token_ids'] == b['token_ids'] for a, b in pairs)}
    else:
        assert data['status'] == 'complete'
    matrix = capture.request_matrix(manifest)
    assert len(matrix) == len(selected) == 64
    for item, (row, group, index) in zip(matrix, selected):
        assert row['status'] == 'complete'
        assert identity(row) == (item['case']['id'], item['condition']['id'], item['seed'])
    result = {'execution': 'segmented' if completion else 'single_batch', 'sources': sources,
              'manifest_sha256': digest(MANIFEST), 'overlap': overlaps,
              'selection': [{'case': row['case'], 'condition': row['condition'], 'seed': row['seed'],
                             'source': group, 'source_row_index': index} for row, group, index in selected],
              'conditions': {}}
    for condition in manifest['conditions']:
        rows = [row for row, _, _ in selected if row['condition'] == condition['id']]
        result['conditions'][condition['id']] = {
            'requests': len(rows), 'finish_reasons': dict(Counter(row['finish_reason'] for row in rows)),
            'final_answers': sum(bool(row['final_text']) for row in rows),
            'thinking_closed': sum(row['thinking_closed'] is True for row in rows),
            'unexpected_thinking': sum(row['unexpected_thinking'] for row in rows),
            'first_final_text_s': stats([row['first_final_text_s'] for row in rows]),
            'first_raw_text_s': stats([row['first_text_s'] for row in rows]),
            'generation_elapsed_s': stats([row['elapsed_s'] for row in rows]),
            'generated_tokens': stats([row['generated_tokens'] for row in rows]),
            'sampled_process_peak_bytes': max(row['sampled_process_peak_bytes'] for row in rows)}
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--completion', type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.source, args.completion), ensure_ascii=False, indent=2))
