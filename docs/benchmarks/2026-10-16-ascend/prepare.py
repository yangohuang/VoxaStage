"""Reconstruct the frozen ASCEND subset; no network or ASR requests."""
import argparse,collections,hashlib,io,json,math,re,sys,wave
from pathlib import Path
import pyarrow
import pyarrow.parquet as pq
import soundfile as sf

SHA='a4c81d2b5ed6124f052089a695972808c16e0ce0c365ec9773c5d1a8fcf043a7'
REV='737e9800ae31be9932ba8464c80366559bd28424'
TERMS='agent, Pipecat, MiniCPM-o, DINet, FlashHead, StreamingTalker, VAD, ASR, WebRTC, Python'
def digest(b):return hashlib.sha256(b).hexdigest()
def write(p,d):p.write_text(json.dumps(d,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('parquet',type=Path);p.add_argument('output',type=Path)
 a=p.parse_args();assert digest(a.parquet.read_bytes())==SHA
 table=pq.read_table(a.parquet);assert table.num_rows==1315
 rows=table.to_pylist();assert len({r['id'] for r in rows})==1315
 eligible=[];excluded=collections.Counter()
 for r in rows:
  text=r['transcription'];duration=r['duration']
  if r['language']!='mixed':excluded['not_mixed']+=1
  elif not(re.search('[\u4e00-\u9fff]',text) and re.search('[A-Za-z]',text)):excluded['missing_han_or_ascii_letter']+=1
  elif '[unk]' in text.lower():excluded['unknown_token']+=1
  elif not(math.isfinite(duration) and 0<duration<=30):excluded['outside_duration']+=1
  else:eligible.append(r)
 selected=sorted(eligible,key=lambda r:(digest(('voxastage-ascend-v1:'+r['id']).encode()),r['id']))[:60]
 assert len(selected)==60
 a.output.mkdir(parents=True,exist_ok=False);(a.output/'audio').mkdir()
 manifests=[dict(version=1,cases=[]) for _ in range(3)];details=[]
 for n,r in enumerate(selected):
  identifier='ascend-'+r['id'];assert re.fullmatch('[A-Za-z0-9_-]{1,64}',identifier)
  raw=r['audio']['bytes'];assert isinstance(raw,bytes)
  data,sr=sf.read(io.BytesIO(raw),dtype='int16',always_2d=True)
  assert sr==16000 and data.shape[1]==1 and 0<len(data)<=480000
  wav=io.BytesIO()
  with wave.open(wav,'wb') as w:w.setparams((1,2,16000,0,'NONE','not compressed'));w.writeframes(data.astype('<i2').tobytes())
  b=wav.getvalue();relative=f'audio/{identifier}.wav';(a.output/relative).write_bytes(b)
  manifests[n//20]['cases'].append(dict(id=identifier,audio=relative,sha256=digest(b),source='human',reference=r['transcription'],reference_kind='human_transcript',transcript_reviewed=True))
  details.append(dict(id=identifier,dataset_id=r['id'],rank=n,selection_sha256=digest(('voxastage-ascend-v1:'+r['id']).encode()),encoded_audio_sha256=digest(raw),wav_sha256=digest(b),pcm_sha256=digest(data.astype('<i2').tobytes()),samples=len(data),duration_s=len(data)/sr,declared_duration_s=r['duration'],topic=r['topic'],speaker=r['original_speaker_id'],session=r['session_id']))
 for n,m in enumerate(manifests):write(a.output/f'inputs-{n+1}.json',m)
 write(a.output/'variants.json',dict(version=1,variants=[dict(id='plain',context=''),dict(id='terms',context=TERMS)]))
 write(a.output/'selection.json',dict(dataset='CAiRE/ASCEND',revision=REV,split='test',license='CC-BY-SA-4.0',parquet_sha256=SHA,total_rows=len(rows),eligible=len(eligible),excluded=dict(excluded),selected=details,reference_provenance='Manual transcription by ASCEND annotators according to the paper; not independently listened to or corrected by VoxaStage.',transformation='Decode stored audio to int16, write mono PCM16 WAV; no resampling, trimming or gain adjustment.',tools=dict(python=sys.version,pyarrow=pyarrow.__version__,soundfile=sf.__version__,libsndfile=sf.__libsndfile_version__),preparation_source_sha256=digest(Path(__file__).read_bytes())))
 print(json.dumps(dict(eligible=len(eligible),excluded=dict(excluded),selected=len(details),seconds=sum(r['duration_s'] for r in details),speakers=dict(collections.Counter(r['speaker'] for r in details)),topics=dict(collections.Counter(r['topic'] for r in details)))))
if __name__=='__main__':main()
