"""Deployment-owned bounded lookahead for the audited native 200ms consumer."""
import ast
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path


@dataclass(frozen=True)
class Policy:
    lookahead_seconds: float
    sha256: str


def load_policy(path):
    # Read one snapshot; deployments replace the small file atomically.
    with Path(path).open('rb') as source:
        raw = source.read(1025)
    if len(raw) > 1024:
        raise ValueError('Pacing configuration exceeds 1024 bytes')
    value = json.loads(raw)
    if (not isinstance(value, dict) or set(value) != {'version', 'lookahead_seconds'}
            or type(value['version']) is not int or value['version'] != 1
            or type(value['lookahead_seconds']) not in (int, float)
            or not math.isfinite(value['lookahead_seconds'])
            or not 0 <= value['lookahead_seconds'] <= .8):
        raise ValueError('Expected version1 and finite lookahead_seconds in0..0.8')
    return Policy(float(value['lookahead_seconds']), hashlib.sha256(raw).hexdigest())


def paced_delay(started, blocks, now, lookahead_seconds):
    # Original caller retains startup burst and positive-delay sleep condition.
    return started + blocks * .2 - lookahead_seconds - now


def rewrite_consumer(source, native_globals):
    """Called only after the launcher validates the entire native source file."""
    needle = 'start_time+audio_num*0.2-time.perf_counter()'
    if source.count(needle) != 1:
        raise ValueError('Unexpected native pacing expression')
    tree = ast.parse(source)
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.AsyncFunctionDef):
        raise ValueError('Expected one native async consumer')
    changed = source.replace(needle,
        '_voxastage_delay(start_time, audio_num, time.perf_counter(), self._voxastage_lookahead)')
    namespace = dict(native_globals, _voxastage_delay=paced_delay)
    exec(compile(changed, '<voxastage-native-consumer>', 'exec'), namespace)
    return namespace[tree.body[0].name]
