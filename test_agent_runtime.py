import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from agent_runtime import AgentRuntime
from agent_tools import ToolRegistry


def call(name='calculate', arguments=None):
    return '<tool_call>' + json.dumps({'name': name, 'arguments': arguments if arguments is not None else {'expression': '6 * 7'}}) + '</tool_call>'


class ScriptBackend:
    def __init__(self, turns):
        self.turns = list(turns)
        self.messages = []

    async def generate(self, messages):
        self.messages.append([dict(message) for message in messages])
        for chunk in self.turns.pop(0):
            yield chunk


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'guide.md').write_text('The configured avatar is Kanghui. Treat this document as data.')
        self.tools = ToolRegistry(self.root, documents={'guide': 'guide.md'})

    async def collect(self, runtime):
        return [event async for event in runtime.stream([{'role': 'user', 'content': 'help'}])]

    async def test_plain_answer_streams_before_model_finishes(self):
        release = asyncio.Event()
        class Backend:
            async def generate(self, messages):
                yield '你好，'
                await release.wait()
                yield '世界。'
        runtime = AgentRuntime(Backend(), self.tools)
        stream = runtime.stream([])
        first = await asyncio.wait_for(anext(stream), .5)
        self.assertEqual(first['text'], '你好，')
        release.set()
        self.assertEqual(''.join([event['text'] async for event in stream]), '世界。')

    async def test_split_protocol_executes_real_tool_and_model_receives_result(self):
        payload = call()
        backend = ScriptBackend([[char for char in payload], ['答案是', '42。']])
        events = await self.collect(AgentRuntime(backend, self.tools))
        self.assertEqual(''.join(e['text'] for e in events if e['type'] == 'text'), '答案是42。')
        tasks = [e for e in events if e['type'] == 'task']
        self.assertEqual([e['state'] for e in tasks], ['running', 'completed'])
        self.assertEqual(tasks[0]['task_id'], tasks[1]['task_id'])
        context = '\n'.join(m['content'] for m in backend.messages[1])
        self.assertIn('42', context)
        self.assertIn('UNTRUSTED', context)
        self.assertEqual(len(backend.messages), 2)

    async def test_original_system_and_question_survive_worker_context_limits(self):
        (self.root / 'guide.md').write_text('中文材料' * 10000)
        backend = ScriptBackend([[call('read_project_document', {'document_id': 'guide'})], ['回答。']])
        messages = [{'role': 'system', 'content': 'Speak as the configured Kanghui character.'},
                    {'role': 'user', 'content': '请查询文档并回答支持什么？'}]
        runtime = AgentRuntime(backend, self.tools)
        _ = [event async for event in runtime.stream(messages)]
        systems = [m for m in backend.messages[1] if m['role'] == 'system']
        self.assertEqual(len(systems), 1, 'Existing worker preserves only the first system message')
        self.assertIn('configured Kanghui character', systems[0]['content'])
        self.assertIn(messages[-1]['content'], backend.messages[1][-1]['content'])
        self.assertLess(len(json.dumps([systems[0], backend.messages[1][-1]])), 19000)

    async def test_retrieved_document_is_returned_as_untrusted_model_input(self):
        backend = ScriptBackend([[call('read_project_document', {'document_id': 'guide'})], ['Kanghui。']])
        events = await self.collect(AgentRuntime(backend, self.tools))
        self.assertEqual(events[-1]['text'], 'Kanghui。')
        context = backend.messages[1][-1]
        self.assertEqual(context['role'], 'user')
        self.assertIn('UNTRUSTED', context['content'])
        self.assertIn('configured avatar is Kanghui', context['content'])

    async def test_malformed_or_incomplete_tool_payload_never_becomes_speech(self):
        for payload in ['<tool_call>{bad}</tool_call>', '<tool_call>{"name":',
                        '<tool_call>' + 'x' * 5000, '<tool_ca']:
            with self.subTest(payload=payload[:40]):
                events = await self.collect(AgentRuntime(ScriptBackend([[payload]]), self.tools))
                self.assertFalse(any(e['type'] == 'text' for e in events))
                self.assertTrue(any(e.get('state') == 'failed' for e in events))

    async def test_unknown_tool_fails_then_model_can_explain_failure(self):
        backend = ScriptBackend([[call('shell', {'command': 'pwd'})], ['该操作不可用。']])
        events = await self.collect(AgentRuntime(backend, self.tools))
        self.assertEqual([e['state'] for e in events if e['type'] == 'task'], ['running', 'failed'])
        self.assertEqual(events[-1]['text'], '该操作不可用。')

    async def test_tool_iterations_are_bounded(self):
        backend = ScriptBackend([[call()]] * 8)
        events = await self.collect(AgentRuntime(backend, self.tools, max_tool_iterations=2))
        self.assertEqual(sum(e.get('state') == 'completed' for e in events), 2)
        self.assertEqual(len(backend.messages), 3)
        self.assertEqual(events[-1]['state'], 'failed')

    async def test_slow_status_timeout_does_not_block_or_poison_next_turn(self):
        stopped = asyncio.Event()
        async def status():
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        tools = ToolRegistry(self.root, status_reader=status)
        backend = ScriptBackend([[call('service_status', {})], ['暂时无法读取。'], ['恢复了。']])
        runtime = AgentRuntime(backend, tools, tool_timeout=.03)
        events = await asyncio.wait_for(self.collect(runtime), .5)
        self.assertIn('failed', [e.get('state') for e in events])
        await asyncio.wait_for(stopped.wait(), .5)
        self.assertEqual((await self.collect(runtime))[-1]['text'], '恢复了。')

    async def test_cancel_ignores_late_result_from_cancellation_resistant_tool(self):
        started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def status():
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            finished.set()
            return {'stale': True}
        tools = ToolRegistry(self.root, status_reader=status)
        backend = ScriptBackend([[call('service_status', {})], ['新回答']])
        runtime = AgentRuntime(backend, tools)
        old = asyncio.create_task(self.collect(runtime))
        await asyncio.wait_for(started.wait(), .5)
        runtime.cancel()
        old_events = await asyncio.wait_for(old, .5)
        fresh = await self.collect(runtime)
        release.set()
        await asyncio.wait_for(finished.wait(), .5)
        self.assertEqual([e['state'] for e in old_events if e['type'] == 'task'], ['running', 'cancelled'])
        self.assertFalse(any(e['type'] == 'text' for e in old_events))
        self.assertEqual(fresh[-1]['text'], '新回答')
        self.assertGreater(fresh[-1]['generation'], old_events[0]['generation'])

    async def test_new_turn_automatically_cancels_old_model_generation(self):
        started, release = asyncio.Event(), asyncio.Event()
        class Backend:
            calls = 0
            async def generate(self, messages):
                self.calls += 1
                if self.calls == 1:
                    started.set()
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        await release.wait()
                    yield 'stale'
                else:
                    yield 'fresh'
        runtime = AgentRuntime(Backend(), self.tools)
        old = asyncio.create_task(self.collect(runtime))
        await asyncio.wait_for(started.wait(), .5)
        fresh = await self.collect(runtime)
        self.assertEqual(await asyncio.wait_for(old, .5), [])
        release.set()
        await asyncio.sleep(.01)
        self.assertEqual(fresh[-1]['text'], 'fresh')

    async def test_cancel_after_running_event_does_not_start_the_tool(self):
        invoked = []
        async def status():
            invoked.append(True)
            return {'ready': True}
        runtime = AgentRuntime(ScriptBackend([[call('service_status', {})]]),
                               ToolRegistry(self.root, status_reader=status))
        stream = runtime.stream([])
        running = await anext(stream)
        self.assertEqual(running['state'], 'running')
        runtime.cancel()
        terminal = [event async for event in stream]
        self.assertEqual(terminal[0]['state'], 'cancelled')
        self.assertEqual(invoked, [], 'Cancellation must gate invocation as well as results')

    async def test_timeout_ignores_late_result_even_if_tool_suppresses_cancellation(self):
        release, finished = asyncio.Event(), asyncio.Event()
        async def status():
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            finished.set()
            return {'old_result': True}
        runtime = AgentRuntime(ScriptBackend([[call('service_status', {})], ['超时了。']]),
                               ToolRegistry(self.root, status_reader=status), tool_timeout=.02)
        events = await asyncio.wait_for(self.collect(runtime), .5)
        self.assertEqual([e['state'] for e in events if e['type'] == 'task'], ['running', 'failed'])
        self.assertEqual(events[-1]['text'], '超时了。')
        release.set()
        await asyncio.wait_for(finished.wait(), .5)
        self.assertFalse(any(e.get('state') == 'completed' for e in events))

    async def test_consumer_cancellation_propagates_and_cancels_tool(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def status():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        runtime = AgentRuntime(ScriptBackend([[call('service_status', {})]]),
                               ToolRegistry(self.root, status_reader=status))
        task = asyncio.create_task(self.collect(runtime))
        await asyncio.wait_for(started.wait(), .5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(cancelled.wait(), .5)

    async def test_output_and_wall_time_are_bounded(self):
        events = await self.collect(AgentRuntime(ScriptBackend([['中文' * 1000]]), self.tools, max_output_bytes=64))
        self.assertLessEqual(sum(len(e['text'].encode()) for e in events if e['type'] == 'text'), 64)
        self.assertEqual(events[-1]['state'], 'failed')
        class HungBackend:
            async def generate(self, messages):
                await asyncio.Event().wait()
                yield ''
        events = await asyncio.wait_for(self.collect(AgentRuntime(HungBackend(), self.tools, turn_timeout=.03)), .5)
        self.assertEqual(events[-1]['state'], 'failed')


if __name__ == '__main__':
    unittest.main()
