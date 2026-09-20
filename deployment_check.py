"""Bounded deployment diagnostics; no model imports or inference requests.

Injected request_json(url, *, method, timeout) returns a JSON object.
Injected tcp_check(host, port, *, timeout) returns a reachability boolean.
All public report messages are fixed text: worker responses and exceptions are
never copied into a report.
"""
import http.client
import json
import math
import os
from pathlib import Path
import re
import socket
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from api_backends import configured_api
from avatar_profiles import ProfileRegistry, _backend_url
from avatar_providers import ProviderRegistry
from dialogue_backends import DialogueRegistry
from turn_settings import TurnSettings

ENV_LIMIT = 64 * 1024
RESPONSE_LIMIT = 256 * 1024
_ENV_KEY = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')


def _read_text(path, limit):
    with path.open('rb') as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError('Configuration file exceeds its size limit')
    return data.decode('utf-8')


def build_environment(root, env_file=None, environ=None):
    """Merge persisted settings, literal dotenv and process values, in that order.

    Relative dotenv paths resolve under root. Quotes are stripped without
    escapes, interpolation, command substitution or shell evaluation.
    """
    root = Path(root)
    try:
        path = root / 'runtime/backend-env.json'
        settings = json.loads(_read_text(path, ENV_LIMIT)) if path.exists() else {}
        if not isinstance(settings, dict) or any(
            not _ENV_KEY.fullmatch(key) or not key.startswith('PIPECAT_')
            or not isinstance(value, str) or '\x00' in value
            for key, value in settings.items()
        ):
            raise ValueError
        path = Path(env_file) if env_file is not None else Path('.env')
        if not path.is_absolute():
            path = root / path
        if env_file is not None or path.exists():
            for line in _read_text(path, ENV_LIMIT).splitlines():
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if line.startswith('export '):
                    line = line[7:].lstrip()
                key, separator, value = line.partition('=')
                key, value = key.strip(), value.strip()
                if not separator or not _ENV_KEY.fullmatch(key) or '\x00' in value:
                    raise ValueError
                if value.startswith(('"', "'")):
                    if len(value) < 2 or value[-1] != value[0]:
                        raise ValueError
                    value = value[1:-1]
                settings[key] = value
        result = {**settings, **(os.environ if environ is None else environ)}
        # backend.py appends its service paths directly; normalize the same
        # environment that check and the launched process will both receive.
        for key in ('PIPECAT_ASR_URL', 'PIPECAT_LLM_URL', 'PIPECAT_TTS_URL'):
            if key in result:
                result[key] = result[key].rstrip('/')
        return result
    except (OSError, ValueError, TypeError, RecursionError):
        raise ValueError('Invalid or unreadable deployment environment configuration') from None


def _http_url(url):
    if (not isinstance(url, str) or not url or len(url) > 2048
            or any(char.isspace() or ord(char) < 32 for char in url) or '\\' in url):
        raise ValueError('Invalid service URL')
    parsed = urlsplit(url)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.fragment or parsed.query):
        raise ValueError('Invalid service URL')
    _ = parsed.port
    return parsed


