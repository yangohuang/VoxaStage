
import hashlib,importlib.metadata,json,os,re,subprocess,sys
from pathlib import Path
assert sys.flags.isolated==1
cfg=Path('/opt/worker/pyvenv.cfg').read_text()
assert 'include-system-site-packages = false' in cfg
assert os.getuid()==65532
lock=Path('/opt/requirements.lock').read_text()
assert hashlib.sha256(lock.encode()).hexdigest()=='b23b79149066245978787ecab7f05d5bb363d956a0bbbe40047c54c8a8cc2313'
packages={}
for name,version in re.findall(r'^([\w.-]+)==([^\s]+)',lock,re.M):
 d=importlib.metadata.distribution(name)
 assert d.version==version,(name,version,d.version)
 location=str(d.locate_file(''))
 assert location.startswith('/opt/worker/lib/python3.12/site-packages'),(name,location)
 packages[name]={'version':d.version,'location':location}
assert len(packages)==63
check=subprocess.run([sys.executable,'-m','pip','check'],capture_output=True,text=True,timeout=30)
assert check.returncode==0,check.stdout+check.stderr
import torch,transformers,bitsandbytes,accelerate
from transformers import AutoConfig,AutoModelForCausalLM,AutoTokenizer
import getpass
assert getpass.getuser()=="voxastage"
from torch._inductor.runtime.cache_dir_utils import cache_dir
assert cache_dir().startswith("/tmp/")
assert not torch.cuda.is_available()
worker_sha=hashlib.sha256(Path('/opt/app/llm_server.py').read_bytes()).hexdigest()
assert worker_sha=='52499584c781f0232f113e5cb01731fc74f726dd9866e530da00ed4ca879d295'
system=subprocess.check_output(['dpkg-query','-W','-f=${Package} ${Version}\n','libgomp1','libstdc++6','libc6','ca-certificates'],text=True)
print(json.dumps({'scope':'CPU-only environment probe; no model inference','python':sys.version,'uid':os.getuid(),'isolated':sys.flags.isolated,'sys_path':sys.path,'pyvenv_cfg':cfg,'locked_packages':packages,'pip_version':importlib.metadata.version('pip'),'pip_check':check.stdout,'system_packages':system,'torch_import':torch.__version__,'transformers_import':transformers.__version__,'bitsandbytes_import':bitsandbytes.__version__,'accelerate_import':accelerate.__version__,'cuda_available':torch.cuda.is_available(),'worker_sha256':worker_sha}),flush=True)
