import hashlib,http.server,io,json,os,select,shutil,subprocess,sys,threading,time,wave
from pathlib import Path
assert os.getuid()==65532
manifest=json.loads(Path('/trial/manifest.json').read_text())
class Handler(http.server.BaseHTTPRequestHandler):
    posts=0
    def log_message(self,*args):pass
    def reply(self,body):
        raw=json.dumps(body).encode();self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    def do_GET(self):self.reply({'ready':True,'busy':False,'model':'Qwen3-ASR-1.7B'})
    def do_POST(self):
        type(self).posts+=1
        body=self.rfile.read(int(self.headers['Content-Length']))
        with wave.open(io.BytesIO(body)) as f:seconds=f.getnframes()/f.getframerate()
        self.reply({'model':'Qwen3-ASR-1.7B','text':'fixture','audio_s':seconds,'elapsed_s':0.001})
server=http.server.ThreadingHTTPServer(('127.0.0.1',18315),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
results=[]
try:
 for mode in ['complete_fixture','terminate_after_first_reply']:
    Handler.posts=0;events=[];start=time.monotonic();pending=b''
    with open('/tmp/drill-client-stderr.log','wb') as stderr:
        proc=subprocess.Popen([sys.executable,'-I','-u','/trial/asr-server/deploy/protocol_client.py','/trial/manifest.json'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=stderr)
        try:
            stopped=False
            while True:
                if time.monotonic()-start>60:raise TimeoutError('fixture_deadline')
                if not select.select([proc.stdout],[],[],.2)[0]:continue
                chunk=os.read(proc.stdout.fileno(),65536)
                if not chunk:break
                pending+=chunk
                while b'\n' in pending:
                    line,pending=pending.split(b'\n',1);event=json.loads(line);events.append(event)
                    if event['event']=='request_ready':
                        if mode=='terminate_after_first_reply' and Handler.posts==1:
                            proc.terminate();stopped=True;break
                        proc.stdin.write((json.dumps({'continue':event['case']})+'\n').encode());proc.stdin.flush()
                if stopped:break
            proc.wait(timeout=10)
        finally:
            if proc.poll() is None:proc.kill();proc.wait(timeout=5)
        checkpoints=[e['report'] for e in events if e['event']=='checkpoint']
        assert checkpoints
        if mode=='complete_fixture':
            assert proc.returncode==0 and Handler.posts==92
            assert events[-1]=={'event':'finished','requests':92}
            assert checkpoints[-1]['status']=='passed'
        else:
            assert proc.returncode!=0 and Handler.posts==1
            latest=checkpoints[-1]
            assert [t['status'] for t in latest['trials']]==['ok','running']
            for key in ['cases','variants','source_sha256','health_snapshot_sha256']:assert key in latest
        results.append({'mode':mode,'exit_code':proc.returncode,'http_posts':Handler.posts,'events':len(events),'checkpoints':len(checkpoints),'last_report_status':checkpoints[-1]['status'],'last_trial_statuses':[t['status'] for t in checkpoints[-1]['trials']],'full_metadata_retained':all(k in checkpoints[-1] for k in ['cases','variants','source_sha256','health_snapshot_sha256'])})
        shutil.rmtree('/tmp/capture')
finally:server.shutdown();server.server_close()
print(json.dumps({'scope':'CPU fake HTTP service with real frozen client CLI; no model inference','results':results,'manifest_sha256':hashlib.sha256(Path('/trial/manifest.json').read_bytes()).hexdigest()}),flush=True)
