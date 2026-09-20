"""Bounded, local conversation snapshots with atomic cross-process persistence.

The configured runtime directory must be outside source exports. This Linux store
keeps private files in its ``conversations`` child; callers never supply paths.
Snapshots contain adapter history, not executable configuration. Profile/backend
metadata is immutable; the application resolves it against deployed resources.
"""
import base64
import binascii
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import json
import math
import os
from pathlib import Path
import re
import stat
import uuid


class ConversationError(ValueError):
    """Invalid, missing, conflicting or oversized local conversation."""


class InvalidConversation(ConversationError):
    pass


class ConversationNotFound(ConversationError):
    pass


class ConversationLimit(ConversationError):
    pass


class ConversationConflict(ConversationError):
    pass


_ID = re.compile(r'[0-9a-f]{32}\Z')
_PROFILE = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z')
_FIELDS = {'id', 'version', 'backend', 'profile_id', 'title', 'created_at',
           'updated_at', 'revision', 'snapshot', 'transcript'}
_STATUSES = {'generated', 'heard', 'interrupted', 'pending', 'failed', 'input'}


def _require(condition, message='Invalid conversation data'):
    if not condition:
        raise InvalidConversation(message)


def _string(value, limit, *, empty=True):
    _require(type(value) is str and (empty or bool(value)) and len(value) <= limit)


def _id(value):
    _require(type(value) is str and _ID.fullmatch(value) is not None, 'Invalid conversation ID')
    return value


def _base64(value, limit, *, pcm=False):
    _string(value, limit, empty=False)
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise InvalidConversation('Invalid stored media encoding') from exc
    _require(bool(data) and (not pcm or len(data) % 2 == 0), 'Invalid stored media length')


