"""Validate and summarize final-answer timing for the fixed context/thinking experiment.

Usage: python docs/benchmarks/2026-10-16-summarize-thinking-context.py RESULTS_JSON
Run at the experiment source revision so the collector digest remains verifiable.
"""
import hashlib,importlib.util,json,math,statistics,sys
from collections import Counter
from pathlib import Path
root=Path(__file__).resolve().parents[2]
collector=root/'docs/benchmarks/2026-10-16-run-model-decoding.py'
manifest_path=root/'docs/benchmarks/2026-10-16-thinking-context-input.json'
spec=importlib.util.spec_from_file_location('collector',collector); c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
source=Path(sys.argv[1]); data=json.loads(source.read_text());m=json.loads(manifest_path.read_text())
assert data['status']=='complete'
assert data['manifest_sha256']==hashlib.sha256(manifest_path.read_bytes()).hexdigest()
assert data['collector_sha256']==hashlib.sha256(collector.read_bytes()).hexdigest()
expected=c.request_matrix(m);assert len(expected)==len(data['rows'])==64
for item,row in zip(expected,data['rows']):
 assert (row['case'],row['condition'],row['seed'])==(item['case']['id'],item['condition']['id'],item['seed'])
 messages,thinking,kwargs=c.request_configuration(m,item['case'],item['condition'])
 assert row['messages']==messages and row['enable_thinking']==thinking and row['generation_kwargs']==kwargs
for row in [data['warmup']]+data['rows']:
 assert row['status']=='complete' and row['finish_reason'] in ['eos','length']
 assert row['generated_tokens']==len(row['token_ids'])
 assert row['text']==row['decoded_text']==''.join(x['text'] for x in row['chunks'])
 assert all(row[k]==v for k,v in c.final_answer_view(row['chunks'],row['enable_thinking']).items())
 assert row['finish_reason']==c.finish_reason(row['token_ids'],row['eos_token_ids'],row['generation_kwargs']['max_new_tokens'],None)
 assert [x['elapsed_s'] for x in row['chunks']]==sorted(x['elapsed_s'] for x in row['chunks'])
 assert row['process_memory_sample_errors']==0

def stats(xs):
 xs=sorted(x for x in xs if x is not None)
 return {'n':len(xs),'median':statistics.median(xs) if xs else None,'p95_nearest_rank':xs[math.ceil(.95*len(xs))-1] if xs else None}
out={'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'manifest_sha256':data['manifest_sha256'],'conditions':{}}
for condition in m['conditions']:
 rows=[r for r in data['rows'] if r['condition']==condition['id']]
 out['conditions'][condition['id']]={'requests':len(rows),'finish_reasons':dict(Counter(r['finish_reason'] for r in rows)),'final_answers':sum(bool(r['final_text']) for r in rows),'thinking_closed':sum(r['thinking_closed'] is True for r in rows),'unexpected_thinking':sum(r['unexpected_thinking'] for r in rows),'first_final_text_s':stats([r['first_final_text_s'] for r in rows]),'first_raw_text_s':stats([r['first_text_s'] for r in rows]),'generation_elapsed_s':stats([r['elapsed_s'] for r in rows]),'generated_tokens':stats([r['generated_tokens'] for r in rows]),'sampled_process_peak_bytes':max(r['sampled_process_peak_bytes'] for r in rows)}
print(json.dumps(out,ensure_ascii=False,indent=2))
