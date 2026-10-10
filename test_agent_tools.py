import asyncio
import json
import random
import tempfile
import unittest
from pathlib import Path

from agent_tools import ToolRegistry, _chinese_excerpt


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

    async def test_natural_chinese_question_finds_separated_document_terms(self):
        (self.root / 'guide.md').write_text('每张图片记录采集时间。上下文最多保留两张图片及对应历史。')
        query = '视觉图片的采集时间和上下文历史如何管理？'
        result = await self.tools.execute('search_project', {'query': query})
        self.assertEqual(result['matches'][0]['document_id'], 'guide')
        self.assertIn('采集时间', result['matches'][0]['excerpt'])

    async def test_chinese_fallback_requires_two_distinct_bigrams(self):
        (self.root / 'guide.md').write_text('图片' * 50 + '。此处无其他查询内容。')
        result = await self.tools.execute('search_project', {'query': '图片采集历史管理'})
        self.assertEqual(result['matches'], [])

    async def test_exact_match_precedes_chinese_fallback(self):
        (self.root / 'guide.md').write_text('采集时间管理')
        (self.root / 'other.md').write_text('采集和时间以及管理' * 20)
        tools = ToolRegistry(self.root, documents={'guide': 'guide.md', 'other': 'other.md'})
        result = await tools.execute('search_project', {'query': '采集时间管理'})
        self.assertEqual([x['document_id'] for x in result['matches']], ['guide'])

    async def test_chinese_fallback_excerpt_covers_dense_later_evidence(self):
        (self.root / 'guide.md').write_text('视觉功能介绍。' + '无关介绍。' * 150
            + '图片采集时间记录为 captured_at_ms，上下文保留两张图片及对应历史。')
        result = await self.tools.execute('search_project', {
            'query': '视觉图片的采集时间和上下文历史如何管理？'})
        excerpt = result['matches'][0]['excerpt']
        self.assertIn('captured_at_ms', excerpt)
        self.assertIn('两张图片', excerpt)
        self.assertLessEqual(len(excerpt), 600)

    async def test_chinese_excerpt_keeps_distant_terms_that_fit_one_window(self):
        (self.root / 'guide.md').write_text('x' * 100 + '图片' + 'x' * 548 + '历史' + 'x' * 100)
        result = await self.tools.execute('search_project', {'query': '图片的历史如何管理？'})
        excerpt = result['matches'][0]['excerpt']
        self.assertIn('图片', excerpt)
        self.assertIn('历史', excerpt)
        self.assertLessEqual(len(excerpt), 600)

    def test_chinese_excerpt_matches_exhaustive_window_coverage(self):
        rng = random.Random(20261010)
        pairs = {'图片', '历史', '时间'}
        for _ in range(25):
            content = ''.join(rng.choice(['图片', '历史', '时间', 'x' * 7]) for _ in range(35))
            for size in (8, 24, 60):
                expected = max(sum(p in content[i:i + size] for p in pairs)
                               for i in range(len(content)))
                excerpt = _chinese_excerpt(content, pairs, size)
                self.assertEqual(sum(p in excerpt for p in pairs), expected)
                self.assertLessEqual(len(excerpt), size)
                self.assertIn(excerpt, content)

    async def test_exact_search_retains_document_id_tie_break(self):
        (self.root / 'guide.md').write_text('exact')
        (self.root / 'other.md').write_text('exact')
        tools = ToolRegistry(self.root, documents={'z': 'guide.md', 'a': 'other.md'})
        result = await tools.execute('search_project', {'query': 'exact', 'limit': 1})
        self.assertEqual(result['matches'][0]['document_id'], 'a')

    async def test_chinese_fallback_keeps_latin_identifier_whole(self):
        (self.root / 'guide.md').write_text('supports MiniCPM and 图片采集')
        result = await self.tools.execute('search_project', {'query': 'unknownlatinidentifier'})
        self.assertEqual(result['matches'], [])
        result = await self.tools.execute('search_project', {'query': '图片、采集怎么处理？', 'limit': 1})
        self.assertEqual(len(result['matches']), 1)

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
