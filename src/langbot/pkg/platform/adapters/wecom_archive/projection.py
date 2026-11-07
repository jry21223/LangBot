"""Narrow owner changes into the shared native ingress contract, without bodies."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import re

from langbot.pkg.platform.native_contract import SourceBinding, SourceEnvelope, SourcePage
from .reader import SourceUnavailable

_KINDS = {'created', 'baseline', 'baseline_snapshot', 'changed', 'attachments_changed', 'recalled'}
_MESSAGE_FIELDS = {
    'source_seq',
    'message_id',
    'chat_id',
    'chat_type',
    'sender_user_id',
    'sender_identity_type',
    'sender_access_type',
    'message_type',
    'sent_at',
    'action',
    'recalled_message_id',
}
_MEDIA_FIELDS = {'mediaId', 'mediaType', 'sourceMd5', 'contentSha256', 'sourceFileSize', 'contentSize'}


def _text(value, maximum=512):
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or any(ord(c) < 32 for c in value):
        raise ValueError()
    return value


def _reference(value):
    if not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,512}', _text(value)) or '://' in value:
        raise ValueError()
    return value


def _instant(value):
    instant = datetime.fromisoformat(_text(value).replace('Z', '+00:00'))
    if instant.utcoffset() is None:
        raise ValueError()
    return instant


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
        raise ValueError()
    return value


def _materialization(value):
    if not isinstance(value, dict):
        raise ValueError()
    status = value.get('status')
    if status in {'pending', 'recalled'}:
        if set(value) != {'status', 'evidence_fingerprint'}:
            raise ValueError()
        _digest(value['evidence_fingerprint'])
    elif status == 'complete':
        if set(value) != {'status', 'parser_version', 'input_hash', 'result_sha256', 'text_bytes', 'parts'}:
            raise ValueError()
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', _text(value['parser_version'])):
            raise ValueError()
        for field in ('input_hash', 'result_sha256'):
            _digest(value[field])
        if any(
            type(value[field]) is not int or not 0 <= value[field] <= 2147483647 for field in ('text_bytes', 'parts')
        ):
            raise ValueError()
    else:
        raise ValueError()
    return deepcopy(value)


def _message(value):
    if not isinstance(value, dict):
        raise ValueError()
    result = {key: deepcopy(value[key]) for key in _MESSAGE_FIELDS if key in value}
    for key in ('message_id', 'chat_id', 'chat_type', 'sender_user_id', 'sender_identity_type', 'message_type'):
        _text(result[key])
    if 'sender_access_type' in result:
        _text(result['sender_access_type'])
    if type(result['source_seq']) is not int or result['source_seq'] < 0:
        raise ValueError()
    _instant(result['sent_at'])
    if result.get('action', 'send') not in {'send', 'recall'}:
        raise ValueError()
    if result.get('action') == 'recall':
        _text(result['recalled_message_id'])
        if result['message_type'] != 'revoke':
            raise ValueError()
    if 'content_materialization' in value:
        result['content_materialization'] = _materialization(value['content_materialization'])
    if 'media_evidence' in value:
        media = value['media_evidence']
        if not isinstance(media, list) or len(media) > 100 or any(not isinstance(row, dict) for row in media):
            raise ValueError()
        narrowed = []
        for row in media:
            clean = {key: deepcopy(row[key]) for key in _MEDIA_FIELDS if key in row}
            for key, value in clean.items():
                if key in {'sourceFileSize', 'contentSize'}:
                    if type(value) is not int or not 0 <= value <= 9223372036854775807:
                        raise ValueError()
                elif key == 'contentSha256':
                    _digest(value)
                elif key == 'sourceMd5':
                    if not isinstance(value, str) or not re.fullmatch(r'[a-fA-F0-9]{32}', value):
                        raise ValueError()
                else:
                    _text(value)
            narrowed.append(clean)
        result['media_evidence'] = narrowed
    return result


def _attachments(value):
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError()
    result = []
    for row in value:
        if not isinstance(row, dict) or set(row) not in (
            {'reference', 'status'},
            {'reference', 'status', 'type', 'sha256', 'size'},
        ):
            raise ValueError()
        _reference(row['reference'])
        if row['status'] not in {'saved', 'missing', 'withdrawn'}:
            raise ValueError()
        if 'type' in row:
            if row['type'] not in {'file', 'image', 'voice', 'video', 'unknown'}:
                raise ValueError()
            if row['sha256'] is not None:
                _digest(row['sha256'])
            if row['size'] is not None and (type(row['size']) is not int or row['size'] < 0):
                raise ValueError()
            if row['status'] == 'saved' and (row['type'] == 'unknown' or row['sha256'] is None or row['size'] is None):
                raise ValueError()
        result.append(deepcopy(row))
    return result


def _envelope(item, binding):
    if item['source'] != binding.source or item['scope'] != binding.scope or item['kind'] not in _KINDS:
        raise ValueError()
    record, version, original = _text(item['record']), _reference(item['version']), _reference(item['original'])
    if type(item['sequence']) is not int or item['sequence'] < 1 or type(item['original_available']) is not bool:
        raise ValueError()
    for field in ('business_at', 'observed_at'):
        _instant(item[field])
    _digest(item['projection_hash'])
    goal = item.get('goal_reference')
    if goal is not None and (not isinstance(goal, str) or not re.fullmatch(r'GD-[A-Za-z0-9-]{1,60}', goal)):
        raise ValueError()
    saved = item['saved']
    if not isinstance(saved, dict) or type(saved.get('sequence')) is not int:
        raise ValueError()
    if any(saved[key] != item[key] for key in ('version', 'sequence', 'observed_at', 'original')):
        raise ValueError()
    message, attachments = _message(saved['message']), _attachments(saved['attachments'])
    if message['message_id'] != record or _instant(message['sent_at']) != _instant(item['business_at']):
        raise ValueError()
    recalled = item['kind'] == 'recalled'
    if recalled != (message.get('action', 'send') == 'recall'):
        raise ValueError()
    material = message.get('content_materialization', {})
    eligible = (
        item['original_available']
        and item['kind'] not in {'baseline', 'baseline_snapshot', 'recalled'}
        and message['sender_identity_type'] in {'EMPLOYEE', 'CUSTOMER'}
        and all(row['status'] == 'saved' for row in attachments)
        and (message['message_type'] == 'text' or (attachments and material.get('status') == 'complete'))
    )
    payload = {
        key: deepcopy(item[key])
        for key in (
            'sequence',
            'kind',
            'business_at',
            'observed_at',
            'original',
            'projection_hash',
            'original_available',
        )
    }
    payload['goal_reference'] = goal
    payload['saved'] = {key: deepcopy(saved[key]) for key in ('version', 'sequence', 'observed_at', 'original')}
    payload['saved'].update(message=message, attachments=attachments)
    payload.update(source=binding.source, scope=binding.scope, record=record, version=version)
    return SourceEnvelope(
        record, version, original, item['kind'], {'change': payload, 'dispatch_eligible': bool(eligible)}
    )


def project_page(page: dict, binding: SourceBinding) -> SourcePage:
    """Validate the whole owner page before returning anything for admission."""
    try:
        if (
            not isinstance(page, dict)
            or page.get('source') != binding.source
            or page.get('scope') != binding.scope
            or page.get('complete_page') is not True
            or page.get('gaps') != []
            or page.get('coverage') not in {'success', 'success_zero'}
            or not isinstance(page.get('items'), list)
            or len(page['items']) > 100
        ):
            raise ValueError()
        previous = page['previous_cursor']
        if previous is not None:
            _text(previous, 4096)
        next_cursor = _text(page['next_cursor'], 4096)
        events = tuple(_envelope(item, binding) for item in page['items'])
        return SourcePage(previous, next_cursor, events, complete=True)
    except (ValueError, KeyError, TypeError, AttributeError):
        raise SourceUnavailable('invalid_page') from None


def validate_change(change: dict, binding: SourceBinding) -> None:
    """Validate the exact narrowed saved contract before durable admission."""
    try:
        canonical = _envelope(change, binding).payload['change']
        if canonical != change:
            raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError):
        raise SourceUnavailable('invalid_page') from None
