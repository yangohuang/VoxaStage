"""CPU-only probe for the isolated ASR image; does not load model weights."""
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest

assert sys.flags.isolated == 1
assert os.getuid() == 65532
cfg = Path('/opt/worker/pyvenv.cfg').read_text()
assert 'include-system-site-packages = false' in cfg
lock = Path('/opt/requirements.lock').read_text()
assert hashlib.sha256(lock.encode()).hexdigest() == 'f3179c3d8fe7fb84cba010aaff5e5c4b3d774bfb56d1bcb8eab55e36f0796975'
packages = {}
for name, version in re.findall(r'^([\w.-]+)==([^\s]+)', lock, re.M):
    distribution = importlib.metadata.distribution(name)
    location = str(distribution.locate_file(''))
    assert distribution.version == version, (name, version, distribution.version)
    assert location.startswith('/opt/worker/lib/python3.12/site-packages'), (name, location)
    packages[name] = {'version': distribution.version, 'location': location}
assert len(packages) == 111
check = subprocess.run([sys.executable, '-m', 'pip', 'check'], capture_output=True, text=True, timeout=30)
assert check.returncode == 0, check.stdout + check.stderr
import torch
import transformers
import librosa
import soundfile
import sox
from qwen_asr import Qwen3ASRModel
assert not torch.cuda.is_available()
assert Path(os.environ['NUMBA_CACHE_DIR']).is_relative_to('/tmp')
# Execute the existing HTTP/audio contract tests against the image's exact worker.
for name, path in [('asr_server', '/opt/app/asr_server.py'), ('test_asr_server', '/probe/test_asr_server.py')]:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules['test_asr_server'])
result = unittest.TextTestRunner(verbosity=2).run(suite)
assert result.testsRun == 3 and result.wasSuccessful()
worker_hash = hashlib.sha256(Path('/opt/app/asr_server.py').read_bytes()).hexdigest()
assert worker_hash == '8cc1438a1ddd31c26356262b43eceb8a7e5ea5b0751d48c96a8db20d7ea04a66'
system = subprocess.check_output(['dpkg-query', '-W', '-f=${Package} ${Version}\n',
                                  'libgomp1', 'libstdc++6', 'libc6', 'sox', 'ca-certificates'], text=True)
print(json.dumps({'scope': 'CPU installation and protocol tests; no model inference',
                  'python': sys.version, 'uid': os.getuid(), 'isolated': sys.flags.isolated,
                  'pyvenv_cfg': cfg, 'sys_path': sys.path, 'locked_packages': packages,
                  'pip_version': importlib.metadata.version('pip'), 'pip_check': check.stdout,
                  'system_packages': system, 'torch_import': torch.__version__,
                  'transformers_import': transformers.__version__,
                  'qwen_model_class': Qwen3ASRModel.__name__, 'cuda_available': torch.cuda.is_available(),
                  'protocol_tests': result.testsRun, 'worker_sha256': worker_hash}, ensure_ascii=False))
