"""Proposed owner interface tests; no source service or credential is installed."""

from unittest.mock import AsyncMock
import pytest

from langbot.pkg.platform.adapters.agent_wechat.projection import digest
from langbot.pkg.platform.adapters.agent_wechat.owner_contract import (
    OwnerContract,
    OwnerChange,
    OwnerPage,
    owner_envelope,
    owner_source_page,
    require_current_original,
    accept_owner_page,
)


def fixture_validator(change):
    from langbot.pkg.platform.adapters.agent_wechat.projection import validate_change

    validate_change(
        change,
        reference_scheme='owner-fixture',
        change_kinds=frozenset({'created', 'baseline', 'changed', 'attachments_changed', 'recalled'}),
        verify_projection_hash=False,
    )


FIXTURE_CONTRACT = OwnerContract('synthetic-owner-not-a-service', fixture_validator)


def owner_change(*, sequence=101, version='b' * 64, state='COMPLETE', wx_cursor='opaque-wx-cursor'):
    change = {
        'source': 'existing-wechat',
        'scope': 'existing-scope',
        'record': 'a' * 64,
        'version': version,
        'sequence': sequence,
        'kind': 'attachments_changed',
        'business_at': '2026-11-08T00:00:00Z',
        'observed_at': '2026-11-08T00:01:00Z',
        'original': 'owner-fixture:message/' + 'a' * 64 + '/' + version,
        'goal_reference': None,
        'original_available': state == 'COMPLETE',
        'saved': {
            'account_id': 'account-1',
            'thread_id': 'group-1',
            'message_id': 'msg-1',
            'sender_id': 'sender-1',
            'server_id': '9007199254740993123',
            'direction': 'incoming',
            'message_type': 'image',
            'body_available': False,
        },
    }
    change['projection_hash'] = digest({key: value for key, value in change.items() if key not in {'source', 'scope'}})
    return OwnerChange(
        'synthetic-owner-not-a-service',
        change,
        wx_cursor,
        state,
        'c' * 64,
        'existing-parser',
        'parser-v1',
        'd' * 64 if state == 'COMPLETE' else None,
        dispatch_eligible=state == 'COMPLETE',
    )


def envelope(item):
    return owner_envelope(
        item,
        source='existing-wechat',
        scope='existing-scope',
        account='account-1',
        thread='group-1',
        contract=FIXTURE_CONTRACT,
    )


def test_owner_identity_version_reference_and_sequence_are_preserved_not_rehashed():
    item = owner_change()
    output = envelope(item)
    assert output.record == item.change['record']
    assert output.version == item.change['version']
    assert output.reference == item.change['original']
    assert output.payload['change'] == item.change
    assert output.payload['change']['sequence'] == 101
    assert output.payload['event_cursor'] == 'opaque-wx-cursor'
    assert output.payload['dispatch_eligible'] is True
    assert 'input_sha256' not in output.payload  # The owner remains the canonical artifact store.
    item.change['saved']['thread_id'] = 'mutated-after-admission'
    assert output.payload['change']['saved']['thread_id'] == 'group-1'


def test_derived_completion_and_reparse_must_arrive_with_new_owner_version():
    pending = owner_change(sequence=101, state='PENDING')
    complete = owner_change(sequence=102, version='e' * 64)
    reparsed = owner_change(sequence=103, version='f' * 64)
    assert not envelope(pending).payload['dispatch_eligible']
    assert [envelope(c).version for c in [pending, complete, reparsed]] == ['b' * 64, 'e' * 64, 'f' * 64]
    for item in [complete, reparsed]:
        require_current_original(
            item, reference=item.change['original'], version=item.change['version'], contract=FIXTURE_CONTRACT
        )
    with pytest.raises(ValueError, match='not_complete'):
        require_current_original(
            pending, reference=pending.change['original'], version=pending.change['version'], contract=FIXTURE_CONTRACT
        )
    with pytest.raises(ValueError, match='version'):
        require_current_original(
            reparsed,
            reference=complete.change['original'],
            version=complete.change['version'],
            contract=FIXTURE_CONTRACT,
        )


