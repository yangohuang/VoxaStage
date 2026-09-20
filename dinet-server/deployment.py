"""Deployment overrides for one audited installed service; no native code bundled."""
import hashlib
import inspect
import json
from pathlib import Path
import re

FACTORY_SHA256 = 'e507716bdc9c1062a1b11c258397d2df58f0a55e2fcffcfd211465d684599233'
PROXY_SHA256 = 'e6b76ee2aca62a94d40c01d37704d920468548a7f2bcb77f8b305cea4818289f'


def load_character(path):
    with Path(path).open('rb') as source:
        raw = source.read(8193)
    if len(raw) > 8192:
        raise ValueError('Character configuration exceeds 8192 bytes')
    spec = json.loads(raw)
    keys = {'version', 'id', 'tensor_width', 'model', 'action', 'reference'}
    if (not isinstance(spec, dict) or set(spec) != keys
            or type(spec['version']) is not int or spec['version'] != 1
            or type(spec['tensor_width']) is not int or spec['tensor_width'] != 256
            or not isinstance(spec['id'], str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', spec['id'])):
        raise ValueError('Invalid character configuration; only audited 256 mode is supported')
    for key in ('model', 'action', 'reference'):
        value = spec[key]
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValueError('Character paths must be absolute: ' + key)
        path = Path(value)
        if not (path.is_file() if key == 'model' else path.is_dir()):
            raise ValueError('Missing character resource or incorrect path type: ' + key)
    return spec


def rewrite_factory(source, namespace):
    if hashlib.sha256(source.encode()).hexdigest() != FACTORY_SHA256:
        raise ValueError('Native model factory differs from audited source')
    token = '"dinet_tensor_width":128'
    if source.count(token) != 1:
        raise ValueError('Ambiguous model tensor configuration')
    scope = dict(namespace)
    exec(compile(source.replace(token, '"dinet_tensor_width":256'),
                 '<avatar-character-factory>', 'exec'), scope)
    return scope['create_stream_generator']


def apply_character(service, creator, root, spec):
    # Validate/compile before changing the singleton or replacing its factory.
    factory = rewrite_factory(inspect.getsource(creator.create_stream_generator), vars(creator))
    settings = service.get_settings(str(root / 'etc/config.yaml'))
    settings.resource_mode = 'fixed'
    settings.fixed_character_id = spec['id']
    settings.fixed_video_model_path = spec['model']
    settings.fixed_video_action_path = spec['action']
    settings.fixed_video_ref_path = spec['reference']
    creator.create_stream_generator = factory


def rewrite_proxy(raw):
    if hashlib.sha256(raw).hexdigest() != PROXY_SHA256:
        raise ValueError('Native proxy differs from audited source')
    source = raw.decode()
    replacements = (
        ('rec_queue.put_nowait(message)', 'await rec_queue.put(message)', 2),
        ('                await asyncio.gather(*tasks)', '''                try:
                    await asyncio.gather(*tasks)
                finally:
                    stop_event.set()
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)''', 1),
        ('websockets.connect(worker_ws_url, compression=None)',
         'websockets.connect(worker_ws_url, compression=None, close_timeout=.5)', 1),
    )
    for before, after, count in replacements:
        if source.count(before) != count:
            raise ValueError('Unexpected native proxy structure')
        source = source.replace(before, after)
    return source
