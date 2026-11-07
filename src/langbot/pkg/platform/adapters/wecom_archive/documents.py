"""Bounded private document windows; partial bytes never imply full coverage.

No cache or shared-state writes. Parts JSON is parsed only after the entire
original UTF-8 wire stream has passed its manifest digest.
"""

from hashlib import sha256
import json
import math
import re

from .transport import read_budget
from .reader import SourceUnavailable

_LIMITS = {'text': 8_000_000, 'parts': 16_000_000}
_MAX_CHUNKS = 4000


def _fail():
    raise SourceUnavailable('invalid_document')


def _hash(value):
    return sha256(value).hexdigest()


def _digest(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) is not None


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def _event(source, event, agent):
    if (
        not isinstance(event, dict)
        or event.get('source') != source.source
        or event.get('scope') != source.scope
        or not isinstance(agent, str)
        or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,200}', agent)
        or not isinstance(event.get('evidence'), str)
        or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,255}', event['evidence'])
        or '://' in event['evidence']
        or not isinstance(event.get('version'), str)
        or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,512}', event['version'])
    ):
        raise SourceUnavailable('permission_denied')


def _manifest(value, event):
    if (
        not isinstance(value, dict)
        or set(value) != {'reference', 'version', 'parser_version', 'result_sha256', 'chunk_bytes', 'streams'}
        or value['reference'] != event['evidence']
        or value['version'] != event['version']
        or not isinstance(value['parser_version'], str)
        or not re.fullmatch(r'[A-Za-z0-9._-]{0,128}', value['parser_version'])
        or not _digest(value['result_sha256'])
        or type(value['chunk_bytes']) is not int
        or value['chunk_bytes'] != 8192
        or not isinstance(value['streams'], dict)
        or set(value['streams']) != set(_LIMITS)
    ):
        _fail()
    for name, limit in _LIMITS.items():
        stream = value['streams'][name]
        if (
            not isinstance(stream, dict)
            or set(stream) != {'bytes', 'sha256', 'chunks'}
            or not _integer(stream['bytes'], 0, limit)
            or not _digest(stream['sha256'])
            or not _integer(stream['chunks'], 1, _MAX_CHUNKS)
            or stream['chunks'] < max(1, math.ceil(stream['bytes'] / 8192))
            or stream['chunks'] > max(1, math.ceil(stream['bytes'] / 8189))
        ):
            _fail()
    return value


