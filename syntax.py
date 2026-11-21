from collections import Counter, OrderedDict, deque
import array
import ast
import base64
import copy
import ctypes
import ctypes.wintypes as wintypes
from datetime import datetime
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
import zipfile
from urllib.parse import urlparse
import requests
import hashlib
import faulthandler
import logging
from logging.handlers import RotatingFileHandler
THEO_URL = 'https://offsets.imtheo.lol/fflags.hpp'
REPOSITORY = 'Astraliscute/Syntax'
RAW_ROOT = f'https://raw.githubusercontent.com/{REPOSITORY}'
DOWNLOAD_MAX_BYTES = 4 * 1024 * 1024
MAX_OFFSETS = 100000
SOURCE_PREFIXES = ('DFFlag', 'FFlag', 'DFFloat', 'FFloat', 'DFString', 'FString', 'DFInt', 'FInt', 'DFLog', 'FLog')
NAME_RE = re.compile('[A-Za-z_][A-Za-z0-9_]{0,159}\\Z')
SOURCE_OFFSET_RE = re.compile('(?:uintptr_t|uint64_t)\\s+([A-Za-z_][A-Za-z0-9_]*)\\s*=\\s*0x([0-9a-fA-F]{1,16})\\s*;')

def source_clean_name(name):
    return next((name[len(prefix):] for prefix in SOURCE_PREFIXES if name.startswith(prefix)), name)

def atomic_json(path, data, *, indent=2):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    try:
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=indent, allow_nan=False, separators=(',', ':') if indent is None else None)
            stream.flush()
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)

def download(url, get=None, cancel=None, *, deadline=None, limit=DOWNLOAD_MAX_BYTES):
    now = time.monotonic()
    deadline = now + 25 if deadline is None else min(deadline, now + 25)
    if cancel and cancel.is_set():
        raise RuntimeError('Download cancelled')
    if deadline <= now:
        raise TimeoutError('Download timed out')
    with (get or requests.get)(url, stream=True, timeout=(min(4, deadline - now), min(8, deadline - now))) as response:
        response.raise_for_status()
        content = bytearray()
        for chunk in response.iter_content(65536):
            if cancel and cancel.is_set():
                raise RuntimeError('Download cancelled')
            if time.monotonic() > deadline:
                raise TimeoutError('Download timed out')
            content.extend(chunk)
            if len(content) > limit:
                raise ValueError('Download too large')
    return bytes(content)

def validate_offsets(document):
    if not isinstance(document, dict) or not isinstance(document.get('flags'), dict):
        raise ValueError('Invalid offsets document')
    flags = document['flags']
    if not 1 <= len(flags) <= MAX_OFFSETS:
        raise ValueError('Invalid offsets count')
    for name, rva in flags.items():
        if not isinstance(name, str) or not NAME_RE.fullmatch(name) or type(rva) is not int or (not 4096 <= rva < 2147483648):
            raise ValueError('Invalid flag offset')
    version = document.get('version', '')
    if not isinstance(version, str) or (version and (not re.fullmatch('version-[a-fA-F0-9]{8,64}', version))):
        raise ValueError('Invalid Roblox version')
    if document.get('source') not in ('theo', 'local'):
        raise ValueError('Invalid offsets source')
    if document['source'] == 'local':
        identity = document.get('identity')
        if not isinstance(identity, dict) or identity.get('version') != version.lower() or type(identity.get('timestamp')) is not int or (not 0 <= identity['timestamp'] <= 4294967295) or (type(identity.get('image_size')) is not int) or (not 65536 <= identity['image_size'] <= 2147483648) or any((rva >= identity['image_size'] for rva in flags.values())):
            raise ValueError('Invalid dump identity')
    document['version'] = version.lower()
    return document

def parse_theo(text):
    if len(text.encode('utf-8')) > DOWNLOAD_MAX_BYTES:
        raise ValueError('Offsets too large')
    section = re.search('namespace\\s+FFlags\\s*\\{([^{}]*)\\}', text, re.S)
    if not section:
        raise ValueError('FFlags namespace missing')
    flags = {}
    for name, rva in SOURCE_OFFSET_RE.findall(section.group(1)):
        rva = int(rva, 16)
        if name in flags and flags[name] != rva:
            raise ValueError('Conflicting flag offsets')
        flags[name] = rva
    version = re.search('version-[a-fA-F0-9]{8,64}', text)
    container = re.search('namespace\\s+FFlagList\\s*\\{([^{}]*)\\}', text, re.S)
    layout = {name: int(rva, 16) for name, rva in SOURCE_OFFSET_RE.findall(container.group(1))} if container else {}
    return validate_offsets({'schema': 1, 'source': 'theo', 'version': version.group(0) if version else '', 'flags': flags, 'layout': layout, 'created': time.time()})

def matches_identity(document, identity):
    if document.get('source') == 'local':
        return bool(identity and document.get('identity') == identity)
    return bool(identity and identity.get('version') and (document.get('version') == identity['version'].lower()))

class OffsetStore:

    def __init__(self, directory, source='theo', get=None):
        self.directory = Path(directory) / 'offsets'
        self.source = source
        self._get = get
        self._lock = threading.RLock()
        self._cached = {}
        self._at = {}
        self.cancel = threading.Event()
        self.revision = 0

    def set_source(self, source):
        if source not in ('theo', 'local'):
            raise ValueError('Invalid offsets source')
        with self._lock:
            if source != self.source:
                self.source = source
                self.revision += 1

    def document(self, identity=None, force=False):
        with self._lock:
            source = self.source
            if source == 'local':
                if identity:
                    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
                    path = self.directory / (key + '.json')
                else:
                    path = self.directory / 'latest.json'
                if not path.exists() or path.stat().st_size > DOWNLOAD_MAX_BYTES:
                    raise ValueError('Dump offsets first')
                document = validate_offsets(config_json(read_limited(path, DOWNLOAD_MAX_BYTES)))
                if identity and (not matches_identity(document, identity)):
                    raise ValueError('Dump this Roblox version')
                return document
            if not force and source in self._cached and (time.monotonic() - self._at.get(source, 0) < 600):
                return self._cached[source]
            path = self.directory / 'theo.json'
            try:
                document = parse_theo(download(THEO_URL, self._get, self.cancel).decode('utf-8-sig'))
                atomic_json(path, document)
            except (requests.RequestException, ValueError, OSError):
                if force or not path.exists() or path.stat().st_size > DOWNLOAD_MAX_BYTES:
                    raise
                document = validate_offsets(config_json(read_limited(path, DOWNLOAD_MAX_BYTES)))
            self._cached[source] = document
            self._at[source] = time.monotonic()
            self.revision += 1
            return document

    def save_dump(self, document):
        validate_offsets(document)
        identity = document.get('identity')
        if document.get('source') != 'local' or not identity:
            raise ValueError('Missing dump identity')
        if len(json.dumps(document, separators=(',', ':'), ensure_ascii=False).encode('utf-8')) > DOWNLOAD_MAX_BYTES:
            raise ValueError('Offsets dump too large')
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
        with self._lock:
            path = self.directory / (key + '.json')
            lines = ['#pragma once', '#include <cstdint>', 'namespace FFlags {']
            lines.extend((f'    inline constexpr uintptr_t {name}=0x{rva:X};' for name, rva in sorted(document['flags'].items())))
            lines.append('}')
            self.directory.mkdir(parents=True, exist_ok=True)
            header = path.with_suffix('.hpp')
            temporary = header.with_suffix('.hpp.tmp')
            try:
                temporary.write_text('\n'.join(lines) + '\n', encoding='utf-8')
                temporary.replace(header)
            finally:
                temporary.unlink(missing_ok=True)
            atomic_json(path, document, indent=None)
            atomic_json(self.directory / 'latest.json', document, indent=None)
            self.revision += 1
            return str(path)

