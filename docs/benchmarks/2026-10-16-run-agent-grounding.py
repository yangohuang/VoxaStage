"""Probe current text Agent code; does not run browser, ASR, TTS or avatar media."""
import argparse
import ast
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import httpx
import backend as backend_module
from backend import LocalBackend, bounded_messages
from agent_runtime import AgentRuntime
from agent_tools import ToolRegistry, DEFAULT_DOCUMENTS


def system_literal(path):
    value = None
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'SYSTEM' for t in node.targets):
            value = ast.literal_eval(node.value)
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) and node.target.id == 'SYSTEM':
            assert isinstance(node.op, ast.Add)
            value += ast.literal_eval(node.value)
    assert isinstance(value, str) and value
    return value


async def run(output):
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x') as file:
        file.write('{}\n')
    manifest = Path(__file__).with_name('2026-10-16-agent-grounding-input.json')
    inputs = json.loads(manifest.read_text())
    system = system_literal(ROOT / 'bot.py')
    sources = ['bot.py', 'backend.py', 'agent_runtime.py', 'agent_tools.py'] + list(DEFAULT_DOCUMENTS.values())
    result = {'status': 'running', 'manifest_sha256': hashlib.sha256(manifest.read_bytes()).hexdigest(),
              'sources': {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in sources},
              'system_prompt': system, 'rows': [], 'scope': inputs['scope']}

    def save():
        temporary = output.with_suffix('.tmp')
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        temporary.replace(output)

    async with httpx.AsyncClient(trust_env=False, timeout=120) as client:
        async def idle():
            app = await client.get('http://127.0.0.1:18314/health')
            llm = await client.get('http://127.0.0.1:18311/health')
            app.raise_for_status(); llm.raise_for_status()
            a, m = app.json(), llm.json()
            assert a.get('active_sessions') == 0 and m.get('ready') and not m.get('busy')
            return {k: (Path(str(m[k])).name if k == 'model' else m[k]) for k in ('model', 'device', 'quantization') if k in m}

        class ObservedBackend(LocalBackend):
            async def generate(self, messages):
                trace = {'messages': bounded_messages(messages), 'max_new_tokens': 256, 'chunks': [], 'text': '', 'complete': False}
                row['model_calls'].append(trace)
                start = time.monotonic()
                try:
                    async for text in super().generate(messages):
                        trace['chunks'].append({'text': text, 'elapsed_s': time.monotonic() - start})
                        trace['text'] += text
                        yield text
                    trace['complete'] = True
                finally:
                    trace['elapsed_s'] = time.monotonic() - start
                    save()

        class ObservedTools(ToolRegistry):
            async def execute(self, name, arguments):
                trace = {'name': name, 'arguments': arguments, 'result': None}
                row['tools'].append(trace)
                try:
                    trace['result'] = await super().execute(name, arguments)
                    return trace['result']
                finally:
                    save()

        try:
            result['model'] = await idle()
            for case in inputs['cases']:
                assert await idle() == result['model']
                row = {'case': case['id'], 'text': '', 'events': [], 'tools': [], 'model_calls': [], 'elapsed_s': None}
                result['rows'].append(row)
                backend = ObservedBackend(client)
                assert backend.llm_api is None and backend_module.LLM_URL == 'http://127.0.0.1:18311'
                tools = ObservedTools(ROOT)
                runtime = AgentRuntime(backend, tools)
                messages = [{'role': 'system', 'content': system}] + case['messages']
                start = time.monotonic()
                try:
                    async for event in runtime.stream(messages):
                        row['events'].append(event)
                        if event['type'] == 'text':
                            row['text'] += event['text']
                    row['elapsed_s'] = time.monotonic() - start
                    save()
                finally:
                    runtime.cancel()
                print(json.dumps({'case': row['case'], 'text': row['text'], 'tools': [x['name'] for x in row['tools']], 'calls': len(row['model_calls'])}, ensure_ascii=False), flush=True)
                if any(e.get('state') == 'failed' for e in row['events']):
                    result['has_failed_tasks'] = True
            result['model_after'] = await idle()
            assert result['model_after'] == result['model']
            result['status'] = 'complete'
        except BaseException as exc:
            result['status'] = 'stopped'
            result['error_type'] = type(exc).__name__
            raise
        finally:
            save()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    asyncio.run(run(parser.parse_args().output))
