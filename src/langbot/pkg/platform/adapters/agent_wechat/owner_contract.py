"""Pure proposed owner-canonical boundary, not an installed source client.

The existing source owner retains the account credential, original Journal,
materialization artifacts and OA reader audience. LangBot receives metadata
only. Facts in these records do not grant access: the runtime still needs its
separately approved current authorization and metadata-only source client.
Network paths and the final wire schema remain owned by the existing service.
"""

from copy import deepcopy
from dataclasses import dataclass
from typing import Callable, Protocol
import re

from langbot.pkg.platform.native_contract import SourceEnvelope, SourcePage
from .projection import identity, validate_change


@dataclass(frozen=True)
class OwnerContract:
    name: str
    validate: Callable[[dict], None]


def _legacy_text(change):
    validate_change(
        change, reference_scheme='wechat', change_kinds=frozenset({'created', 'baseline'}), verify_projection_hash=False
    )
    if change['saved']['message_type'] != 'text':
        raise ValueError('legacy_text_flavor_required')


LEGACY_TEXT = OwnerContract('personal-wechat-text-v1', _legacy_text)


def _native_v1(change):
    validate_change(
        change,
        reference_scheme='wechat-native',
        change_kinds=frozenset({'created', 'baseline', 'changed', 'attachments_changed', 'recalled'}),
        verify_projection_hash=False,
    )


NATIVE_V1 = OwnerContract('personal-wechat-native-v1', _native_v1)
# Flavor selection is explicit; registering native v1 does not migrate legacy history.


@dataclass(frozen=True)
class OwnerChange:
    contract: str
    change: dict
    wx_cursor: str
    materialization: str
    input_sha256: str | None
    parser_id: str | None
    parser_version: str | None
    result_sha256: str | None
    dispatch_eligible: bool = False


@dataclass(frozen=True)
class OwnerPage:
    previous_cursor: str
    next_cursor: str
    changes: tuple[OwnerChange, ...]
    complete: bool


class OwnerMetadataClient(Protocol):
    """Proposed logical operations, deliberately no raw wx message or byte API."""

    async def current_metadata(self, reference: str, version: str) -> OwnerChange: ...

    async def read_metadata_page(self, cursor: str, limit: int) -> OwnerPage: ...

    async def acknowledge_metadata(self, page: OwnerPage, durable_receipt) -> None: ...


def _checked(item, contract):
    if not isinstance(contract, OwnerContract) or not isinstance(item, OwnerChange) or item.contract != contract.name:
        raise ValueError('owner_contract_version_unknown')
    if not isinstance(item.materialization, str) or item.materialization not in {
        'PENDING',
        'COMPLETE',
        'FAILED',
        'RECALLED',
    }:
        raise ValueError('owner_materialization_unknown')
    if type(item.dispatch_eligible) is not bool:
        raise ValueError('owner_eligibility_invalid')
    identity(item.wx_cursor)  # Raw recovery token; never converted to the source sequence.
    contract.validate(item.change)
    for value in (item.input_sha256, item.result_sha256):
        if value is not None and (not isinstance(value, str) or re.fullmatch('[0-9a-f]{64}', value) is None):
            raise ValueError('owner_materialization_hash_invalid')
    for value in (item.parser_id, item.parser_version):
        if value is not None and (not isinstance(value, str) or len(value) > 128):
            raise ValueError('owner_parser_pin_invalid')
        if value is not None:
            identity(value)
    if item.materialization == 'COMPLETE' and any(
        value is None for value in (item.input_sha256, item.parser_id, item.parser_version, item.result_sha256)
    ):
        raise ValueError('owner_complete_proof_required')
    if item.materialization != 'COMPLETE' and item.change['original_available']:
        raise ValueError('owner_incomplete_original_available')
    if item.materialization == 'RECALLED' and item.change['kind'] != 'recalled':
        raise ValueError('owner_recall_binding_invalid')


def owner_envelope(item, *, source, scope, account, thread, contract=LEGACY_TEXT):
    _checked(item, contract)
    change = item.change
    saved = change['saved']
    if (
        change['source'] != source
        or change['scope'] != scope
        or saved['account_id'] != account
        or saved['thread_id'] != thread
    ):
        raise ValueError('owner_source_binding_mismatch')
    eligible = (
        item.dispatch_eligible
        and item.materialization == 'COMPLETE'
        and change['kind'] not in {'baseline', 'recalled'}
        and change['original_available']
        and saved['sender_id'] is not None
        and saved['server_id'] is not None
    )
    # Owner version binds materialization input/parser/result. Rehashing it here
    # would create a second identity and replay previously consumed history.
    return SourceEnvelope(
        change['record'],
        change['version'],
        change['original'],
        change['kind'],
        {'change': deepcopy(change), 'event_cursor': item.wx_cursor, 'dispatch_eligible': bool(eligible)},
    )


def owner_source_page(page, *, previous_sequence, **binding):
    if (
        not isinstance(page, OwnerPage)
        or page.complete is not True
        or not isinstance(page.changes, tuple)
        or len(page.changes) > 100
    ):
        raise ValueError('owner_complete_page_required')
    identity(page.previous_cursor)
    identity(page.next_cursor)
    if type(previous_sequence) is not int or previous_sequence < 0:
        raise ValueError('owner_sequence_invalid')
    events, versions = [], set()
    for item in page.changes:
        envelope = owner_envelope(item, **binding)
        sequence = item.change['sequence']
        if sequence <= previous_sequence:
            raise ValueError('owner_sequence_not_monotonic')
        key = (envelope.record, envelope.version)
        if key in versions:
            raise ValueError('owner_source_version_reused')
        versions.add(key)
        previous_sequence = sequence
        events.append(envelope)
    return SourcePage(page.previous_cursor, page.next_cursor, tuple(events))


def require_current_original(item, *, reference, version, contract=LEGACY_TEXT):
    """Pure consistency check only; OA authority remains in the original reader."""
    _checked(item, contract)
    if item.change['original'] != reference or item.change['version'] != version:
        raise ValueError('owner_current_version_mismatch')
    if item.materialization != 'COMPLETE' or not item.change['original_available'] or item.change['kind'] == 'recalled':
        raise ValueError('owner_original_not_complete')


async def accept_owner_page(store, page, *, authorize, acknowledge, previous_sequence, **binding):
    source_page = owner_source_page(page, previous_sequence=previous_sequence, **binding)
    receipt = await store.accept_page(source_page, authorize=authorize)
    await authorize()
    await acknowledge(page, receipt)
    return receipt