class EDumper:

    def __init__(self, process, cancel=None, progress=None, timeout=90):
        self.process = process
        self.cancel = cancel or threading.Event()
        self.progress = progress or (lambda message: None)
        self.deadline = time.monotonic() + timeout
        self.pages = OrderedDict()
        self.nodes = 0

    def check(self):
        if self.cancel.is_set():
            raise RuntimeError('Dump cancelled')
        if time.monotonic() > self.deadline:
            raise TimeoutError('Dump timed out')
        if not self.process.alive():
            raise RuntimeError('Roblox closed during dump')

    @staticmethod
    def pointer(value):
        return 65536 <= value < 140737488355328 and value % 8 == 0

    def read(self, address, size):
        if not 65536 <= address < 140737488355328 or not 0 < size <= 65536:
            return None
        page = address & ~4095
        offset = address - page
        if offset + size > 4096:
            return self.process.read(address, size)
        if page not in self.pages:
            self.pages[page] = self.process.read(page, 4096)
            if len(self.pages) > 256:
                self.pages.popitem(last=False)
        data = self.pages[page]
        if data is None:
            return self.process.read(address, size)
        return data[offset:offset + size]

    def u64(self, address):
        raw = self.read(address, 8)
        return struct.unpack('<Q', raw)[0] if raw and len(raw) == 8 else 0

    def name(self, address):
        raw = self.read(address, 32)
        if not raw or len(raw) != 32:
            return ''
        size, capacity = struct.unpack_from('<QQ', raw, 16)
        if not 1 <= size <= 160 or capacity < size or capacity > 4096:
            return ''
        if capacity < 16:
            data = raw[:size]
        else:
            pointer = struct.unpack_from('<Q', raw)[0]
            data = self.read(pointer, size) if self.pointer(pointer) else None
        try:
            value = data.decode('ascii') if data else ''
        except UnicodeDecodeError:
            return ''
        return value if NAME_RE.fullmatch(value) else ''

    def walk(self, head, limit=100000):
        seen = {head}
        current = self.u64(head)
        for index in range(limit):
            if current == head:
                return
            if not self.pointer(current) or current in seen:
                raise ValueError('Corrupt FFlag list')
            if index % 128 == 0:
                self.check()
            seen.add(current)
            yield current
            current = self.u64(current)
        raise ValueError('FFlag list exceeds limit')

    def infer_layout(self, head):
        samples = []
        for node in self.walk(head, 100000):
            name = self.name(node + 16)
            if name:
                samples.append((node, name))
            if len(samples) >= 16:
                break
        if len(samples) < 4:
            return None
        candidates = []
        for to_flag in range(32, 88, 8):
            self.check()
            for to_value in range(0, 256, 8):
                addresses = []
                for node, _ in samples:
                    descriptor = self.u64(node + to_flag)
                    address = self.u64(descriptor + to_value) if self.pointer(descriptor) else 0
                    if self.process.is_data_address(address, 4):
                        addresses.append(address)
                score = len(set(addresses))
                if score >= max(4, len(samples) * 3 // 4):
                    candidates.append((score, to_flag, to_value, tuple(addresses)))
        candidates.sort(key=lambda item: item[0], reverse=True)
        if not candidates:
            return None
        best = candidates[0]
        if any((item[0] == best[0] and item[3] != best[3] for item in candidates[1:])):
            return None
        return (best[1], best[2])

    def locate(self, hints=None):
        sections = [section for section in self.process.sections if section['writable'] and (not section['executable'])]
        hint = (hints or {}).get('Pointer', 0)
        if 4096 <= hint < self.process.image_size:
            candidates = [(hint, 8)]
        else:
            candidates = []
        candidates.extend(((section['rva'], section['size']) for section in sections))
        scanned = 0
        checked = set()
        for rva, length in candidates:
            for offset in range(0, length, 65536):
                self.check()
                amount = min(65536, length - offset)
                scanned += amount
                if scanned > 256 * 1024 * 1024:
                    raise ValueError('Dump scan limit reached')
                raw = self.process.read(self.process.base_address + rva + offset, amount)
                if not raw:
                    continue
                for index in range(0, len(raw) - 7, 8):
                    if index % 2048 == 0:
                        self.check()
                    pointer = struct.unpack_from('<Q', raw, index)[0]
                    if not self.pointer(pointer) or pointer in checked:
                        continue
                    checked.add(pointer)
                    if len(checked) > 1000000:
                        raise ValueError('Dump candidate limit reached')
                    marker = self.u64(pointer)
                    if marker != 1065353216:
                        continue
                    head = self.u64(pointer + 8)
                    if not self.pointer(head):
                        continue
                    try:
                        layout = self.infer_layout(head)
                    except ValueError:
                        continue
                    if layout:
                        return (rva + offset + index, head, layout)
                self.progress(f'Scanning container · {scanned // 1048576} MiB')
        raise ValueError('FFlag container unavailable')

    def dump(self, hints=None):
        pointer, head, (to_flag, to_value) = self.locate(hints)
        self.progress('Reading FFlags')
        flags = {}
        for node in self.walk(head):
            name = self.name(node + 16)
            descriptor = self.u64(node + to_flag)
            address = self.u64(descriptor + to_value) if self.pointer(descriptor) else 0
            if name and self.process.is_data_address(address, 1):
                rva = address - self.process.base_address
                if name in flags and flags[name] != rva:
                    raise ValueError('Ambiguous flag addresses')
                flags[name] = rva
        if len(flags) < 4:
            raise ValueError('Incomplete offsets dump')
        self.check()
        return validate_offsets({'schema': 1, 'source': 'local', 'version': self.process.identity.get('version', ''), 'identity': self.process.identity, 'created': time.time(), 'flags': flags, 'layout': {'Pointer': pointer, 'ToFlag': to_flag, 'ToValue': to_value}})

def config_path(value):
    if not isinstance(value, str) or len(value) > 240 or (not re.fullmatch('configs/(?:[A-Za-z0-9][A-Za-z0-9_.-]*/)*[A-Za-z0-9][A-Za-z0-9_.-]*\\.json', value)) or any((part.endswith('.') for part in value.split('/'))):
        raise ValueError('Invalid config path')
    return PurePosixPath(value)

def read_limited(path, limit):
    with Path(path).open('rb') as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError('File too large')
    return content

def config_json(content):

    def unique_object(pairs):
        data = dict(pairs)
        if len(data) != len(pairs):
            raise ValueError('Duplicate config key')
        return data

    def reject_constant(value):
        raise ValueError('Non-finite config number')
    try:
        return json.loads(content, object_pairs_hook=unique_object, parse_constant=reject_constant)
    except RecursionError as error:
        raise ValueError('Config nested too deeply') from error

def validate_manifest(data):
    if not isinstance(data, dict) or type(data.get('schema')) is not int or data['schema'] != 1:
        raise ValueError('Invalid config manifest')
    presets = str(config_path(data.get('presets')))
    packs = data.get('packs', [])
    if not isinstance(packs, list) or len(packs) > 100:
        raise ValueError('Invalid community packs')
    seen = set()
    validated = []
    for pack in packs:
        if not isinstance(pack, dict):
            raise ValueError('Invalid community pack')
        identifier = pack.get('id')
        if not isinstance(identifier, str) or not re.fullmatch('[a-z0-9-]{1,64}', identifier) or identifier in seen:
            raise ValueError('Invalid community pack')
        seen.add(identifier)
        path = str(config_path(pack.get('file')))
        if path == presets:
            raise ValueError('Pack points to presets')
        name, description = (pack.get('name'), pack.get('description', ''))
        if not isinstance(name, str) or not name.strip() or len(name) > 80:
            raise ValueError('Invalid pack name')
        if not isinstance(description, str) or len(description) > 400:
            raise ValueError('Invalid pack description')
        name.encode('utf-8')
        description.encode('utf-8')
        validated.append({'id': identifier, 'name': name, 'description': description, 'file': path})
    return {'schema': 1, 'presets': presets, 'packs': validated}

class RepositoryConfigs:

    def __init__(self, resources, directory, get=None):
        self.resources = Path(resources)
        self.directory = Path(directory) / 'repository'
        self.get = get
        self.branch = 'main'
        self.cancel = threading.Event()

    def bundled(self, name):
        path = self.resources / str(config_path(name))
        return config_json(read_limited(path, DOWNLOAD_MAX_BYTES))

    @staticmethod
    def _paths(manifest):
        return dict.fromkeys([manifest['presets'], *(pack['file'] for pack in manifest['packs'])])

    def _snapshot(self, manifest, files, validate):
        if not isinstance(files, dict) or files.keys() != self._paths(manifest).keys():
            raise ValueError('Incomplete config snapshot')
        for name, value in files.items():
            validate(name, value, is_presets=name == manifest['presets'])
        return {'manifest': manifest, 'files': files}

    def refresh(self, validate):
        last_error = None
        deadline = time.monotonic() + 30
        for branch in ('main', 'master'):
            try:
                if self.cancel.is_set():
                    raise ValueError('Config refresh cancelled')
                content = download(f'{RAW_ROOT}/{branch}/configs/manifest.json', self.get, self.cancel, deadline=deadline)
                manifest = validate_manifest(config_json(content))
                files, total_bytes = ({}, len(content))
                for name in self._paths(manifest):
                    if self.cancel.is_set():
                        raise ValueError('Config refresh cancelled')
                    content = download(f'{RAW_ROOT}/{branch}/{name}', self.get, self.cancel, deadline=deadline, limit=DOWNLOAD_MAX_BYTES - total_bytes)
                    total_bytes += len(content)
                    files[name] = config_json(content)
                result = self._snapshot(manifest, files, validate)
                if self.cancel.is_set():
                    raise ValueError('Config refresh cancelled')
                if time.monotonic() >= deadline:
                    raise TimeoutError('Config refresh timed out')
                atomic_json(self.directory / 'snapshot.json', result, indent=None)
                self.branch = branch
                return result
            except (requests.RequestException, ValueError, OSError, RuntimeError) as error:
                last_error = error
                if self.cancel.is_set() or time.monotonic() >= deadline:
                    break
        raise ValueError('Repository configs unavailable') from last_error

    def load_bundled(self, validate):
        manifest = validate_manifest(self.bundled('configs/manifest.json'))
        files = {name: self.bundled(name) for name in self._paths(manifest)}
        return self._snapshot(manifest, files, validate)

    def load(self, validate):
        path = self.directory / 'snapshot.json'
        try:
            snapshot = config_json(read_limited(path, DOWNLOAD_MAX_BYTES * 2))
            if not isinstance(snapshot, dict):
                raise ValueError('Invalid config snapshot')
            manifest = validate_manifest(snapshot.get('manifest'))
            return self._snapshot(manifest, snapshot.get('files'), validate)
        except (ValueError, TypeError, OSError):
            return self.load_bundled(validate)

class PrefixCatalog:
    URL = 'https://raw.githubusercontent.com/MaximumADHD/Roblox-Client-Tracker/roblox/FVariables.txt'

    def __init__(self, resources, directory, get=None):
        self.path = Path(directory) / 'prefixes.json'
        self.get = get
        self._lock = threading.Lock()
        self.cancel = threading.Event()
        self.mapping = {}
        self.revision = 0
        self._at = 0
        try:
            bundled = config_json(read_limited(Path(resources) / 'configs' / 'prefixes.json', DOWNLOAD_MAX_BYTES))
            if self._valid(bundled):
                self.mapping = bundled
        except (OSError, ValueError, TypeError):
            pass
        self._bundled = self.mapping.copy()
        try:
            document = config_json(read_limited(self.path, DOWNLOAD_MAX_BYTES))
            if isinstance(document, dict) and self._valid(document.get('prefixes')):
                self.mapping.update(document['prefixes'])
                ambiguous = document.get('ambiguous', [])
                if isinstance(ambiguous, list):
                    for clean in ambiguous:
                        if isinstance(clean, str):
                            self.mapping.pop(clean, None)
                created = document.get('created')
                if type(created) in (int, float) and 0 < created <= time.time():
                    self._at = created
        except (OSError, ValueError, TypeError):
            pass

    @staticmethod
    def _valid(mapping):
        return isinstance(mapping, dict) and len(mapping) <= MAX_OFFSETS and all((isinstance(name, str) and NAME_RE.fullmatch(name) and (prefix in SOURCE_PREFIXES) for name, prefix in mapping.items()))

    def refresh(self, force=False):
        with self._lock:
            if self.cancel.is_set() or (not force and time.time() - self._at < 86400):
                return self.mapping
            raw = download(self.URL, self.get, self.cancel).decode('utf-8-sig')
            mapping, ambiguous = ({}, set())
            for full in re.findall('^\\[C\\+\\+\\][ \\t]+(D?F(?:Flag|Int|Float|String|Log)[A-Za-z0-9_]+)[ \\t]*\\r?$', raw, re.M):
                clean = source_clean_name(full)
                prefix = full[:-len(clean)]
                if clean in mapping and mapping[clean] != prefix:
                    ambiguous.add(clean)
                mapping[clean] = prefix
            for clean in ambiguous:
                mapping.pop(clean, None)
            if not self._valid(mapping) or len(mapping) < 100:
                raise ValueError('Prefix catalog unavailable')
            if self.cancel.is_set():
                return self.mapping
            known = {**self._bundled, **mapping}
            for clean in ambiguous:
                known.pop(clean, None)
            atomic_json(self.path, {'created': time.time(), 'prefixes': mapping, 'ambiguous': sorted(ambiguous)})
            if known != self.mapping:
                self.mapping = known
                self.revision += 1
            self._at = time.time()
            return self.mapping
THEME_COLORS = {'purple': '#b09bc9', 'dark': '#bfc0c5', 'midnight': '#a3b3cb', 'ocean': '#8ab7c7', 'forest': '#a0bea6', 'rose': '#c5a0b2', 'amber': '#c6b08b'}
THEMES = tuple(THEME_COLORS)
_fault_stream = None

def theme_image(theme, resources):
    from PIL import Image, ImageColor, ImageOps
    color = THEME_COLORS[theme if theme in THEMES else 'purple']
    with Image.open(Path(resources) / 'syntax.ico') as original:
        image = original.convert('RGBA')
    shadow = tuple(channel // 8 for channel in ImageColor.getrgb(color))
    result = ImageOps.colorize(ImageOps.grayscale(image), shadow, 'white', mid=color, midpoint=140)
    result.putalpha(image.getchannel('A'))
    return result

def theme_logos(resources):
    import io
    from PIL import Image
    logos = {}
    for theme in THEMES:
        image = theme_image(theme, resources)
        image.thumbnail((96, 96), Image.Resampling.LANCZOS)
        with io.BytesIO() as buffer:
            image.save(buffer, format='PNG', optimize=True)
            logos[theme] = 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')
    return logos

class SingleInstance:

    def __init__(self, directory):
        self.primary = True
        self.mutex = None
        self.event = None
        self.thread = None
        self.stop_event = threading.Event()
        if sys.platform != 'win32':
            return
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateEventW.restype = wintypes.HANDLE
        self.kernel.SetEvent.argtypes = [wintypes.HANDLE]
        self.kernel.SetEvent.restype = wintypes.BOOL
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        identifier = hashlib.sha256(str(Path(directory).resolve()).lower().encode()).hexdigest()[:20]
        name = 'Local\\Syntax-' + identifier
        ctypes.set_last_error(0)
        self.mutex = self.kernel.CreateMutexW(None, False, name)
        if not self.mutex:
            raise OSError(ctypes.get_last_error(), 'Cannot create application mutex')
        self.primary = ctypes.get_last_error() != 183
        self.event = self.kernel.CreateEventW(None, False, False, name + '-show')
        if not self.event:
            self.close()
            raise OSError('Cannot create application event')
        if not self.primary:
            self.kernel.SetEvent(self.event)

    def listen(self, restore):
        if not self.event or not self.primary or self.thread is not None:
            return

        def worker():
            while not self.stop_event.is_set():
                if self.kernel.WaitForSingleObject(self.event, 500) == 0 and (not self.stop_event.is_set()):
                    try:
                        restore()
                    except Exception:
                        logging.getLogger('syntax').exception('Window restore failed')
        self.thread = threading.Thread(target=worker, name='syntax-instance', daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.event:
            self.kernel.SetEvent(self.event)
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
        for name in ('event', 'mutex'):
            handle = getattr(self, name)
            if handle:
                self.kernel.CloseHandle(handle)
                setattr(self, name, None)

def configure_logging(directory):
    global _fault_stream
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger('syntax')
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(directory / 'syntax.log', maxBytes=1024 * 1024, backupCount=3, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(threadName)s %(message)s'))
        logger.addHandler(handler)
    if _fault_stream is None:
        path = directory / 'native-crash.log'
        if path.exists() and path.stat().st_size > 1024 * 1024:
            path.replace(directory / 'native-crash.previous.log')
        _fault_stream = path.open('a', encoding='utf-8')
        faulthandler.enable(_fault_stream, all_threads=True)

    def thread_error(arguments):
        logger.error('Background worker failed', exc_info=(arguments.exc_type, arguments.exc_value, arguments.exc_traceback))

    def main_error(kind, value, traceback):
        logger.critical('Application failed', exc_info=(kind, value, traceback))
    threading.excepthook = thread_error
    sys.excepthook = main_error
    return logger

def startup_command(script):
    if getattr(sys, 'frozen', False):
        arguments = [str(Path(sys.executable).resolve()), '--background']
    else:
        executable = Path(sys.executable).resolve()
        if executable.name.lower() == 'python.exe' and executable.with_name('pythonw.exe').exists():
            executable = executable.with_name('pythonw.exe')
        arguments = [str(executable), str(Path(script).resolve()), '--background']
    return subprocess.list2cmdline(arguments)

def set_startup(enabled, script):
    if sys.platform != 'win32':
        if enabled:
            raise RuntimeError('Windows startup required')
        return
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, 'Software\\Microsoft\\Windows\\CurrentVersion\\Run', 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, 'Syntax', 0, winreg.REG_SZ, startup_command(script))
        else:
            try:
                winreg.DeleteValue(key, 'Syntax')
            except FileNotFoundError:
                pass

class DesktopIntegration:

    def __init__(self, app, resources):
        self.app = app
        self.resources = Path(resources)
        self.icon = None
        self.ready = threading.Event()
        self.error = ''
        self.exiting = False
        self._lock = threading.Lock()

    def start(self):
        if sys.platform != 'win32':
            return
        with self._lock:
            if self.icon is not None or self.exiting:
                return
            self.ready.clear()
            self.error = ''
            try:
                import pystray
                picture = theme_image(self.app.settings.get('theme'), self.resources)
                self.icon = pystray.Icon('Syntax', picture, 'Syntax', menu=pystray.Menu(pystray.MenuItem('Open Syntax', self.restore, default=True), pystray.MenuItem('Exit', self.exit)))
                self.icon.run_detached(setup=self._setup)
            except Exception as error:
                self.error = str(error)
                self.icon = None
                self.app.log(f'[!] Tray unavailable: {error}')

    def _setup(self, icon):
        if self.exiting:
            icon.stop()
            return
        icon.visible = True
        self.ready.set()

    def set_theme(self, theme):
        with self._lock:
            if self.icon is None or self.exiting:
                return
            try:
                self.icon.icon = theme_image(theme, self.resources)
            except Exception as error:
                self.app.log(f'[!] Tray theme failed: {error}')

    def hide(self):
        self.start()
        if self.icon is None or not self.ready.wait(2) or self.exiting:
            return False
        if self.app.window:
            self.app.set_window_activity(False)
            self.app.window.hide()
        return True

    def restore(self, *arguments):
        if self.app.window and (not self.exiting):
            self.app.window.show()
            self.app.window.restore()
            self.app.set_window_activity(True)

    def exit(self, *arguments):
        self.exiting = True
        if self.app.window:
            self.app.window.destroy()

    def closing(self):
        if not self.exiting and self.app.settings.get('tray'):
            self.start()
            if self.icon is not None and self.ready.is_set():
                threading.Thread(target=self.hide, name='syntax-hide', daemon=True).start()
                return False
        return True

    def stop(self):
        self.exiting = True
        self.ready.clear()
        icon, self.icon = (self.icon, None)
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                logging.getLogger('syntax').exception('Tray shutdown failed')
VERSION = '2.0.0'
EXECUTABLE_NAME = 'Syntax.exe'
UPDATE_ARCHIVE_NAME = EXECUTABLE_NAME + '.zip'
MAX_UPDATE_BYTES = 128 * 1024 * 1024
GITHUB_OWNER = 'Astraliscute'
GITHUB_REPO = 'Syntax'
OFFSET_STORE = None
HEADERS = {'Accept': 'application/vnd.github+json', 'User-Agent': 'Syntax/' + VERSION, 'X-GitHub-Api-Version': '2022-11-28'}

def repository(owner, repo):
    owner, repo = (str(owner).strip(), str(repo).strip())
    if not re.fullmatch('[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})', owner):
        raise ValueError('Invalid GitHub owner')
    if not re.fullmatch('[A-Za-z0-9_.-]{1,100}', repo) or repo in ('.', '..'):
        raise ValueError('Invalid repository name')
    return (owner, repo)

def version_key(tag):
    match = re.fullmatch(r'(?i)(?:release[-_ ]*|v)?(\d+)\.(\d+)(?:\.(\d+))?(?:[-+]([0-9A-Za-z.-]+))?', str(tag).strip())
    if not match:
        raise ValueError('Invalid version tag')
    return tuple((int(part or 0) for part in match.groups()[:3]))

def check_release(owner, repo, get=None):
    result = {'state': 'unconfigured', 'current': VERSION, 'latest': '', 'url': '', 'asset': '', 'download': '', 'size': 0, 'sha256': '', 'error': ''}
    if not owner or not repo:
        return result
    response = None
    try:
        owner, repo = repository(owner, repo)
        response = (get or requests.get)(f'https://api.github.com/repos/{owner}/{repo}/releases/latest', headers=HEADERS, timeout=(4, 8))
        if response.status_code == 404:
            return {**result, 'state': 'no_release', 'error': 'Release not found'}
        if response.status_code in (403, 429):
            return {**result, 'state': 'error', 'error': 'Request limit reached'}
        response.raise_for_status()
        release = response.json()
        if not isinstance(release, dict):
            raise ValueError('Invalid release response')
        tag = str(release.get('tag_name', ''))
        if len(tag) > 100:
            raise ValueError('Invalid version tag')
        result['latest'] = tag
        newer = version_key(tag) > version_key(VERSION)
        if release.get('draft') or release.get('prerelease'):
            raise ValueError('Stable release unavailable')
        url = str(release.get('html_url', ''))
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.netloc != 'github.com' or not parsed.path.lower().startswith(f'/{owner}/{repo}/releases/tag/'.lower()):
            raise ValueError('Invalid release URL')
        result['url'] = url
        assets = release.get('assets')
        if not isinstance(assets, list) or len(assets) > 1000:
            raise ValueError('Invalid release assets')
        matches = [asset for asset in assets if isinstance(asset, dict) and asset.get('name') == UPDATE_ARCHIVE_NAME]
        if not matches:
            return {**result, 'state': 'missing_asset', 'error': 'Syntax.exe.zip missing'}
        if len(matches) != 1:
            raise ValueError('Duplicate update assets')
        asset = matches[0]
        download_url = asset.get('browser_download_url', '')
        from urllib.parse import unquote
        target = urlparse(download_url)
        parts = target.path.split('/')
        prefix = f'/{owner}/{repo}/releases/download/'.lower()
        if (target.scheme != 'https' or target.netloc != 'github.com' or target.query or target.fragment
                or not target.path.lower().startswith(prefix) or len(parts) != 7
                or unquote(parts[-2]) != tag or parts[-1] != UPDATE_ARCHIVE_NAME):
            raise ValueError('Invalid download URL')
        size = asset.get('size')
        if asset.get('state') != 'uploaded' or type(size) is not int or not 22 <= size <= MAX_UPDATE_BYTES:
            raise ValueError('Invalid update asset')
        digest = asset.get('digest')
        if digest is not None and not re.fullmatch(r'sha256:[a-fA-F0-9]{64}', str(digest)):
            raise ValueError('Invalid update digest')
        result.update(state='available' if newer else 'current', asset=UPDATE_ARCHIVE_NAME, download=download_url,
                      size=size, sha256=digest.split(':', 1)[1].lower() if digest else '')
        return result
    except (ValueError, TypeError, KeyError, requests.RequestException) as error:
        message = str(error) if isinstance(error, ValueError) else 'Update check failed'
        return {**result, 'state': 'error', 'error': message}
    finally:
        if response is not None and callable(getattr(response, 'close', None)):
            response.close()

def executable_digest(path, size=0, digest=''):
    path = Path(path)
    actual = path.stat().st_size
    if not 256 <= actual <= MAX_UPDATE_BYTES or size and actual != size:
        raise ValueError('Invalid executable size')
    with path.open('rb') as stream:
        header = stream.read(64)
        if header[:2] != b'MZ':
            raise ValueError('Invalid executable header')
        offset = struct.unpack_from('<I', header, 60)[0]
        if not 64 <= offset <= min(actual - 26, 1024 * 1024):
            raise ValueError('Invalid executable offset')
        stream.seek(offset)
        pe = stream.read(26)
        if pe[:4] != b'PE\0\0' or struct.unpack_from('<H', pe, 4)[0] != 0x8664 or struct.unpack_from('<H', pe, 24)[0] != 0x20B:
            raise ValueError('Unsupported executable architecture')
        stream.seek(0)
        value = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    result = value.hexdigest()
    if digest and result != digest.lower():
        raise ValueError('Update checksum mismatch')
    return result

def download_update(release, path, cancel, progress=None, get=None):
    progress = progress or (lambda received, total: None)
    deadline = time.monotonic() + 180
    received = 0
    checksum = hashlib.sha256()
    path = Path(path)
    created = False
    try:
        if cancel.is_set():
            raise RuntimeError('Update cancelled')
        with (get or requests.get)(release['download'], stream=True, timeout=(5, 15)) as response:
            response.raise_for_status()
            final = urlparse(getattr(response, 'url', release['download']))
            if final.scheme != 'https' or not (final.hostname == 'github.com' or (final.hostname or '').endswith('.githubusercontent.com')):
                raise ValueError('Invalid download redirect')
            with path.open('xb') as stream:
                created = True
                for chunk in response.iter_content(65536):
                    if cancel.is_set():
                        raise RuntimeError('Update cancelled')
                    if time.monotonic() > deadline:
                        raise TimeoutError('Update download timed out')
                    if not chunk:
                        continue
                    received += len(chunk)
                    if received > release['size'] or received > MAX_UPDATE_BYTES:
                        raise ValueError('Update download too large')
                    stream.write(chunk)
                    checksum.update(chunk)
                    progress(received, release['size'])
                stream.flush()
                os.fsync(stream.fileno())
        if cancel.is_set():
            raise RuntimeError('Update cancelled')
        if received != release['size'] or not 22 <= received <= MAX_UPDATE_BYTES:
            raise ValueError('Invalid archive size')
        digest = checksum.hexdigest()
        if release['sha256'] and digest != release['sha256']:
            raise ValueError('Update checksum mismatch')
        return digest
    except Exception:
        if created:
            path.unlink(missing_ok=True)
        raise

def extract_update_archive(path, destination, cancel):
    destination = Path(destination)
    created = False
    deadline = time.monotonic() + 60
    try:
        if cancel.is_set():
            raise RuntimeError('Update cancelled')
        if not 22 <= Path(path).stat().st_size <= MAX_UPDATE_BYTES:
            raise ValueError('Invalid archive size')
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) != 1 or entries[0].filename != EXECUTABLE_NAME:
                raise ValueError('Invalid update archive')
            entry = entries[0]
            mode = entry.external_attr >> 16 & 0o170000
            if entry.is_dir() or entry.flag_bits & 1 or mode not in (0, 0o100000):
                raise ValueError('Invalid archive entry')
            if entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise ValueError('Unsupported archive compression')
            if not 256 <= entry.file_size <= MAX_UPDATE_BYTES:
                raise ValueError('Invalid executable size')
            received = 0
            with archive.open(entry) as source, destination.open('xb') as output:
                created = True
                while True:
                    if cancel.is_set():
                        raise RuntimeError('Update cancelled')
                    if time.monotonic() > deadline:
                        raise TimeoutError('Update extraction timed out')
                    chunk = source.read(65536)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > entry.file_size or received > MAX_UPDATE_BYTES:
                        raise ValueError('Update extraction too large')
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if received != entry.file_size:
                raise ValueError('Incomplete update archive')
        if cancel.is_set():
            raise RuntimeError('Update cancelled')
        return executable_digest(destination, received)
    except Exception:
        if created:
            destination.unlink(missing_ok=True)
        raise

def prepare_update(release, target, directory, cancel, progress=None, get=None):
    target = Path(target).resolve()
    if target.name.lower() != EXECUTABLE_NAME.lower() or not target.is_file():
        raise ValueError('Run installed Syntax.exe')
    previous = executable_digest(target)
    updates = Path(directory) / 'updates'
    token = uuid.uuid4().hex
    workspace = updates / token
    workspace.mkdir(parents=True)
    source = workspace / EXECUTABLE_NAME
    archive = target.with_name(UPDATE_ARCHIVE_NAME + '.bak')
    downloaded = False
    pending = None
    try:
        download_update(release, archive, cancel, progress, get)
        downloaded = True
        digest = extract_update_archive(archive, source, cancel)
        size = source.stat().st_size
        with tempfile.NamedTemporaryFile(prefix='Syntax-update-', suffix='.exe', dir=target.parent, delete=False) as stream:
            pending = Path(stream.name)
        shutil.copy2(source, pending)
        executable_digest(pending, size, digest)
        data = {'schema': 1, 'pid': os.getpid(), 'token': token, 'version': release['latest'], 'current': VERSION,
                'source': str(source.resolve()), 'target': str(target), 'pending': str(pending.resolve()),
                'backup': str(target.with_name('Syntax-backup-' + token + '.bak')), 'size': size,
                'sha256': digest, 'previous': previous}
        manifest = workspace / 'update.json'
        atomic_json(manifest, data)
        return manifest, data
    except Exception:
        if pending is not None:
            pending.unlink(missing_ok=True)
        source.unlink(missing_ok=True)
        raise
    finally:
        if downloaded:
            archive.unlink(missing_ok=True)

def launch_application(path):
    return subprocess.Popen([str(path)], cwd=str(Path(path).parent), close_fds=True,
                            env={**os.environ, 'PYINSTALLER_RESET_ENVIRONMENT': '1'})

def replace_update_file(source, target):
    for attempt in range(100):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 99:
                raise
            time.sleep(0.1)

def wait_update_parent(pid, cancel, timeout=45):
    if sys.platform != 'win32':
        raise RuntimeError('Windows updater required')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:
            return
        raise OSError('Cannot verify application exit')
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if Path(cancel).exists():
                raise RuntimeError('Update cancelled')
            result = kernel.WaitForSingleObject(handle, 100)
            if result == 0:
                return
            if result != 258:
                raise OSError('Application wait failed')
        raise TimeoutError('Application exit timed out')
    finally:
        kernel.CloseHandle(handle)

def read_update_manifest(path, directory):
    path = Path(path).resolve()
    updates = (Path(directory) / 'updates').resolve()
    if path.name != 'update.json' or path.parent.parent != updates or not re.fullmatch('[a-f0-9]{32}', path.parent.name):
        raise ValueError('Invalid update workspace')
    data = config_json(read_limited(path, 8192))
    if not isinstance(data, dict) or data.get('schema') != 1:
        raise ValueError('Invalid update manifest')
    if type(data.get('pid')) is not int or not 0 < data['pid'] <= 0xFFFFFFFF or data['pid'] == os.getpid():
        raise ValueError('Invalid update process')
    if not re.fullmatch('[a-f0-9]{32}', str(data.get('token', ''))):
        raise ValueError('Invalid update token')
    if version_key(data.get('version', '')) != version_key(VERSION) or version_key(data['version']) <= version_key(data.get('current', '')):
        raise ValueError('Update version mismatch')
    paths = {name: Path(data[name]).resolve() for name in ('source', 'target', 'pending', 'backup')}
    target = paths['target']
    if paths['source'] != path.parent / EXECUTABLE_NAME or target.name.lower() != EXECUTABLE_NAME.lower():
        raise ValueError('Invalid update target')
    if target.parent == path.parent or paths['pending'].parent != target.parent or paths['backup'].parent != target.parent:
        raise ValueError('Invalid update directory')
    if len(set(paths.values())) != 4 or not paths['pending'].name.startswith('Syntax-update-') or not paths['backup'].name.startswith('Syntax-backup-'):
        raise ValueError('Invalid update paths')
    for name in ('sha256', 'previous'):
        if not re.fullmatch('[a-f0-9]{64}', str(data.get(name, ''))):
            raise ValueError('Invalid update checksum')
    if type(data.get('size')) is not int or not 256 <= data['size'] <= MAX_UPDATE_BYTES:
        raise ValueError('Invalid update size')
    return data, paths, path.parent

def finish_update(manifest, directory=None, wait=None, launch=None):
    directory = Path(directory or data_directory())
    workspace = None
    paths = {}
    exited = moved = False
    data = {}
    launch = launch or launch_application
    try:
        data, paths, workspace = read_update_manifest(manifest, directory)
        executable_digest(paths['source'], data['size'], data['sha256'])
        executable_digest(paths['pending'], data['size'], data['sha256'])
        executable_digest(paths['target'], digest=data['previous'])
        atomic_json(workspace / 'ready.json', {'state': 'ready', 'token': data['token']})
        (wait or wait_update_parent)(data['pid'], workspace / 'cancel')
        exited = True
        if (workspace / 'cancel').exists():
            raise RuntimeError('Update cancelled')
        executable_digest(paths['target'], digest=data['previous'])
        if paths['backup'].exists():
            raise ValueError('Update backup already exists')
        replace_update_file(paths['target'], paths['backup'])
        moved = True
        replace_update_file(paths['pending'], paths['target'])
        executable_digest(paths['target'], data['size'], data['sha256'])
        try:
            atomic_json(directory / 'updates' / 'result.json', {'state': 'done', 'version': data['version']})
        except OSError:
            pass
        launch(paths['target'])
        try:
            paths['backup'].unlink(missing_ok=True)
        except OSError:
            pass
        return 0
    except Exception as error:
        if moved and paths.get('backup', Path()).is_file():
            try:
                replace_update_file(paths['backup'], paths['target'])
            except OSError:
                pass
        if workspace is not None:
            report = {'state': 'error', 'token': data['token'], 'error': error_message(error)}
            try:
                atomic_json(workspace / 'ready.json', report)
                atomic_json(directory / 'updates' / 'result.json', report)
            except OSError:
                pass
        if paths.get('pending', Path()).is_file():
            try:
                paths['pending'].unlink()
            except OSError:
                pass
        if exited and paths.get('target', Path()).is_file():
            try:
                executable_digest(paths['target'], digest=data['previous'])
                launch(paths['target'])
            except (OSError, ValueError):
                pass
        return 1
if sys.platform == 'win32':
    import pymem
    import pymem.process
    PREFIX_SET = ['FFlag', 'DFFlag', 'FInt', 'DFInt', 'FFloat', 'DFFloat', 'FString', 'DFString', 'FLog', 'DFLog']
    _PREFIX_ORDER = sorted(PREFIX_SET, key=len, reverse=True)
    OFFSETS_URL = 'https://offsets.imtheo.lol/fflags.hpp'
    OFFSET_LINE_RE = re.compile(b'(?:inline\\s+constexpr\\s+)?uintptr_t\\s+([A-Za-z_][A-Za-z0-9_]{0,127})\\s*=\\s*0x([0-9a-fA-F]{1,8})')
    RVA_MIN = 1048576
    RVA_MAX = 268435456
    OFFSETS_CACHE_TTL = 600
    _IMPORT_OFFSETS_CACHE = {'at': 0.0, 'names': set()}
    _k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _ntdll = ctypes.WinDLL('ntdll', use_last_error=True)
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k32.GetExitCodeProcess.restype = wintypes.BOOL
    _k32.VirtualProtectEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    _k32.VirtualProtectEx.restype = wintypes.BOOL
    _k32.WriteProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    _k32.WriteProcessMemory.restype = wintypes.BOOL
    _k32.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    _k32.ReadProcessMemory.restype = wintypes.BOOL
    _ntdll.NtWriteVirtualMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    _ntdll.NtWriteVirtualMemory.restype = ctypes.c_long
    _ntdll.NtReadVirtualMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    _ntdll.NtReadVirtualMemory.restype = ctypes.c_long
    _ntdll.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
    _ntdll.NtQueryInformationProcess.restype = ctypes.c_long
    _k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    _k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    _k32.Module32FirstW.restype = wintypes.BOOL
    _k32.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    _k32.Module32NextW.restype = wintypes.BOOL
    _k32.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    _k32.VirtualQueryEx.restype = ctypes.c_size_t

    class MEMORY_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [('BaseAddress', ctypes.c_void_p), ('AllocationBase', ctypes.c_void_p), ('AllocationProtect', wintypes.DWORD), ('PartitionId', wintypes.WORD), ('RegionSize', ctypes.c_size_t), ('State', wintypes.DWORD), ('Protect', wintypes.DWORD), ('Type', wintypes.DWORD)]

    class MODULEENTRY32W(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('th32ModuleID', wintypes.DWORD), ('th32ProcessID', wintypes.DWORD), ('GlblcntUsage', wintypes.DWORD), ('ProccntUsage', wintypes.DWORD), ('modBaseAddr', ctypes.POINTER(ctypes.c_byte)), ('modBaseSize', wintypes.DWORD), ('hModule', wintypes.HMODULE), ('szModule', ctypes.c_wchar * 256), ('szExePath', ctypes.c_wchar * 260)]

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD), ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.POINTER(ctypes.c_ulong)), ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD), ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', ctypes.c_long), ('dwFlags', wintypes.DWORD), ('szExeFile', ctypes.c_wchar * 260)]

    class PROCESS_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [('Reserved1', ctypes.c_void_p), ('PebBaseAddress', ctypes.c_void_p), ('Reserved2', ctypes.c_void_p * 2), ('UniqueProcessId', ctypes.c_void_p), ('Reserved3', ctypes.c_void_p)]
    TH32CS_SNAPPROCESS = 2
    TH32CS_SNAPMODULE = 8
    TH32CS_SNAPMODULE32 = 16
    INVALID_HANDLE = ctypes.c_void_p(-1).value
    PROCESS_VM_READ = 16
    PROCESS_VM_WRITE = 32
    PROCESS_VM_OPERATION = 8
    PROCESS_QUERY_LIMITED = 4096
    PROCESS_ACCESS_STEALTH = PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION
    PAGE_EXECUTE_READWRITE = 64
    MEM_COMMIT = 4096
    MEM_RESERVE = 8192
    _mem_lock = threading.RLock()

    def split_prefix(name):
        for p in _PREFIX_ORDER:
            if name.startswith(p):
                return (p, name[len(p):])
        return ('', name)

    def clean_flag_name(name):
        return split_prefix(name)[1]

    def fetch_importable_flag_names():
        if OFFSET_STORE is not None:
            return {clean_flag_name(name) for name in OFFSET_STORE.document()['flags']}
        now = time.time()
        if _IMPORT_OFFSETS_CACHE['names'] and now - _IMPORT_OFFSETS_CACHE['at'] < OFFSETS_CACHE_TTL:
            return set(_IMPORT_OFFSETS_CACHE['names'])
        response = requests.get(OFFSETS_URL, timeout=10)
        response.raise_for_status()
        names = set()
        for match in OFFSET_LINE_RE.findall(response.content):
            try:
                rva = int(match[1], 16)
                if RVA_MIN <= rva <= RVA_MAX:
                    names.add(clean_flag_name(match[0].decode('ascii')))
            except (UnicodeDecodeError, ValueError):
                continue
        _IMPORT_OFFSETS_CACHE['at'] = now
        _IMPORT_OFFSETS_CACHE['names'] = names
        return set(names)

    def get_screen_center():
        try:
            u = ctypes.windll.user32
            u.SetProcessDPIAware()
            return (u.GetSystemMetrics(0), u.GetSystemMetrics(1))
        except Exception:
            return (1920, 1080)
    _proc_cache = {'at': 0.0, 'pids': []}

    def find_roblox_processes():
        now = time.time()
        if now - _proc_cache['at'] < 1:
            return _proc_cache['pids']
        pids = []
        try:
            for p in pymem.process.list_processes():
                if p.th32ProcessID and 'robloxplayerbeta.exe' in p.szExeFile.decode('utf-8', 'ignore').lower():
                    pids.append(p.th32ProcessID)
        except Exception:
            pass
        _proc_cache['at'] = now
        _proc_cache['pids'] = pids
        return pids

    class AdvancedInjector:

        def __init__(self, pid, console, read_only=False):
            self.pid = pid
            self._h_process = None
            self.base_address = 0
            self.is_attached = False
            self.attach_time = time.time()
            self.offsets = {}
            self.offsets_fetched_at = 0.0
            self._live_addrs = {}
            self._live_addrs_pid = None
            self._locked_attrs = set()
            self._locked_attrs_pid = None
            self._locked_value_ptrs = set()
            self._locked_value_ptrs_pid = None
            self._verify_lock_running = False
            self.console = console
            self.read_only = read_only
            self.sections = []
            self.image_size = 0
            self.identity = {}
            if struct.calcsize('P') != 8:
                raise RuntimeError('64-bit Python required')
            if not self.open_process_for_write(write_access=not read_only):
                raise RuntimeError('Roblox process access denied')
            try:
                if not self.get_roblox_base():
                    raise RuntimeError('Roblox base unavailable')
                self._load_image()
            except Exception:
                self.close()
                raise

        def open_process_for_write(self, write_access=True):
            if not self.pid:
                return False
            if self._h_process:
                return True
            access = PROCESS_VM_READ | PROCESS_QUERY_LIMITED | 1024
            if write_access:
                access |= PROCESS_VM_WRITE | PROCESS_VM_OPERATION
            ladder = [access]
            last_err = 0
            for access in ladder:
                h = _k32.OpenProcess(access, False, self.pid)
                if h:
                    self._h_process = h
                    self.is_attached = True
                    return True
                last_err = ctypes.get_last_error()
            self.console(f'[!] OpenProcess failed (err {last_err})')
            return False

        def alive(self):
            if not self._h_process or not self.is_attached:
                return False
            code = wintypes.DWORD()
            return bool(_k32.GetExitCodeProcess(self._h_process, ctypes.byref(code)) and code.value == 259)

        def read(self, address, size):
            if not self._h_process or not 0 < size <= 1024 * 1024 or (not 65536 <= address < 1 << 47):
                return None
            with _mem_lock:
                buffer = ctypes.create_string_buffer(size)
                count = ctypes.c_size_t()
                ok = _k32.ReadProcessMemory(self._h_process, ctypes.c_void_p(address), buffer, size, ctypes.byref(count))
                return buffer.raw if ok and count.value == size else None

        def _load_image(self):
            dos = self.read(self.base_address, 64)
            if not dos or dos[:2] != b'MZ':
                raise ValueError('Invalid Roblox image')
            pe_offset = struct.unpack_from('<I', dos, 60)[0]
            if not 64 <= pe_offset <= 65536:
                raise ValueError('Invalid PE header')
            header = self.read(self.base_address + pe_offset, 264)
            if not header or header[:4] != b'PE\x00\x00' or struct.unpack_from('<H', header, 4)[0] != 34404:
                raise ValueError('64-bit Roblox required')
            count = struct.unpack_from('<H', header, 6)[0]
            timestamp = struct.unpack_from('<I', header, 8)[0]
            optional_size = struct.unpack_from('<H', header, 20)[0]
            if not 1 <= count <= 96 or not 112 <= optional_size <= 4096 or struct.unpack_from('<H', header, 24)[0] != 523:
                raise ValueError('Invalid image sections')
            self.image_size = struct.unpack_from('<I', header, 80)[0]
            if not 65536 <= self.image_size <= 2147483648:
                raise ValueError('Invalid image size')
            raw = self.read(self.base_address + pe_offset + 24 + optional_size, count * 40)
            if not raw:
                raise ValueError('Image sections unreadable')
            for index in range(count):
                size, rva, raw_size = struct.unpack_from('<III', raw, index * 40 + 8)
                characteristics = struct.unpack_from('<I', raw, index * 40 + 36)[0]
                size = max(size, raw_size)
                if rva < 4096 or rva + size > self.image_size:
                    raise ValueError('Invalid image section bounds')
                self.sections.append({'rva': rva, 'size': size, 'writable': bool(characteristics & 2147483648), 'executable': bool(characteristics & 536870912)})
            path = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(path))
            if not _k32.QueryFullProcessImageNameW(self._h_process, 0, path, ctypes.byref(length)):
                raise RuntimeError('Roblox version unreadable')
            version = re.search('version-[a-fA-F0-9]{8,64}', path.value)
            self.identity = {'version': version.group(0).lower() if version else '', 'timestamp': timestamp, 'image_size': self.image_size}

        def is_data_address(self, address, size=1):
            rva = address - self.base_address
            return any((section['writable'] and (not section['executable']) and (section['rva'] <= rva) and (rva + size <= section['rva'] + section['size']) for section in self.sections))

        def get_roblox_base(self):
            if self.base_address:
                return self.base_address
            if not self._h_process and (not self.open_process_for_write()):
                pass
            if self._h_process:
                try:
                    pbi = PROCESS_BASIC_INFORMATION()
                    rl = ctypes.c_ulong(0)
                    st = _ntdll.NtQueryInformationProcess(self._h_process, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), ctypes.byref(rl))
                    if st == 0 and pbi.PebBaseAddress:
                        buf = ctypes.create_string_buffer(8)
                        br = ctypes.c_size_t(0)
                        rd = _ntdll.NtReadVirtualMemory(self._h_process, ctypes.c_void_p(pbi.PebBaseAddress + 16), buf, 8, ctypes.byref(br))
                        if rd == 0 and br.value == 8:
                            self.base_address = struct.unpack('<Q', buf.raw[:8])[0]
                            self.console(f'[+] Roblox base (PEB): 0x{self.base_address:X}')
                            return self.base_address
                except Exception:
                    pass
            try:
                snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, self.pid)
                if snap and snap != INVALID_HANDLE:
                    me = MODULEENTRY32W()
                    me.dwSize = ctypes.sizeof(MODULEENTRY32W)
                    if _k32.Module32FirstW(snap, ctypes.byref(me)):
                        while True:
                            if me.szModule.lower() == 'robloxplayerbeta.exe':
                                self.base_address = ctypes.cast(me.modBaseAddr, ctypes.c_void_p).value or 0
                                _k32.CloseHandle(snap)
                                self.console(f'[+] Roblox base (Toolhelp32): 0x{self.base_address:X}')
                                return self.base_address
                            if not _k32.Module32NextW(snap, ctypes.byref(me)):
                                break
                    _k32.CloseHandle(snap)
            except Exception as e:
                self.console(f'[!] get_roblox_base error: {e}')
            return 0

        def fetch_offsets(self, force=False):
            if OFFSET_STORE is not None:
                try:
                    document = OFFSET_STORE.document(identity=self.identity, force=force)
                    if not matches_identity(document, self.identity):
                        raise ValueError('Offsets mismatch; dump this version')
                    offsets = {}
                    for name, rva in document['flags'].items():
                        if self.is_data_address(self.base_address + rva):
                            clean = clean_flag_name(name)
                            if clean in offsets and offsets[clean] != rva:
                                raise ValueError('Ambiguous offsets')
                            offsets[clean] = rva
                    if not offsets:
                        raise ValueError('No writable flag offsets')
                    self.offsets = offsets
                    self.offsets_fetched_at = time.monotonic()
                    return True
                except Exception as error:
                    self.console(f'[!] {error}')
                    self.offsets = {}
                    raise
            if not force and self.offsets and (time.time() - self.offsets_fetched_at < OFFSETS_CACHE_TTL):
                return True
            try:
                r = requests.get(OFFSETS_URL, timeout=10)
                r.raise_for_status()
                raw = OFFSET_LINE_RE.findall(r.content)
                offsets = {}
                for match in raw:
                    try:
                        name = match[0].decode('ascii')
                        rva = int(match[1], 16)
                        if RVA_MIN <= rva <= RVA_MAX:
                            offsets[clean_flag_name(name)] = rva
                    except Exception:
                        continue
                self.offsets = offsets
                self.offsets_fetched_at = time.time()
                self.console(f'[+] Loaded {len(self.offsets)} offsets')
                return True
            except Exception as e:
                self.console(f'[!] Offset fetch failed: {e}')
                return False

        def query_region(self, addr):
            if not self._h_process and (not self.open_process_for_write()):
                return None
            mbi = MEMORY_BASIC_INFORMATION()
            if not _k32.VirtualQueryEx(self._h_process, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)):
                return None
            return {'base': mbi.BaseAddress or 0, 'alloc_base': mbi.AllocationBase or 0, 'size': mbi.RegionSize, 'state': mbi.State, 'protect': mbi.Protect, 'type': mbi.Type}

        def scan_live_flags(self, names=None, force_rescan=False):
            with _mem_lock:
                if not force_rescan and self._live_addrs_pid == self.pid and self._live_addrs:
                    return self._live_addrs
                if not self.is_attached:
                    return {}
                base = self.get_roblox_base()
                if not base:
                    return {}
                clean_targets = {clean_flag_name(n) for n in names} if names else None
                wanted = self.offsets if clean_targets is None else {k: v for k, v in self.offsets.items() if k in clean_targets}
                live = {}
                for clean, rva in wanted.items():
                    live[clean] = [{'abs_addr': base + rva, 'full_name': clean, 'type': 'unknown', 'source': 'offsets'}]
                self._live_addrs = live
                self._live_addrs_pid = self.pid
                self.console(f'[+] Live flags: {len(live)}')
                return live

        def get_live_flag_address(self, name):
            with _mem_lock:
                if self._live_addrs_pid != self.pid:
                    return None
                return self._live_addrs.get(clean_flag_name(name)) or self._live_addrs.get(name)

        def read_flag_at_address(self, flag_type, abs_addr):
            if not self._h_process:
                return None
            if flag_type in ('bool', 'log'):
                size = 1
            elif flag_type in ('int', 'float'):
                size = 4
            else:
                return None
            if not self.is_data_address(abs_addr, size):
                return None
            buf = ctypes.create_string_buffer(size)
            br = ctypes.c_size_t(0)
            st = _ntdll.NtReadVirtualMemory(self._h_process, ctypes.c_void_p(abs_addr), buf, ctypes.c_size_t(size), ctypes.byref(br))
            if st == 0 and br.value == size:
                if flag_type == 'bool':
                    if buf.raw[0] not in (0, 1):
                        return None
                    return 'true' if buf.raw[0] else 'false'
                if flag_type == 'log':
                    return str(buf.raw[0])
                if flag_type == 'int':
                    return str(struct.unpack('<i', buf.raw[:4])[0])
                value = struct.unpack('<f', buf.raw[:4])[0]
                return repr(value) if math.isfinite(value) else None
            return None

        def write_flag_at_address(self, flag_type, abs_addr, value):
            if not self._h_process:
                return (False, 'No process handle')
            if flag_type == 'bool':
                text = str(value).strip().lower()
                if text not in ('true', 'false', '1', '0', 'yes', 'no'):
                    return (False, 'Invalid boolean')
                val = text in ('true', '1', 'yes')
                data = struct.pack('<B', 1 if val else 0)
            elif flag_type == 'int':
                try:
                    v = int(value)
                    data = struct.pack('<i', v)
                except (ValueError, struct.error):
                    return (False, f'Invalid int: {value}')
            elif flag_type == 'log':
                try:
                    data = struct.pack('<B', int(value))
                except (ValueError, struct.error):
                    return (False, 'Invalid log level')
            elif flag_type == 'float':
                try:
                    number = float(value)
                    if not math.isfinite(number):
                        return (False, 'Invalid float')
                    data = struct.pack('<f', number)
                except (ValueError, OverflowError, struct.error):
                    return (False, f'Invalid float: {value}')
            elif flag_type == 'string':
                return (False, 'Live string replacement unsupported')
            else:
                return (False, f'Unsupported type: {flag_type}')
            return self._write_raw(abs_addr, data)

        def _write_raw(self, abs_addr, data):
            with _mem_lock:
                if len(data) not in (1, 4) or (len(data) == 4 and abs_addr % 4) or self.read_only or (not self.alive()) or (not self.is_data_address(abs_addr, len(data))):
                    return (False, 'Invalid process or address')
                info = self.query_region(abs_addr)
                if not info or info['state'] != MEM_COMMIT or info['protect'] not in (4, 8) or (abs_addr + len(data) > info['base'] + info['size']):
                    return (False, 'Flag memory is not writable')
                size = len(data)
                buf = ctypes.create_string_buffer(data)
                bw = ctypes.c_size_t(0)
                st = _ntdll.NtWriteVirtualMemory(self._h_process, ctypes.c_void_p(abs_addr), ctypes.byref(buf), ctypes.c_size_t(size), ctypes.byref(bw))
                if st == 0 and bw.value == size:
                    return (True, f'OK|NtWrite (0x{abs_addr:X})')
                return (False, f'Write failed: 0x{abs_addr:X}; status=0x{st & 4294967295:08X}')

        def close(self):
            self._verify_lock_running = False
            with _mem_lock:
                if self._h_process:
                    _k32.CloseHandle(self._h_process)
                    self._h_process = None
                self.is_attached = False

    class FlagManager:

        def __init__(self, console_cb):
            self.console = console_cb
            self.injector = None
            self.active_flags = {}
            self.running = False
            self.turbo_thread = None
            self.injection_active = False

        def stop(self):
            self.running = False
            self.injection_active = False
            t = self.turbo_thread
            if t and t.is_alive():
                t.join(timeout=0.2)

