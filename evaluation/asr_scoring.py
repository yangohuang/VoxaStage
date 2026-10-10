"""Offline ASR evidence scoring; synthesis prompts are not human transcripts."""
import argparse
import json
from pathlib import Path
import unicodedata


def normalize(text):
    """Versioned policy: NFKC + casefold, remove whitespace and punctuation."""
    text = unicodedata.normalize('NFKC', text).casefold()
    return ''.join(c for c in text if not c.isspace() and not unicodedata.category(c).startswith('P'))


def distance(reference, hypothesis):
    """Levenshtein distance with unit insertion, deletion and substitution cost."""
    if len(reference) < len(hypothesis):
        reference, hypothesis = hypothesis, reference
    previous = list(range(len(hypothesis) + 1))
    for i, left in enumerate(reference, 1):
        current = [i]
        for j, right in enumerate(hypothesis, 1):
            current.append(min(current[-1] + 1, previous[j] + 1,
                               previous[j - 1] + (left != right)))
        previous = current
    return previous[-1]


def validate(records):
    if not isinstance(records, list) or not 1 <= len(records) <= 10000:
        raise ValueError('expected 1..10000 evidence records')
    variants, references = {}, {}
    for row in records:
        if not isinstance(row, dict):
            raise ValueError('each record must be an object')
        for key in ('case_id', 'variant'):
            if not isinstance(row.get(key), str) or not row[key].strip() or len(row[key]) > 120:
                raise ValueError(f'invalid {key}')
        if not isinstance(row.get('reference'), str) or len(row['reference']) > 4000:
            raise ValueError('invalid reference')
        kind = row.get('reference_kind')
        reviewed = row.get('transcript_reviewed')
        if (kind not in ('human_transcript', 'synthesis_prompt') or type(reviewed) is not bool
                or reviewed != (kind == 'human_transcript')):
            raise ValueError('reference provenance and review flag must agree')
        if row.get('status') not in ('ok', 'error'):
            raise ValueError('status must be ok or error')
        if row['status'] == 'ok':
            if not isinstance(row.get('text'), str) or len(row['text']) > 4000:
                raise ValueError('successful result must have bounded text, including empty text')
        elif row.get('text') is not None:
            raise ValueError('failed result must not contain a scored hypothesis')
        case, variant = row['case_id'], row['variant']
        bucket = variants.setdefault(variant, {})
        if case in bucket:
            raise ValueError('duplicate case in variant; represent repeats as distinct case IDs')
        signature = (row['reference'], kind, reviewed)
        if case in references and references[case] != signature:
            raise ValueError('reference differs between variants')
        references[case] = signature
        bucket[case] = row
    if any(set(bucket) != set(references) for bucket in variants.values()):
        raise ValueError('all variants must include every case, including explicit errors')
    return variants


def score(records):
    variants = validate(records)
    output = dict(schema_version=1, normalization='nfkc-casefold-no-punctuation-or-whitespace-v1',
                  unit='unicode_codepoint', aggregation='sum_edits_divided_by_sum_reference_characters',
                  quality_scope='offline text comparison; no physical-device or perceptual quality claim',
                  variants={})
    for name, cases in sorted(variants.items()):
        result = dict(attempted=len(cases), succeeded=0, failed=0, complete=True,
                      reference_groups={}, cases=[])
        for case, row in sorted(cases.items()):
            kind = row['reference_kind']
            group = result['reference_groups'].setdefault(kind, dict(
                metric='normalized_cer' if kind == 'human_transcript' else 'prompt_character_difference_rate',
                attempted=0, failed=0, scored_cases=0, reference_characters=0,
                attempted_reference_characters=0,
                rate_scope='successful_nonempty_references_only',
                edit_count=0, exact_match_cases=0, empty_reference_cases=0,
                empty_reference_nonempty_outputs=0, empty_reference_inserted_characters=0))
            group['attempted'] += 1
            reference = normalize(row['reference'])
            group['attempted_reference_characters'] += len(reference)
            detail = dict(case_id=case, status=row['status'], reference_kind=kind)
            if row['status'] == 'error':
                result['failed'] += 1
                result['complete'] = False
                group['failed'] += 1
                detail.update(edit_count=None, reference_characters=len(reference), rate=None)
            else:
                result['succeeded'] += 1
                group['scored_cases'] += 1
                hypothesis = normalize(row['text'])
                edits = distance(reference, hypothesis)
                detail.update(reference_characters=len(reference), edit_count=edits,
                              rate=edits / len(reference) if reference else None)
                group['exact_match_cases'] += int(edits == 0)
                if reference:
                    group['reference_characters'] += len(reference)
                    group['edit_count'] += edits
                else:
                    group['empty_reference_cases'] += 1
                    group['empty_reference_nonempty_outputs'] += int(bool(hypothesis))
                    group['empty_reference_inserted_characters'] += len(hypothesis)
            result['cases'].append(detail)
        for group in result['reference_groups'].values():
            denominator = group['reference_characters']
            group['rate'] = group['edit_count'] / denominator if denominator else None
            attempted = group['attempted_reference_characters']
            group['reference_character_coverage'] = denominator / attempted if attempted else None
            group['complete'] = group['failed'] == 0
        output['variants'][name] = result
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve() or (
            args.output.exists() and args.input.samefile(args.output)):
        parser.error('output must not overwrite input evidence')
    try:
        result = score(json.loads(args.input.read_text(encoding='utf-8')))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    # A partial comparison is recorded but never silently treated as a passing run.
    return 1 if any(not variant['complete'] for variant in result['variants'].values()) else 0


if __name__ == '__main__':
    raise SystemExit(main())