def manifest_sha256(manifest):
    """Client protocol pin, distinct from owner canonical parser-result digest."""
    return _hash(json.dumps(manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode())


def _read_manifest(source, event, agent):
    before = source.authorize_evidence(event, 'dispatch', agent)
    value = _manifest(source._request('/original-manifest', reference=event['evidence']), event)
    if source.authorize_evidence(event, 'dispatch', agent) != before:
        raise SourceUnavailable('permission_denied')
    return value


def read_manifest(source, event, *, agent):
    _event(source, event, agent)
    with read_budget(source.timeout):
        return _read_manifest(source, event, agent)


def _read_chunk(source, event, agent, manifest, stream, index):
    _manifest(manifest, event)
    if (
        not isinstance(stream, str)
        or stream not in _LIMITS
        or not _integer(index, 0, manifest['streams'][stream]['chunks'] - 1)
    ):
        _fail()
    before = source.authorize_evidence(event, 'dispatch', agent)
    if _read_manifest(source, event, agent) != manifest:
        _fail()
    value = source._request('/original-chunk', reference=event['evidence'], stream=stream, index=index)
    if (
        not isinstance(value, dict)
        or set(value)
        != {'reference', 'version', 'stream', 'index', 'content', 'bytes', 'sha256', 'has_more', 'next_index'}
        or value['reference'] != event['evidence']
        or value['version'] != event['version']
        or value['stream'] != stream
        or type(value['index']) is not int
        or value['index'] != index
        or not isinstance(value['content'], str)
        or not _integer(value['bytes'], 0, 8192)
        or not _digest(value['sha256'])
        or type(value['has_more']) is not bool
    ):
        _fail()
    try:
        wire = value['content'].encode('utf-8')
    except UnicodeError:
        _fail()
    more = index + 1 < manifest['streams'][stream]['chunks']
    if (
        len(wire) != value['bytes']
        or _hash(wire) != value['sha256']
        or value['has_more'] != more
        or (more and (type(value['next_index']) is not int or value['next_index'] != index + 1))
        or (not more and value['next_index'] is not None)
        or value['bytes'] > manifest['streams'][stream]['bytes']
        or (more and value['bytes'] < 8189)
        or (
            not more
            and manifest['streams'][stream]['chunks'] == 1
            and (
                len(wire) != manifest['streams'][stream]['bytes']
                or _hash(wire) != manifest['streams'][stream]['sha256']
            )
        )
    ):
        _fail()
    if (
        _read_manifest(source, event, agent) != manifest
        or source.authorize_evidence(event, 'dispatch', agent) != before
    ):
        raise SourceUnavailable('permission_denied')
    return value


def read_chunk(source, event, *, agent, manifest, stream, index):
    _event(source, event, agent)
    with read_budget(source.timeout):
        return _read_chunk(source, event, agent, manifest, stream, index)


def read_window(source, event, *, agent):
    """Return at most the first text chunk; parts are explicitly unread."""
    _event(source, event, agent)
    with read_budget(source.timeout):
        before = source.authorize_evidence(event, 'dispatch', agent)
        manifest = _read_manifest(source, event, agent)
        page = _read_chunk(source, event, agent, manifest, 'text', 0)
        if source.authorize_evidence(event, 'dispatch', agent) != before:
            raise SourceUnavailable('permission_denied')
        coverage = {}
        for name, metadata in manifest['streams'].items():
            read = name == 'text'
            coverage[name] = dict(
                complete=read and metadata['chunks'] == 1,
                bytes=page['bytes'] if read else 0,
                chunks=1 if read else 0,
                total_bytes=metadata['bytes'],
                total_chunks=metadata['chunks'],
                sha256=metadata['sha256'],
            )
        return dict(
            reference=manifest['reference'],
            version=manifest['version'],
            parser_version=manifest['parser_version'],
            result_sha256=manifest['result_sha256'],
            manifest_sha256=manifest_sha256(manifest),
            manifest=manifest,
            segments=[{key: page[key] for key in ('stream', 'index', 'content', 'bytes', 'sha256')}],
            coverage=coverage,
            complete=False,
        )


def read_stream(source, event, *, agent, manifest, stream, max_chunks=100, max_bytes=1_000_000):
    """All-or-fail stream read; no partial parts JSON is ever interpreted."""
    _event(source, event, agent)
    _manifest(manifest, event)
    if (
        not isinstance(stream, str)
        or stream not in _LIMITS
        or not _integer(max_chunks, 1, _MAX_CHUNKS)
        or not _integer(max_bytes, 0, _LIMITS.get(stream, 0))
    ):
        _fail()
    metadata = manifest['streams'][stream]
    if metadata['chunks'] > max_chunks or metadata['bytes'] > max_bytes:
        raise SourceUnavailable('document_budget_exceeded')
    with read_budget(source.timeout):
        before = source.authorize_evidence(event, 'dispatch', agent)
        wires = [
            _read_chunk(source, event, agent, manifest, stream, index)['content'].encode()
            for index in range(metadata['chunks'])
        ]
        wire = b''.join(wires)
        if len(wire) != metadata['bytes'] or _hash(wire) != metadata['sha256']:
            _fail()
        if source.authorize_evidence(event, 'dispatch', agent) != before:
            raise SourceUnavailable('permission_denied')
        if stream == 'parts':
            try:
                value = json.loads(wire)
            except (ValueError, UnicodeError):
                _fail()
            if not isinstance(value, list):
                _fail()
            return value
        return wire.decode('utf-8')
