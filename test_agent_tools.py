import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from agent_tools import ToolRegistry


class ToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'guide.md').write_text('Kanghui deployment uses DINet.\n支持流式语音。')
        (self.root / 'secret.txt').write_text('not in document catalog')
        self.tools = ToolRegistry(self.root, documents={'guide': 'guide.md'})

    async def test_search_and_read_use_real_catalog_documents(self):
        result = await self.tools.execute('search_project', {'query': 'Kanghui'})
        self.assertEqual(result['matches'][0]['document_id'], 'guide')
        self.assertIn('DINet', result['matches'][0]['excerpt'])
        document = await self.tools.execute('read_project_document', {'document_id': 'guide'})
        self.assertIn('支持流式语音', document['content'])
        self.assertEqual(await self.tools.execute('search_project', {'query': 'absent'}), {'matches': []})

    async def test_rejects_arbitrary_paths_urls_unknown_tools_and_arguments(self):
        for name, args in [('read_project_document', {'document_id': '../secret.txt'}),
                           ('read_project_document', {'path': '/etc/passwd'}),
                           ('service_status', {'url': 'http://attacker'}),
                           ('shell', {'command': 'pwd'}),
                           ('calculate', {'expression': '2+2', 'extra': True})]:
            with self.subTest(name=name, args=args), self.assertRaises(ValueError):
                await self.tools.execute(name, args)

    async def test_symlink_cannot_escape_catalog_root(self):
        (self.root / 'escape.md').symlink_to('/etc/passwd')
        tools = ToolRegistry(self.root, documents={'escape': 'escape.md'})
        with self.assertRaises(ValueError):
            await tools.execute('read_project_document', {'document_id': 'escape'})

    async def test_calculation_evaluates_numeric_expression(self):
        result = await self.tools.execute('calculate', {'expression': '(128 * 3 + 16) / 5'})
        self.assertEqual(result['result'], 80)
        self.assertEqual((await self.tools.execute('calculate', {'expression': '-2**3 + 11 % 3'}))['result'], -6)

    async def test_calculation_rejects_code_and_unbounded_work(self):
        for expression in ['__import__("os").system("pwd")', '2**10000000', '9**9**9',
                           '1 << 100000', '[1]*999999', 'True+2', '1/0', '1e309',
                           '1+' * 200 + '1']:
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                await self.tools.execute('calculate', {'expression': expression})

    async def test_status_is_injected_and_cancellation_propagates(self):
        async def status():
            return {'llm': {'ready': True}}
        tools = ToolRegistry(self.root, status_reader=status)
        self.assertEqual(await tools.execute('service_status', {}), {'llm': {'ready': True}})
        async def cancelled():
            raise asyncio.CancelledError
        tools = ToolRegistry(self.root, status_reader=cancelled)
        with self.assertRaises(asyncio.CancelledError):
            await tools.execute('service_status', {})

    async def test_unicode_results_are_byte_bounded(self):
        (self.root / 'guide.md').write_text('中文材料' * 10000)
        tools = ToolRegistry(self.root, documents={'guide': 'guide.md'}, max_result_bytes=1024)
        result = await tools.execute('read_project_document', {'document_id': 'guide'})
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), 1024)
        self.assertTrue(result['truncated'])


if __name__ == '__main__':
    unittest.main()