class _ReleaseEngine:

    def __getattr__(self, name):
        try:
            return globals()[name]
        except KeyError:
            raise AttributeError(name) from None
PREFIXES = ('DFFlag', 'FFlag', 'DFFloat', 'FFloat', 'DFString', 'FString', 'DFInt', 'FInt', 'DFLog', 'FLog')
MAX_FLAGS = 5000

def flag_parts(name):
    name = str(name).strip()
    if not re.fullmatch('[A-Za-z_][A-Za-z0-9_]{0,159}', name):
        raise ValueError('Invalid FFlag name')
    for prefix in PREFIXES:
        if name.startswith(prefix):
            if name == prefix:
                raise ValueError('Missing FFlag name')
            return (prefix, name[len(prefix):])
    return ('', name)

def flag_type(name, value):
    prefix, _ = flag_parts(name)
    if 'String' in prefix:
        return 'string'
    if 'Float' in prefix:
        return 'float'
    if 'Log' in prefix:
        return 'log'
    if 'Int' in prefix:
        return 'int'
    if 'Flag' in prefix:
        return 'bool'
    text = str(value).strip().lower()
    if text in ('true', 'false'):
        return 'bool'
    try:
        int(text)
        return 'int'
    except ValueError:
        try:
            float(text)
            return 'float'
        except ValueError:
            return 'string'

def normalize_flag(name, value, enabled=True, source='raw'):
    if isinstance(value, (dict, list)) or value is None:
        raise ValueError('Invalid FFlag value')
    prefix, suffix = flag_parts(name)
    value = str(value).lower() if isinstance(value, bool) else str(value)
    if len(value.encode('utf-8')) > 4096:
        raise ValueError('FFlag value too long')
    kind = flag_type(name, value)
    if kind == 'bool' and value.strip().lower() not in ('true', 'false', '1', '0', 'yes', 'no'):
        raise ValueError('Use true or false')
    if kind in ('int', 'log'):
        try:
            number = int(value)
        except ValueError:
            raise ValueError('Invalid integer') from None
        if kind == 'log' and (not 0 <= number <= 255):
            raise ValueError('Log level: 0–255')
        if kind == 'int' and (not -2147483648 <= number <= 2147483647):
            raise ValueError('32-bit integer required')
    if kind == 'float':
        import math
        try:
            number = float(value)
        except ValueError:
            raise ValueError('Invalid number') from None
        if not math.isfinite(number) or abs(number) > 3.402823466e+38:
            raise ValueError('Invalid number')
    return {'name': prefix + suffix, 'prefix': prefix, 'suffix': suffix, 'value': value, 'type': kind, 'enabled': bool(enabled), 'source': source}

def scalar_value(kind, value):
    if kind == 'bool':
        return 'true' if str(value).strip().lower() in ('true', '1', 'yes') else 'false'
    if kind in ('int', 'log'):
        return str(int(value))
    if kind == 'float':
        return repr(struct.unpack('<f', struct.pack('<f', float(value)))[0])
    raise ValueError('Unsupported scalar type')

class LockFController:

    def __init__(self, logger, engine=None, platform=None):
        self.log = logger
        self.engine = engine
        self.platform = sys.platform if platform is None else platform
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._rows = []
        self._generation = 0
        self._applied_generation = -1
        self._manager = None
        self._pid = 0
        self._originals = {}
        self._applied_values = {}
        self._lock_threads = []
        self._retry_at = 0.0
        self._refresh_offsets = False
        self._flag_retry_at = 0.0
        self._LockF = True
        self._pending_apply = 0
        self._request_serial = 0
        self._LockF_blocked = False
        self._last_write_at = 0.0
        self._last_write_generation = -1
        self._settle_seconds = 3 if engine is None else 0
        self._seen_pids = {}
        self._state = {'state': 'waiting', 'pid': 0, 'applied': 0, 'failed': 0, 'error': '', 'flags': {}, 'offsets': 0}

    def set_flags(self, rows):
        with self._lock:
            if rows == self._rows:
                return
            self._rows = copy.deepcopy(rows)
            self._generation += 1
            self._LockF_blocked = False
        self._wake.set()

    def set_LockF(self, enabled):
        with self._lock:
            enabled = bool(enabled)
            if enabled != self._LockF:
                self._LockF = enabled
                if enabled:
                    self._LockF_blocked = False
        self._wake.set()

    def request_apply(self):
        if self._stop.is_set():
            raise RuntimeError('Application is closing')
        with self._lock:
            self._request_serial += 1
            self._pending_apply = self._request_serial
            self._LockF_blocked = False
            self._retry_at = 0
        self._wake.set()

    def _finish_request(self, request):
        with self._lock:
            if self._pending_apply == request:
                self._pending_apply = 0

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._state)

    def _status(self, **values):
        with self._lock:
            if any((self._state.get(key) != value for key, value in values.items())):
                self._state.update(values)
                self._state['revision'] = self._state.get('revision', 0) + 1

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name='syntax-LockF', daemon=True)
            self._thread.start()

    def close(self):
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=12)
        return not self._thread or not self._thread.is_alive()

    def offsets(self, force=False):
        if self.engine is None:
            raise RuntimeError('Windows required')
        if force:
            self.engine._IMPORT_OFFSETS_CACHE['at'] = 0
        names = self.engine.fetch_importable_flag_names()
        return sorted(names, key=str.casefold)

    def refresh(self):
        with self._lock:
            self._generation += 1
            self._refresh_offsets = True
        self._retry_at = 0
        self._wake.set()

    def _stop_turbo(self):
        if self._manager:
            self._manager.stop()
            thread = self._manager.turbo_thread
            if thread and thread.is_alive():
                thread.join(timeout=6)
                if thread.is_alive():
                    raise RuntimeError('Worker still running')

    def _restore(self, names):
        if not self._manager or not self._manager.injector:
            return
        injector = self._manager.injector
        for name in names:
            record = self._originals.get(name)
            if record is None:
                continue
            kind, address, original = record
            current = injector.read_flag_at_address(kind, address)
            previous = self._applied_values.get(name)
            if current is None or previous is None or current != previous[2]:
                self._originals.pop(name, None)
                self._applied_values.pop(name, None)
                continue
            if current == original:
                self._originals.pop(name, None)
                self._applied_values.pop(name, None)
                continue
            ok, detail = injector.write_flag_at_address(kind, address, original)
            if ok:
                del self._originals[name]
                self._applied_values.pop(name, None)
            else:
                self.log(f'[!] Restore failed: {name}')

    def _disconnect(self, restore=False):
        injector = self._manager.injector if self._manager else None
        try:
            if self._manager:
                self._stop_turbo()
            if injector:
                injector._verify_lock_running = False
                for thread in self._lock_threads:
                    thread.join(timeout=6)
                if restore:
                    with self.engine._mem_lock:
                        self._restore(list(self._originals))
        except Exception as error:
            self.log(f'[!] Cleanup failed: {error}')
        finally:
            try:
                if injector:
                    injector.close()
            except Exception as error:
                self.log(f'[!] Close failed: {error}')
            self._manager = None
            self._pid = 0
            self._originals.clear()
            self._applied_values.clear()
            self._lock_threads.clear()
            self._applied_generation = -1

    def _connect(self, pid):
        self._status(state='connecting', error='', pid=pid)
        manager = self.engine.FlagManager(self.log)
        try:
            manager.injector = self.engine.AdvancedInjector(pid, self.log)
            if not manager.injector.get_roblox_base():
                raise RuntimeError('Roblox address unavailable')
            if not manager.injector.fetch_offsets() or not manager.injector.offsets:
                raise RuntimeError('Offsets unavailable')
        except Exception:
            if manager.injector:
                manager.injector.close()
            raise
        self._manager = manager
        self._pid = pid
        self.log(f'[+] Roblox ready: {pid}')

    def _apply(self, rows, generation, force=False):
        with self._lock:
            if generation != self._generation or self._stop.is_set() or not force and not self._LockF:
                return
        self._stop_turbo()
        manager = self._manager
        injector = manager.injector
        self._status(state='applying', error='')
        selected = {r['name']: r for r in rows if r.get('enabled', True)}
        old_names = set(self._originals)
        with self.engine._mem_lock:
            self._restore(old_names - set(selected))
            live = injector.scan_live_flags(list(selected), force_rescan=True)
            active, results = ({}, {})
            applied = failed = 0
            completed = True
            for row in rows:
                with self._lock:
                    if self._stop.is_set() or generation != self._generation or not force and not self._LockF:
                        completed = False
                        break
                name, kind = (row['name'], row['type'])
                if not row.get('enabled', True):
                    results[name] = 'disabled'
                    continue
                if row.get('needs_type') or not row.get('prefix'):
                    results[name] = 'unknown'
                    failed += 1
                    continue
                if kind == 'string':
                    results[name] = 'unsupported'
                    failed += 1
                    continue
                clean = self.engine.clean_flag_name(name)
                entries = live.get(clean, [])
                if not entries:
                    results[name] = 'missing'
                    failed += 1
                    continue
                success = False
                expected = scalar_value(kind, row['value'])
                for entry in entries:
                    address = entry['abs_addr']
                    previous = self._applied_values.get(name)
                    if not force and previous and previous[:2] == (kind, row['value']) and (self._originals.get(name, ())[:2] == (kind, address)):
                        success = True
                        continue
                    current = injector.read_flag_at_address(kind, address)
                    if current is None:
                        continue
                    original = self._originals.get(name)
                    if original is not None and original[:2] != (kind, address):
                        self._restore([name])
                        if name in self._originals:
                            self.log(f'[!] {name} · Restore failed')
                            continue
                    if name not in self._originals:
                        self._originals[name] = (kind, address, current)
                    if current == expected:
                        self._applied_values[name] = (kind, row['value'], current)
                        success = True
                        continue
                    ok, detail = injector.write_flag_at_address(kind, address, row['value'])
                    if ok:
                        self._last_write_at = time.monotonic()
                        self._last_write_generation = generation
                        readback = injector.read_flag_at_address(kind, address)
                        if readback == expected:
                            success = True
                            self._applied_values[name] = (kind, row['value'], readback)
                        else:
                            self.log(f'[!] {name} · Verification changed')
                    else:
                        detail = str(detail).split('; applied via JSON', 1)[0]
                        self.log(f'[!] {name} · {detail}')
                results[name] = 'applied' if success else 'failed'
                if success:
                    applied += 1
                    active[clean] = dict(row)
                else:
                    failed += 1
            manager.active_flags = active
        manager.injection_active = bool(active)
        self._applied_generation = generation if completed else -1
        self._flag_retry_at = 0
        self._status(state='active' if applied and (not failed) else 'partial' if applied else 'error', pid=self._pid, applied=applied, failed=failed, flags=results, offsets=len(injector.offsets), error='' if applied else 'FFlags not applied')
        if not completed:
            self._status(state='pending', error='Apply pending')
        self.log(f'[=] Applied: {applied}. Failed: {failed}')

    def _tick(self):
        with self._lock:
            rows, generation = (self._rows, self._generation)
            LockF, request = (self._LockF, self._pending_apply)
        pids = self.engine.find_roblox_processes()
        now = time.monotonic()
        self._seen_pids = {pid: self._seen_pids.get(pid, now) for pid in pids[:32]}
        if self._pid and (self._pid not in pids or not self._manager.injector.alive()):
            if self._last_write_at and time.monotonic() - self._last_write_at < 10 and self._last_write_generation == generation:
                with self._lock:
                    self._LockF_blocked = True
                self.log('[!] Lock FFlags paused')
            self._disconnect()
            self._status(state='waiting', pid=0, applied=0, failed=0, flags={}, error='')
        if self._LockF_blocked and not request:
            self._status(state='paused', pid=0, applied=0, failed=0, flags={}, error='Review flags; click Apply')
            return
        if not pids:
            self._status(state='waiting', pid=0, applied=0, failed=0, flags={}, error='')
            return
        if not LockF and not request:
            if generation != self._applied_generation:
                self._status(state='pending', pid=pids[0], error='Apply pending')
            return
        if not request and not self._manager and now - self._seen_pids[pids[0]] < self._settle_seconds:
            self._status(state='settling', pid=pids[0], error='Waiting for client startup')
            return
        enabled = any((row.get('enabled', True) for row in rows))
        if not enabled:
            if self._manager:
                self._disconnect(restore=True)
            self._status(state='ready', pid=pids[0], applied=0, failed=0, error='', flags={row['name']: 'disabled' for row in rows})
            self._finish_request(request)
            return
        if not self._manager:
            self._connect(pids[0])
        with self._lock:
            refresh = self._refresh_offsets
            self._refresh_offsets = False
        if refresh and (not self._manager.injector.fetch_offsets()):
            raise RuntimeError('Offset refresh failed')
        if refresh:
            self._restore(list(self._originals))
            self._applied_values.clear()
        with self._lock:
            rows, generation = (self._rows, self._generation)
            LockF, current_request = (self._LockF, self._pending_apply)
        if self._stop.is_set():
            return
        if not LockF and not current_request:
            self._status(state='pending', pid=pids[0], error='Apply pending')
            return
        request = current_request
        if not any((row.get('enabled', True) for row in rows)):
            self._disconnect(restore=True)
            self._status(state='ready', pid=pids[0], applied=0, failed=0, error='', flags={row['name']: 'disabled' for row in rows})
            self._finish_request(request)
            return
        if generation != self._applied_generation or request:
            self._apply(rows, generation, force=bool(request))
            self._finish_request(request)

    def _run(self):
        if self.platform != 'win32' and self.engine is None:
            self._status(state='unsupported', error='Windows required')
            return
        try:
            if self.engine is None:
                self.engine = _ReleaseEngine()
                self.engine._k32.VirtualQueryEx.argtypes = [self.engine.wintypes.HANDLE, self.engine.ctypes.c_void_p, self.engine.ctypes.POINTER(self.engine.MEMORY_BASIC_INFORMATION), self.engine.ctypes.c_size_t]
            while not self._stop.is_set():
                self._wake.wait(1.5)
                self._wake.clear()
                if self._stop.is_set():
                    break
                if time.monotonic() < self._retry_at:
                    continue
                try:
                    self._tick()
                except Exception as error:
                    self.log(f'[!] {error}')
                    self._disconnect(restore=True)
                    self._status(state='error', pid=0, applied=0, failed=0, flags={}, error=str(error))
                    self._retry_at = time.monotonic() + 5
        except Exception as error:
            self.log(f'[!] Connection failed: {error}')
            self._status(state='error', error=str(error))
        finally:
            self._disconnect(restore=True)
URL = 'https://offsets.imtheo.lol/fflags.hpp'
MAX_BYTES = 4 * 1024 * 1024
CACHE_SECONDS = 600
OFFSET_RE = re.compile('uintptr_t\\s+([A-Za-z_][A-Za-z0-9_]{0,159})\\s*=\\s*0x([0-9a-fA-F]{1,16})')
NAMESPACE_RE = re.compile('namespace\\s+FFlags\\s*\\{(.*?)^\\s*\\}', re.S | re.M)

def parse_catalog(source):
    if len(source.encode('utf-8')) > MAX_BYTES:
        raise ValueError('Catalog too large')
    section = NAMESPACE_RE.search(source)
    if not section:
        raise ValueError('FFlags namespace missing')
    entries = {}
    for name, address in OFFSET_RE.findall(section.group(1)):
        address = int(address, 16)
        if 1048576 <= address <= 268435456:
            entries[name] = {'name': name, 'rva': hex(address)}
    if not entries:
        raise ValueError('No FFlags found')
    return sorted(entries.values(), key=lambda item: item['name'].casefold())

class OffsetCatalog:

    def __init__(self, get=None, store=None):
        self._get = get or requests.get
        self.store = store
        self._lock = threading.RLock()
        self._entries = []
        self._lookup = {}
        self._at = 0
        self.known_names = None
        self.revision = 0
        self._store_revision = -1

    def entries(self, force=False):
        with self._lock:
            if self._entries and (not force) and (time.monotonic() - self._at < CACHE_SECONDS) and (self.store is None or self._store_revision == self.store.revision):
                return self._entries
            if self.store:
                document = self.store.document(force=force)
                entries = sorted(({'name': name, 'rva': hex(rva)} for name, rva in document['flags'].items()), key=lambda entry: entry['name'].casefold())
                self._store_revision = self.store.revision
            else:
                entries = parse_catalog(download(URL, self._get).decode('utf-8-sig'))
            self._entries = entries
            self._lookup = {entry['name']: entry for entry in entries}
            self.known_names = frozenset((flag_parts(entry['name'])[1] for entry in entries))
            self.revision += 1
            self._at = time.monotonic()
            return entries

    def find(self, name):
        self.entries()
        with self._lock:
            return self._lookup.get(name)

    def search(self, query='', page=0, limit=60, force=False):
        query = str(query).strip().casefold()[:160]
        page, limit = (max(0, int(page)), max(1, min(100, int(limit))))
        entries = self.entries(force)
        filtered = [entry for entry in entries if query in entry['name'].casefold()] if query else entries
        start = page * limit
        return {'items': filtered[start:start + limit], 'total': len(filtered), 'count': len(entries), 'page': page, 'more': start + limit < len(filtered)}
APP_NAME = 'Syntax'
DISPLAY_VERSION = 'v' + (VERSION[:-2] if VERSION.endswith('.0') else VERSION)
DISCORD_URL = 'https://discord.gg/98HWke3zMg'
DISCORD_APP_ID = '1552123891118243901'
RESOURCES = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
MAX_IMPORT_BYTES = 4 * 1024 * 1024

