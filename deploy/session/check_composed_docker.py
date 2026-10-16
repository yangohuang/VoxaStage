"""CPU-only Docker lifecycle drill; fake health and protocol, never inference."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import threading

from composed_trial import ComposedTrial, install_signal_handlers


SERVER = '''
from http.server import BaseHTTPRequestHandler,HTTPServer
import json,sys
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  data=json.dumps({'ready':True,'busy':False,'active_sessions':0}).encode()
  self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
 def log_message(self,*args):pass
HTTPServer(('127.0.0.1',int(sys.argv[1])),Handler).serve_forever()
'''
PROTOCOL = '''
import json,sys
cases=json.loads(sys.argv[1]);fail=sys.argv[2]=='fail'
for index,case in enumerate(cases):
 print(json.dumps({'event':'request_ready','case':case}),flush=True)
 assert json.loads(sys.stdin.readline())=={'continue':case}
 print(json.dumps({'event':'fixture_partial','case':case}),flush=True)
 if fail and index==1:
  print(json.dumps({'event':'failed','error_type':'InjectedFixtureFailure'}),flush=True)
  sys.exit(1)
 print(json.dumps({'event':'complete','case':case}),flush=True)
print(json.dumps({'event':'finished','requests':len(cases)}),flush=True)
'''


class FixtureTrial(ComposedTrial):
    def __init__(self,*args,fail=False,**kwargs):
        super().__init__(*args,**kwargs)
        self.fail=fail
        self.state['scope']='CPU-only lifecycle fixture: no GPU, model loading, audio, avatar or physical playback'

    def gpu_admit(self,role=None):
        self.state['gpu_admission']='not_applicable_cpu_fixture'

    def verify_all(self):
        self.verify_payload()
        self.state['model_hash_checks']='not_applicable_no_model_mounts'

    def interpreter_for(self,role):
        return '/opt/application/bin/python'

    def create_args_for(self,role,unit):
        return ['docker','create','--name',unit.name,'--label','org.voxastage.validation-run='+self.run_id,
                '--network','host','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
                '--cpus','1','--memory','256m','--tmpfs','/tmp:rw,nosuid,size=16m',
                '--entrypoint','/opt/application/bin/python',self.config['images'][role],
                '-I','-u','-c',SERVER,str(self.config['ports'][role])]

    def exec_args(self):
        return ['docker','exec','-i',self.units['app'].owned.container_id,self.interpreter_for('app'),'-I','-u','-c',
                PROTOCOL,json.dumps([c['case'] for c in self.manifest['cases']]),'fail' if self.fail else 'pass']


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        data=json.dumps(dict(ready=True,busy=False,active_sessions=0,status='ready',speaker_ids=['fixture-male'])).encode()
        self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    do_POST=do_GET
    def log_message(self,*args):pass


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',required=True)
    parser.add_argument('--payload',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    install_signal_handlers()
    args.output.mkdir(parents=True,exist_ok=False)
    health=ThreadingHTTPServer(('127.0.0.1',0),Health)
    thread=threading.Thread(target=health.serve_forever,daemon=True);thread.start()
    port=health.server_address[1]
    results=[]
    try:
        config=dict(images={role:args.image for role in ComposedTrial.ROLES},models={},
                    daily_health=[f'http://127.0.0.1:{port}/health']*3,
                    tts_url=f'http://127.0.0.1:{port}',avatar_url=f'ws://127.0.0.1:{port}/v1/stream',
                    male_speaker='fixture-male',minimum_free_mib=16384)
        manifest=json.loads((args.payload/'manifest.json').read_text())
        for fail in (False,True):
            # New ports for each batch: TIME_WAIT from the preceding HTTP
            # connections must not prevent reaching the injected protocol error.
            sockets=[socket.socket() for _ in range(3)]
            try:
                for sock in sockets:sock.bind(('127.0.0.1',0))
                config['ports']=dict(zip(ComposedTrial.ROLES,[s.getsockname()[1] for s in sockets]))
            finally:
                for sock in sockets:sock.close()
            trial=FixtureTrial(config,manifest,args.payload,args.output/('failure' if fail else 'success'),fail=fail)
            result=trial.run()
            assert result['status']==('failed' if fail else 'passed'),result
            assert all(row['removed'] for row in result['units'].values()),result
            assert not result['cleanup_errors'],result
            assert result['owned_loopback_listeners']==list(ComposedTrial.ROLES),result
            assert 'fixture_partial' in (trial.output/'events.jsonl').read_text()
            results.append(result)
    finally:
        health.shutdown();health.server_close();thread.join(timeout=3)
    result=dict(scope='CPU Docker fixture only',cases=results,
                fixture_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    (args.output/'summary.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({'passed':True,'cases':len(results),'removed_containers':6}))


if __name__=='__main__':main()
