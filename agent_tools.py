"""Small deployment-owned, read-only tool catalog for the cascade agent."""

import ast
import json
import math
import operator
import re
from pathlib import Path


DEFAULT_DOCUMENTS = {
    'readme': 'README.md',
    'acceptance': 'docs/ACCEPTANCE.md',
    'demo-walkthrough': 'docs/DEMO-WALKTHROUGH.md',
    'source-install': 'docs/SOURCE-INSTALL-VALIDATION.md',
    'visual-interaction': 'docs/VISUAL-INTERACTION.md',
    'avatar-output': 'docs/AVATAR-OUTPUT-BASELINE-2026-09-19.md',
    'dinet-attribution': 'docs/DINET-DELIVERY-ATTRIBUTION.md',
}


def bounded_result(result, max_bytes):
    """Return valid JSON data even when an external reader returns a large result."""
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(encoded) <= max_bytes:
        return result
    # Escaping the serialized prefix can grow it. Trim against the final JSON bytes.
    prefix = encoded[:max_bytes].decode('utf-8', errors='ignore')
    while True:
        bounded = {'truncated': True, 'content': prefix}
        size = len(json.dumps(bounded, ensure_ascii=False).encode('utf-8'))
        if size <= max_bytes:
            return bounded
        prefix = prefix[:max(0, len(prefix) - max(1, (size - max_bytes + 1) // 2))]


def _chinese_excerpt(content, pairs, size=600):
    """Choose a bounded window with most distinct query pairs in linear time."""
    pairs = set(pairs)
    hits = [(i, content[i:i + 2]) for i in range(len(content) - 1)
            if content[i:i + 2] in pairs]
    counts = {}
    left = right = best_start = best_count = 0
    for anchor, _ in hits:
        start = anchor
        while right < len(hits) and hits[right][0] + 2 <= start + size:
            term = hits[right][1]
            counts[term] = counts.get(term, 0) + 1
            right += 1
        while left < right and hits[left][0] < start:
            term = hits[left][1]
            counts[term] -= 1
            if not counts[term]:
                del counts[term]
            left += 1
        if len(counts) > best_count:
            # Add context only from spare space, preserving the last covered pair.
            spare = size - (hits[right - 1][0] + 2 - start)
            best_count = len(counts)
            best_start = max(0, start - min(100, spare))
    return content[best_start:best_start + size]


class ToolRegistry:
    def __init__(self, project_root, *, documents=None, status_reader=None, max_result_bytes=8192):
        self.project_root = Path(project_root).resolve()
        self.documents = dict(DEFAULT_DOCUMENTS if documents is None else documents)
        self.status_reader = status_reader
        if not 128 <= max_result_bytes <= 65536:
            raise ValueError('Tool result limit must be between 128 and 65536 bytes')
        self.max_result_bytes = max_result_bytes
        if len(self.documents) > 64:
            raise ValueError('Document catalog is too large')
        for document_id, path in self.documents.items():
            if not isinstance(document_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', document_id):
                raise ValueError('Invalid document catalog ID')
            relative = Path(path)
            if relative.is_absolute() or '..' in relative.parts or relative.suffix.lower() != '.md':
                raise ValueError('Catalog documents must be relative markdown paths')

    def instructions(self):
        catalog = ', '.join(self.documents)
        return (
            'Available read-only tools (use only these exact names and arguments):\n'
            'search_project: {"query":"search terms","limit":5}; searches project documents.\n'
            'read_project_document: {"document_id":"catalog ID"}; reads a project document.\n'
            'calculate: {"expression":"(128 * 3 + 16) / 5"}; arithmetic only.\n'
            'service_status: {}; reads configured service status.\n'
            f'Document catalog IDs: {catalog}.'
        )

    def _document(self, document_id):
        if document_id not in self.documents:
            raise ValueError('Unknown document ID; use the configured catalog')
        path = (self.project_root / self.documents[document_id]).resolve()
        if not path.is_relative_to(self.project_root) or not path.is_file():
            raise ValueError('Document unavailable or outside configured root')
        with path.open('rb') as handle:
            raw = handle.read(65537)
        return raw[:65536].decode('utf-8', errors='replace'), len(raw) > 65536

    async def execute(self, name, arguments):
        schemas = {
            'search_project': ({'query'}, {'query', 'limit'}),
            'read_project_document': ({'document_id'}, {'document_id'}),
            'calculate': ({'expression'}, {'expression'}),
            'service_status': (set(), set()),
        }
        if not isinstance(name, str) or name not in schemas:
            raise ValueError('Unknown tool')
        if not isinstance(arguments, dict):
            raise ValueError('Tool arguments must be an object')
        required, allowed = schemas[name]
        if not required <= arguments.keys() or arguments.keys() - allowed:
            raise ValueError('Invalid tool arguments')
        if name == 'calculate':
            result = {'expression': arguments['expression'], 'result': self._calculate(arguments['expression'])}
        elif name == 'read_project_document':
            document_id = arguments['document_id']
            if not isinstance(document_id, str):
                raise ValueError('Document ID must be a string')
            content, truncated = self._document(document_id)
            result = {'document_id': document_id, 'content': content, 'truncated': truncated}
        elif name == 'search_project':
            query, limit = arguments['query'], arguments.get('limit', 5)
            if not isinstance(query, str) or not 1 <= len(query.strip()) <= 256:
                raise ValueError('Search query must contain 1 to 256 characters')
            if type(limit) is not int or not 1 <= limit <= 8:
                raise ValueError('Search limit must be between 1 and 8')
            terms = query.casefold().split()
            documents = []
            for document_id in self.documents:
                try:
                    content, _ = self._document(document_id)
                except (ValueError, OSError):
                    continue
                documents.append((document_id, content, content.casefold()))

            def match(needles, *, fallback=False):
                found = []
                for document_id, content, folded in documents:
                    hits = [term for term in needles if term in folded]
                    # A lone common pair is insufficient evidence for a sentence query.
                    if not hits or (fallback and len(hits) < 2):
                        continue
                    score = len(hits) if fallback else sum(folded.count(term) for term in needles)
                    offset = min(folded.find(term) for term in hits)
                    found.append({'document_id': document_id, 'score': score,
                                  'excerpt': (_chinese_excerpt(content, needles) if fallback
                                              else content[max(0, offset - 100):offset + 500])})
                found.sort(key=lambda item: (-item['score'], item['document_id']))
                return found

            matches = match(terms)
            if not matches:
                # Keep exact/space-delimited search first; do not fragment Latin identifiers.
                pairs = sorted({run[i:i + 2] for run in re.findall(r'[\u3400-\u9fff]+', query)
                                for i in range(len(run) - 1)})
                matches = match(pairs, fallback=True)
            result = {'matches': matches[:limit]}
        else:
            if self.status_reader is None:
                raise ValueError('Service status reader is not configured')
            result = await self.status_reader()
        return bounded_result(result, self.max_result_bytes)

    @staticmethod
    def _calculate(expression):
        if not isinstance(expression, str) or not 1 <= len(expression) <= 256:
            raise ValueError('Arithmetic expression must contain 1 to 256 characters')
        binary = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
                  ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
                  ast.Mod: operator.mod, ast.Pow: operator.pow}
        unary = {ast.UAdd: operator.pos, ast.USub: operator.neg}
        try:
            tree = ast.parse(expression.strip(), mode='eval')
            if sum(1 for _ in ast.walk(tree)) > 64:
                raise ValueError('Arithmetic expression is too complex')

            def evaluate(node):
                if isinstance(node, ast.Constant) and type(node.value) in (int, float):
                    value = node.value
                elif isinstance(node, ast.UnaryOp) and type(node.op) in unary:
                    value = unary[type(node.op)](evaluate(node.operand))
                elif isinstance(node, ast.BinOp) and type(node.op) in binary:
                    left, right = evaluate(node.left), evaluate(node.right)
                    if isinstance(node.op, ast.Pow) and (abs(right) > 100 or abs(left) > 1e12):
                        raise ValueError('Exponent exceeds arithmetic limits')
                    value = binary[type(node.op)](left, right)
                else:
                    raise ValueError('Only numeric arithmetic is allowed')
                if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1e100:
                    raise ValueError('Arithmetic result exceeds limits')
                return value

            return evaluate(tree.body)
        except (SyntaxError, ArithmeticError, TypeError, RecursionError) as exc:
            raise ValueError('Invalid or out-of-range arithmetic expression') from exc
