"""Check a selected local deployment and launch the existing app in foreground."""
import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys

from deployment_check import build_environment, inspect_deployment

ROOT = Path(__file__).resolve().parent


def check(name, status, message, *, required=True, help='docs/DEPLOYMENT.md'):
    return dict(name=name, status=status, required=required, message=message, help=help)


def choose_python(root, choice):
    if choice:
        if '/' not in choice and shutil.which(choice):
            return shutil.which(choice)
        path = Path(choice)
        return str(path if path.is_absolute() else root / path)
    local = root / '.venv/bin/python'
    # Do not resolve the symlink: its location selects the virtual environment.
    return str(local) if local.is_file() else sys.executable


def resource_environment(root, env):
    result = dict(env)
    paths = result.get('NLTK_DATA', str(root / '.venv/nltk_data')).split(os.pathsep)
    result['NLTK_DATA'] = os.pathsep.join(str(Path(p) if Path(p).is_absolute() else root / p)
                                        for p in paths if p)
    for name, default in [('NUMBA_CACHE_DIR', 'runtime/numba-cache')]:
        path = Path(result.get(name) or default)
        result[name] = str(path if path.is_absolute() else root / path)
    if result.get('PIPECAT_CONVERSATION_DIR'):
        path = Path(result['PIPECAT_CONVERSATION_DIR'])
        result['PIPECAT_CONVERSATION_DIR'] = str(path if path.is_absolute() else root / path)
    return result


_RUNTIME_PROBE = r'''
import importlib.metadata as metadata, json, os, re, sys
from pathlib import Path
lock=Path(sys.argv[1]).read_text()
if len(lock)>262144:raise ValueError('Oversized lock')
missing=[];mismatch=[]
for line in lock.splitlines():
    if not line.strip() or line.startswith('#'):continue
    match=re.fullmatch(r'([A-Za-z0-9_.-]+)(?:\[[^\]]+\])?==([^\s;]+)',line.strip())
    if not match:raise ValueError('Unsupported lock entry')
    name,expected=match.groups()
    try:actual=metadata.version(name)
    except metadata.PackageNotFoundError:missing.append(name);continue
    if actual!=expected:mismatch.append(name)
files=('collocations.tab','sent_starters.txt','abbrev_types.txt','ortho_context.tab')
nltk=any(all((Path(p)/'tokenizers/punkt_tab/english'/f).is_file() for f in files)
         for p in os.environ.get('NLTK_DATA','').split(os.pathsep) if p)
print(json.dumps(dict(version=list(sys.version_info[:2]),missing=missing,mismatch=mismatch,nltk=nltk)))
'''


def inspect_runtime(python, root, env):
    try:
        process = subprocess.run([python, '-c', _RUNTIME_PROBE, str(root / 'requirements.lock')],
                                 cwd=root, env=env, text=True, capture_output=True, timeout=20)
        if process.returncode or len(process.stdout) > 32768:
            raise ValueError('Runtime probe failed')
        data = json.loads(process.stdout)
        if (not isinstance(data, dict) or type(data.get('nltk')) is not bool
                or not isinstance(data.get('version'), list)
                or any(not isinstance(data.get(key), list) or
                       any(not isinstance(item, str) for item in data[key])
                       for key in ('missing', 'mismatch'))):
            raise ValueError('Invalid runtime probe')
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return [check('runtime', 'fail', '无法检查应用环境；先运行 bash install_demo.sh，或用 --python 指定应用解释器。')]
    ready_python = data['version'] == [3, 11]
    checks = [check('python', 'pass' if ready_python else 'fail',
                    'Python 3.11 已准备。' if ready_python else '应用锁定环境需要 Python 3.11。')]
    failed = len(data['missing']) + len(data['mismatch'])
    checks.append(check('dependencies', 'fail' if failed else 'pass',
                        f'有 {failed} 项应用依赖缺失或与锁文件不符；运行 bash install_demo.sh。'
                        if failed else '应用依赖与 requirements.lock 一致。'))
    checks.append(check('nltk', 'pass' if data['nltk'] else 'fail',
                        '分句资源已准备。' if data['nltk'] else '缺少分句资源；运行 bash install_demo.sh 或检查 NLTK_DATA。'))
    return checks


def port_available(port):
    try:
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', port))
        return True
    except OSError:
        return False


def display(report, as_json):
    if as_json:
        print(json.dumps(report, ensure_ascii=False))
        return
    labels = {'pass': '通过', 'warn': '提示', 'fail': '未通过'}
    for item in report['checks']:
        print(f"[{labels[item['status']]}] {item['name']}: {item['message']}")
        if item['status'] != 'pass' and item.get('help'):
            print('  说明：' + item['help'])
    print('启动前检查通过；提示项所述的模型效果仍需单独验证。' if report['ready'] else '请先处理未通过的项目；应用尚未启动。')


def main(argv=None, *, root=ROOT, environ=None):
    root = Path(root).absolute()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['check', 'run'])
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--backend', choices=['cascade', 'minicpm'])
    parser.add_argument('--profile')
    parser.add_argument('--require-vision', action='store_true')
    parser.add_argument('--python', dest='python_path')
    parser.add_argument('--port', type=int, default=18314)
    parser.add_argument('--json', action='store_true', help='Machine-readable check result; check only')
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('--port must be between 1 and 65535')
    if args.json and args.action != 'check':
        parser.error('--json is only available for check')
    try:
        env = resource_environment(root, build_environment(root, env_file=args.env_file, environ=environ))
        python = choose_python(root, args.python_path)
        report = inspect_deployment(root, env, backend=args.backend, profile=args.profile,
                                    require_vision=args.require_vision)
        report['checks'] = inspect_runtime(python, root, env) + report['checks']
        if args.action == 'run' and not port_available(args.port):
            report['checks'].append(check('port', 'fail', '应用端口已占用；请换用 --port，不会停止已有服务。'))
        report['ready'] = not any(c['required'] and c['status'] == 'fail' for c in report['checks'])
    except (OSError, ValueError):
        report = dict(ready=False, checks=[check('configuration', 'fail',
                      '配置无法读取或格式无效；检查 .env、runtime 配置文件和所选角色。')])
        display(report, args.json)
        return 2
    display(report, args.json)
    if not report['ready']:
        return 1
    if args.action == 'check':
        return 0
    env['PIPECAT_DIALOGUE_BACKEND'] = report['backend']
    env['PIPECAT_AVATAR_PROFILE'] = report['profile']
    env.setdefault('OMP_NUM_THREADS', '2')
    try:
        Path(env['NUMBA_CACHE_DIR']).mkdir(parents=True, exist_ok=True)
        print(f'打开 http://127.0.0.1:{args.port}/avatar；Ctrl+C 停止应用。', flush=True)
        os.execve(python, [python, str(root / 'bot.py'), '--host', '127.0.0.1',
                          '--port', str(args.port), '-t', 'webrtc'], env)
    except OSError:
        print('应用无法启动；检查解释器、工作目录与缓存目录权限。', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