def error_message(error):
    if isinstance(error, json.JSONDecodeError):
        return 'Invalid JSON'
    if isinstance(error, OSError):
        return 'File operation failed'
    message = str(error)
    return message if len(message.split()) <= 4 else 'Operation failed'

def render_html():
    html = (RESOURCES / 'ui.html').read_text(encoding='utf-8')
    html = html.replace('Syntax v2.0</title>', 'Syntax ' + DISPLAY_VERSION + '</title>').replace('>v2.0<', '>' + DISPLAY_VERSION + '<')
    icon = 'data:image/x-icon;base64,' + base64.b64encode((RESOURCES / 'syntax.ico').read_bytes()).decode('ascii')
    html = html.replace('src="syntax.ico"', f'src="{icon}"').replace('href="syntax.ico"', f'href="{icon}"')
    return html.replace('window.syntaxLogos = {};', 'window.syntaxLogos=' + json.dumps(theme_logos(RESOURCES)) + ';')

def data_directory():
    if sys.platform == 'win32':
        return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local')) / 'Syntax'
    return Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'Syntax'

class JsonObjectPairs(list):
    pass

def ensure_row_ids(rows):
    result, used = (copy.deepcopy(rows), set())
    for row in result:
        identifier = row.get('id', '')
        if not isinstance(identifier, str) or not re.fullmatch('[a-f0-9]{32}', identifier) or identifier in used:
            identifier = uuid.uuid4().hex
        row['id'] = identifier
        used.add(identifier)
        row.pop('missing', None)
        row.pop('duplicate', None)
    return result

def annotate_rows(rows, names):
    counts = Counter((row['suffix'] for row in rows))
    return [{**copy.deepcopy(row), 'duplicate': counts[row['suffix']] > 1, 'missing': None if names is None else row['suffix'] not in names} for row in rows]

def execution_rows(rows):
    counts = Counter((row['suffix'] for row in rows))
    return [copy.deepcopy(row) for row in rows if counts[row['suffix']] == 1]

def parse_flags(content):
    try:
        data = json.loads(content, object_pairs_hook=JsonObjectPairs)
    except RecursionError as error:
        raise ValueError('JSON nested too deeply') from error
    if isinstance(data, (dict, list)) and len(data) > MAX_FLAGS:
        raise ValueError(f'Maximum {MAX_FLAGS} FFlags')
    if isinstance(data, JsonObjectPairs):
        rows = [normalize_flag(name, value) for name, value in data]
    elif isinstance(data, dict):
        rows = [normalize_flag(name, value) for name, value in data.items()]
    elif isinstance(data, list):
        rows = []
        for entry in data:
            if isinstance(entry, JsonObjectPairs):
                entry = dict(entry)
            if not isinstance(entry, dict):
                raise ValueError('Invalid FFlag entry')
            name = entry.get('name') or str(entry.get('prefix', '')) + str(entry.get('suffix', ''))
            row = normalize_flag(name, entry.get('value'), entry.get('enabled', True), entry.get('source', 'raw'))
            if entry.get('needs_type'):
                row.update(type='unknown', needs_type=True, enabled=False)
            if 'prefix_pending_enabled' in entry:
                row['prefix_pending_enabled'] = bool(entry['prefix_pending_enabled'])
            row['id'] = entry.get('id', '')
            rows.append(row)
    else:
        raise ValueError('FFlag JSON required')
    if len(rows) > MAX_FLAGS:
        raise ValueError(f'Maximum {MAX_FLAGS} FFlags')
    return ensure_row_ids(rows)

LEGACY_LOCKF_KEY = 'auto_apply'

class Settings:

    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / 'status.json'
        self.lock = threading.RLock()
        self.data = {'version': VERSION, 'f': [], 'rpc': True, 'motion': True, 'snow': True, 'saved_presets': [], 'preset_overrides': {}, 'theme': 'purple', 'tray': True, 'startup': True, 'desktop_defaults': 1, 'offset_source': 'theo', 'LockF': True}
        self.load_error = ''
        if self.path.exists():
            try:
                if self.path.stat().st_size > 16 * MAX_IMPORT_BYTES:
                    raise ValueError('Settings file too large')
                raw = config_json(read_limited(self.path, 16 * MAX_IMPORT_BYTES))
                if not isinstance(raw, dict):
                    raise ValueError('Invalid settings file')
                self.data['f'] = parse_flags(json.dumps(raw.get('f', [])))
                self.data['rpc'] = bool(raw.get('rpc', True))
                self.data['motion'] = bool(raw.get('motion', True))
                self.data['snow'] = bool(raw.get('snow', True))
                self.data['LockF'] = bool(raw.get('LockF', raw.get(LEGACY_LOCKF_KEY, True)))
                if raw.get('desktop_defaults') == 1:
                    for key in ('tray', 'startup'):
                        self.data[key] = bool(raw.get(key, True))
                self.data['theme'] = raw.get('theme') if raw.get('theme') in THEMES else 'purple'
                self.data['offset_source'] = raw.get('offset_source') if raw.get('offset_source') in ('theo', 'local') else 'theo'
                saved = raw.get('saved_presets', [])
                for preset in saved[:100] if isinstance(saved, list) else []:
                    try:
                        self.data['saved_presets'].append({'id': str(preset['id']), 'name': str(preset['name'])[:60], 'rows': parse_flags(json.dumps(preset['rows']))})
                    except (ValueError, TypeError, KeyError):
                        continue
                overrides = raw.get('preset_overrides', {})
                for identifier, rows in list(overrides.items())[:100] if isinstance(overrides, dict) else []:
                    try:
                        self.data['preset_overrides'][identifier] = parse_flags(json.dumps(rows))
                    except (ValueError, TypeError):
                        continue
            except (ValueError, OSError, TypeError, AttributeError) as error:
                self.load_error = str(error)
                try:
                    shutil.copy2(self.path, self.path.with_suffix('.corrupt-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.json'))
                except OSError:
                    pass

    def get(self, key):
        with self.lock:
            return copy.deepcopy(self.data[key])

    def set(self, key, value):
        self.set_many({key: value})

    def set_many(self, values):
        with self.lock:
            updated = copy.deepcopy(self.data)
            updated.update(copy.deepcopy(values))
            updated['version'] = VERSION
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.json.tmp')
            try:
                with temporary.open('w', encoding='utf-8') as stream:
                    json.dump(updated, stream, ensure_ascii=False, indent=2, allow_nan=False)
                    stream.flush()
                    if stream.tell() > 16 * MAX_IMPORT_BYTES:
                        raise ValueError('Settings file too large')
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
            self.data = updated
BUILTIN_PRESET_FILES = {'black-box': {'FIntCameraFarZPlane': {'value': '250', 'control': {'label': 'Black Box', 'type': 'dropdown', 'options': {'Near': '80', 'Medium': '250', 'Far': '500', 'Huge': '1000'}}}, 'otherdatainblackboxthingy': {'name': 'Black Box'}}, 'fps-boost': {'DFIntDebugDynamicRenderKiloPixels': {'value': '37', 'control': {'label': 'Render KiloPixels', 'type': 'dropdown', 'options': {'144p': '37', '240p': '77', '360p': '230', '480p': '410', '720p': '922'}}}, 'FIntCameraFarZPlane': {'value': '80', 'control': {'label': 'Camera Distance', 'type': 'dropdown', 'options': {'Near': '80', 'Medium': '250', 'Far': '500'}}}, 'preset': {'name': 'FPS Boost'}}, 'fps-max': {'RenderTextureTotalBudgetCount': {'value': '0', 'control': {'label': 'No Textures', 'type': 'button', 'value': '0'}}, 'TextureCompositorActiveJobs': {'value': '0', 'control': {'label': 'No Compositing', 'type': 'button', 'value': '0'}}, 'preset': {'name': 'FPS Max'}}, 'fps-performance': {'DFIntDebugDynamicRenderKiloPixels': {'value': '230', 'control': {'label': 'Render KiloPixels', 'type': 'dropdown', 'options': {'144p': '37', '240p': '77', '360p': '230', '480p': '410', '720p': '922'}}}, 'FRMFinalDistanceMaxStud': {'value': '250', 'control': {'label': 'Max Studs', 'type': 'integer'}}, 'preset': {'name': 'FPS Performance'}}, 'hitbox-sync': {'DFIntS2PhysicsSenderRate': {'value': '250', 'control': {'label': 'Hitbox Sync', 'type': 'integer'}}, 'preset': {'name': 'Hitbox Sync'}}, 'remove-textures': {'RenderTextureTotalBudgetCount': {'value': '0', 'control': {'label': 'Remove Textures', 'type': 'button', 'value': '0'}}, 'preset': {'name': 'Remove Textures'}}, 'resolution': {'DFIntDebugDynamicRenderKiloPixels': {'value': '922', 'control': {'label': 'Resolution', 'type': 'dropdown', 'options': {'144p': '37', '240p': '77', '360p': '230', '480p': '410', '720p': '922', '1080p': '2074'}}}, 'preset': {'name': 'Resolution'}}, 'scc': {'fos': {'flags': {'DFIntDebugDynamicRenderKiloPixels': '922', 'FIntCameraFarZPlane': '300', 'DFIntS2PhysicsSenderRate': '45'}, 'controls': {'DFIntDebugDynamicRenderKiloPixels': {'label': 'Resolution', 'type': 'dropdown', 'options': {'144p': '37', '240p': '77', '360p': '230', '480p': '410', '720p': '922', '1080p': '2074'}}}}, 'fps': {'flags': {'DFIntDebugDynamicRenderKiloPixels': '410', 'FIntCameraFarZPlane': '300', 'FRMFinalDistanceMaxStud': '250', 'DFIntS2PhysicsSenderRate': '45'}}, 'preset': {'name': 'Presets'}, 'quality': {'flags': {'DFIntDebugDynamicRenderKiloPixels': '2074', 'FIntDebugForceMSAASamples': '4', 'FIntDebugFRMOptionalMSAALevelOverride': '4', 'FIntCameraFarZPlane': '1000'}}}, 'sharpening': {'FIntDebugFRMOptionalMSAALevelOverride': {'value': '4'}, 'FIntDebugForceMSAASamples': {'value': '4', 'control': {'label': 'Sharpening', 'type': 'dropdown', 'options': {'1': '1', '2': '2', '4': '4', '8': '8'}, 'connects_to': 'FIntDebugFRMOptionalMSAALevelOverride'}}, 'preset': {'name': 'Sharpening'}}}

def load_presets():
    path = RESOURCES / 'configs' / 'presets.json'
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    source = copy.deepcopy(BUILTIN_PRESET_FILES)
    items = []
    for identifier, files in source.items():
        item = {'id': identifier, 'name': identifier.replace('-', ' ').title(), 'flags': {}, 'controls': {}}
        for filename, value in files.items():
            name = filename[:-5] if filename.endswith('.json') else filename
            if isinstance(value, dict) and 'name' in value and ('value' not in value) and ('flags' not in value):
                item['name'] = str(value['name'])
                item['controls'].update(value.get('controls', {}))
            elif isinstance(value, dict) and 'value' in value:
                item['flags'][name] = value['value']
                if isinstance(value.get('control'), dict):
                    item['controls'][name] = value['control']
                item['controls'].update(value.get('controls', {}))
            elif isinstance(value, dict) and 'flags' in value:
                item['flags'].update(value['flags'])
                item['controls'].update(value.get('controls', {}))
            elif isinstance(value, dict):
                item['flags'].update({key: val for key, val in value.items() if not isinstance(val, (dict, list))})
            else:
                item['flags'][name] = value
        if item['flags']:
            items.append(item)
    return items

