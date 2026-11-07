"""Project wx originals to durable metadata; raw content stays in the source."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import re

from langbot.pkg.platform.native_contract import SourceEnvelope


def digest(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def identity(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 1024 or any(ord(c) < 32 for c in value):
        raise ValueError('invalid_native_identity')
    return value


def timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError('invalid_source_timestamp')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('invalid_source_timestamp') from exc
    if parsed.tzinfo is None:
        raise ValueError('timezone_required')
    return parsed.astimezone(timezone.utc)


def event_sequence(cursor: object) -> int:
    """wx v1.4 stores journal positions as positive decimal signed i64 values."""
    if (
        not isinstance(cursor, str)
        or not cursor.isascii()
        or not cursor.isdecimal()
        or len(cursor) > 19
        or not 1 <= int(cursor) <= 9223372036854775807
        or str(int(cursor)) != cursor
    ):
        raise ValueError('invalid_event_cursor')
    return int(cursor)


def project_message(
    message: dict,
    *,
    cursor: str,
    origin: str,
    account: str,
    allowed_threads: set[str],
    activation_at: str,
    source: str,
    scope: str,
    assets: list[dict] | None = None,
    observed_at: str | None = None,
) -> SourceEnvelope | None:
    """Preserve the durable baseline while allowing post-activation reconnect catch-up."""
    if message.get('accountId') != account:
        raise ValueError('message_account_mismatch')
    thread = identity(message.get('threadId'))
    if thread not in allowed_threads or message.get('direction') != 'incoming':
        return None
    message_id = identity(message.get('id'))
    native_version = identity(message.get('version'))
    server_id = message.get('serverId')
    if server_id is not None:
        server_id = identity(server_id)
    sender = message.get('senderId')
    if sender is not None:
        sender = identity(sender)
    start = timestamp(activation_at)
    created_at = message.get('createdAt')
    try:
        after_activation = timestamp(created_at) >= start
    except ValueError:
        after_activation = False
    projected_assets = []
    for asset in assets if assets is not None else message.get('media', []):
        if not isinstance(asset, dict) or asset.get('accountId') != account or asset.get('messageId') != message_id:
            raise ValueError('asset_binding_mismatch')
        asset_id = identity(asset.get('id'))
        kind = asset.get('kind')
        if not isinstance(kind, str) or kind not in {'image', 'file', 'voice', 'video', 'sticker'}:
            raise ValueError('invalid_media_kind')
        state = asset.get('state')
        if not isinstance(state, str) or state not in {
            'pending',
            'available',
            'expired',
            'unsupported',
            'failed',
            'cancelled',
            'needs_review',
        }:
            raise ValueError('invalid_media_state')
        content_hash = asset.get('sha256')
        byte_length = asset.get('byteLength')
        content_type = asset.get('contentType')
        if content_hash is not None and (
            not isinstance(content_hash, str) or re.fullmatch('[0-9a-f]{64}', content_hash) is None
        ):
            raise ValueError('invalid_media_hash')
        if byte_length is not None and (type(byte_length) is not int or not 0 <= byte_length <= 9223372036854775807):
            raise ValueError('invalid_media_length')
        if content_type is not None and (
            not isinstance(content_type, str)
            or len(content_type) > 255
            or re.fullmatch(r'[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+', content_type) is None
        ):
            raise ValueError('invalid_media_content_type')
        if state == 'available' and (content_hash is None or byte_length is None or content_type is None):
            raise ValueError('available_media_integrity_required')
        projected_assets.append(
            {
                'asset_id': asset_id,
                'kind': kind,
                'state': state,
                'sha256': content_hash,
                'byte_length': byte_length,
                'content_type': content_type,
            }
        )
    projected_assets.sort(key=lambda item: item['asset_id'])
    recalled = message.get('recall') is not None
    msg_type = message.get('type')
    if type(msg_type) is not int or not 0 <= msg_type <= 2147483647:
        raise ValueError('invalid_message_type')
    body = message.get('body')
    is_text = msg_type == 1 and isinstance(body, str) and bool(body)
    is_media = msg_type in {3, 34, 43, 47, 49} and bool(projected_assets)
    media_ready = is_media and all(item['state'] == 'available' for item in projected_assets)
    origin_verified = origin in {'live', 'catchup'}
    eligible = bool(
        sender and server_id and origin_verified and after_activation and not recalled and (is_text or media_ready)
    )
    body_hash = sha256(body.encode()).hexdigest() if isinstance(body, str) and not recalled else None
    record = digest([account, thread, message_id])
    version = digest(
        {'native_version': native_version, 'body_hash': body_hash, 'recalled': recalled, 'media': projected_assets}
    )
    reference = 'wechat-native:message/' + record + '/' + version
    kind = 'recalled' if recalled else ('created' if origin_verified and after_activation else 'baseline')
    payload = {
        'native': {
            'account_id': account,
            'thread_id': thread,
            'message_id': message_id,
            'server_id': server_id,
            'sender_id': sender,
        },
        'native_version': native_version,
        'event_cursor': identity(cursor),
        'business_at': created_at,
        'origin': origin if origin in {'live', 'catchup', 'local_send', 'profile_import'} else 'unknown',
        'direction': 'incoming',
        'message_type': msg_type,
        'body_sha256': body_hash,
        'media': projected_assets,
        'recalled': recalled,
        'dispatch_eligible': eligible,
        'qualification': ('recalled' if recalled else 'eligible' if eligible else 'baseline_or_incomplete'),
    }
    message_type = {1: 'text', 3: 'image', 34: 'voice', 43: 'video', 47: 'sticker', 49: 'file'}.get(msg_type, 'unknown')
    observed = observed_at or created_at
    timestamp(observed)
    sequence = event_sequence(cursor)
    change = {
        'source': identity(source),
        'scope': identity(scope),
        'record': record,
        'version': version,
        'sequence': sequence,
        'kind': kind,
        'business_at': created_at or observed,
        'observed_at': observed,
        'original': reference,
        'goal_reference': None,
        'original_available': not recalled and bool(is_text or media_ready),
        'saved': {
            'account_id': account,
            'thread_id': thread,
            'message_id': message_id,
            'sender_id': sender,
            'server_id': server_id,
            'direction': 'incoming',
            'message_type': message_type,
            'body_available': bool(is_text and not recalled),
        },
    }
    change['projection_hash'] = digest(change)
    payload['change'] = change
    return SourceEnvelope(record, version, reference, kind, payload)


def validate_change(change: dict) -> None:
    """Validate the native saved contract before durable acceptance and source ACK."""
    required = {
        'source',
        'scope',
        'record',
        'version',
        'sequence',
        'kind',
        'business_at',
        'observed_at',
        'original',
        'goal_reference',
        'original_available',
        'saved',
        'projection_hash',
    }
    saved_keys = {
        'account_id',
        'thread_id',
        'message_id',
        'sender_id',
        'server_id',
        'direction',
        'message_type',
        'body_available',
    }
    if not isinstance(change, dict) or set(change) != required:
        raise ValueError('invalid_native_change_keys')
    for key in ('source', 'scope', 'record', 'version', 'original', 'projection_hash'):
        identity(change[key])
    for key in ('record', 'version', 'projection_hash'):
        if len(change[key]) != 64 or any(c not in '0123456789abcdef' for c in change[key]):
            raise ValueError('invalid_native_change_hash')
    if change['original'] != 'wechat-native:message/' + change['record'] + '/' + change['version']:
        raise ValueError('invalid_native_original_binding')
    if type(change['sequence']) is not int:
        raise ValueError('invalid_native_sequence')
    event_sequence(str(change['sequence']))
    if change['kind'] not in {'created', 'baseline', 'recalled'} or change['goal_reference'] is not None:
        raise ValueError('invalid_native_change_kind')
    timestamp(change['business_at'])
    timestamp(change['observed_at'])
    if type(change['original_available']) is not bool:
        raise ValueError('invalid_native_availability')
    saved = change['saved']
    if not isinstance(saved, dict) or set(saved) != saved_keys:
        raise ValueError('invalid_native_saved_keys')
    for key in ('account_id', 'thread_id', 'message_id'):
        identity(saved[key])
    for key in ('sender_id', 'server_id'):
        if saved[key] is not None:
            identity(saved[key])
    if saved['direction'] != 'incoming' or saved['message_type'] not in {
        'text',
        'image',
        'voice',
        'file',
        'video',
        'sticker',
        'unknown',
    }:
        raise ValueError('invalid_native_saved_type')
    if type(saved['body_available']) is not bool:
        raise ValueError('invalid_native_body_availability')
    if change['kind'] == 'recalled' and (saved['body_available'] or change['original_available']):
        raise ValueError('recalled_original_available')
    if digest({key: value for key, value in change.items() if key != 'projection_hash'}) != change['projection_hash']:
        raise ValueError('native_projection_hash_mismatch')
