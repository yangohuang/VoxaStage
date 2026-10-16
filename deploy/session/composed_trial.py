"""Guarded three-container session trial. Configuration and raw output are private."""
import argparse
import hashlib
import inspect
import json
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit, urlunsplit
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'llm-server/deploy'))
from container_trial import Controller, command, install_signal_handlers
from container_guard import OccupancyGuard, OwnedContainer, validate_idle_health

READ_STATES = '''
import json,sys,urllib.request
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
states=[]
for url,method in json.loads(sys.argv[1]):
 with opener.open(urllib.request.Request(url,method=method),timeout=2) as response:
  body=response.read(65537)
  if len(body)>65536:raise RuntimeError('health_response_too_large')
  states.append(json.loads(body))
print(json.dumps(states))
'''


def read_states(requests):
    result = subprocess.run([sys.executable, '-I', '-c', READ_STATES, json.dumps(requests)],
                            check=True, capture_output=True, text=True, timeout=8)
    return json.loads(result.stdout)


def loopback_url(value, scheme):
    parsed = urlsplit(value)
    if (parsed.scheme != scheme or parsed.hostname != '127.0.0.1' or not parsed.port
            or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment):
        raise ValueError('explicit_loopback_url_required')
    return parsed


def listener_owned(rows, fd_links, port):
    listeners=[]
    for row in rows:
        fields=row.split()
        if len(fields)>=10 and fields[3]=='0A' and fields[1].endswith(f':{port:04X}'):
            listeners.append(fields)
    return (len(listeners)==1 and listeners[0][1].startswith('0100007F:')
            and 'socket:['+listeners[0][9]+']' in fd_links)