class Api:

    def __init__(self, directory=None, controller=None, catalog=None, background=False):
        global OFFSET_STORE
        self.settings = Settings(directory or data_directory())
        self.window = None
        self.lock = threading.RLock()
        self.logs = deque(maxlen=400)
        self.log_id = 0
        self.revision = 0
        self.rows = self.settings.get('f')
        self.prefixes = PrefixCatalog(RESOURCES, self.settings.directory)
        self.offset_store = OffsetStore(self.settings.directory, self.settings.get('offset_source'))
        OFFSET_STORE = self.offset_store
        self.repository = RepositoryConfigs(RESOURCES, self.settings.directory)
        self._config_lock = threading.Lock()
        self.config_state = {'state': 'bundled', 'error': ''}
        self.community = []
        self.presets = []
        self.preset_revision = 0
        self._repository_snapshot = None
        snapshot = self.repository.load(self._validate_repository_file)
        try:
            self._install_repository(snapshot)
        except (ValueError, TypeError, KeyError):
            self._install_repository(self.repository.load_bundled(self._validate_repository_file))
        self.rows = self._resolve_prefixes(self.rows)
        self.catalog = catalog or OffsetCatalog(store=self.offset_store)
        self.controller = controller or LockFController(self.log)
        self.controller.set_LockF(self.settings.get('LockF'))
        self.controller.set_flags(execution_rows(self.rows))
        self.update = {'state': 'unconfigured', 'current': VERSION, 'latest': '', 'url': '', 'error': ''}
        self._update_lock = threading.Lock()
        self._update_stop = threading.Event()
        self._installing_update = False
        self._startup_checked = threading.Event()
        self._startup_thread = None
        self._startup_timeout = 10
        self._startup_services_lock = threading.Lock()
        self._services_started = False
        self._background = bool(background)
        self._startup_state = {'state': 'connecting', 'message': 'Loading settings', 'completed': 1, 'total': 4, 'update_state': 'pending'}
        self._closing = False
        self._rpc_stop = threading.Event()
        self._dump_cancel = threading.Event()
        self._dump_thread = None
        self._dump_state = {'state': 'idle', 'message': '', 'count': 0, 'path': ''}
        self.desktop = DesktopIntegration(self, RESOURCES)
        self.window_active = True
        self._row_payload_key = None
        self._row_payload = []
        if self.settings.load_error:
            self.log('[!] Settings unavailable')
        self._consume_update_report()

    def _consume_update_report(self):
        path = self.settings.directory / 'updates' / 'result.json'
        try:
            report = config_json(read_limited(path, 4096))
            if isinstance(report, dict) and report.get('state') == 'done':
                self.log('[+] Update installed')
            elif isinstance(report, dict) and report.get('state') == 'error':
                self.log('[!] Update failed: ' + str(report.get('error', '')))
            path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass

    def log(self, message):
        import logging
        logging.getLogger('syntax').info(str(message)[:4096])
        with self.lock:
            self.log_id += 1
            self.logs.append({'id': self.log_id, 'time': datetime.now().strftime('%H:%M:%S'), 'text': str(message)[:4096]})

    @staticmethod
    def _validate_repository_file(name, data, *, is_presets=None):
        if is_presets is None:
            is_presets = name.endswith('/presets.json')
        if is_presets:
            if not isinstance(data, list) or len(data) > 100:
                raise ValueError('Invalid repository presets')
            identifiers = set()
            for item in data:
                if not isinstance(item, dict) or not isinstance(item.get('id'), str) or (not re.fullmatch('[a-z0-9-]{1,64}', item['id'])):
                    raise ValueError('Invalid preset identifier')
                if item['id'] in identifiers or not isinstance(item.get('name'), str) or len(item['name']) > 80:
                    raise ValueError('Invalid preset name')
                if not item['name'].strip():
                    raise ValueError('Invalid preset name')
                item['name'].encode('utf-8')
                identifiers.add(item['id'])
                if not isinstance(item.get('flags'), dict) or not isinstance(item.get('controls', {}), dict):
                    raise ValueError('Invalid preset flags')
                parse_flags(json.dumps(item['flags']))
                for key, control in item.get('controls', {}).items():
                    if key not in item['flags']:
                        raise ValueError('Control flag missing')
                    if not isinstance(control, dict) or control.get('type') not in ('dropdown', 'integer', 'button'):
                        raise ValueError('Invalid preset control')
                    label = control.get('label', key)
                    if not isinstance(label, str) or len(label) > 160:
                        raise ValueError('Invalid control label')
                    label.encode('utf-8')
                    options = control.get('options', {})
                    if not isinstance(options, dict) or len(options) > 100:
                        raise ValueError('Invalid preset options')
                    if control['type'] == 'dropdown' and (not options):
                        raise ValueError('Empty preset options')
                    for option, value in options.items():
                        if not isinstance(option, str) or len(option) > 80:
                            raise ValueError('Invalid option label')
                        option.encode('utf-8')
                        normalize_flag(key, value)
                    if 'value' in control:
                        normalize_flag(key, control['value'])
                    linked = control.get('connects_to')
                    if linked is not None and (not isinstance(linked, str) or linked not in item['flags']):
                        raise ValueError('Linked control flag missing')
        else:
            if not isinstance(data, dict):
                raise ValueError('Invalid community pack')
            parse_flags(json.dumps(data))

    def _resolve_prefixes(self, rows):
        resolved = copy.deepcopy(rows)
        mapping = self.prefixes.mapping
        for row in resolved:
            if row['prefix']:
                expected = mapping.get(row['suffix'])
                if expected and flag_type(expected + row['suffix'], row['value']) != flag_type(row['name'], row['value']):
                    row.setdefault('prefix_pending_enabled', row['enabled'])
                    row.update(type='unknown', needs_type=True, enabled=False)
                continue
            prefix = mapping.get(row['suffix'])
            if prefix:
                row.update(normalize_flag(prefix + row['suffix'], row['value'], row.pop('prefix_pending_enabled', row['enabled']), row['source']))
                row.pop('needs_type', None)
            else:
                row.setdefault('prefix_pending_enabled', row['enabled'])
                row.update(type='unknown', needs_type=True, enabled=False)
        return resolved

    def _install_repository(self, snapshot):
        if snapshot == self._repository_snapshot:
            return False
        manifest, files = (snapshot['manifest'], snapshot['files'])
        overrides = self.settings.get('preset_overrides')
        presets = []
        for item in files[manifest['presets']]:
            item = {key: copy.deepcopy(item[key]) for key in ('id', 'name', 'flags', 'controls') if key in item}
            item['controls'] = item.get('controls', {})
            item['rows'] = self._resolve_prefixes(overrides.get(item['id'], parse_flags(json.dumps(item['flags']))))
            item['builtin'] = True
            presets.append(item)
        presets.extend(({**preset, 'builtin': False, 'flags': {row['name']: row['value'] for row in preset['rows']}, 'controls': {}, 'rows': self._resolve_prefixes(preset['rows'])} for preset in self.settings.get('saved_presets')))
        community = [{**pack, 'rows': self._resolve_prefixes(parse_flags(json.dumps(files[pack['file']])))} for pack in manifest['packs']]
        with self.lock:
            self.presets = presets
            self.community = community
            self.preset_revision += 1
            self._repository_snapshot = copy.deepcopy(snapshot)
        return True

    def refresh_configs(self):
        if self._closing or not self._config_lock.acquire(blocking=False):
            return {'ok': True, 'busy': True}
        with self.lock:
            self.config_state = {'state': 'loading', 'error': ''}

        def worker():
            try:
                snapshot = self.repository.refresh(self._validate_repository_file)
                if not self._closing:
                    self._install_repository(snapshot)
                    with self.lock:
                        self.config_state = {'state': 'github', 'error': ''}
                    self.log('[+] Repository configs updated')
            except Exception as error:
                with self.lock:
                    self.config_state = {'state': 'offline', 'error': str(error)}
                self.log('[i] Using local repository configs')
            finally:
                self._config_lock.release()
        threading.Thread(target=worker, name='syntax-configs', daemon=True).start()
        return {'ok': True}

    def get_community(self):
        with self.lock:
            return {'ok': True, 'items': [{**copy.deepcopy(pack), 'rows': annotate_rows(pack['rows'], self.catalog.known_names)} for pack in self.community], 'revision': self.preset_revision, 'catalog_revision': self.catalog.revision, 'source': copy.deepcopy(self.config_state)}

    def apply_community(self, identifier):
        with self.lock:
            pack = next((pack for pack in self.community if pack['id'] == identifier), None)
            if pack is None:
                return {'ok': False, 'error': 'Pack not found'}
            try:
                additions = self._resolve_prefixes(pack['rows'])
                replaced = {row['suffix'] for row in additions}
                result = self._commit([row for row in self.rows if row['suffix'] not in replaced] + additions)
                self.log('[+] Community pack: ' + pack['name'])
                return result
            except (OSError, ValueError) as error:
                return {'ok': False, 'error': str(error)}

    def add_prefixes(self):
        try:
            try:
                self.prefixes.refresh(force=True)
            except (requests.RequestException, ValueError, OSError) as error:
                self.log(f'[i] Cached prefixes: {error}')
            with self.lock:
                resolved = self._resolve_prefixes(self.rows)
                changed = sum((old['name'] != new['name'] for old, new in zip(self.rows, resolved)))
                result = self._commit(resolved)
                for item in self.presets + self.community:
                    item['rows'] = self._resolve_prefixes(item['rows'])
                self.preset_revision += 1
                return {**result, 'changed': changed, 'unresolved': sum((not row['prefix'] for row in resolved))}
        except (ValueError, OSError) as error:
            return {'ok': False, 'error': str(error)}

    def start(self):
        with self.lock:
            if self._startup_thread is not None or self._closing:
                return
            self._startup_thread = threading.Thread(target=self._run_startup, name='syntax-startup', daemon=True)
        self._startup_thread.start()

    def _set_startup(self, state, message, completed):
        with self.lock:
            if self._closing or self._services_started and state not in ('finishing', 'ready'):
                return
            self._startup_state.update(state=state, message=message, completed=completed)

    def _run_startup(self):
        if self._closing:
            return
        try:
            self._set_startup('desktop', 'Preparing Windows', 1)
            try:
                self._initialize_desktop()
            except Exception as error:
                self.log('[!] Desktop setup: ' + error_message(error))
            if self._closing:
                return
            self._set_startup('checking', 'Checking updates', 2)
            self.check_updates()
            checked = self._startup_checked.wait(self._startup_timeout)
            if self._closing:
                return
            with self.lock:
                result = copy.deepcopy(self.update)
                self._startup_state['update_state'] = result.get('state', 'error') if checked else 'timeout'
            if not checked:
                self.log('[i] Update check timed out')
            elif result.get('state') in ('error', 'missing_asset', 'no_release'):
                self.log('[i] ' + str(result.get('error') or 'Update check unavailable'))
            if result.get('state') == 'available' and checked and not self._background and not self._services_started:
                self._set_startup('update_available', 'Update available', 3)
                if sys.platform == 'win32' and getattr(sys, 'frozen', False):
                    self.install_update()
                return
            self._start_services()
        except Exception as error:
            self.log('[!] Startup failed: ' + error_message(error))
            self._start_services()

    def _start_services(self):
        with self._startup_services_lock:
            if self._services_started or self._closing:
                return
            if self.update.get('state') in ('downloading', 'preparing', 'restarting'):
                self._set_startup('update_available', 'Updating Syntax', 3)
                return
            self._services_started = True
            self._set_startup('finishing', 'Starting services', 3)
            starters = (self.controller.start,
                        lambda: threading.Thread(target=self._prefetch_catalog, name='syntax-catalog', daemon=True).start(),
                        lambda: threading.Thread(target=self._rpc_loop, name='syntax-rpc', daemon=True).start(),
                        self.refresh_configs)
            for starter in starters:
                if self._closing:
                    return
                try:
                    starter()
                except Exception as error:
                    self.log('[!] Startup service: ' + error_message(error))
            self._set_startup('ready', 'Ready', 4)

    def complete_startup(self):
        if self._closing:
            return {'ok': False, 'error': 'Application is closing'}
        if self.update.get('state') in ('downloading', 'preparing', 'restarting'):
            return {'ok': False, 'error': 'Update in progress'}
        self._start_services()
        return {'ok': True}

    def _initialize_desktop(self):
        self.desktop.start()
        if sys.platform != 'win32':
            return
        values = {}
        try:
            if self.settings.get('startup'):
                set_startup(True, Path(__file__))
        except (OSError, ValueError, RuntimeError) as error:
            values['startup'] = False
            self.log(f'[!] Startup failed: {error}')
        try:
            self.settings.set_many(values)
        except (OSError, ValueError) as error:
            self.log(f'[!] Preferences save failed: {error}')

    def _prefetch_catalog(self):
        try:
            try:
                self.prefixes.refresh()
                with self.lock:
                    resolved = self._resolve_prefixes(self.rows)
                    if resolved != self.rows:
                        self._commit(resolved)
                    for item in self.presets + self.community:
                        item['rows'] = self._resolve_prefixes(item['rows'])
                    self.preset_revision += 1
            except Exception as error:
                self.log(f'[i] Prefix catalog offline: {error}')
            self.catalog.entries()
        except Exception:
            pass

    def shutdown(self):
        if self._closing:
            return
        self._closing = True
        self._rpc_stop.set()
        self._update_stop.set()
        self._startup_checked.set()
        self._dump_cancel.set()
        self.repository.cancel.set()
        self.offset_store.cancel.set()
        self.prefixes.cancel.set()
        self.desktop.stop()
        if self._dump_thread and self._dump_thread is not threading.current_thread():
            self._dump_thread.join(timeout=3)
        if not self.controller.close():
            self.log('[!] Memory shutdown timed out')

    def _rpc_loop(self):
        presence = None
        try:
            while not self._rpc_stop.is_set():
                if self.settings.get('rpc'):
                    try:
                        if presence is None:
                            from pypresence import Presence
                            presence = Presence(DISCORD_APP_ID)
                            presence.connect()
                        active = sys.platform == 'win32' and bool(find_roblox_processes())
                        state = 'Injecting FFlags with Syntax' if active else 'chilling :>'
                        presence.update(details='Syntax ' + DISPLAY_VERSION, state=state, large_image='syntax',
                                        buttons=[{'label': 'Discord Server', 'url': DISCORD_URL},
                                                 {'label': 'Github Page', 'url': f'https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}'}])
                    except Exception:
                        if presence:
                            try:
                                presence.close()
                            except Exception:
                                pass
                        presence = None
                elif presence:
                    try:
                        presence.close()
                    except Exception:
                        pass
                    presence = None
                self._rpc_stop.wait(15)
        finally:
            if presence:
                try:
                    presence.close()
                except Exception:
                    pass

    def _commit(self, rows):
        if len(rows) > MAX_FLAGS:
            raise ValueError(f'Maximum {MAX_FLAGS} FFlags')
        rows = ensure_row_ids(rows)
        rows = self._resolve_prefixes(rows)
        if rows == self.rows:
            return {'ok': True, 'count': len(rows)}
        self.settings.set('f', rows)
        self.rows = copy.deepcopy(rows)
        self.revision += 1
        self.controller.set_flags(execution_rows(rows))
        return {'ok': True, 'count': len(rows)}

    def get_state(self, after=0, after_revision=-1, after_catalog=-1, after_status=-1):
        with self.lock:
            state = self.controller.snapshot()
            key = (self.revision, self.catalog.revision)
            rows = None
            if int(after_revision) != self.revision or int(after_catalog) != self.catalog.revision:
                if key != self._row_payload_key:
                    self._row_payload = annotate_rows(self.rows, self.catalog.known_names)
                    self._row_payload_key = key
                rows = self._row_payload
            return {'ok': True, 'version': DISPLAY_VERSION, 'revision': self.revision, 'rows': rows, 'status': state if int(after_status) != state.get('revision', 0) else None, 'catalog_revision': self.catalog.revision, 'preset_revision': self.preset_revision, 'logs': [entry for entry in self.logs if entry['id'] > int(after)], 'update': copy.deepcopy(self.update), 'startup': copy.deepcopy(self._startup_state), 'settings': {key: self.settings.get(key) for key in ('rpc', 'motion', 'snow', 'theme', 'tray', 'startup', 'offset_source', 'LockF')}, 'dump': copy.deepcopy(self._dump_state), 'configs': copy.deepcopy(self.config_state), 'window_active': self.window_active}

    def apply_flags(self):
        try:
            with self.lock:
                if self._closing:
                    raise RuntimeError('Application is closing')
                self.controller.set_flags(execution_rows(self.rows))
                self.controller.request_apply()
            return {'ok': True, 'queued': True}
        except (ValueError, RuntimeError) as error:
            return {'ok': False, 'error': str(error)}

    def add_flag(self, name, value):
        try:
            row = normalize_flag(name, value)
            with self.lock:
                rows = copy.deepcopy(self.rows)
                rows.append(row)
                return self._commit(rows)
        except (ValueError, OSError) as error:
            return {'ok': False, 'error': error_message(error)}

    def edit_flag(self, name, value):
        try:
            with self.lock:
                rows = copy.deepcopy(self.rows)
                row = next((r for r in rows if r['id'] == name or r['name'] == name), None)
                if row is None:
                    raise ValueError('FFlag not found')
                row.update(normalize_flag(row['name'], value, row['enabled'], row['source']))
                return self._commit(rows)
        except (ValueError, OSError) as error:
            return {'ok': False, 'error': error_message(error)}

    def toggle_flag(self, name, enabled):
        try:
            with self.lock:
                rows = copy.deepcopy(self.rows)
                row = next((r for r in rows if r['id'] == name or r['name'] == name), None)
                if row is None:
                    raise ValueError('FFlag not found')
                if enabled and row.get('needs_type'):
                    raise ValueError('Choose FFlag type')
                row['enabled'] = bool(enabled)
                return self._commit(rows)
        except (ValueError, OSError) as error:
            return {'ok': False, 'error': error_message(error)}

    def delete_flag(self, name):
        try:
            with self.lock:
                target = next((row for row in self.rows if row['id'] == name or row['name'] == name), None)
                return self._commit([row for row in self.rows if row is not target])
        except OSError as error:
            return {'ok': False, 'error': error_message(error)}

    def clear_flags(self):
        try:
            with self.lock:
                return self._commit([])
        except OSError as error:
            return {'ok': False, 'error': error_message(error)}

    def set_raw_flags(self, content):
        try:
            if len(content.encode('utf-8')) > MAX_IMPORT_BYTES:
                raise ValueError('JSON too large')
            rows = parse_flags(content)
            with self.lock:
                return self._commit(rows)
        except (ValueError, TypeError, OSError) as error:
            return {'ok': False, 'error': error_message(error)}

    def get_raw_flags(self):
        with self.lock:
            records = len({row['suffix'] for row in self.rows}) < len(self.rows) or any((row.get('needs_type') or not row['enabled'] for row in self.rows))
            data = copy.deepcopy(self.rows) if records else {row['name']: row['value'] for row in self.rows}
            return {'ok': True, 'content': json.dumps(data, indent=2)}

    def _dialog(self, save=False):
        if self.window is None:
            return None
        import webview
        if save:
            paths = self.window.create_file_dialog(webview.FileDialog.SAVE, save_filename='Syntax_Flags.json', file_types=('JSON (*.json)',))
        else:
            paths = self.window.create_file_dialog(webview.FileDialog.OPEN, allow_multiple=True, file_types=('JSON (*.json)',))
        if not paths:
            return None
        if save:
            return paths[0] if isinstance(paths, (list, tuple)) else paths
        return list(paths) if isinstance(paths, (list, tuple)) else [paths]

    def load_flags(self):
        try:
            paths = self._dialog()
            if not paths:
                return {'ok': False, 'cancelled': True}
            if sum((Path(path).stat().st_size for path in paths)) > MAX_IMPORT_BYTES:
                raise ValueError('JSON too large')
            merged = []
            for path in paths:
                for row in parse_flags(Path(path).read_text(encoding='utf-8-sig')):
                    merged.append(row)
            with self.lock:
                return self._commit(merged)
        except (OSError, ValueError) as error:
            return {'ok': False, 'error': error_message(error)}

    def export_flags(self):
        try:
            path = self._dialog(save=True)
            if not path:
                return {'ok': False, 'cancelled': True}
            Path(path).write_text(self.get_raw_flags()['content'] + '\n', encoding='utf-8')
            return {'ok': True}
        except OSError as error:
            return {'ok': False, 'error': error_message(error)}

    def get_presets(self):
        with self.lock:
            items = []
            for preset in self.presets:
                rows = annotate_rows(preset['rows'], self.catalog.known_names)
                items.append({**copy.deepcopy(preset), 'rows': rows, 'flags': {row['name']: row['value'] for row in rows}, 'controls': self._preset_controls(preset, rows), 'missing': sum((row['missing'] is True for row in rows)), 'duplicates': sum((row['duplicate'] for row in rows))})
            return {'ok': True, 'items': items, 'revision': self.preset_revision, 'catalog_revision': self.catalog.revision}

    @staticmethod
    def _preset_controls(preset, rows):
        names = {row['suffix']: row['name'] for row in rows}
        controls = {}
        for name, control in preset.get('controls', {}).items():
            full = names.get(source_clean_name(name))
            if full is None:
                continue
            control = copy.deepcopy(control)
            linked = control.get('connects_to')
            if linked:
                target = names.get(source_clean_name(linked))
                if target:
                    control['connects_to'] = target
                else:
                    control.pop('connects_to')
            controls[full] = control
        return controls

    def inspect_flags(self, content):
        try:
            if len(content.encode('utf-8')) > MAX_IMPORT_BYTES:
                raise ValueError('JSON too large')
            rows = parse_flags(content)
            return {'ok': True, 'rows': annotate_rows(rows, self.catalog.known_names), 'source_available': self.catalog.known_names is not None}
        except (ValueError, TypeError) as error:
            return {'ok': False, 'error': error_message(error)}

    def save_preset(self, name, content):
        try:
            name = str(name).strip()
            if not name or len(name) > 60:
                raise ValueError('Preset name required')
            result = self.inspect_flags(content)
            if not result['ok']:
                return result
            rows = ensure_row_ids(result['rows'])
            if not rows:
                raise ValueError('Add FFlags first')
            with self.lock:
                saved = self.settings.get('saved_presets')
                if len(saved) >= 100:
                    raise ValueError('Maximum 100 presets')
                item = {'id': 'saved-' + uuid.uuid4().hex, 'name': name, 'rows': rows}
                self.settings.set('saved_presets', saved + [item])
                self.presets.append({**copy.deepcopy(item), 'builtin': False, 'controls': {}, 'flags': {row['name']: row['value'] for row in rows}})
                self.preset_revision += 1
                return {'ok': True, 'id': item['id'], 'count': len(rows)}
        except (ValueError, OSError) as error:
            return {'ok': False, 'error': error_message(error)}

    def debloat_flags(self):
        try:
            self.catalog.entries()
            names = self.catalog.known_names
            with self.lock:
                kept = [row for row in self.rows if row['suffix'] in names]
                removed = len(self.rows) - len(kept)
                return {**self._commit(kept), 'removed': removed}
        except Exception:
            return {'ok': False, 'error': 'Debloat failed'}

    def debloat_draft(self, content):
        try:
            result = self.inspect_flags(content)
            if not result['ok']:
                return result
            self.catalog.entries()
            names = self.catalog.known_names
            kept = [row for row in result['rows'] if row['suffix'] in names]
            return {'ok': True, 'rows': annotate_rows(kept, names), 'removed': len(result['rows']) - len(kept)}
        except Exception:
            return {'ok': False, 'error': 'Debloat failed'}

    def debloat_presets(self, identifier=''):
        try:
            self.catalog.entries()
            names = self.catalog.known_names
            with self.lock:
                presets = copy.deepcopy(self.presets)
                targets = [preset for preset in presets if preset['id'] == identifier] if identifier else [preset for preset in presets if not preset['builtin']]
                if identifier and (not targets):
                    raise ValueError('Preset not found')
                removed = 0
                for preset in targets:
                    kept = [row for row in preset['rows'] if row['suffix'] in names]
                    removed += len(preset['rows']) - len(kept)
                    preset['rows'] = kept
                    preset['flags'] = {row['name']: row['value'] for row in kept}
                overrides = {preset['id']: preset['rows'] for preset in presets if preset['builtin']}
                saved = [{key: preset[key] for key in ('id', 'name', 'rows')} for preset in presets if not preset['builtin']]
                self.settings.set_many({'saved_presets': saved, 'preset_overrides': overrides} if identifier else {'saved_presets': saved})
                self.presets = presets
                self.preset_revision += 1
                return {'ok': True, 'removed': removed}
        except Exception as error:
            return {'ok': False, 'error': error_message(error)}

    def apply_preset(self, identifier, overrides=None):
        try:
            item = next((p for p in self.presets if p['id'] == identifier), None)
            if item is None:
                raise ValueError('Preset not found')
            additions = copy.deepcopy(item['rows'])
            controls = self._preset_controls(item, additions)
            values = {row['name']: row['value'] for row in additions}
            changed_names = set()
            for name, value in (overrides or {}).items():
                if name in values:
                    values[name] = value
                    changed_names.add(name)
                    linked = controls.get(name, {}).get('connects_to')
                    if linked:
                        values[linked] = value
                        changed_names.add(linked)
            for row in additions:
                if row['name'] in changed_names:
                    row['value'] = str(values[row['name']])
                if not row.get('needs_type'):
                    row.update(normalize_flag(row['name'], row['value'], row['enabled'], 'preset'))
                row['id'] = uuid.uuid4().hex
            with self.lock:
                replaced = {row['suffix'] for row in additions}
                rows = [row for row in self.rows if row['suffix'] not in replaced] + additions
                result = self._commit(rows)
            self.log('[+] ' + item['name'])
            return result
        except (ValueError, TypeError, OSError) as error:
            return {'ok': False, 'error': error_message(error)}

    def validate_flags(self):
        try:
            self.catalog.entries()
            available = self.catalog.known_names
            with self.lock:
                missing = [row['name'] for row in self.rows if row['suffix'] not in available]
            return {'ok': True, 'missing': missing, 'count': len(available)}
        except Exception as error:
            return {'ok': False, 'error': error_message(error)}

    def sync_offsets(self):
        try:
            entries = self.catalog.entries(force=True)
            self.controller.refresh()
            return {'ok': True, 'count': len(entries)}
        except Exception as error:
            return {'ok': False, 'error': error_message(error)}

    def select_offset_source(self, source):
        previous = self.offset_store.source
        try:
            self.offset_store.set_source(source)
            if source == 'local':
                self.offset_store.document()
            self.settings.set('offset_source', source)
            self.catalog._at = 0
            self.catalog.known_names = None
            self.controller.refresh()
            self.catalog.revision += 1
            return {'ok': True}
        except (ValueError, OSError) as error:
            self.offset_store.set_source(previous)
            return {'ok': False, 'error': str(error)}

    def dump_offsets(self):
        if sys.platform != 'win32':
            return {'ok': False, 'error': 'Windows required'}
        with self.lock:
            if self._dump_thread and self._dump_thread.is_alive():
                return {'ok': True, 'busy': True}
            self._dump_cancel.clear()
            self._dump_state = {'state': 'running', 'message': 'Locating Roblox', 'count': 0, 'path': ''}

            def progress(message):
                with self.lock:
                    self._dump_state['message'] = message

            def worker():
                process = None
                try:
                    pids = find_roblox_processes()
                    if not pids:
                        raise ValueError('Start Roblox first')
                    process = AdvancedInjector(pids[0], self.log, read_only=True)
                    hints = self.offset_store._cached.get('theo', {}).get('layout', {})
                    dumper = EDumper(process, self._dump_cancel, progress)
                    document = dumper.dump(hints)
                    path = self.offset_store.save_dump(document)
                    with self.lock:
                        self._dump_state = {'state': 'done', 'message': 'Offsets saved', 'count': len(document['flags']), 'path': path}
                    self.log(f"[+] Dumped {len(document['flags'])} offsets: {path}")
                    if self.offset_store.source == 'local':
                        with self.lock:
                            self.catalog._at = 0
                            self.catalog.known_names = None
                            self.catalog.revision += 1
                        self.controller.refresh()
                except Exception as error:
                    with self.lock:
                        self._dump_state = {'state': 'cancelled' if self._dump_cancel.is_set() else 'error', 'message': str(error), 'count': 0, 'path': ''}
                    self.log(f'[!] Offset dump: {error}')
                finally:
                    if process:
                        process.close()
            self._dump_thread = threading.Thread(target=worker, name='syntax-dumper', daemon=True)
            self._dump_thread.start()
        return {'ok': True}

    def cancel_dump(self):
        self._dump_cancel.set()
        return {'ok': True}

    def save_preferences(self, rpc, motion, snow=True, theme=None, tray=None, startup=None, LockF=None):
        try:
            values = {'rpc': bool(rpc), 'motion': bool(motion), 'snow': bool(snow)}
            if LockF is not None:
                values['LockF'] = bool(LockF)
            if theme is not None:
                if theme not in THEMES:
                    raise ValueError('Unknown color theme')
                values['theme'] = theme
            if tray is not None:
                if tray:
                    self.desktop.start()
                    if self.desktop.icon is None or not self.desktop.ready.wait(2):
                        raise ValueError('System tray unavailable')
                values['tray'] = bool(tray)
            if startup is not None:
                if bool(startup) != self.settings.get('startup'):
                    set_startup(bool(startup), Path(__file__))
                values['startup'] = bool(startup)
            try:
                self.settings.set_many(values)
            except OSError:
                if startup is not None:
                    set_startup(self.settings.get('startup'), Path(__file__))
                raise
            if LockF is not None:
                self.controller.set_LockF(bool(LockF))
            if theme is not None:
                self.desktop.set_theme(theme)
            return {'ok': True}
        except (OSError, ValueError, RuntimeError) as error:
            return {'ok': False, 'error': str(error)}

    def check_updates(self):
        if self._closing or not self._update_lock.acquire(blocking=False):
            return {'ok': True, 'busy': True}
        owner, repo = (GITHUB_OWNER, GITHUB_REPO)
        self._startup_checked.clear()
        with self.lock:
            self.update = {'state': 'checking', 'current': VERSION, 'latest': '', 'url': '', 'error': ''}

        def worker():
            try:
                result = check_release(owner, repo)
                with self.lock:
                    self.update = result
            except Exception as error:
                result = {'state': 'error', 'current': VERSION, 'latest': '', 'url': '', 'error': error_message(error)}
                with self.lock:
                    self.update = result
            finally:
                self._update_lock.release()
                self._startup_checked.set()
            if self._startup_state.get('state') == 'update_available' and result.get('state') != 'available' and not self._closing:
                self._start_services()
        threading.Thread(target=worker, name='syntax-updates', daemon=True).start()
        return {'ok': True}

    def install_update(self):
        if sys.platform != 'win32' or not getattr(sys, 'frozen', False):
            return {'ok': False, 'error': 'Packaged application required'}
        if self._closing or not self._update_lock.acquire(blocking=False):
            return {'ok': True, 'busy': True}
        with self.lock:
            release = copy.deepcopy(self.update)
            if release.get('state') != 'available' or release.get('asset') != UPDATE_ARCHIVE_NAME:
                self._update_lock.release()
                return {'ok': False, 'error': 'No update available'}
            self.update.update(state='downloading', received=0, error='')

        def worker():
            manifest = None
            data = None
            try:
                def progress(received, total):
                    with self.lock:
                        self.update.update(received=received, size=total)
                manifest, data = prepare_update(release, Path(sys.executable), self.settings.directory, self._update_stop, progress)
                with self.lock:
                    self.update['state'] = 'preparing'
                helper = subprocess.Popen([data['source'], '--finish-update', str(manifest)], close_fds=True,
                                          creationflags=0x00000008 | 0x00000200,
                                          env={**os.environ, 'PYINSTALLER_RESET_ENVIRONMENT': '1'})
                ready = manifest.parent / 'ready.json'
                deadline = time.monotonic() + 30
                while True:
                    if self._update_stop.is_set():
                        raise RuntimeError('Update cancelled')
                    if ready.exists():
                        status = config_json(read_limited(ready, 4096))
                        if not isinstance(status, dict) or status.get('token') != data['token'] or status.get('state') != 'ready':
                            raise RuntimeError('Update helper failed')
                        break
                    if helper.poll() is not None:
                        raise RuntimeError('Update helper exited')
                    if time.monotonic() >= deadline:
                        raise TimeoutError('Update preparation timed out')
                    self._update_stop.wait(0.1)
                self._installing_update = True
                with self.lock:
                    self.update['state'] = 'restarting'
                self.desktop.exit()
            except Exception as error:
                self._installing_update = False
                try:
                    if manifest is not None:
                        (manifest.parent / 'cancel').touch()
                    if data is not None:
                        Path(data['pending']).unlink(missing_ok=True)
                except OSError:
                    pass
                with self.lock:
                    self.update = {**release, 'state': 'error', 'error': error_message(error)}
                self.log('[!] Update failed: ' + error_message(error))
            finally:
                self._update_lock.release()
        threading.Thread(target=worker, name='syntax-update-install', daemon=True).start()
        return {'ok': True}

    def open_release(self):
        with self.lock:
            url = self.update.get('url', '')
        if url:
            webbrowser.open(url)
            return {'ok': True}
        return {'ok': False, 'error': 'Release not found'}

    def _catalog_hints(self):
        hints = {}
        for preset in self.presets:
            for name, value in preset['flags'].items():
                row = normalize_flag(name, value)
                hints[row['suffix']] = row
        with self.lock:
            hints.update({row['suffix']: copy.deepcopy(row) for row in self.rows if not row.get('needs_type')})
        return hints

    def _catalog_item(self, entry, hints):
        prefix, clean = flag_parts(entry['name'])
        hint = hints.get(clean)
        if not prefix:
            prefix = self.prefixes.mapping.get(clean, '')
        if prefix:
            default = 'true' if 'Flag' in prefix else '' if 'String' in prefix else '0'
            hint = normalize_flag(prefix + clean, hint['value'] if hint else default)
        return {**entry, 'clean': clean, 'full_name': hint['name'] if hint else entry['name'], 'type': hint['type'] if hint else 'unknown', 'value': hint['value'] if hint else ''}

    def get_catalog(self, query='', page=0, limit=60, force=False):
        try:
            try:
                prefix, clean = flag_parts(query)
                query = clean if prefix else query
            except ValueError:
                pass
            result = self.catalog.search(query, page, limit, force)
            hints = self._catalog_hints()
            return {'ok': True, **result, 'revision': self.catalog.revision, 'items': [self._catalog_item(entry, hints) for entry in result['items']]}
        except Exception:
            return {'ok': False, 'error': 'Offsets unavailable'}

    def add_catalog_flag(self, name):
        try:
            entry = self.catalog.find(str(name))
            if entry is None:
                raise ValueError('FFlag not found')
            metadata = self._catalog_item(entry, self._catalog_hints())
            with self.lock:
                if any((row['suffix'] == metadata['clean'] for row in self.rows)):
                    return {'ok': True, 'count': len(self.rows)}
                row = normalize_flag(metadata['full_name'], metadata['value'], source='catalog')
                if metadata['type'] == 'unknown':
                    row.update(type='unknown', needs_type=True, enabled=False)
                return self._commit([row] + self.rows)
        except Exception as error:
            return {'ok': False, 'error': error_message(error)}

    def set_flag_type(self, name, kind):
        try:
            prefixes = {'bool': 'FFlag', 'int': 'FInt', 'float': 'FFloat', 'string': 'FString', 'log': 'FLog'}
            if kind not in prefixes:
                raise ValueError('Invalid FFlag type')
            with self.lock:
                rows = copy.deepcopy(self.rows)
                row = next((row for row in rows if row['id'] == name or row['name'] == name), None)
                if row is None:
                    raise ValueError('FFlag not found')
                prefix = ('D' if row['prefix'].startswith('D') else '') + prefixes[kind]
                value = row['value']
                if row.get('needs_type') or kind != row['type']:
                    value = 'true' if kind == 'bool' else '' if kind == 'string' else '0'
                replacement = normalize_flag(prefix + row['suffix'], value, row['enabled'] or row.get('needs_type', False), row['source'])
                replacement['id'] = row['id']
                rows[rows.index(row)] = replacement
                return self._commit(rows)
        except (ValueError, OSError) as error:
            return {'ok': False, 'error': error_message(error)}

    def open_discord(self):
        webbrowser.open(DISCORD_URL)
        return {'ok': True}

    def open_catalog_source(self):
        if self.offset_store.source == 'local':
            path = self.offset_store.directory
            path.mkdir(parents=True, exist_ok=True)
            if sys.platform == 'win32':
                os.startfile(str(path))
            else:
                webbrowser.open(path.as_uri())
        else:
            webbrowser.open('https://offsets.imtheo.lol/fflags.hpp')
        return {'ok': True}

    def minimize_window(self):
        if self.window:
            if not self.settings.get('tray') or not self.desktop.hide():
                self.window.minimize()
                self.set_window_activity(False)
        return {'ok': True}

    def set_window_activity(self, active):
        self.window_active = bool(active)
        if self.window:
            try:
                self.window.run_js('window.syntaxWindowActive && window.syntaxWindowActive(' + ('true' if active else 'false') + ')')
            except Exception:
                pass

    def close_window(self):
        if self.window:
            self.window.destroy()
        return {'ok': True}

class UiBridge:

    def __init__(self, app):
        names = ('add_catalog_flag', 'add_flag', 'add_prefixes', 'apply_community', 'apply_flags', 'apply_preset', 'cancel_dump', 'check_updates', 'clear_flags', 'close_window', 'complete_startup', 'debloat_draft', 'debloat_flags', 'debloat_presets', 'delete_flag', 'dump_offsets', 'edit_flag', 'export_flags', 'get_catalog', 'get_community', 'get_presets', 'get_raw_flags', 'get_state', 'inspect_flags', 'install_update', 'load_flags', 'minimize_window', 'open_catalog_source', 'open_discord', 'open_release', 'refresh_configs', 'save_preferences', 'save_preset', 'select_offset_source', 'set_flag_type', 'set_raw_flags', 'sync_offsets', 'toggle_flag', 'validate_flags')
        for name in names:
            setattr(self, name, getattr(app, name))

def main():
    import webview
    instance = SingleInstance(data_directory())
    if not instance.primary:
        instance.close()
        return
    configure_logging(data_directory())
    app = Api(background='--background' in sys.argv)
    html = render_html()
    window = webview.create_window(APP_NAME + ' ' + DISPLAY_VERSION, html=html, js_api=UiBridge(app), width=1240, height=800, min_size=(980, 640), frameless=True, easy_drag=False, background_color='#110a1d')
    app.window = window
    window.events.closing += app.desktop.closing
    window.events.minimized += lambda: app.set_window_activity(False)
    window.events.restored += lambda: app.set_window_activity(True)
    window.events.shown += lambda: app.set_window_activity(True)

    def start():
        app.start()
        instance.listen(app.desktop.restore)
        if '--background' in sys.argv:
            app.desktop.hide()
    try:
        webview.start(start, debug='--debug' in sys.argv, gui='edgechromium' if sys.platform == 'win32' else None)
    finally:
        instance.close()
        app.shutdown()
ROOT = Path(__file__).resolve().parent
SOURCES = ('syntax.py',)
ASSETS = ('ui.html', 'syntax.ico', 'configs')

def wine_path(path):
    return subprocess.check_output(['winepath', '-w', str(path)], text=True, stderr=subprocess.DEVNULL).strip()

def python_command():
    if sys.platform == 'win32':
        return [sys.executable]
    if not shutil.which('wine'):
        raise RuntimeError('Wine is required')
    executable = os.environ.get('SYNTAX_WINDOWS_PYTHON')
    if not executable:
        prefix = Path(os.environ.get('WINEPREFIX', Path.home() / '.wine'))
        candidates = sorted((prefix / 'drive_c' / 'Program Files').glob('Python*/python.exe'))
        executable = wine_path(candidates[-1]) if candidates else 'python'
    return ['wine', executable]