def _request_json(url, *, method='GET', timeout=3):
    parsed = _http_url(url)
    connection_type = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
    connection = connection_type(parsed.hostname, parsed.port, timeout=timeout)
    deadline = time.monotonic() + timeout
    timer = response = None
    expired = threading.Event()
    try:
        connection.request(method, parsed.path or '/', headers={'Accept': 'application/json'})
        # Retain the socket: HTTPConnection clears its reference when a
        # response announces Connection: close, before its body is consumed.
        active_socket = connection.sock

        def interrupt_response():
            expired.set()
            try:
                active_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        # A socket timeout alone resets for every header read. Interrupt the
        # underlying socket at the overall deadline, including slow headers.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Service probe timed out')
        timer = threading.Timer(remaining, interrupt_response)
        timer.daemon = True
        timer.start()
        response = connection.getresponse()
        # Redirects are deliberately not followed, even to the same origin.
        if not 200 <= response.status < 300:
            raise ValueError('Service returned an unsuccessful status')
        length = response.getheader('Content-Length')
        if length is not None and (int(length) < 0 or int(length) > RESPONSE_LIMIT):
            raise ValueError('Service response exceeds its size limit')
        body = bytearray()
        while len(body) <= RESPONSE_LIMIT:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Service probe timed out')
            if active_socket is not None and active_socket.fileno() >= 0:
                active_socket.settimeout(remaining)
            chunk = response.read1(min(16384, RESPONSE_LIMIT + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
        if len(body) > RESPONSE_LIMIT:
            raise ValueError('Service response exceeds its size limit')
        if length is not None and len(body) != int(length):
            raise ValueError('Service response was truncated')
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError('Expected a service status object')
        if expired.is_set() or time.monotonic() >= deadline:
            raise TimeoutError('Service probe timed out')
        return data
    except Exception:
        if expired.is_set():
            raise TimeoutError('Service probe timed out') from None
        raise
    finally:
        if timer is not None:
            timer.cancel()
            timer.join()
        if response is not None:
            response.close()
        connection.close()


def _tcp_check(host, port, *, timeout=3):
    with socket.create_connection((host, port), timeout=timeout):
        return True


def inspect_deployment(root, env, *, backend=None, profile=None,
                       require_vision=False, timeout=3, request_json=None, tcp_check=None):
    """Inspect the selected dialogue and avatar chain without running inference."""
    report = dict(ready=False, backend=None, profile=None, provider=None, checks=[])

    def add(name, status, message, help='', *, required=True):
        report['checks'].append(dict(name=name, status=status, required=required,
                                     message=message, help=help))

    try:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError
        root = Path(root)
        selected_env = dict(env)
        TurnSettings.from_env(selected_env)
        if backend is not None:
            selected_env['PIPECAT_DIALOGUE_BACKEND'] = backend
        if profile is not None:
            selected_env['PIPECAT_AVATAR_PROFILE'] = profile
        providers = ProviderRegistry(root / 'runtime/avatar-config.json', env=selected_env)
        profiles = ProfileRegistry(providers, root / 'runtime/avatar-profiles.json', env=selected_env)
        dialogue = DialogueRegistry(root / 'runtime/dialogue-config.json', env=selected_env)
        selected_backend = dialogue.resolve(backend)
        selected_profile = profiles.resolve(profile)
        avatar_url = selected_profile.url or providers.providers[selected_profile.provider].url
        _backend_url(avatar_url)
        report.update(backend=selected_backend, profile=selected_profile.id, provider=selected_profile.provider)
    except Exception:
        add('configuration', 'fail', 'Deployment configuration or selection is invalid.',
            'Check backend/profile selections, VAD/turn timing and runtime configuration; use a positive finite timeout.')
        return report

    add('configuration', 'pass', 'Selected deployment configuration is valid.')
    request_json = request_json or _request_json
    tcp_check = tcp_check or _tcp_check

    def probe(name, url, valid, *, method='GET', help):
        try:
            _http_url(url)
            data = request_json(url, method=method, timeout=timeout)
            passed = isinstance(data, dict) and valid(data)
        except Exception:
            data, passed = {}, False
        add(name, 'pass' if passed else 'fail',
            'Service readiness check passed.' if passed else 'Service readiness check failed.', help)
        return data if isinstance(data, dict) else {}

    if selected_backend == 'cascade':
        for kind, port in (('asr', 18315), ('llm', 18311)):
            prefix = 'PIPECAT_' + kind.upper()
            help = f'Configure {prefix}_MODE and its local worker or API settings; see DEPLOYMENT.md.'
            try:
                api = configured_api(kind, selected_env)
            except Exception:
                add(kind, 'fail', 'API mode configuration is invalid.', help)
                continue
            if api is not None:
                add(kind, 'warn', 'API configuration is valid; availability is unverified. No API request was made.', help)
            else:
                base = selected_env.get(prefix + '_URL', f'http://127.0.0.1:{port}')
                probe(kind, base.rstrip('/') + '/health', lambda data: data.get('ready') is True, help=help)
        voice = selected_profile.voice
        speaker = selected_env.get('PIPECAT_' + voice.upper() + '_SPEAKER_ID',
                                   'female11' if voice == 'female' else 'pipecat_male')
        probe('tts', selected_env.get('PIPECAT_TTS_URL', 'http://127.0.0.1:19006').rstrip('/') + '/api/tts/list',
              lambda data: isinstance(data.get('speaker_ids'), list) and speaker in data['speaker_ids'],
              method='POST', help='Start Index-TTS and register the selected profile voice speaker; see DEPLOYMENT.md.')
        if require_vision:
            add('vision', 'fail', 'The selected dialogue backend does not provide visual input.',
                'Select MiniCPM with image input enabled.')
    else:
        data = probe('minicpm', dialogue.url.rstrip('/') + '/health', lambda data: data.get('ready') is True,
                     help='Start the configured MiniCPM worker; see omni-server/README.md.')
        if require_vision:
            capabilities = data.get('capabilities')
            enabled = isinstance(capabilities, dict) and capabilities.get('image_input') is True
            add('vision', 'pass' if enabled else 'fail',
                'Visual input is available.' if enabled else 'Required visual input is unavailable.',
                'Enable image input in the configured MiniCPM worker.')

    parsed = urlsplit(avatar_url)
    if selected_profile.provider == 'dinet':
        try:
            reachable = tcp_check(parsed.hostname, parsed.port or (443 if parsed.scheme == 'wss' else 80), timeout=timeout) is True
        except Exception:
            reachable = False
        add('avatar', 'pass' if reachable else 'fail',
            'DINet TCP endpoint is reachable.' if reachable else 'DINet TCP endpoint is unreachable.',
            'Start the configured DINet service; see video-server/README.md.')
        add('avatar-model', 'warn', 'DINet model readiness is unverified; TCP reachability does not validate inference.',
            'Validate the deployed character with a separate playback acceptance test.', required=False)
    else:
        path = parsed.path.rstrip('/')
        for suffix in ('/v1/stream', '/stream'):
            if path.endswith(suffix):
                path = path[:-len(suffix)]
                break
        health_url = urlunsplit(('https' if parsed.scheme == 'wss' else 'http', parsed.netloc, path + '/health', '', ''))
        valid = (lambda data: data.get('status') == 'ready') if selected_profile.provider == 'streamingtalker' else (lambda data: data.get('ready') is True)
        probe('avatar', health_url, valid, help='Start the selected avatar worker; see DEPLOYMENT.md.')
    idle = root / 'runtime/idle' / selected_profile.idle_url.rsplit('/', 1)[-1]
    exists = idle.is_file()
    add('idle', 'pass' if exists else 'warn',
        'The selected idle video is available.' if exists else 'Idle video is missing; the page will use its placeholder.',
        'Install the selected idle asset under runtime/idle.', required=False)
    report['ready'] = not any(item['required'] and item['status'] == 'fail' for item in report['checks'])
    return report