class ComposedTrial(Controller):
    """Reuse reviewed ownership cleanup and bounded JSONL/stdin supervision."""
    ROLES = ('llm', 'asr', 'app')

    def __init__(self, config, manifest, payload, output, invoke=command):
        self.config = config
        self.frozen_manifest = manifest
        self.manifest = dict(manifest, requests=[{'case':c['case']} for c in manifest['cases']],
                             timeouts_s={'startup':30, 'request':max(c['timeout_s'] for c in manifest['cases']),
                                         'recovery':5, 'overall':sum(c['timeout_s'] for c in manifest['cases'])+60})
        self.payload = Path(payload).resolve()
        self.output = Path(output)
        self.invoke = invoke
        self.units = {}
        self.guard = self.child = None
        self.run_id = uuid.uuid4().hex
        ports = config['ports']
        if (set(ports) != set(self.ROLES) or len(set(ports.values())) != 3
                or any(type(p) is not int or not 1024 <= p <= 65535 for p in ports.values())):
            raise ValueError('three_distinct_unprivileged_ports_required')
        if type(config['minimum_free_mib']) is not int or config['minimum_free_mib'] < 16384:
            raise ValueError('minimum_free_memory_too_low')
        endpoints = [(url,'http') for url in config['daily_health']]
        if len(endpoints)!=3:
            raise ValueError('three_daily_health_urls_required')
        endpoints += [(config['tts_url'],'http'), (config['avatar_url'],'ws')]
        if any(loopback_url(url,scheme).port in ports.values() for url,scheme in endpoints):
            raise ValueError('trial_port_conflicts_with_forward')
        if loopback_url(config['tts_url'],'http').path:
            raise ValueError('tts_origin_required')
        for role in self.ROLES:
            OwnedContainer('0'*64,config['images'][role],self.run_id,invoke)
        source_names = ['deploy/session/composed_trial.py','deploy/session/session_client.py',
                        'llm-server/deploy/container_trial.py','llm-server/deploy/container_guard.py',
                        'llm-server/deploy/protocol_client.py']
        self.sources = {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in source_names}
        self.state = dict(status='preparing',run_id=self.run_id,units={},cleanup_errors=[],
                          health_probes=0,max_health_probe_s=0,source_sha256=self.sources,
                          config_sha256=hashlib.sha256(json.dumps(config,sort_keys=True).encode()).hexdigest(),
                          manifest_sha256=hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest())

    def probe(self):
        started=time.monotonic()
        try:
            states=read_states([(url,'GET') for url in self.config['daily_health']])
            validate_idle_health(states[0],states[1])
            validate_idle_health(states[0],states[2])
        finally:
            self.state['health_probes']+=1
            self.state['max_health_probe_s']=max(self.state['max_health_probe_s'],time.monotonic()-started)

    def external_check(self):
        avatar=urlsplit(self.config['avatar_url'])
        health=urlunsplit(('http',avatar.netloc,'/health','',''))
        tts,avatar=read_states([(self.config['tts_url']+'/api/tts/list','POST'),(health,'GET')])
        if self.config['male_speaker'] not in tts.get('speaker_ids',[]):
            raise RuntimeError('male_speaker_unavailable')
        if avatar.get('status')!='ready' or avatar.get('busy') is not False:
            raise RuntimeError('avatar_busy_or_unavailable')
        self.state.setdefault('external_checks',[]).append(dict(tts_speaker_available=True,
             tts_occupancy_observable=False,avatar_ready=True,avatar_idle=True))

    def check_ports(self):
        sockets=[]
        try:
            for port in self.config['ports'].values():
                sock=socket.socket();sockets.append(sock)
                sock.bind(('127.0.0.1',port))
        finally:
            for sock in sockets:sock.close()

    def gpu_admit(self,role=None):
        required=8192 if role=='asr' else self.config['minimum_free_mib']
        values=self.invoke(['nvidia-smi','--query-gpu=memory.free','--format=csv,noheader,nounits']).splitlines()
        if len(values)!=1 or int(values[0]) < required:
            raise RuntimeError('insufficient_or_ambiguous_gpu_memory')
        self.state.setdefault('gpu_admissions',[]).append(dict(role=role,free_mib=int(values[0]),required_mib=required))

    def digest_file(self, root, name):
        relative=Path(name);path=Path(root)/relative
        if (relative.is_absolute() or '..' in relative.parts or path.is_symlink()
                or not path.resolve().is_relative_to(Path(root).resolve())):
            raise RuntimeError('unsafe_manifest_path')
        digest=hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda:stream.read(4*1024*1024),b''):
                if self.guard is not None:self.guard.raise_if_failed()
                digest.update(block)
        return digest.hexdigest()

    def verify_payload(self):
        declared=self.frozen_manifest['payload_sha256']
        for name,expected in declared.items():
            if self.digest_file(self.payload,name)!=expected:
                raise RuntimeError('payload_hash_mismatch')
        required=set(self.sources)|{'deploy/session/cases.json'}
        required.update('inputs/'+c['file'] for c in self.manifest['cases'] if c['kind']=='wav')
        if not required.issubset(declared):
            raise RuntimeError('payload_required_files_missing')
        if any(declared[name]!=digest for name,digest in self.sources.items()):
            raise RuntimeError('payload_running_source_mismatch')
        if json.loads((self.payload/'manifest.json').read_text())!=self.frozen_manifest:
            raise RuntimeError('frozen_manifest_mismatch')
        if json.loads((self.payload/'deploy/session/cases.json').read_text())['cases']!=self.manifest['cases']:
            raise RuntimeError('frozen_cases_mismatch')
        for case in self.manifest['cases']:
            if case['kind']=='wav' and declared['inputs/'+case['file']]!=case['sha256']:
                raise RuntimeError('frozen_audio_mismatch')

    def verify_all(self):
        self.verify_payload()
        verified={}
        for role,count in [('llm',10),('asr',12)]:
            provenance=self.manifest['models'][role]
            files=provenance['files']
            if len(files)!=count or not provenance['revision']:
                raise RuntimeError('model_manifest_incomplete')
            verified[role]={}
            for name,expected in files.items():
                if Path(name).name!=name:raise RuntimeError('invalid_model_filename')
                actual=self.digest_file(self.config['models'][role],name)
                if actual!=expected:raise RuntimeError('model_hash_mismatch')
                verified[role][name]=actual
        self.state.setdefault('model_hash_checks',[]).append(verified)
        self.save()

    def create_args_for(self,role,unit):
        args=['docker','create','--name',unit.name,'--label','org.voxastage.validation-run='+self.run_id,
              '--network','host','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
              '--tmpfs','/tmp:rw,nosuid,size=512m','--env','USER=voxastage','--env','HOME=/tmp']
        port=str(self.config['ports'][role]);image=self.config['images'][role]
        if role!='app':
            args+=['--gpus','all','--mount','type=bind,src='+str(Path(self.config['models'][role]).resolve())+',dst=/model,readonly']
            if role=='llm':return args+[image,'--model','/model','--port',port,'--quantization','4bit']
            return args+[image,'--engine','qwen','--model','/model','--model-name','Qwen3-ASR-1.7B','--port',port]
        env={'PIPECAT_LLM_URL':f"http://127.0.0.1:{self.config['ports']['llm']}",
             'PIPECAT_ASR_URL':f"http://127.0.0.1:{self.config['ports']['asr']}",
             'PIPECAT_LLM_MODEL':'Qwen3-4B','PIPECAT_ASR_MODEL':'Qwen3-ASR-1.7B',
             'PIPECAT_TTS_URL':self.config['tts_url'],'PIPECAT_MALE_SPEAKER_ID':self.config['male_speaker'],
             'PIPECAT_AVATAR_PROVIDER':'streamingtalker','PIPECAT_AVATAR_URL':self.config['avatar_url'],
             'PIPECAT_CONVERSATION_DIR':'/opt/app/runtime/conversations',
             'PIPECAT_ASR_CONTEXT':'Pipecat, MiniCPM-o, DINet, FlashHead, StreamingTalker, agent, VAD, ASR, LLM, TTS'}
        for key,value in env.items():args+=['--env',key+'='+value]
        return args+['--cpus','2','--memory','4g','--tmpfs','/opt/app/runtime:rw,nosuid,size=64m,uid=65532,gid=65532',
                     '--mount','type=bind,src='+str(self.payload)+',dst=/trial,readonly',
                     '--entrypoint','/opt/application/bin/python',image,
                     '-m','uvicorn','bot:app','--host','127.0.0.1','--port',port]

    def wait_ready(self,role):
        deadline=time.monotonic()+180
        while time.monotonic()<deadline:
            self.guard.raise_if_failed()
            metadata=json.loads(self.invoke(['docker','container','inspect',self.units[role].owned.container_id]))[0]
            if metadata['State'].get('Running') is not True:
                raise RuntimeError('trial_service_exited_during_startup')
            try:
                state=read_states([(f"http://127.0.0.1:{self.config['ports'][role]}/health",'GET')])[0]
                if state.get('ready') is True and (state.get('active_sessions')==0 if role=='app' else state.get('busy') is False):
                    self.verify_listener(role)
                    return
            except (subprocess.SubprocessError,ValueError):
                pass
            time.sleep(.2)
        raise TimeoutError('trial_service_startup_timeout')

    def verify_listener(self,role):
        source='from pathlib import Path\nimport os,sys\n'+inspect.getsource(listener_owned)+'''
links=set()
for path in Path('/proc/1/fd').iterdir():
 try:links.add(os.readlink(path))
 except FileNotFoundError:pass
if not listener_owned(Path('/proc/net/tcp').read_text().splitlines(),links,int(sys.argv[1])):
 raise RuntimeError('listener_not_owned_loopback')
print('owned_loopback')
'''
        result=self.invoke(['docker','exec',self.units[role].owned.container_id,self.interpreter_for(role),'-I','-c',source,
                            str(self.config['ports'][role])])
        if result.strip()!='owned_loopback':raise RuntimeError('listener_ownership_unverified')
        self.state.setdefault('owned_loopback_listeners',[]).append(role)

    def interpreter_for(self,role):
        return '/opt/application/bin/python' if role=='app' else '/opt/worker/bin/python'

    def exec_args(self):
        self.verify_payload()
        return ['docker','exec','-i',self.units['app'].owned.container_id,'/opt/application/bin/python','-I','-u',
                '/trial/deploy/session/session_client.py','--manifest','/trial/manifest.json','--inputs','/trial/inputs',
                '--url',f"http://127.0.0.1:{self.config['ports']['app']}"]

    def abort(self):
        errors=[]
        for unit in reversed(tuple(self.units.values())):
            if unit.owned is not None:
                try:unit.owned.stop()
                except Exception as error:errors.append(str(error))
        if errors:raise RuntimeError(';'.join(errors))

    def cleanup(self):
        if self.guard is not None:
            try:
                self.guard.stop();self.guard.raise_if_failed()
            except Exception as error:self.state['cleanup_errors'].append(str(error))
            if self.guard.abort_error:self.state['cleanup_errors'].append(self.guard.abort_error)
        for role,unit in reversed(tuple(self.units.items())):
            if unit.owned is None and unit.create_attempted:
                try:unit.discover_after_create_failure()
                except Exception as error:unit.state['cleanup_errors'].append(str(error))
            if unit.owned is not None:
                try:
                    unit.owned.stop()
                except Exception as error:
                    unit.state['cleanup_errors'].append(str(error))
                else:
                    # Diagnostics failing must not strand a safely stopped unit.
                    try:
                        (unit.output/'worker.log').write_text(self.invoke(['docker','logs','--tail','2000',unit.owned.container_id]))
                        unit.state['container_exit']=json.loads(self.invoke(['docker','container','inspect',unit.owned.container_id]))[0].get('State')
                    except Exception as error:unit.state['cleanup_errors'].append(str(error))
                    try:
                        unit.owned.remove();unit.state['removed']=True
                    except Exception as error:unit.state['cleanup_errors'].append(str(error))
            self.state['units'][role]=unit.state
            self.state['cleanup_errors'].extend(unit.state['cleanup_errors'])
        if self.state['cleanup_errors']:self.state['status']='failed'

    def run(self):
        self.output.mkdir(parents=True,exist_ok=False);self.save()
        self.guard=OccupancyGuard(self.probe,self.abort)
        try:
            self.guard.start();self.check_ports();self.external_check();self.gpu_admit();self.verify_all()
            self.guard.check_now();self.gpu_admit()
            for role in self.ROLES:
                unit=Controller(self.config['images'][role],self.payload,self.manifest,{},self.output/role,[],invoke=self.invoke)
                unit.run_id=self.run_id;unit.name='voxastage-composed-'+self.run_id+'-'+role
                unit.state.update(run_id=self.run_id,name=unit.name)
                unit.output.mkdir();self.units[role]=unit;self.state['units'][role]=unit.state
                self.guard.check_now();unit.create_attempted=True;self.save()
                cid=self.invoke(self.create_args_for(role,unit)).strip()
                unit.owned=OwnedContainer(cid,unit.image,self.run_id,self.invoke)
                unit.state['container_id']=cid;self.save()
                self.guard.check_now()
                if role!='app':self.gpu_admit(role)
                unit.owned.start();self.wait_ready(role)
            self.external_check();self.guard.check_now()
            self.state['status']='running';self.save();self.protocol()
            self.guard.check_now();self.verify_all();self.external_check()
            self.state['status']='passed'
        except BaseException as error:
            self.state.update(status='failed',error_type=type(error).__name__,error=str(error))
        finally:
            saved={sig:signal.getsignal(sig) for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP)}
            try:
                for sig in saved:signal.signal(sig,signal.SIG_IGN)
                self.cleanup();self.save()
            finally:
                for sig,handler in saved.items():signal.signal(sig,handler)
        return self.state


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for arg in ('config','manifest','payload','output'):parser.add_argument('--'+arg,type=Path,required=True)
    args=parser.parse_args();install_signal_handlers()
    state=ComposedTrial(json.loads(args.config.read_text()),json.loads(args.manifest.read_text()),args.payload,args.output).run()
    print(json.dumps(state),flush=True)
    if state['status']!='passed':raise SystemExit(1)


if __name__=='__main__':main()