def build_windows():
    command = python_command()
    environment = {**os.environ, 'WINEDEBUG': '-all', 'PYTHONIOENCODING': 'utf-8'}
    temporary_root = '/tmp/opencode' if sys.platform != 'win32' and Path('/tmp/opencode').is_dir() else None
    with tempfile.TemporaryDirectory(prefix='syntax-build-', dir=temporary_root) as directory:
        stage = Path(directory)
        for filename in SOURCES + ASSETS:
            if (ROOT / filename).is_dir():
                shutil.copytree(ROOT / filename, stage / filename)
            else:
                shutil.copy2(ROOT / filename, stage / filename)
        target = str(stage) if sys.platform == 'win32' else wine_path(stage)
        args = ['-m', 'PyInstaller', '--noconfirm', '--onefile', '--windowed', '--name', Path(EXECUTABLE_NAME).stem, '--collect-all', 'webview', '--collect-all', 'pypresence', '--hidden-import', 'pystray._win32', '--hidden-import', 'PIL.IcoImagePlugin', '--exclude-module', 'numpy', '--exclude-module', 'tkinter', '--exclude-module', 'win32ui', '--hidden-import', 'pymem', '--hidden-import', 'pymem.process', '--icon', target + '/syntax.ico', '--paths', target, '--distpath', target + '/dist', '--workpath', target + '/build', '--specpath', target]
        for filename in ASSETS:
            args.extend(['--add-data', target + '/' + filename + ';' + (filename if (ROOT / filename).is_dir() else '.')])
        args.append(target + '/syntax.py')
        subprocess.run(command + args, cwd=stage, env=environment, check=True)
        subprocess.run(command + [target + '/syntax.py', '--verify-package', target + '/dist/' + EXECUTABLE_NAME, target], cwd=stage, env=environment, check=True)
        output = ROOT / EXECUTABLE_NAME
        pending = output.with_suffix('.exe.tmp')
        shutil.copy2(stage / 'dist' / EXECUTABLE_NAME, pending)
        os.replace(pending, output)
        for path in ROOT.iterdir():
            if path.name.lower() == EXECUTABLE_NAME.lower() and path.name != EXECUTABLE_NAME:
                if path.samefile(output):
                    temporary = output.with_suffix('.case.tmp')
                    os.replace(path, temporary)
                    os.replace(temporary, output)
                else:
                    path.unlink()
        print(f'Built: {output}')

def check_source():
    if not getattr(sys, 'frozen', False):
        for filename in SOURCES:
            check_python_source((ROOT / filename).read_text(encoding='utf-8'))
    html = render_html()
    assert 'lang="en"' in html
    assert 'data:image/x-icon;base64,' in html
    assert '<script src=' not in html
    assert GITHUB_OWNER == 'Astraliscute' and GITHUB_REPO == 'Syntax'
    repository_configs = RepositoryConfigs(RESOURCES, Path(tempfile.gettempdir()) / 'syntax-source-check')
    snapshot = repository_configs.load_bundled(Api._validate_repository_file)
    assert len(snapshot['manifest']['packs']) >= 3
    print('Source verification passed.')

def check_python_source(source):
    import io
    import tokenize
    tree = ast.parse(source)
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            raise ValueError('Python comments found')
    if any((marker in source for marker in (chr(34) * 3, chr(39) * 3))):
        raise ValueError('Triple quotes found')
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and ast.get_docstring(node):
            raise ValueError('Docstring found')
        value = None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
        if isinstance(node, ast.JoinedStr):
            value = ''.join((item.value if isinstance(item, ast.Constant) else 'value' for item in node.values))
        if value is not None and len(value.split()) > 5:
            raise ValueError(f'Long text: line {node.lineno}')
    return tree

def verify_package(executable, resources):
    import importlib
    import marshal
    import types
    readers = importlib.import_module('PyInstaller.archive.readers')
    archive = readers.CArchiveReader(executable)
    resources = Path(resources)
    for filename in ('ui.html', 'syntax.ico'):
        assert archive.extract(filename) == (resources / filename).read_bytes(), filename
    archived = {name.replace(chr(92), '/'): name for name in archive.toc}
    for path in (resources / 'configs').rglob('*.json'):
        name = path.relative_to(resources).as_posix()
        assert name in archived, name
        assert archive.extract(archived[name]) == path.read_bytes(), name
    modules = archive.open_embedded_archive('PYZ.pyz').toc
    assert not any(name.lower().endswith('win32ui.pyd') for name in archive.toc)
    for name in ('pymem', 'pymem.process', 'pystray', 'pystray._win32', 'PIL.Image', 'PIL.ImageOps', 'PIL.ImageColor'):
        assert name in modules, name
    for name in ('syntax_sources', 'syntax_desktop', 'release_engine', 'offset_catalog', 'updates'):
        assert name not in modules, name

    def normalize(code):
        constants = tuple((normalize(value) if isinstance(value, types.CodeType) else value for value in code.co_consts))
        return code.replace(co_filename='', co_consts=constants)
    source = (resources / 'syntax.py').read_text(encoding='utf-8')
    packaged = marshal.loads(archive.extract('syntax'))
    assert normalize(packaged) == normalize(compile(source, 'syntax.py', 'exec'))
    print('Package verification passed.')