class ConversationStore:
    def __init__(self, runtime_dir, *, max_sessions=20, max_session_bytes=4 * 1024 * 1024,
                 max_total_bytes=64 * 1024 * 1024, max_turns=40):
        for value in (max_sessions, max_session_bytes, max_total_bytes, max_turns):
            _require(type(value) is int and value > 0, 'Storage limits must be positive integers')
        self.directory = Path(os.path.abspath(runtime_dir)) / 'conversations'
        self.max_sessions = max_sessions
        self.max_session_bytes = max_session_bytes
        self.max_total_bytes = max_total_bytes
        self.max_turns = max_turns
        with self._locked():
            pass

    def _open_directory(self):
        # Walk with openat/O_NOFOLLOW, including ancestors; never follow a link.
        current = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for component in self.directory.parts[1:]:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current)
                except FileExistsError:
                    pass
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                  dir_fd=current)
                os.close(current)
                current = next_fd
            info = os.fstat(current)
            _require(info.st_uid == os.geteuid(), 'Conversation directory has a different owner')
            os.fchmod(current, 0o700)
            return current
        except OSError as exc:
            os.close(current)
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise InvalidConversation('Conversation storage must not contain symlinks') from exc
            raise
        except BaseException:
            os.close(current)
            raise

    def _open_file(self, directory, filename, flags, mode=0o600):
        try:
            fd = os.open(filename, flags | os.O_NOFOLLOW | os.O_NONBLOCK,
                         mode, dir_fd=directory)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise InvalidConversation('Conversation file must not be a symlink') from exc
            raise
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            os.close(fd)
            raise InvalidConversation('Conversation file must be a private regular file')
        return fd

    @contextmanager
    def _locked(self):
        directory = self._open_directory()
        lock = None
        try:
            lock = self._open_file(directory, '.lock', os.O_CREAT | os.O_RDWR)
            fcntl.flock(lock, fcntl.LOCK_EX)
            # A crashed atomic write can leave an orphan. Only our strict names
            # are removed, while holding the same lock used by every writer.
            for name in os.listdir(directory):
                if re.fullmatch(r'\.[0-9a-f]{32}\.tmp', name):
                    os.unlink(name, dir_fd=directory)
            yield directory
        finally:
            if lock is not None:
                os.close(lock)
            os.close(directory)

    def _names(self, directory):
        return [name for name in os.listdir(directory)
                if name.endswith('.json') and _ID.fullmatch(name[:-5])]

    def _validate(self, record):
        _require(type(record) is dict and set(record) == _FIELDS)
        _id(record['id'])
        _require(type(record['version']) is int and record['version'] == 1)
        _require(record['backend'] in ('cascade', 'minicpm'))
        _require(type(record['profile_id']) is str and _PROFILE.fullmatch(record['profile_id']))
        _string(record['title'], 200, empty=False)
        _require(type(record['revision']) is int and record['revision'] >= 1)
        for field in ('created_at', 'updated_at'):
            _string(record[field], 40, empty=False)
            try:
                stamp = datetime.fromisoformat(record[field])
            except ValueError as exc:
                raise InvalidConversation('Invalid conversation timestamp') from exc
            _require(stamp.tzinfo is not None)
        snapshot = record['snapshot']
        _require(type(snapshot) is dict and set(snapshot) == {'history'})
        history = snapshot['history']
        _require(type(history) is list)
        if record['backend'] == 'cascade':
            _require(len(history) <= self.max_turns * 2, 'Too many conversation messages')
            for message in history:
                _require(type(message) is dict and set(message) == {'role', 'content'})
                _require(message['role'] in ('user', 'assistant'))
                _string(message['content'], 32768)
        else:
            _require(len(history) <= self.max_turns, 'Too many conversation turns')
            for turn in history:
                _require(type(turn) is list and len(turn) == 2)
                user, assistant = turn
                self._validate_user(user)
                _require(type(assistant) is dict and set(assistant) == {'role', 'text'}
                         and assistant['role'] == 'assistant')
                _string(assistant['text'], 32768)
        transcript = record['transcript']
        _require(type(transcript) is list and len(transcript) <= self.max_turns * 2)
        for message in transcript:
            _require(type(message) is dict and {'role', 'text'} <= set(message)
                     and set(message) <= {'role', 'text', 'status', 'generated_text', 'heard_text'})
            _require(message['role'] in ('user', 'assistant'))
            for field in ('text', 'generated_text', 'heard_text'):
                if field in message:
                    _string(message[field], 32768)
            if 'status' in message:
                _require(type(message['status']) is str and message['status'] in _STATUSES)

    @staticmethod
    def _validate_user(user):
        _require(type(user) is dict and set(user) <=
                 {'role', 'text', 'audio', 'images', 'images_omitted'} and user.get('role') == 'user')
        _require('text' in user or 'audio' in user)
        if 'text' in user:
            _string(user['text'], 32768)
        if 'audio' in user:
            _base64(user['audio'], 2_560_000, pcm=True)
        if 'images_omitted' in user:
            _require(type(user['images_omitted']) is bool)
        if 'images' in user:
            images = user['images']
            _require(type(images) is list and 1 <= len(images) <= 2)
            for item in images:
                _require(type(item) is dict and set(item) == {'id', 'source', 'captured_at_ms', 'data'})
                _require(type(item['id']) is str and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', item['id']))
                _require(item['source'] in ('upload', 'camera'))
                stamp = item['captured_at_ms']
                _require(type(stamp) in (int, float) and 0 <= stamp <= 600000 and math.isfinite(stamp))
                _base64(item['data'], 349528)

    def _read(self, directory, session_id):
        try:
            fd = self._open_file(directory, session_id + '.json', os.O_RDONLY)
        except FileNotFoundError as exc:
            raise ConversationNotFound('Conversation not found') from exc
        with os.fdopen(fd, 'rb') as stream:
            raw = stream.read(self.max_session_bytes + 1)
        if len(raw) > self.max_session_bytes:
            raise ConversationLimit('Stored conversation exceeds size limit')
        try:
            record = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise InvalidConversation('Corrupt conversation file') from exc
        self._validate(record)
        _require(record['id'] == session_id, 'Conversation ID mismatch')
        return record

    def _write(self, directory, record):
        self._validate(record)
        try:
            raw = json.dumps(record, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
        except (ValueError, UnicodeError) as exc:
            raise InvalidConversation('Invalid conversation encoding') from exc
        if len(raw) > self.max_session_bytes:
            raise ConversationLimit('Conversation exceeds size limit')
        names = self._names(directory)
        filename = record['id'] + '.json'
        if len(names) + (filename not in names) > self.max_sessions:
            raise ConversationLimit('Conversation count limit reached; delete an old conversation')
        total = len(raw)
        for name in names:
            fd = self._open_file(directory, name, os.O_RDONLY)
            try:
                if name != filename:
                    total += os.fstat(fd).st_size
            finally:
                os.close(fd)
        if total > self.max_total_bytes:
            raise ConversationLimit('Local conversation storage limit reached')
        temporary = '.' + uuid.uuid4().hex + '.tmp'
        try:
            fd = self._open_file(directory, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, filename, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
        # Decode our bytes to return an independent object, not references owned
        # by an adapter which will keep mutating its live history.
        return json.loads(raw)

    def create(self, *, backend, profile_id, title='New conversation'):
        now = datetime.now(timezone.utc).isoformat()
        record = dict(id=uuid.uuid4().hex, version=1, backend=backend,
                      profile_id=profile_id, title=title, created_at=now,
                      updated_at=now, revision=1, snapshot={'history': []}, transcript=[])
        with self._locked() as directory:
            return self._write(directory, record)

    def list(self):
        with self._locked() as directory:
            result = []
            for filename in self._names(directory):
                record = self._read(directory, filename[:-5])
                result.append({key: value for key, value in record.items()
                               if key not in ('snapshot', 'transcript')})
            return sorted(result, key=lambda item: item['updated_at'], reverse=True)

    def load(self, session_id):
        _id(session_id)
        with self._locked() as directory:
            return self._read(directory, session_id)

    def save(self, session_id, *, snapshot, transcript=None, title=None, expected_revision=None):
        _id(session_id)
        if expected_revision is not None:
            _require(type(expected_revision) is int and expected_revision >= 1)
        with self._locked() as directory:
            record = self._read(directory, session_id)
            if expected_revision is not None and expected_revision != record['revision']:
                raise ConversationConflict('Conversation changed; reload before saving')
            record.update(snapshot=snapshot, revision=record['revision'] + 1,
                          updated_at=datetime.now(timezone.utc).isoformat())
            if transcript is not None:
                record['transcript'] = transcript
            if title is not None:
                record['title'] = title
            return self._write(directory, record)

    def delete(self, session_id):
        _id(session_id)
        with self._locked() as directory:
            try:
                # unlink never follows symlinks; damaged entries remain deletable.
                os.unlink(session_id + '.json', dir_fd=directory)
            except FileNotFoundError:
                return False
            os.fsync(directory)
            return True
