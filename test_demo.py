import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import demo

class DemoTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        (self.root/'bot.py').write_text('')
        (self.root/'requirements.lock').write_text('pipecat-ai==1.10.0\n')
    def report(self,ready=True):
        return dict(ready=ready,backend='minicpm',profile='portrait',provider='flashhead',checks=[
            dict(name='minicpm',status='pass' if ready else 'fail',required=True,message='就绪' if ready else '未就绪',help='omni-server/README.md')])
    def invoke(self,args,report=None,runtime=None):
        stream=io.StringIO()
        with patch('demo.build_environment',return_value={'PATH':'/usr/bin','PIPECAT_MINICPM_URL':'http://private/secret','PIPECAT_LLM_API_KEY':'do-not-display'}),patch('demo.inspect_deployment',return_value=report or self.report()),patch('demo.inspect_runtime',return_value=runtime or [demo.check('runtime','pass','已准备')]),patch('demo.port_available',return_value=True),patch('demo.os.execve') as execute,contextlib.redirect_stdout(stream):
            code=demo.main(args,root=self.root)
        return code,stream.getvalue(),execute
    def test_failed_required_check_never_executes_or_creates_runtime(self):
        code,out,execute=self.invoke(['run'],self.report(False))
        self.assertEqual(code,1);execute.assert_not_called()
        self.assertFalse((self.root/'runtime').exists())
        self.assertNotIn('do-not-display',out);self.assertNotIn('private/secret',out)
    def test_json_report_and_exit_match_dependency_failure(self):
        code,out,execute=self.invoke(['check','--json'],runtime=[demo.check('runtime','fail','缺少依赖')])
        self.assertEqual(code,1);self.assertFalse(json.loads(out)['ready']);execute.assert_not_called()
    def test_run_preserves_secrets_in_child_only_and_applies_actual_selection(self):
        code,out,execute=self.invoke(['run','--python','my-env/bin/python','--port','18490'])
        self.assertEqual(code,0);path,argv,env=execute.call_args.args
        self.assertEqual(path,str(self.root/'my-env/bin/python'))
        self.assertEqual(argv,[path,str(self.root/'bot.py'),'--host','127.0.0.1','--port','18490','-t','webrtc'])
        self.assertEqual(env['PIPECAT_DIALOGUE_BACKEND'],'minicpm')
        self.assertEqual(env['PIPECAT_AVATAR_PROFILE'],'portrait')
        self.assertEqual(env['PIPECAT_LLM_API_KEY'],'do-not-display')
        self.assertNotIn('do-not-display',out)
        self.assertEqual(env['NUMBA_CACHE_DIR'],str(self.root/'runtime/numba-cache'))
    def test_occupied_port_refuses_start(self):
        with patch('demo.build_environment',return_value={}),patch('demo.inspect_deployment',return_value=self.report()),patch('demo.inspect_runtime',return_value=[]),patch('demo.port_available',return_value=False),patch('demo.os.execve') as execute,contextlib.redirect_stdout(io.StringIO()):
            code=demo.main(['run'],root=self.root)
        self.assertEqual(code,1);execute.assert_not_called()
    def test_explicit_missing_env_is_clean_error_without_echo(self):
        with patch('demo.build_environment',side_effect=ValueError('private-secret')),contextlib.redirect_stdout(io.StringIO()) as out:
            code=demo.main(['check','--json'],root=self.root)
        self.assertEqual(code,2);self.assertNotIn('private-secret',out.getvalue())
        self.assertFalse(json.loads(out.getvalue())['ready'])
    def test_interpreter_keeps_venv_symlink_path(self):
        env=self.root/'.venv/bin';env.mkdir(parents=True)
        (env/'python').symlink_to('/usr/bin/python3')
        self.assertEqual(demo.choose_python(self.root,None),str(env/'python'))
    def test_relative_resource_directories_use_project_root(self):
        env=demo.resource_environment(self.root,{'NLTK_DATA':'assets/nltk','NUMBA_CACHE_DIR':'cache/numba'})
        self.assertEqual(env['NLTK_DATA'],str(self.root/'assets/nltk'))
        self.assertEqual(env['NUMBA_CACHE_DIR'],str(self.root/'cache/numba'))
    def test_runtime_reports_mismatch_and_missing_nltk_without_raw_child_output(self):
        from types import SimpleNamespace
        response=dict(version=[3,11],missing=['pipecat-ai'],mismatch=[],nltk=False)
        with patch('demo.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(response),stderr='secret')):
            checks=demo.inspect_runtime('/python',self.root,{})
        self.assertTrue(any(c['status']=='fail' and c['name']=='dependencies' for c in checks))
        self.assertTrue(any(c['status']=='fail' and c['name']=='nltk' for c in checks))
        self.assertNotIn('secret',json.dumps(checks))
    def test_runtime_never_prints_arbitrary_interpreter_stdout_on_failure(self):
        from types import SimpleNamespace
        with patch('demo.subprocess.run',return_value=SimpleNamespace(returncode=1,stdout='secret-key',stderr='secret-url')):
            result=demo.inspect_runtime('/python',self.root,{})
        self.assertEqual(result[0]['status'],'fail')
        self.assertNotIn('secret',json.dumps(result))