def run_tests():
    import io
    import types
    import unittest
    from unittest.mock import Mock, patch
    syntax = sys.modules[__name__]
    THEO = '\n'.join(['version-1234567890abcdef', 'namespace FFlagOffsets{', 'namespace FFlagList{inline constexpr uintptr_t Pointer=0x1100;}', 'namespace FFlags{', 'inline constexpr uintptr_t First=0x2000;', 'inline constexpr uintptr_t Second=0x2010;', '}', '}'])

    class Response:

        def __init__(self, text):
            self.content = text.encode() if isinstance(text, str) else text

        def __enter__(self):
            return self

        def __exit__(self, *arguments):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            for start in range(0, len(self.content), size):
                yield self.content[start:start + size]

    class FakeInjector:
        created = []

        def __init__(self, pid, log):
            self.pid = pid
            self.is_attached = True
            self.values = {4096: '10', 8192: '20'}
            self.offsets = {'First': 4096, 'Second': 8192}
            self.writes = []
            self.live = {}
            self._verify_lock_running = False
            self.created.append(self)

        def get_roblox_base(self):
            return 1

        def fetch_offsets(self, force=False):
            return True

        def alive(self):
            return self.is_attached

        def scan_live_flags(self, names, force_rescan=False):
            self.live = {syntax.source_clean_name(name): [{'abs_addr': self.offsets[syntax.source_clean_name(name)]}] for name in names if syntax.source_clean_name(name) in self.offsets}
            return self.live

        def read_flag_at_address(self, kind, address):
            return self.values.get(address)

        def write_flag_at_address(self, kind, address, value):
            if not self.is_attached:
                raise AssertionError('Write after close')
            self.values[address] = value
            self.writes.append((address, value))
            return (True, 'OK')

        def close(self):
            self.is_attached = False

    class FakeManager:

        def __init__(self, log):
            self.injector = None
            self.turbo_thread = None
            self.active_flags = {}
            self.injection_active = False

        def stop(self):
            self.injection_active = False

        def start_turbo(self):
            raise AssertionError('Periodic writer must never start')

    def fake_engine(pids):
        return types.SimpleNamespace(FlagManager=FakeManager, AdvancedInjector=FakeInjector, clean_flag_name=syntax.source_clean_name, _mem_lock=threading.RLock(), find_roblox_processes=lambda: list(pids))

    class FixtureProcess:

        def __init__(self):
            self.base_address = 5368709120
            self.image_size = 131072
            self.identity = {'version': 'version-1234567890abcdef', 'timestamp': 123, 'image_size': self.image_size}
            self.sections = [{'rva': 4096, 'size': 65536, 'writable': True, 'executable': False}]
            self.memory = {self.base_address: bytearray(self.image_size), 2097152: bytearray(2097152)}
            self.read_count = 0
            self.running = True
            self.map = 2097152
            self.head = 2162688
            self.put64(self.base_address + 4352, self.map)
            self.put64(self.map, 1065353216)
            self.put64(self.map + 8, self.head)
            self.nodes = [2228224 + index * 256 for index in range(6)]
            self.put64(self.head, self.nodes[0])
            self.put64(self.head + 8, self.nodes[-1])
            for index, node in enumerate(self.nodes):
                self.put64(node, self.nodes[index + 1] if index + 1 < len(self.nodes) else self.head)
                name = ('LongFlagValueName' if index == 5 else 'FlagValue') + str(index)
                raw = name.encode()
                if len(raw) < 16:
                    self.write(node + 16, raw + b'\x00' * (16 - len(raw)) + struct.pack('<QQ', len(raw), 15))
                else:
                    address = 3407872
                    self.write(address, raw)
                    self.write(node + 16, struct.pack('<QQQQ', address, 0, len(raw), 31))
                descriptor = 3145728 + index * 256
                self.put64(node + 48, descriptor)
                self.put64(descriptor + 32, self.base_address + 20480 + index * 8)

        def write(self, address, data):
            for start, memory in self.memory.items():
                if start <= address and address + len(data) <= start + len(memory):
                    memory[address - start:address - start + len(data)] = data
                    return
            raise AssertionError('Fixture bounds')

        def put64(self, address, value):
            self.write(address, struct.pack('<Q', value))

        def read(self, address, size):
            self.read_count += 1
            for start, memory in self.memory.items():
                if start <= address and address + size <= start + len(memory):
                    return bytes(memory[address - start:address - start + size])
            return None

        def alive(self):
            return self.running

        def is_data_address(self, address, size=1):
            return self.base_address + 4096 <= address and address + size <= self.base_address + 69632

    class ValidationTests(unittest.TestCase):

        def test_integer_overflow_rejected(self):
            for value in ('2147483648', '-2147483649'):
                with self.assertRaises(ValueError):
                    syntax.normalize_flag('DFIntFirst', value)

        def test_log_is_one_byte(self):
            self.assertEqual(syntax.normalize_flag('FLogNetwork', '7')['type'], 'log')
            with self.assertRaises(ValueError):
                syntax.normalize_flag('FLogNetwork', '256')

        def test_nonfinite_rejected(self):
            for value in ('nan', 'inf', '1e40'):
                with self.assertRaises(ValueError):
                    syntax.normalize_flag('FFloatFirst', value)

        def test_duplicate_json_preserved_and_paused(self):
            rows = syntax.parse_flags('{"DFIntFirst":"1","DFIntFirst":"2"}')
            self.assertEqual(len(rows), 2)
            self.assertNotEqual(rows[0]['id'], rows[1]['id'])
            self.assertEqual(syntax.execution_rows(rows), [])

        def test_invalid_import_unchanged(self):
            with tempfile.TemporaryDirectory() as directory:
                app = syntax.Api(directory)
                app.add_flag('DFIntFirst', '1')
                before = copy.deepcopy(app.rows)
                self.assertFalse(app.set_raw_flags('{"DFIntFirst": 2147483648}')['ok'])
                self.assertEqual(app.rows, before)
                app.shutdown()

        def test_corrupt_settings_backed_up(self):
            with tempfile.TemporaryDirectory() as directory:
                (Path(directory) / 'status.json').write_text('broken', encoding='utf-8')
                settings = syntax.Settings(directory)
                self.assertTrue(settings.load_error)
                self.assertTrue(list(Path(directory).glob('status.corrupt-*.json')))
                settings.set('theme', 'forest')
                self.assertEqual(syntax.Settings(directory).get('theme'), 'forest')

    class OffsetTests(unittest.TestCase):

        def test_nested_theo_namespace(self):
            document = parse_theo(THEO)
            self.assertEqual(document['flags'], {'First': 8192, 'Second': 8208})
            self.assertEqual(document['layout']['Pointer'], 4352)

        def test_version_mismatch_blocked(self):
            document = parse_theo(THEO)
            self.assertFalse(matches_identity(document, {'version': 'version-0000000000000000'}))
            self.assertFalse(matches_identity(document, {'version': ''}))
            self.assertTrue(matches_identity(document, {'version': document['version']}))

        def test_source_defaults_theo_and_offline_cache(self):
            with tempfile.TemporaryDirectory() as directory:
                store = OffsetStore(directory, get=lambda *args, **kwargs: Response(THEO))
                self.assertEqual(store.source, 'theo')
                self.assertEqual(len(store.document()['flags']), 2)
                with patch('requests.get', side_effect=syntax.requests.ConnectionError('offline')):
                    cached = OffsetStore(directory).document()
                self.assertEqual(len(cached['flags']), 2)

        def test_local_dump_bound_to_exact_image(self):
            process = FixtureProcess()
            document = EDumper(process).dump()
            with tempfile.TemporaryDirectory() as directory:
                store = OffsetStore(directory)
                path = Path(store.save_dump(document))
                self.assertTrue(path.exists())
                self.assertTrue(path.with_suffix('.hpp').exists())
                store.set_source('local')
                self.assertEqual(store.document(process.identity)['flags'], document['flags'])
                with self.assertRaises(ValueError):
                    store.document({**process.identity, 'timestamp': 124})

        def test_invalid_offset_rejected(self):
            with self.assertRaises(ValueError):
                parse_theo(THEO.replace('0x2000', '0xFFFFFFFFFFFFFFFF'))

    class DumperTests(unittest.TestCase):

        def test_ambiguous_layout_not_guessed(self):
            process = FixtureProcess()
            for index in range(len(process.nodes)):
                process.put64(3145728 + index * 256 + 40, process.base_address + 24576 + index * 8)
            self.assertIsNone(EDumper(process).infer_layout(process.head))

        def test_layout_inference_checks_cancellation(self):
            process = FixtureProcess()
            cancel = threading.Event()
            cancel.set()
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                EDumper(process, cancel).infer_layout(process.head)

        def test_real_traversal_and_layout_inference(self):
            process = FixtureProcess()
            result = EDumper(process).dump()
            self.assertEqual(len(result['flags']), 6)
            self.assertEqual(result['layout'], {'Pointer': 4352, 'ToFlag': 48, 'ToValue': 32})
            self.assertIn('LongFlagValueName5', result['flags'])
            self.assertLess(process.read_count, 1000)

        def test_cycle_terminates(self):
            process = FixtureProcess()
            process.put64(process.nodes[-1], process.nodes[2])
            with self.assertRaisesRegex(ValueError, 'Corrupt'):
                list(EDumper(process).walk(process.head))
            with self.assertRaises(ValueError):
                EDumper(process).dump()

        def test_cancellation(self):
            event = threading.Event()
            event.set()
            with self.assertRaisesRegex(RuntimeError, 'cancelled'):
                EDumper(FixtureProcess(), event).dump()

        def test_timeout(self):
            with self.assertRaises(TimeoutError):
                EDumper(FixtureProcess(), timeout=-1).dump()

        def test_process_exit(self):
            process = FixtureProcess()
            process.running = False
            with self.assertRaisesRegex(RuntimeError, 'closed'):
                EDumper(process).dump()

        def test_missing_container_fails(self):
            process = FixtureProcess()
            process.put64(process.base_address + 4352, 0)
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                EDumper(process).dump()

    class WorkerTests(unittest.TestCase):

        def test_manual_mode_waits_for_apply(self):
            self.worker.set_LockF(False)
            self.worker.set_flags(syntax.parse_flags('{"DFIntFirst":"50"}'))
            self.worker._tick()
            self.assertIsNone(self.worker._manager)
            self.assertEqual(self.worker.snapshot()['state'], 'pending')
            self.worker.request_apply()
            self.worker._tick()
            injector = self.worker._manager.injector
            self.assertEqual(injector.writes, [(4096, '50')])
            for index in range(20):
                self.worker._tick()
            self.assertEqual(injector.writes, [(4096, '50')])

        def test_manual_edits_wait_for_next_apply(self):
            injector = self.apply('{"DFIntFirst":"50"}')
            self.worker.set_LockF(False)
            self.worker.set_flags(syntax.parse_flags('{"DFIntFirst":"55"}'))
            self.worker._tick()
            self.assertEqual(injector.values[4096], '50')
            self.worker.request_apply()
            self.worker._tick()
            self.assertEqual(injector.values[4096], '55')

        def test_manual_request_waits_for_client(self):
            self.worker.set_LockF(False)
            self.worker.set_flags(syntax.parse_flags('{"DFIntFirst":"50"}'))
            self.pids.clear()
            self.worker.request_apply()
            self.worker._tick()
            self.assertIsNone(self.worker._manager)
            self.pids.append(456)
            self.worker._tick()
            self.assertEqual(self.worker._manager.injector.writes, [(4096, '50')])
            self.assertEqual(self.worker._pending_apply, 0)

        def test_fast_exit_pauses_LockF_restart(self):
            self.apply('{"DFIntFirst":"50"}')
            self.pids.clear()
            self.worker._tick()
            self.assertEqual(self.worker.snapshot()['state'], 'paused')
            self.pids.append(456)
            self.worker._tick()
            self.assertIsNone(self.worker._manager)
            self.worker.request_apply()
            self.worker._tick()
            self.assertEqual(self.worker._manager.injector.writes, [(4096, '50')])

        def test_disabling_LockF_during_connection_stops_writes(self):
            def fetch(injector, force=False):
                self.worker.set_LockF(False)
                return True
            self.worker.set_flags(syntax.parse_flags('{"DFIntFirst":"50"}'))
            with patch.object(FakeInjector, 'fetch_offsets', fetch):
                self.worker._tick()
            self.assertEqual(FakeInjector.created[-1].writes, [])

        def test_apply_checks_current_values(self):
            injector = self.apply('{"DFIntFirst":"50"}')
            injector.values[4096] = '77'
            self.worker.request_apply()
            self.worker._tick()
            self.assertEqual(injector.values[4096], '50')
            self.assertEqual(len(injector.writes), 2)

        def test_cleanup_failure_closes_handle(self):
            injector = self.apply('{"DFIntFirst":"50"}')
            with patch.object(injector, 'read_flag_at_address', side_effect=RuntimeError('read failed')):
                self.worker._disconnect(restore=True)
            self.assertFalse(injector.is_attached)
            self.assertIsNone(self.worker._manager)

        def test_startup_delay_and_manual_override(self):
            self.worker._settle_seconds = 30
            self.worker.set_flags(syntax.parse_flags('{"DFIntFirst":"50"}'))
            self.worker._tick()
            self.assertEqual(self.worker.snapshot()['state'], 'settling')
            self.assertIsNone(self.worker._manager)
            self.worker.request_apply()
            self.worker._tick()
            self.assertEqual(self.worker._manager.injector.writes, [(4096, '50')])

        def test_LockF_disable_interrupts_batch(self):
            write = FakeInjector.write_flag_at_address
            def interrupt(injector, kind, address, value):
                result = write(injector, kind, address, value)
                self.worker.set_LockF(False)
                return result
            with patch.object(FakeInjector, 'write_flag_at_address', interrupt):
                injector = self.apply('{"DFIntFirst":"50","DFIntSecond":"60"}')
            self.assertEqual(injector.writes, [(4096, '50')])
            self.worker.set_LockF(True)
            self.worker._tick()
            self.assertEqual(injector.writes, [(4096, '50'), (8192, '60')])

        def setUp(self):
            self.pids = [123]
            self.worker = syntax.LockFController(lambda message: None, fake_engine(self.pids), platform='win32')

        def tearDown(self):
            self.worker._disconnect(restore=True)

        def apply(self, content):
            self.worker.set_flags(syntax.parse_flags(content))
            self.worker._tick()
            return self.worker._manager.injector

        def test_no_periodic_or_unchanged_writes(self):
            injector = self.apply('{"DFIntFirst":"50"}')
            self.assertEqual(injector.writes, [(4096, '50')])
            for _ in range(100):
                self.worker._tick()
            self.assertEqual(len(injector.writes), 1)
            self.apply('{"DFIntFirst":"50","DFIntSecond":"60"}')
            self.assertEqual(len(injector.writes), 2)

        def test_disabled_restores_original(self):
            injector = self.apply('{"DFIntFirst":"50"}')
            self.worker.set_flags([])
            self.worker._tick()
            self.assertEqual(injector.values[4096], '10')
            self.assertFalse(injector.is_attached)

        def test_external_changes_not_overwritten_on_restore(self):
            injector = self.apply('{"DFIntFirst":"50"}')
            injector.values[4096] = '77'
            self.worker.set_flags([])
            self.worker._tick()
            self.assertEqual(injector.values[4096], '77')

        def test_process_exit_no_stale_write(self):
            injector = self.apply('{"DFIntFirst":"50"}')
            self.pids.clear()
            self.worker._tick()
            self.assertFalse(injector.is_attached)
            self.assertEqual(len(injector.writes), 1)

        def test_string_and_unknown_prefix_never_written(self):
            injector = self.apply('{"FStringFirst":"hi","Second":"12"}')
            self.assertEqual(injector.writes, [])
            self.assertEqual(self.worker.snapshot()['failed'], 2)

        def test_changed_value_writes_once(self):
            injector = self.apply('{"DFIntFirst":"50"}')
            self.apply('{"DFIntFirst":"55"}')
            self.assertEqual(injector.writes, [(4096, '50'), (4096, '55')])
            self.worker._disconnect(restore=True)
            self.assertEqual(injector.values[4096], '10')

        def test_configuration_disabled_while_connecting_never_written(self):

            def fetch(injector, force=False):
                self.worker.set_flags([])
                return True
            self.worker.set_flags(syntax.parse_flags('{"DFIntFirst":"50"}'))
            with patch.object(FakeInjector, 'fetch_offsets', fetch):
                self.worker._tick()
            injector = FakeInjector.created[-1]
            self.assertEqual(injector.writes, [])
            self.assertFalse(injector.is_attached)

        def test_configuration_disabled_during_scan_never_written(self):
            scan = FakeInjector.scan_live_flags

            def interrupt(injector, names, force_rescan=False):
                result = scan(injector, names, force_rescan)
                self.worker.set_flags([])
                return result
            with patch.object(FakeInjector, 'scan_live_flags', interrupt):
                injector = self.apply('{"DFIntFirst":"50"}')
            self.assertEqual(injector.writes, [])

        def test_external_change_before_verification_is_not_captured_as_our_write(self):
            write = FakeInjector.write_flag_at_address

            def concurrent_write(injector, kind, address, value):
                result = write(injector, kind, address, value)
                injector.values[address] = '77'
                return result
            with patch.object(FakeInjector, 'write_flag_at_address', concurrent_write):
                injector = self.apply('{"DFIntFirst":"50"}')
            self.assertEqual(self.worker.snapshot()['failed'], 1)
            self.worker._disconnect(restore=True)
            self.assertEqual(injector.values[4096], '77')
            self.assertEqual(injector.writes, [(4096, '50')])

        def test_equivalent_value_needs_no_write_or_restore(self):
            injector = self.apply('{"DFIntFirst":"010"}')
            self.assertEqual(injector.writes, [])
            self.worker._disconnect(restore=True)
            self.assertEqual(injector.writes, [])

        def test_unchanged_input_does_not_create_generation(self):
            rows = syntax.parse_flags('{"DFIntFirst":"50"}')
            self.worker.set_flags(rows)
            generation = self.worker._generation
            self.worker.set_flags(rows)
            self.assertEqual(self.worker._generation, generation)

        def test_refresh_reuses_already_refreshed_store(self):
            injector = self.apply('{"DFIntFirst":"50"}')
            with patch.object(injector, 'fetch_offsets', wraps=injector.fetch_offsets) as fetch:
                self.worker.refresh()
                self.worker._tick()
            fetch.assert_called_once_with()

        def test_float_readback_compared_at_single_precision(self):
            write = FakeInjector.write_flag_at_address

            def single_precision(injector, kind, address, value):
                result = write(injector, kind, address, value)
                injector.values[address] = syntax.scalar_value(kind, value)
                return result
            with patch.object(FakeInjector, 'write_flag_at_address', single_precision):
                self.apply('{"FFloatFirst":"0.1"}')
            self.assertEqual(self.worker.snapshot()['applied'], 1)

    class ApiTests(unittest.TestCase):

        def _mock_startup(self, result=None):
            mocks = {}
            for name in ('_initialize_desktop', '_prefetch_catalog', '_rpc_loop', 'refresh_configs'):
                item = patch.object(self.app, name)
                mocks[name] = item.start()
                self.addCleanup(item.stop)
            item = patch.object(self.app.controller, 'start')
            mocks['controller'] = item.start()
            self.addCleanup(item.stop)
            def check():
                self.app.update = copy.deepcopy(result or {'state': 'current'})
                self.app._startup_checked.set()
                return {'ok': True}
            item = patch.object(self.app, 'check_updates', side_effect=check)
            mocks['check'] = item.start()
            self.addCleanup(item.stop)
            return mocks

        def test_startup_checks_before_services(self):
            mocks = self._mock_startup()
            events = []
            mocks['_initialize_desktop'].side_effect = lambda: events.append('desktop')
            check = mocks['check'].side_effect
            def checked():
                events.append('updates')
                return check()
            mocks['check'].side_effect = checked
            mocks['controller'].side_effect = lambda: events.append('services')
            self.app._run_startup()
            self.assertEqual(events, ['desktop', 'updates', 'services'])
            self.assertEqual(self.app.get_state()['startup']['state'], 'ready')
            self.assertEqual(self.app.get_state()['startup']['completed'], 4)

        def test_startup_failure_opens_application(self):
            mocks = self._mock_startup({'state': 'error', 'error': 'Offline'})
            self.app._run_startup()
            mocks['controller'].assert_called_once()
            self.assertEqual(self.app._startup_state['state'], 'ready')
            self.assertEqual(self.app._startup_state['update_state'], 'error')

        def test_startup_timeout_is_bounded(self):
            mocks = self._mock_startup()
            mocks['check'].side_effect = None
            self.app._startup_timeout = 0
            self.app._run_startup()
            self.assertEqual(self.app._startup_state['state'], 'ready')
            self.assertEqual(self.app._startup_state['update_state'], 'timeout')

        def test_startup_update_waits_for_choice(self):
            mocks = self._mock_startup({'state': 'available', 'asset': 'Syntax.exe.zip'})
            self.app._run_startup()
            mocks['controller'].assert_not_called()
            self.assertEqual(self.app._startup_state['state'], 'update_available')
            self.assertTrue(self.app.complete_startup()['ok'])
            self.assertEqual(self.app._startup_state['state'], 'ready')
            self.app.complete_startup()
            mocks['controller'].assert_called_once()

        def test_background_startup_does_not_prompt(self):
            mocks = self._mock_startup({'state': 'available', 'asset': 'Syntax.exe.zip'})
            self.app._background = True
            self.app._run_startup()
            mocks['controller'].assert_called_once()
            self.assertEqual(self.app._startup_state['state'], 'ready')

        def test_packaged_startup_installs_zip_update(self):
            mocks = self._mock_startup({'state': 'available', 'asset': 'Syntax.exe.zip'})
            with patch.object(sys, 'platform', 'win32'), patch.object(sys, 'frozen', True, create=True), \
                    patch.object(self.app, 'install_update', return_value={'ok': True}) as install:
                self.app._run_startup()
            install.assert_called_once()
            mocks['controller'].assert_not_called()
            self.assertEqual(self.app._startup_state['state'], 'update_available')

        def test_startup_update_blocks_continue(self):
            mocks = self._mock_startup()
            self.app.update = {'state': 'downloading'}
            self.assertFalse(self.app.complete_startup()['ok'])
            mocks['controller'].assert_not_called()

        def test_startup_service_failure_not_stuck(self):
            mocks = self._mock_startup()
            mocks['controller'].side_effect = RuntimeError('Start failed')
            self.app._run_startup()
            mocks['refresh_configs'].assert_called_once()
            self.assertEqual(self.app._startup_state['state'], 'ready')

        def test_startup_does_not_restart_after_close(self):
            mocks = self._mock_startup()
            self.app.shutdown()
            self.app._run_startup()
            mocks['_initialize_desktop'].assert_not_called()
            mocks['controller'].assert_not_called()

        def test_update_exception_releases_loading(self):
            with patch.object(syntax, 'check_release', side_effect=RuntimeError('Network failed')):
                self.app.check_updates()
                self.assertTrue(self.app._startup_checked.wait(2))
            self.assertEqual(self.app.update['state'], 'error')

        def test_startup_runs_once(self):
            with patch.object(threading, 'Thread') as thread:
                self.app.start()
                self.app.start()
            self.assertEqual(thread.call_count, 1)
            thread.return_value.start.assert_called_once()

        def test_update_requires_packaged_application(self):
            self.assertFalse(self.app.install_update()['ok'])

        def test_update_without_release_rejected(self):
            with patch.object(sys, 'platform', 'win32'), patch.object(sys, 'frozen', True, create=True):
                self.assertFalse(self.app.install_update()['ok'])
            self.assertFalse(self.app._update_lock.locked())

        def test_failed_download_never_exits_application(self):
            self.app.update = {'state': 'available', 'asset': 'Syntax.exe.zip', 'latest': 'v3.0'}
            done = threading.Event()
            with patch.object(sys, 'platform', 'win32'), patch.object(sys, 'frozen', True, create=True), \
                    patch.object(syntax, 'prepare_update', side_effect=ValueError('Download failed')), \
                    patch.object(self.app.desktop, 'exit') as exit_app, \
                    patch.object(self.app, 'log', side_effect=lambda message: done.set()):
                self.assertTrue(self.app.install_update()['ok'])
                self.assertTrue(done.wait(2))
                exit_app.assert_not_called()
            self.assertEqual(self.app.update['state'], 'error')

        def test_update_exits_only_after_ready(self):
            self.app.update = {'state': 'available', 'asset': 'Syntax.exe.zip', 'latest': 'v3.0'}
            workspace = Path(self.directory.name) / 'helper'
            workspace.mkdir()
            manifest = workspace / 'update.json'
            data = {'token': uuid.uuid4().hex, 'source': str(workspace / 'Syntax.exe'), 'pending': str(workspace / 'pending.exe')}
            done = threading.Event()
            def launch(*args, **kwargs):
                syntax.atomic_json(workspace / 'ready.json', {'state': 'ready', 'token': data['token']})
                return types.SimpleNamespace(poll=lambda: None)
            with patch.object(sys, 'platform', 'win32'), patch.object(sys, 'frozen', True, create=True), \
                    patch.object(syntax, 'prepare_update', return_value=(manifest, data)), \
                    patch.object(subprocess, 'Popen', side_effect=launch), \
                    patch.object(self.app.desktop, 'exit', side_effect=done.set):
                self.assertTrue(self.app.install_update()['ok'])
                self.assertTrue(done.wait(2))
            self.assertEqual(self.app.update['state'], 'restarting')
            self.assertTrue(self.app._installing_update)

        def test_desktop_defaults_enabled(self):
            self.assertTrue(self.app.settings.get('tray'))
            self.assertTrue(self.app.settings.get('startup'))

        def test_desktop_defaults_migrate_once(self):
            path = self.app.settings.path
            path.write_text(json.dumps({'tray': False, 'startup': False}))
            settings = syntax.Settings(self.directory.name)
            self.assertTrue(settings.get('tray'))
            self.assertTrue(settings.get('startup'))
            settings.set_many({'tray': False, 'startup': False})
            restored = syntax.Settings(self.directory.name)
            self.assertFalse(restored.get('tray'))
            self.assertFalse(restored.get('startup'))

        def test_startup_defaults_register_command(self):
            with patch.object(self.app.desktop, 'start'), patch.object(sys, 'platform', 'win32'):
                self.app._initialize_desktop()
            self.startup_mock.assert_called_once_with(True, Path(__file__))
            restored = syntax.Settings(self.directory.name)
            self.assertTrue(restored.get('startup'))
            self.assertEqual(restored.get('desktop_defaults'), 1)

        def test_startup_failure_updates_preference(self):
            self.startup_mock.side_effect = OSError('registry unavailable')
            with patch.object(self.app.desktop, 'start'), patch.object(sys, 'platform', 'win32'):
                self.app._initialize_desktop()
            self.assertFalse(self.app.settings.get('startup'))
            self.assertTrue(any('Startup failed' in entry['text'] for entry in self.app.logs))

        def test_dark_theme_persists(self):
            self.assertIn('dark', syntax.THEMES)
            self.assertTrue(self.app.save_preferences(True, True, True, theme='dark')['ok'])
            self.assertEqual(syntax.Settings(self.directory.name).get('theme'), 'dark')

        def test_repository_names_match_json(self):
            expected = json.loads((syntax.RESOURCES / 'configs' / 'presets.json').read_text(encoding='utf-8'))
            presets = self.app.get_presets()['items']
            self.assertEqual([(entry['id'], entry['name']) for entry in presets if entry['builtin']], [(entry['id'], entry['name']) for entry in expected])
            packs = self.app.get_community()['items']
            self.assertTrue(set(entry['id'] for entry in packs).isdisjoint(entry['id'] for entry in presets))

        def test_saved_presets_stay_separate(self):
            self.assertTrue(self.app.save_preset('User sample', '{"FIntExample":"1"}')['ok'])
            items = self.app.get_presets()['items']
            self.assertEqual([entry['name'] for entry in items if not entry['builtin']], ['User sample'])
            self.assertEqual(sum(entry['builtin'] for entry in items), 13)
            self.assertEqual(len(self.app.get_community()['items']), 3)

        def test_preset_controls_follow_resolved_names(self):
            rows = syntax.parse_flags('{"DFIntExample":"1","DFIntSecond":"2"}')
            item = {'id': 'mapped', 'name': 'Mapped', 'builtin': True, 'rows': rows, 'flags': {'Example': '1', 'Second': '2'}, 'controls': {'Example': {'type': 'integer', 'connects_to': 'Second'}}}
            controls = self.app._preset_controls(item, rows)
            self.assertEqual(controls['DFIntExample']['connects_to'], 'DFIntSecond')
            self.app.presets.append(item)
            self.assertTrue(self.app.apply_preset('mapped', {'DFIntExample': '9'})['ok'])
            self.assertEqual({row['name']: row['value'] for row in self.app.rows}, {'DFIntExample': '9', 'DFIntSecond': '9'})

        def test_saved_debloat_preserves_repository(self):
            before = copy.deepcopy(self.app.presets)
            self.app.save_preset('User sample', '{"FIntExample":"1"}')
            self.app.catalog.known_names = frozenset()
            with patch.object(self.app.catalog, 'entries', return_value=[]):
                result = self.app.debloat_presets()
            self.assertTrue(result['ok'])
            self.assertEqual(result['removed'], 1)
            self.assertEqual([entry for entry in self.app.presets if entry['builtin']], before)
            self.assertEqual(self.app.settings.get('preset_overrides'), {})

        def test_LockF_legacy_preference_migrates(self):
            path = self.app.settings.path
            path.write_text(json.dumps({syntax.LEGACY_LOCKF_KEY: False}))
            settings = syntax.Settings(self.directory.name)
            self.assertFalse(settings.get('LockF'))
            settings.set('theme', 'purple')
            saved = json.loads(path.read_text())
            self.assertFalse(saved['LockF'])
            self.assertNotIn(syntax.LEGACY_LOCKF_KEY, saved)

        def test_LockF_current_key_has_priority(self):
            path = self.app.settings.path
            for current, legacy in ((True, False), (False, True)):
                path.write_text(json.dumps({'LockF': current, syntax.LEGACY_LOCKF_KEY: legacy}))
                settings = syntax.Settings(self.directory.name)
                self.assertEqual(settings.get('LockF'), current)

        def test_LockF_preference_persists(self):
            result = self.app.save_preferences(True, True, True, LockF=False)
            self.assertTrue(result['ok'])
            self.assertFalse(syntax.Settings(self.directory.name).get('LockF'))
            self.assertFalse(self.app.controller._LockF)

        def test_apply_queues_manual_request(self):
            self.app.controller.set_LockF(False)
            self.assertTrue(self.app.apply_flags()['ok'])
            self.assertGreater(self.app.controller._pending_apply, 0)

        def test_ui_bridge_has_only_commands(self):
            import inspect
            bridge = syntax.UiBridge(self.app)
            names = [name for name in dir(bridge) if not name.startswith('_')]
            self.assertEqual(len(names), 39)
            self.assertTrue(all(inspect.ismethod(getattr(bridge, name)) for name in names))
            self.assertNotIn('window', names)
            self.assertNotIn('desktop', names)
            self.assertNotIn('shutdown', names)

        def test_version_and_apply_controls(self):
            self.assertEqual(syntax.version_key(syntax.VERSION), syntax.version_key(syntax.DISPLAY_VERSION))
            self.assertEqual(self.app.get_state()['version'], syntax.DISPLAY_VERSION)
            html = syntax.render_html()
            self.assertIn('<strong>Syscall</strong>', html)
            self.assertIn('<small>Local Dumper</small>', html)
            self.assertIn("button('Apply', 'apply'", html)
            self.assertIn('data-preference="LockF"', html)
            self.assertNotIn('data-action="inject"', html)

        def setUp(self):
            self.directory = tempfile.TemporaryDirectory()
            self.startup_patch = patch.object(syntax, 'set_startup')
            self.startup_mock = self.startup_patch.start()
            self.addCleanup(self.startup_patch.stop)
            self.app = syntax.Api(self.directory.name)

        def tearDown(self):
            self.app.shutdown()
            self.directory.cleanup()

        def test_release_presets_all_present(self):
            identifiers = {preset['id'] for preset in self.app.presets}
            self.assertTrue({'black-box', 'sharpening', 'hitbox-sync', 'render-distance', 'remove-textures', 'grey-avatar'} <= identifiers)

        def test_clean_name_gets_authoritative_prefix(self):
            self.app.add_flag('TextureCompositorActiveJobs', '0')
            self.assertEqual(self.app.rows[0]['name'], 'DFIntTextureCompositorActiveJobs')

        def test_unknown_clean_name_paused(self):
            self.app.add_flag('UnlistedThing', '12')
            self.assertFalse(self.app.rows[0]['enabled'])
            self.assertTrue(self.app.rows[0]['needs_type'])

        def test_wrong_known_type_paused(self):
            self.app.add_flag('FFlagCameraFarZPlane', 'true')
            self.assertFalse(self.app.rows[0]['enabled'])
            self.assertTrue(self.app.rows[0]['needs_type'])

        def test_pack_reapply_does_not_duplicate(self):
            for identifier in ('fps', 'potato', 'ping'):
                self.assertTrue(self.app.apply_community(identifier)['ok'])
                count = len(self.app.rows)
                self.assertTrue(self.app.apply_community(identifier)['ok'])
                self.assertEqual(len(self.app.rows), count)
            self.assertEqual(len({row['suffix'] for row in self.app.rows}), len(self.app.rows))

        def test_incremental_state_omits_unchanged_rows(self):
            self.app.set_raw_flags(json.dumps({f'DFIntValue{index}': str(index) for index in range(5000)}))
            first = self.app.get_state()
            second = self.app.get_state(0, first['revision'], first['catalog_revision'], first['status'].get('revision', 0))
            self.assertIsNone(second['rows'])
            self.assertIsNone(second['status'])
            self.assertLess(len(json.dumps(second)), len(json.dumps(first)) / 100)

        def test_preferences_persist_in_one_transaction(self):
            self.assertTrue(self.app.save_preferences(False, False, False, 'ocean', False, False)['ok'])
            self.assertEqual(syntax.Settings(self.directory.name).get('theme'), 'ocean')

        def test_broken_remote_does_not_replace_presets(self):
            before = copy.deepcopy(self.app.presets)
            with self.assertRaises(ValueError):
                self.app._validate_repository_file('configs/presets.json', [{'id': 'bad'}])
            self.assertEqual(self.app.presets, before)

        def test_repository_paths_cannot_escape(self):
            for path in ('../secret.json', 'configs/../../secret.json', '/configs/a.json', 'configs\\a.json'):
                with self.assertRaises(ValueError):
                    config_path(path)

        def test_main_branch_download_matches_bundled(self):
            resources = syntax.RESOURCES

            def get(url, **kwargs):
                name = url.split('/main/', 1)[1]
                return Response((resources / name).read_bytes())
            repository = RepositoryConfigs(resources, self.directory.name, get=get)
            result = repository.refresh(self.app._validate_repository_file)
            self.assertEqual(len(result['files']['configs/presets.json']), 13)
            self.assertTrue((repository.directory / 'snapshot.json').exists())

    class RpcTests(unittest.TestCase):

        def run_rpc(self, processes, enabled=None, fail_update=False, fail_close=False):
            stop = Mock()
            stop.is_set.side_effect = [False] * len(processes) + [True]
            settings = Mock()
            settings.get.side_effect = enabled or [True] * len(processes)
            clients = [Mock(), Mock()]
            if fail_update:
                clients[0].update.side_effect = RuntimeError('Discord disconnected')
            if fail_close:
                clients[0].close.side_effect = RuntimeError('Discord closed')
            constructor = Mock(side_effect=clients)
            app = types.SimpleNamespace(_rpc_stop=stop, settings=settings)
            with patch.dict(sys.modules, {'pypresence': types.SimpleNamespace(Presence=constructor)}), \
                    patch.object(sys, 'platform', 'win32'), \
                    patch.object(syntax, 'find_roblox_processes', side_effect=processes, create=True):
                syntax.Api._rpc_loop(app)
            return clients, constructor

        def test_presence_tracks_roblox_and_buttons(self):
            clients, constructor = self.run_rpc([[], [123], []])
            constructor.assert_called_once_with(syntax.DISCORD_APP_ID)
            updates = [call.kwargs for call in clients[0].update.call_args_list]
            self.assertEqual([value['state'] for value in updates], ['chilling :>', 'Injecting FFlags with Syntax', 'chilling :>'])
            self.assertEqual(updates[0]['details'], 'Syntax ' + syntax.DISPLAY_VERSION)
            self.assertEqual(updates[0]['buttons'], [{'label': 'Discord Server', 'url': 'https://discord.gg/98HWke3zMg'},
                                                   {'label': 'Github Page', 'url': 'https://github.com/Astraliscute/Syntax'}])
            clients[0].close.assert_called_once()

        def test_disabled_presence_never_connects(self):
            clients, constructor = self.run_rpc([[]], enabled=[False])
            constructor.assert_not_called()
            clients[0].update.assert_not_called()

        def test_presence_reconnects_after_disconnect(self):
            clients, constructor = self.run_rpc([[], [123]], fail_update=True)
            self.assertEqual(constructor.call_count, 2)
            self.assertEqual(clients[1].update.call_args.kwargs['state'], 'Injecting FFlags with Syntax')
            clients[0].close.assert_called_once()
            clients[1].close.assert_called_once()

        def test_disabling_presence_tolerates_closed_discord(self):
            clients, constructor = self.run_rpc([[], [], [123]], enabled=[True, False, True], fail_close=True)
            self.assertEqual(constructor.call_count, 2)
            clients[1].update.assert_called_once()
            clients[1].close.assert_called_once()

    class DesktopTests(unittest.TestCase):

        def test_theme_logos_preserve_shape(self):
            from PIL import Image
            with Image.open(syntax.RESOURCES / 'syntax.ico') as original:
                alpha = original.convert('RGBA').getchannel('A').tobytes()
            colors = set()
            for theme in syntax.THEMES:
                image = syntax.theme_image(theme, syntax.RESOURCES)
                self.assertEqual(image.getchannel('A').tobytes(), alpha)
                colors.add(hashlib.sha256(image.tobytes()).hexdigest())
            self.assertEqual(len(colors), 7)

        def test_tray_logo_changes_theme(self):
            app = types.SimpleNamespace(log=lambda message: None)
            desktop = DesktopIntegration(app, syntax.RESOURCES)
            desktop.icon = types.SimpleNamespace(icon=None)
            desktop.set_theme('dark')
            dark = desktop.icon.icon
            self.assertIsNotNone(dark)
            desktop.set_theme('ocean')
            self.assertNotEqual(dark.tobytes(), desktop.icon.icon.tobytes())
            desktop.exiting = True
            last = desktop.icon.icon
            desktop.set_theme('rose')
            self.assertIs(desktop.icon.icon, last)

        def test_shutdown_does_not_restore_or_hide_window(self):

            class Window:

                def show(self):
                    raise AssertionError('Window shown after exit')

                def restore(self):
                    raise AssertionError('Window restored after exit')

                def hide(self):
                    raise AssertionError('Window hidden after exit')
            app = types.SimpleNamespace(window=Window())
            desktop = DesktopIntegration(app, syntax.RESOURCES)
            desktop.stop()
            desktop.restore()
            self.assertFalse(desktop.hide())

        def test_late_tray_setup_does_not_publish_icon_after_exit(self):

            class Icon:
                visible = False
                stopped = False

                def stop(self):
                    self.stopped = True
            desktop = DesktopIntegration(types.SimpleNamespace(), syntax.RESOURCES)
            icon = Icon()
            desktop.stop()
            desktop._setup(icon)
            self.assertTrue(icon.stopped)
            self.assertFalse(icon.visible)
            self.assertFalse(desktop.ready.is_set())

        def test_tray_close_does_not_block_ui_thread(self):
            app = types.SimpleNamespace(settings=types.SimpleNamespace(get=lambda key: True))
            desktop = DesktopIntegration(app, syntax.RESOURCES)
            desktop.icon = object()
            desktop.ready.set()
            desktop.start = lambda: None
            hidden = threading.Event()
            thread_ids = []

            def hide():
                thread_ids.append(threading.get_ident())
                hidden.set()
            desktop.hide = hide
            self.assertFalse(desktop.closing())
            self.assertTrue(hidden.wait(1))
            self.assertNotEqual(thread_ids[0], threading.get_ident())

        def test_explicit_tray_exit_closes_window(self):
            app = types.SimpleNamespace(settings=types.SimpleNamespace(get=lambda key: True))
            desktop = DesktopIntegration(app, syntax.RESOURCES)
            desktop.exiting = True
            self.assertTrue(desktop.closing())

    @unittest.skipUnless(sys.platform == 'win32', 'Win32 ABI test')
    class NativeTests(unittest.TestCase):

        def test_update_waits_for_child_exit(self):
            with tempfile.TemporaryDirectory() as directory:
                child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(0.15)'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    syntax.wait_update_parent(child.pid, Path(directory) / 'cancel', timeout=5)
                finally:
                    child.wait(timeout=5)

        def test_scalar_writes_preserve_adjacent_bytes(self):
            kernel = syntax._k32
            kernel.GetCurrentProcessId.restype = ctypes.c_uint32
            memory = (ctypes.c_ubyte * 16)(*[165] * 16)
            base = ctypes.addressof(memory)
            process = syntax.AdvancedInjector.__new__(syntax.AdvancedInjector)
            process._h_process = kernel.OpenProcess(56 | 1024 | 4096, False, kernel.GetCurrentProcessId())
            self.assertTrue(process._h_process)
            process.read_only = False
            process.is_attached = True
            process.base_address = base
            process.sections = [{'rva': 0, 'size': 16, 'writable': True, 'executable': False}]
            try:
                self.assertTrue(process.write_flag_at_address('log', base, '7')[0])
                self.assertEqual(bytes(memory)[:4], b'\x07\xa5\xa5\xa5')
                self.assertTrue(process.write_flag_at_address('bool', base, 'true')[0])
                self.assertEqual(bytes(memory)[:4], b'\x01\xa5\xa5\xa5')
                self.assertTrue(process.write_flag_at_address('int', base + 4, '42')[0])
                self.assertEqual(bytes(memory)[4:8], struct.pack('<i', 42))
                before = bytes(memory)
                self.assertFalse(process.write_flag_at_address('float', base + 8, 'nan')[0])
                self.assertFalse(process.write_flag_at_address('bool', base, 'invalid')[0])
                self.assertFalse(process.write_flag_at_address('int', base + 1, '1')[0])
                self.assertEqual(bytes(memory), before)
            finally:
                process.close()

        def test_second_instance_notifies_first(self):
            with tempfile.TemporaryDirectory() as directory:
                first = SingleInstance(directory)
                second = None
                try:
                    self.assertTrue(first.primary)
                    notified = threading.Event()
                    first.listen(notified.set)
                    second = SingleInstance(directory)
                    self.assertFalse(second.primary)
                    self.assertTrue(notified.wait(2))
                finally:
                    if second:
                        second.close()
                    first.close()

        def test_process_handle_and_query_abi(self):
            kernel = syntax._k32
            kernel.GetCurrentProcessId.restype = ctypes.c_uint32
            pid = kernel.GetCurrentProcessId()
            process = syntax.AdvancedInjector(pid, lambda message: None, read_only=True)
            try:
                self.assertTrue(process.alive())
                self.assertTrue(process.read(process.base_address, 2) == b'MZ')
                self.assertTrue(process.query_region(process.base_address)['size'] > 0)
                self.assertTrue(process.identity['image_size'] > 0)
                self.assertEqual(ctypes.sizeof(syntax.MEMORY_BASIC_INFORMATION), 48)
                self.assertFalse(process._write_raw(process.base_address, b'X')[0])
            finally:
                process.close()
            self.assertFalse(process.alive())

    class ConfigResponse:

        def __init__(self, content):
            self.content = content if isinstance(content, bytes) else json.dumps(content).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            return None

        def iter_content(self, size):
            yield self.content

    class RepositoryRegressionTests(unittest.TestCase):

        def setUp(self):
            self.temporary = tempfile.TemporaryDirectory()
            self.addCleanup(self.temporary.cleanup)
            self.root = Path(self.temporary.name)
            self.manifest = {'schema': 1, 'presets': 'configs/library.json', 'packs': [{'id': 'sample', 'name': 'Sample', 'file': 'configs/community/sample.json'}]}
            self.preset = {'id': 'sample', 'name': 'Sample', 'flags': {'FIntExample': '2'}, 'controls': {'FIntExample': {'type': 'dropdown', 'options': {'Two': '2'}}}}
            self.files = {'configs/library.json': [self.preset], 'configs/community/sample.json': {'FIntExample': '3'}}

        def get(self, url, **kwargs):
            name = url.split('/', 6)[-1]
            if name == 'configs/manifest.json':
                return ConfigResponse(self.manifest)
            return ConfigResponse(self.files[name])

        def repository(self, get=None):
            return RepositoryConfigs(syntax.RESOURCES, self.root, get=get or self.get)

        def test_manifest_selects_preset_role_for_custom_filename(self):
            store = self.repository()
            snapshot = store.refresh(syntax.Api._validate_repository_file)
            self.assertEqual(snapshot['files']['configs/library.json'][0]['id'], 'sample')
            self.assertEqual(store.load(syntax.Api._validate_repository_file), snapshot)

        def test_optional_packs_default_to_empty_list(self):
            self.manifest.pop('packs')
            snapshot = self.repository().refresh(syntax.Api._validate_repository_file)
            self.assertEqual(snapshot['manifest']['packs'], [])

        def test_invalid_update_preserves_previous_snapshot(self):
            store = self.repository()
            snapshot = store.refresh(syntax.Api._validate_repository_file)
            cache = (store.directory / 'snapshot.json').read_bytes()
            self.preset['controls']['FIntExample']['options'] = {'Broken': []}
            with self.assertRaises(ValueError):
                store.refresh(syntax.Api._validate_repository_file)
            self.assertEqual((store.directory / 'snapshot.json').read_bytes(), cache)
            self.assertEqual(store.load(syntax.Api._validate_repository_file), snapshot)

        def test_bad_cached_controls_fall_back_to_bundled(self):
            self.preset['controls']['FIntExample']['options'] = {'Broken': []}
            store = self.repository()
            store.directory.mkdir()
            (store.directory / 'snapshot.json').write_text(json.dumps({'manifest': self.manifest, 'files': self.files}))
            snapshot = store.load(syntax.Api._validate_repository_file)
            self.assertEqual(snapshot['manifest']['presets'], 'configs/presets.json')

        def test_cache_invalid_unicode_falls_back(self):
            store = self.repository()
            store.directory.mkdir()
            (store.directory / 'snapshot.json').write_bytes(b'\xff')
            self.assertTrue(store.load(syntax.Api._validate_repository_file)['files'])

        def test_cache_excessive_nesting_falls_back(self):
            store = self.repository()
            store.directory.mkdir()
            (store.directory / 'snapshot.json').write_text('[' * 2000 + '0' + ']' * 2000)
            self.assertTrue(store.load(syntax.Api._validate_repository_file)['files'])

        def test_duplicate_json_keys_rejected(self):
            with self.assertRaises(ValueError):
                config_json('{"schema":1,"schema":2}')

        def test_non_finite_json_numbers_rejected(self):
            for constant in ('NaN', 'Infinity', '-Infinity'):
                with self.subTest(constant=constant), self.assertRaises(ValueError):
                    config_json('{"value":' + constant + '}')

        def test_manifest_metadata_cannot_override_pack_rows(self):
            self.manifest['packs'][0].update(rows=['invalid'], flags='invalid')
            pack = validate_manifest(self.manifest)['packs'][0]
            self.assertNotIn('rows', pack)
            self.assertNotIn('flags', pack)

        def test_invalid_manifest_types_rejected(self):
            for field, value in (('schema', True), ('presets', None), ('packs', None)):
                manifest = {**self.manifest, field: value}
                with self.subTest(field=field), self.assertRaises(ValueError):
                    validate_manifest(manifest)
            for field, value in (('id', 1), ('name', ''), ('description', []), ('file', 'configs/library.json')):
                manifest = {**self.manifest, 'packs': [{**self.manifest['packs'][0], field: value}]}
                with self.subTest(field=field), self.assertRaises(ValueError):
                    validate_manifest(manifest)

        def test_invalid_control_values_and_ids_rejected(self):
            for control in ({'type': 'dropdown', 'options': {}}, {'type': 'dropdown', 'options': {'Bad': 'not-an-int'}}, {'type': 'integer', 'connects_to': []}, {'type': 'button', 'label': []}):
                preset = {**self.preset, 'controls': {'FIntExample': control}}
                with self.subTest(control=control), self.assertRaises(ValueError):
                    syntax.Api._validate_repository_file('configs/presets.json', [preset])
            with self.assertRaises(ValueError):
                syntax.Api._validate_repository_file('configs/presets.json', [{**self.preset, 'id': 1}])

        def test_path_rejects_traversal_url_syntax_and_windows_streams(self):
            for name in ('configs/../a.json', 'configs//a.json', 'configs/./a.json', 'configs/a.json?x', 'configs/a.json#x', 'configs/a%2f.json', 'configs/a:b.json', 'configs/a./b.json', 'configs/a\\b.json', None):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    config_path(name)

        def test_master_used_when_main_is_missing(self):
            calls = []

            def get(url, **kwargs):
                calls.append(url)
                if '/main/' in url:
                    raise OSError('missing branch')
                return self.get(url, **kwargs)
            store = self.repository(get)
            self.assertTrue(store.refresh(syntax.Api._validate_repository_file)['files'])
            self.assertEqual(store.branch, 'master')
            self.assertTrue(any(('/Astraliscute/Syntax/main/' in url for url in calls)))

        def test_pre_cancelled_refresh_opens_no_connections(self):
            store = self.repository(get=lambda *args, **kwargs: self.fail('connection opened'))
            store.cancel.set()
            with self.assertRaises(ValueError):
                store.refresh(syntax.Api._validate_repository_file)
            self.assertFalse((store.directory / 'snapshot.json').exists())

        def test_timeout_budget_is_shared_by_files(self):

            def get(*args, **kwargs):
                return ConfigResponse(self.manifest)
            store = self.repository(get)
            with patch.object(time, 'monotonic', side_effect=[0, 0, 0, 31, 31, 31]):
                with self.assertRaises(ValueError):
                    store.refresh(syntax.Api._validate_repository_file)
            self.assertFalse((store.directory / 'snapshot.json').exists())

        def test_download_limits_and_cancellation(self):
            get = lambda *args, **kwargs: ConfigResponse(b'12345')
            with self.assertRaises(ValueError):
                download('https://example.invalid', get, limit=4)
            event = threading.Event()
            event.set()
            with self.assertRaises(RuntimeError):
                download('https://example.invalid', lambda *args, **kwargs: self.fail('connection opened'), event)

    class EditorRegressionTests(unittest.TestCase):

        def setUp(self):
            self.temporary = tempfile.TemporaryDirectory()
            self.addCleanup(self.temporary.cleanup)
            startup_patch = patch.object(syntax, 'set_startup')
            startup_patch.start()
            self.addCleanup(startup_patch.stop)
            self.app = syntax.Api(self.temporary.name)
            self.addCleanup(self.app.shutdown)

        def test_profile_edit_export_and_reload_without_network(self):
            with patch('requests.get', side_effect=AssertionError('unexpected network request')):
                self.assertTrue(self.app.add_flag('FIntExample', '42')['ok'])
                exported = self.app.get_raw_flags()['content']
                self.assertEqual(json.loads(exported), {'FIntExample': '42'})
                restored = syntax.Settings(self.temporary.name).get('f')
                self.assertEqual(restored[0]['value'], '42')

        def test_community_apply_only_modifies_profile(self):
            self.assertTrue(self.app.apply_community('fps')['ok'])
            self.assertEqual(self.app.controller.snapshot()['applied'], 0)
            self.assertEqual(self.app.controller.snapshot()['pid'], 0)

        def test_community_and_presets_report_both_revisions(self):
            for result in (self.app.get_community(), self.app.get_presets()):
                self.assertEqual(result['revision'], self.app.preset_revision)
                self.assertEqual(result['catalog_revision'], self.app.catalog.revision)

        def test_unchanged_repository_does_not_rebuild_library(self):
            revision = self.app.preset_revision
            self.assertFalse(self.app._install_repository(self.app._repository_snapshot))
            self.assertEqual(self.app.preset_revision, revision)

        def test_corrupt_prefix_cache_never_prevents_startup(self):
            root = Path(self.temporary.name)
            for value in ([], None, 'invalid', {'prefixes': [], 'created': []}):
                with self.subTest(value=value):
                    (root / 'prefixes.json').write_text(json.dumps(value))
                    self.assertTrue(PrefixCatalog(syntax.RESOURCES, root).mapping)

        def test_deeply_nested_import_returns_error(self):
            result = self.app.set_raw_flags('[' * 2000 + '0' + ']' * 2000)
            self.assertFalse(result['ok'])

        def test_import_count_rejected_before_row_allocation(self):
            content = json.dumps({f'FIntExample{index}': '1' for index in range(syntax.MAX_FLAGS + 1)})
            with patch.object(syntax, 'normalize_flag', side_effect=AssertionError('oversized import normalized')):
                with self.assertRaisesRegex(ValueError, 'Maximum'):
                    syntax.parse_flags(content)

        def test_failed_settings_write_retains_old_file_and_removes_temporary(self):
            self.app.settings.set('theme', 'purple')
            before = self.app.settings.path.read_bytes()
            with patch.object(os, 'replace', side_effect=OSError('disk error')):
                self.assertFalse(self.app.save_preferences(True, True, True, 'ocean', False, False)['ok'])
            self.assertEqual(self.app.settings.path.read_bytes(), before)
            self.assertFalse(self.app.settings.path.with_suffix('.json.tmp').exists())

        def test_malformed_optional_settings_do_not_discard_flags(self):
            path = Path(self.temporary.name) / 'status.json'
            path.write_text(json.dumps({'f': {'FIntExample': '42'}, 'saved_presets': {}, 'preset_overrides': []}))
            settings = syntax.Settings(self.temporary.name)
            self.assertFalse(settings.load_error)
            self.assertEqual(settings.get('f')[0]['value'], '42')

    class UpdateTests(unittest.TestCase):

        def setUp(self):
            self.temporary = tempfile.TemporaryDirectory()
            self.addCleanup(self.temporary.cleanup)
            self.root = Path(self.temporary.name)
            self.directory = self.root / 'data'
            self.workspace = self.directory / 'updates' / uuid.uuid4().hex
            self.workspace.mkdir(parents=True)
            app = self.root / 'app'
            app.mkdir()
            self.target = app / syntax.EXECUTABLE_NAME
            self.source = self.workspace / syntax.EXECUTABLE_NAME
            self.pending = app / 'Syntax-update-test.exe'
            self.backup = app / 'Syntax-backup-test.bak'
            self.old = self.pe(b'old')
            self.new = self.pe(b'new')
            self.archive = self.zip_image(self.new)
            self.target.write_bytes(self.old)
            self.source.write_bytes(self.new)
            self.pending.write_bytes(self.new)
            self.manifest = self.workspace / 'update.json'
            self.data = {'schema': 1, 'pid': os.getpid() + 12345, 'token': uuid.uuid4().hex,
                         'version': syntax.VERSION, 'current': '0.0.0', 'source': str(self.source),
                         'target': str(self.target), 'pending': str(self.pending), 'backup': str(self.backup),
                         'size': len(self.new), 'sha256': hashlib.sha256(self.new).hexdigest(),
                         'previous': hashlib.sha256(self.old).hexdigest()}
            syntax.atomic_json(self.manifest, self.data)
            tag = f'v{syntax.version_key(syntax.VERSION)[0] + 1}.0.0'
            self.release = {'tag_name': tag, 'html_url': 'https://github.com/Astraliscute/Syntax/releases/tag/' + tag,
                            'draft': False, 'prerelease': False, 'assets': [
                                 {'name': 'Syntax.exe.zip', 'state': 'uploaded', 'size': len(self.archive),
                                  'digest': 'sha256:' + hashlib.sha256(self.archive).hexdigest(),
                                  'browser_download_url': 'https://github.com/Astraliscute/Syntax/releases/download/' + tag + '/Syntax.exe.zip'}]}

        @staticmethod
        def zip_image(body, name='Syntax.exe', compression=zipfile.ZIP_DEFLATED):
            output = io.BytesIO()
            with zipfile.ZipFile(output, 'w', compression=compression) as archive:
                archive.writestr(name, body)
            return output.getvalue()

        @staticmethod
        def pe(marker):
            data = bytearray(512)
            data[:2] = b'MZ'
            struct.pack_into('<I', data, 60, 128)
            data[128:132] = b'PE\0\0'
            struct.pack_into('<H', data, 132, 0x8664)
            struct.pack_into('<H', data, 152, 0x20B)
            data[400:400 + len(marker)] = marker
            return bytes(data)

        def response(self, document=None, body=None, status=200):
            class Reply:
                status_code = status
                url = 'https://release-assets.githubusercontent.com/update'
                closed = False

                def json(inner):
                    return copy.deepcopy(document)

                def close(inner):
                    inner.closed = True

                def __enter__(inner):
                    return inner

                def __exit__(inner, *arguments):
                    inner.close()

                def raise_for_status(inner):
                    if status >= 400:
                        raise requests.HTTPError('Request failed')

                def iter_content(inner, size):
                    yield self.archive if body is None else body
            return Reply()

        def metadata(self, document=None):
            reply = self.response(self.release if document is None else document)
            result = syntax.check_release('Astraliscute', 'Syntax', get=lambda *args, **kwargs: reply)
            self.assertTrue(reply.closed)
            return result

        def finish(self, wait=None, launch=None):
            return syntax.finish_update(self.manifest, self.directory, wait=wait or (lambda *args: None), launch=launch or (lambda path: None))

        def test_release_prefix_tags_supported(self):
            self.assertEqual(syntax.version_key('Release1.1'), (1, 1, 0))
            self.assertEqual(syntax.version_key('release-2.3.4'), (2, 3, 4))
            self.assertEqual(syntax.version_key('v2.0'), (2, 0, 0))
            with self.assertRaises(ValueError):
                syntax.version_key('unknown')

        def test_exact_asset_selected(self):
            self.release['assets'].insert(0, {'name': 'Syntax.exe'})
            result = self.metadata()
            self.assertEqual(result['state'], 'available')
            self.assertEqual(result['asset'], 'Syntax.exe.zip')
            self.assertTrue(result['download'].endswith('/Syntax.exe.zip'))
            self.assertEqual(result['size'], len(self.archive))

        def test_missing_or_lowercase_asset_rejected(self):
            for name in ('syntax.exe.zip', 'Syntax.exe', 'Syntax.1.1.exe.zip', 'Other.exe.zip'):
                self.release['assets'][0]['name'] = name
                result = self.metadata()
                self.assertEqual(result['state'], 'missing_asset')
                self.assertFalse(result['download'])

        def test_latest_endpoint_used(self):
            calls = []
            def get(url, **kwargs):
                calls.append(url)
                return self.response(self.release)
            syntax.check_release('Astraliscute', 'Syntax', get=get)
            self.assertEqual(calls, ['https://api.github.com/repos/Astraliscute/Syntax/releases/latest'])

        def test_release_without_update_is_current(self):
            for tag in (syntax.VERSION, 'Release1.1'):
                self.release['tag_name'] = tag
                self.release['assets'][0]['browser_download_url'] = 'https://github.com/Astraliscute/Syntax/releases/download/' + tag + '/Syntax.exe.zip'
                self.assertEqual(self.metadata()['state'], 'current')

        def test_foreign_asset_url_rejected(self):
            self.release['assets'][0]['browser_download_url'] = 'https://example.invalid/Syntax.exe.zip'
            self.assertEqual(self.metadata()['state'], 'error')

        def test_duplicate_asset_rejected(self):
            self.release['assets'].append(copy.deepcopy(self.release['assets'][0]))
            self.assertEqual(self.metadata()['state'], 'error')

        def test_draft_and_prerelease_rejected(self):
            for key in ('draft', 'prerelease'):
                document = copy.deepcopy(self.release)
                document[key] = True
                self.assertEqual(self.metadata(document)['state'], 'error')

        def test_asset_size_and_digest_validated(self):
            for value in (0, True, syntax.MAX_UPDATE_BYTES + 1):
                document = copy.deepcopy(self.release)
                document['assets'][0]['size'] = value
                self.assertEqual(self.metadata(document)['state'], 'error')
            self.release['assets'][0]['digest'] = 'invalid'
            self.assertEqual(self.metadata()['state'], 'error')

        def test_rate_limit_and_missing_release(self):
            for status, expected in ((404, 'no_release'), (403, 'error'), (429, 'error')):
                reply = self.response(status=status)
                result = syntax.check_release('Astraliscute', 'Syntax', get=lambda *args, **kwargs: reply)
                self.assertEqual(result['state'], expected)
                self.assertTrue(reply.closed)

        def test_download_verified_before_install(self):
            release = self.metadata()
            destination = self.root / 'Syntax.exe.zip.bak'
            progress = []
            digest = syntax.download_update(release, destination, threading.Event(), lambda done, total: progress.append((done, total)), get=lambda *args, **kwargs: self.response())
            self.assertEqual(digest, hashlib.sha256(self.archive).hexdigest())
            self.assertEqual(destination.read_bytes(), self.archive)
            self.assertEqual(self.target.read_bytes(), self.old)
            self.assertEqual(progress[-1], (len(self.archive), len(self.archive)))

        def test_download_checksum_failure_preserves_application(self):
            release = self.metadata()
            release['sha256'] = '0' * 64
            destination = self.root / 'download.exe'
            with self.assertRaises(ValueError):
                syntax.download_update(release, destination, threading.Event(), get=lambda *args, **kwargs: self.response())
            self.assertFalse(destination.exists())
            self.assertEqual(self.target.read_bytes(), self.old)

        def test_partial_and_oversized_download_removed(self):
            for body in (self.archive[:-1], self.archive + b'x'):
                destination = self.root / 'download.exe'
                with self.assertRaises(ValueError):
                    syntax.download_update(self.metadata(), destination, threading.Event(), get=lambda *args, **kwargs: self.response(body=body))
                self.assertFalse(destination.exists())

        def test_existing_download_not_deleted(self):
            destination = self.root / 'download.exe'
            destination.write_bytes(b'keep')
            with self.assertRaises(FileExistsError):
                syntax.download_update(self.metadata(), destination, threading.Event(), get=lambda *args, **kwargs: self.response())
            self.assertEqual(destination.read_bytes(), b'keep')

        def test_download_cancellation_opens_no_connection(self):
            cancel = threading.Event()
            cancel.set()
            with self.assertRaises(RuntimeError):
                syntax.download_update(self.metadata(), self.root / 'download.exe', cancel, get=lambda *args, **kwargs: self.fail('Unexpected connection'))

        def test_html_and_wrong_architecture_rejected(self):
            invalid = self.root / 'invalid.exe'
            invalid.write_bytes(b'<html>' + bytes(506))
            with self.assertRaises(ValueError):
                syntax.executable_digest(invalid)
            data = bytearray(self.new)
            struct.pack_into('<H', data, 132, 0x14C)
            invalid.write_bytes(data)
            with self.assertRaises(ValueError):
                syntax.executable_digest(invalid)

        def test_preparation_does_not_replace_running_application(self):
            archive = self.target.with_name('Syntax.exe.zip.bak')
            reply = self.response()
            def chunks(size):
                self.assertTrue(archive.is_file())
                self.assertEqual(self.target.read_bytes(), self.old)
                yield self.archive
            reply.iter_content = chunks
            manifest, data = syntax.prepare_update(self.metadata(), self.target, self.directory, threading.Event(), get=lambda *args, **kwargs: reply)
            self.assertTrue(manifest.is_file())
            self.assertEqual(Path(data['source']).name, 'Syntax.exe')
            self.assertEqual(Path(data['pending']).read_bytes(), self.new)
            self.assertEqual(data['size'], len(self.new))
            self.assertEqual(data['sha256'], hashlib.sha256(self.new).hexdigest())
            self.assertEqual(self.target.read_bytes(), self.old)
            self.assertFalse(archive.exists())

        def test_archive_rejects_unsafe_member_names(self):
            archive = self.root / 'update.zip'
            output = self.root / 'extracted.exe'
            for name in ('../Syntax.exe', '/Syntax.exe', 'folder/Syntax.exe', 'folder\\Syntax.exe', 'syntax.exe', 'Syntax.exe:stream'):
                with self.subTest(name=name):
                    archive.write_bytes(self.zip_image(self.new, name))
                    with self.assertRaises(ValueError):
                        syntax.extract_update_archive(archive, output, threading.Event())
                    self.assertFalse(output.exists())

        def test_archive_rejects_extra_members(self):
            archive = self.root / 'update.zip'
            archive.write_bytes(self.archive)
            with zipfile.ZipFile(archive, 'a') as package:
                package.writestr('Other.exe', self.new)
            output = self.root / 'extracted.exe'
            with self.assertRaises(ValueError):
                syntax.extract_update_archive(archive, output, threading.Event())
            self.assertFalse(output.exists())

        def test_archive_rejects_symlinks_and_encryption(self):
            archive = self.root / 'update.zip'
            output = self.root / 'extracted.exe'
            entry = zipfile.ZipInfo('Syntax.exe')
            entry.create_system = 3
            entry.external_attr = 0o120777 << 16
            linked = self.zip_image(self.new, entry)
            encrypted = bytearray(self.archive)
            offset = encrypted.index(b'PK\x01\x02')
            struct.pack_into('<H', encrypted, offset + 8, 1)
            for body in (linked, encrypted):
                archive.write_bytes(body)
                with self.assertRaises(ValueError):
                    syntax.extract_update_archive(archive, output, threading.Event())
                self.assertFalse(output.exists())

        def test_archive_rejects_excessive_expanded_size(self):
            archive = self.root / 'update.zip'
            output = self.root / 'extracted.exe'
            body = bytearray(self.archive)
            offset = body.index(b'PK\x01\x02')
            struct.pack_into('<I', body, offset + 24, syntax.MAX_UPDATE_BYTES + 1)
            archive.write_bytes(body)
            with self.assertRaises(ValueError):
                syntax.extract_update_archive(archive, output, threading.Event())
            self.assertFalse(output.exists())

        def test_archive_rejects_corrupt_crc(self):
            archive = self.root / 'update.zip'
            output = self.root / 'extracted.exe'
            body = self.zip_image(self.new, compression=zipfile.ZIP_STORED).replace(b'new', b'bad', 1)
            archive.write_bytes(body)
            with self.assertRaises(zipfile.BadZipFile):
                syntax.extract_update_archive(archive, output, threading.Event())
            self.assertFalse(output.exists())

        def test_archive_validates_executable_contents(self):
            archive = self.root / 'update.zip'
            output = self.root / 'extracted.exe'
            wrong = bytearray(self.new)
            struct.pack_into('<H', wrong, 132, 0x14C)
            for body in (b'<html>' + bytes(506), wrong):
                archive.write_bytes(self.zip_image(body))
                with self.assertRaises(ValueError):
                    syntax.extract_update_archive(archive, output, threading.Event())
                self.assertFalse(output.exists())

        def test_archive_extraction_cancellation_removes_output(self):
            archive = self.root / 'update.zip'
            output = self.root / 'extracted.exe'
            archive.write_bytes(self.archive)
            cancel = Mock()
            cancel.is_set.side_effect = [False, False, True]
            with self.assertRaises(RuntimeError):
                syntax.extract_update_archive(archive, output, cancel)
            self.assertFalse(output.exists())

        def test_archive_extraction_timeout_removes_output(self):
            archive = self.root / 'update.zip'
            output = self.root / 'extracted.exe'
            archive.write_bytes(self.archive)
            with patch.object(time, 'monotonic', side_effect=[0, 61]):
                with self.assertRaises(TimeoutError):
                    syntax.extract_update_archive(archive, output, threading.Event())
            self.assertFalse(output.exists())

        def test_archive_existing_destination_preserved(self):
            archive = self.root / 'update.zip'
            output = self.root / 'extracted.exe'
            archive.write_bytes(self.archive)
            output.write_bytes(b'keep')
            with self.assertRaises(FileExistsError):
                syntax.extract_update_archive(archive, output, threading.Event())
            self.assertEqual(output.read_bytes(), b'keep')

        def test_preparation_preserves_existing_archive(self):
            archive = self.target.with_name('Syntax.exe.zip.bak')
            archive.write_bytes(b'keep')
            with self.assertRaises(FileExistsError):
                syntax.prepare_update(self.metadata(), self.target, self.directory, threading.Event(), get=lambda *args, **kwargs: self.response())
            self.assertEqual(archive.read_bytes(), b'keep')
            self.assertEqual(self.target.read_bytes(), self.old)

        def test_corrupt_archive_never_replaces_application(self):
            body = b'<html>' + bytes(506)
            before = set(self.target.parent.glob('Syntax-update-*'))
            self.release['assets'][0].update(size=len(body), digest='sha256:' + hashlib.sha256(body).hexdigest())
            with self.assertRaises(zipfile.BadZipFile):
                syntax.prepare_update(self.metadata(), self.target, self.directory, threading.Event(), get=lambda *args, **kwargs: self.response(body=body))
            self.assertEqual(self.target.read_bytes(), self.old)
            self.assertFalse(self.target.with_name('Syntax.exe.zip.bak').exists())
            self.assertEqual(set(self.target.parent.glob('Syntax-update-*')), before)
            self.assertEqual(self.pending.read_bytes(), self.new)

        def test_download_without_optional_digest(self):
            self.release['assets'][0].pop('digest')
            destination = self.root / 'update.zip'
            digest = syntax.download_update(self.metadata(), destination, threading.Event(), get=lambda *args, **kwargs: self.response())
            self.assertEqual(digest, hashlib.sha256(self.archive).hexdigest())
            self.assertEqual(destination.read_bytes(), self.archive)

        def test_install_waits_then_replaces(self):
            events = []
            def wait(*args):
                self.assertEqual(self.target.read_bytes(), self.old)
                self.assertEqual(json.loads((self.workspace / 'ready.json').read_text())['state'], 'ready')
                events.append('wait')
            def launch(path):
                self.assertEqual(path.read_bytes(), self.new)
                events.append('launch')
            self.assertEqual(self.finish(wait, launch), 0)
            self.assertEqual(events, ['wait', 'launch'])
            self.assertEqual(self.target.read_bytes(), self.new)
            self.assertFalse(self.backup.exists())

        def test_failed_replace_rolls_back(self):
            replace = syntax.replace_update_file
            alias = self.pending.parent / '..' / self.pending.parent.name / self.pending.name
            for pending in (self.pending, alias):
                with self.subTest(pending=str(pending)):
                    self.pending.write_bytes(self.new)
                    launches = []
                    def fail(source, target):
                        if Path(source).resolve() == pending.resolve():
                            raise OSError('Copy failed')
                        return replace(source, target)
                    with patch.object(syntax, 'replace_update_file', side_effect=fail) as replaced:
                        self.assertEqual(self.finish(launch=lambda path: launches.append(path.read_bytes())), 1)
                    self.assertEqual(replaced.call_count, 3)
                    self.assertEqual(self.target.read_bytes(), self.old)
                    self.assertEqual(launches, [self.old])
                    self.assertFalse(self.backup.exists())
                    self.assertFalse(self.pending.exists())

        def test_failed_restart_rolls_back(self):
            calls = []
            def launch(path):
                calls.append(path.read_bytes())
                if len(calls) == 1:
                    raise OSError('Restart failed')
            self.assertEqual(self.finish(launch=launch), 1)
            self.assertEqual(calls, [self.new, self.old])
            self.assertEqual(self.target.read_bytes(), self.old)

        def test_exit_timeout_preserves_application(self):
            def wait(*args):
                raise TimeoutError('Parent still running')
            launches = []
            self.assertEqual(self.finish(wait, launches.append), 1)
            self.assertEqual(self.target.read_bytes(), self.old)
            self.assertFalse(launches)

        def test_tampered_pending_does_not_exit_application(self):
            self.pending.write_bytes(self.old)
            calls = []
            self.assertEqual(self.finish(lambda *args: calls.append(True)), 1)
            self.assertFalse(calls)
            self.assertEqual(self.target.read_bytes(), self.old)

        def test_manifest_version_must_match_helper(self):
            self.data['version'] = '999.0.0'
            syntax.atomic_json(self.manifest, self.data)
            self.assertEqual(self.finish(), 1)
            self.assertEqual(self.target.read_bytes(), self.old)

        def test_manifest_outside_workspace_rejected(self):
            path = self.root / 'update.json'
            syntax.atomic_json(path, self.data)
            with self.assertRaises(ValueError):
                syntax.read_update_manifest(path, self.directory)

        def test_binary_filename_contract(self):
            self.assertEqual(syntax.EXECUTABLE_NAME, 'Syntax.exe')
            self.assertEqual(syntax.UPDATE_ARCHIVE_NAME, 'Syntax.exe.zip')

    class SourceShapeTests(unittest.TestCase):

        @unittest.skipIf(getattr(sys, 'frozen', False), 'Source file required')
        def test_source_format(self):
            check_python_source((ROOT / 'syntax.py').read_text(encoding='utf-8'))

        def test_single_python_module(self):
            self.assertEqual(SOURCES, ('syntax.py',))
            self.assertIsNone(syntax.__doc__)
    suite = unittest.TestSuite((unittest.defaultTestLoader.loadTestsFromTestCase(case) for case in (ValidationTests, OffsetTests, DumperTests, WorkerTests, ApiTests, RpcTests, DesktopTests, NativeTests, RepositoryRegressionTests, EditorRegressionTests, UpdateTests, SourceShapeTests)))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1
if __name__ == '__main__':
    if '--finish-update' in sys.argv:
        index = sys.argv.index('--finish-update')
        raise SystemExit(finish_update(sys.argv[index + 1]))
    elif '--build' in sys.argv:
        build_windows()
    elif '--check' in sys.argv:
        check_source()
    elif '--test' in sys.argv:
        raise SystemExit(run_tests())
    elif '--verify-package' in sys.argv:
        index = sys.argv.index('--verify-package')
        verify_package(sys.argv[index + 1], sys.argv[index + 2])
    else:
        main()