def test_source_sequence_is_independent_monotonic_and_not_derived_from_wx_cursor():
    first, second = owner_change(sequence=101), owner_change(sequence=102, version='e' * 64)
    page = OwnerPage('owner-cursor-before', 'owner-cursor-after', (first, second), complete=True)
    admitted = owner_source_page(
        page,
        previous_sequence=100,
        source='existing-wechat',
        scope='existing-scope',
        account='account-1',
        thread='group-1',
        contract=FIXTURE_CONTRACT,
    )
    assert admitted.previous_cursor == 'owner-cursor-before'
    assert admitted.next_cursor == 'owner-cursor-after'
    assert [event.payload['change']['sequence'] for event in admitted.events] == [101, 102]
    for changes in [(second, first), (first, first)]:
        with pytest.raises(ValueError, match='sequence'):
            owner_source_page(
                OwnerPage('before', 'after', changes, True),
                previous_sequence=100,
                source='existing-wechat',
                scope='existing-scope',
                account='account-1',
                thread='group-1',
                contract=FIXTURE_CONTRACT,
            )


def test_unknown_contract_incomplete_page_and_wrong_scope_fail_closed():
    from dataclasses import replace

    for item in [replace(owner_change(), contract='unknown-v2'), replace(owner_change(), materialization='UNKNOWN')]:
        with pytest.raises(ValueError):
            envelope(item)
    for field, value in [('scope', 'other'), ('source', 'other')]:
        item = owner_change()
        item.change[field] = value
        with pytest.raises(ValueError):
            envelope(item)
    item = owner_change()
    item.change['saved']['thread_id'] = 'other'
    with pytest.raises(ValueError):
        envelope(item)
    with pytest.raises(ValueError, match='complete'):
        owner_source_page(
            OwnerPage('before', 'after', (owner_change(),), False),
            previous_sequence=100,
            source='existing-wechat',
            scope='existing-scope',
            account='account-1',
            thread='group-1',
            contract=FIXTURE_CONTRACT,
        )


@pytest.mark.asyncio
async def test_owner_consumption_has_only_core_checkpoint_and_no_external_ack():
    store, authorize = AsyncMock(), AsyncMock()
    item = owner_change()
    page = OwnerPage('before', 'after', (item,), True)
    store.accept_page.side_effect = RuntimeError('db not committed')
    with pytest.raises(RuntimeError):
        await accept_owner_page(
            store,
            page,
            authorize=authorize,
            previous_sequence=100,
            source='existing-wechat',
            scope='existing-scope',
            account='account-1',
            thread='group-1',
            contract=FIXTURE_CONTRACT,
        )
    store.accept_page.side_effect = None
    await accept_owner_page(
        store,
        page,
        authorize=authorize,
        previous_sequence=100,
        source='existing-wechat',
        scope='existing-scope',
        account='account-1',
        thread='group-1',
        contract=FIXTURE_CONTRACT,
    )
    assert store.accept_page.await_count == 2


def test_complete_or_reparsed_result_cannot_reuse_pending_owner_version_in_one_page():
    pending = owner_change(sequence=101, state='PENDING')
    complete = owner_change(sequence=102, state='COMPLETE')  # Intentionally invalid owner version reuse.
    with pytest.raises(ValueError, match='version_reused'):
        owner_source_page(
            OwnerPage('before', 'after', (pending, complete), True),
            previous_sequence=100,
            source='existing-wechat',
            scope='existing-scope',
            account='account-1',
            thread='group-1',
            contract=FIXTURE_CONTRACT,
        )


def test_legacy_flavor_preserves_old_text_ref_but_does_not_register_native_media_contract():
    from dataclasses import replace
    from langbot.pkg.platform.adapters.agent_wechat.owner_contract import LEGACY_TEXT

    item = owner_change()
    with pytest.raises(ValueError, match='contract_version_unknown'):
        owner_envelope(item, source='existing-wechat', scope='existing-scope', account='account-1', thread='group-1')
    change = item.change.copy()
    change['saved'] = dict(change['saved'], message_type='text', body_available=True)
    change['kind'] = 'created'
    change['original'] = 'wechat:message/' + change['record'] + '/' + change['version']
    change['projection_hash'] = 'f' * 64  # Owner provenance is preserved, not rewritten as a transport hash.
    legacy = replace(item, contract=LEGACY_TEXT.name, change=change)
    result = owner_envelope(
        legacy, source='existing-wechat', scope='existing-scope', account='account-1', thread='group-1'
    )
    assert result.reference == change['original'] and result.payload['change']['projection_hash'] == 'f' * 64
    bad = replace(legacy, change=dict(change, saved=dict(change['saved'], message_type='image')))
    with pytest.raises(ValueError, match='legacy_text_flavor_required'):
        owner_envelope(bad, source='existing-wechat', scope='existing-scope', account='account-1', thread='group-1')
