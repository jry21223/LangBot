"""Owner metadata consumption tests do not start or ACK the wx collector."""

from unittest.mock import AsyncMock
import pytest

from langbot.pkg.platform.adapters.agent_wechat.owner_source import OwnerNativeSource
from langbot.pkg.platform.native_contract import NativeAccessDenied
from tests.unit_tests.platform.test_agent_wechat_owner_contract import owner_change
from tests.unit_tests.platform.test_agent_wechat_owner_client import owner_server as _owner_server_fixture

owner_server = _owner_server_fixture


def setup_source():
    change = owner_change().change
    change['original'] = change['original'].replace('owner-fixture:', 'wechat-native:')
    client = AsyncMock()
    client.native_changes.return_value = {
        'source': 'existing-wechat',
        'scope': 'existing-scope',
        'previous_cursor': 'owner-before',
        'next_cursor': 'owner-after',
        'complete_page': True,
        'gaps': [],
        'items': [
            {
                'event_id': 'owner-event-1',
                'change': change,
                'native_version': 'wx-v1',
                'event_cursor': 'raw-wx-cursor',
                'media': [],
                'qualification': 'eligible',
                'dispatch_eligible': True,
            }
        ],
    }
    store = AsyncMock()
    store.checkpoint.return_value = 'owner-before'
    store.source_sequence_checkpoint.return_value = None
    store.accepted_events.return_value = {'items': [], 'next_position': 0, 'has_more': False}
    authorize = AsyncMock()
    worker = OwnerNativeSource(client, store, authorize, initial_cursor='owner-before')
    return worker, client, store, authorize


@pytest.mark.asyncio
async def test_owner_page_retains_canonical_change_and_records_cursor_without_external_ack():
    worker, client, store, authorize = setup_source()
    await worker.initialize()
    await worker.poll_once()
    page = store.accept_page.call_args.args[0]
    assert page.previous_cursor == 'owner-before' and page.next_cursor == 'owner-after'
    assert page.events[0].payload['change'] == client.native_changes.return_value['items'][0]['change']
    assert page.events[0].payload['owner_event_id'] == 'owner-event-1'
    assert page.events[0].payload['event_cursor'] == 'raw-wx-cursor'
    assert page.events[0].version == 'b' * 64
    assert worker.last_sequence == 101
    client.ack.assert_not_called()
    assert authorize.await_count >= 2


@pytest.mark.asyncio
async def test_failed_commit_does_not_advance_local_owner_sequence_or_call_any_ack():
    worker, client, store, _ = setup_source()
    await worker.initialize()
    store.accept_page.side_effect = RuntimeError('db unavailable')
    with pytest.raises(RuntimeError):
        await worker.poll_once()
    assert worker.last_sequence == 0
    client.ack.assert_not_called()


@pytest.mark.asyncio
async def test_no_progress_empty_poll_is_not_persisted_or_acknowledged():
    worker, client, store, _ = setup_source()
    client.native_changes.return_value.update(items=[], next_cursor='owner-before')
    await worker.initialize()
    assert await worker.poll_once() is None
    store.accept_page.assert_not_called()
    client.ack.assert_not_called()


@pytest.mark.asyncio
async def test_metadata_reader_cannot_become_an_oa_text_or_media_reader():
    worker, client, _, _ = setup_source()
    for method, args in [
        ('native_original', ('ref', 'version')),
        ('native_original_chunk', ('ref', 'version', 0)),
        ('native_media', ('ref', 'version', 'asset')),
    ]:
        with pytest.raises(NativeAccessDenied, match='metadata_reader_only'):
            await getattr(worker, method)(*args)
    client.native_original.assert_not_called()
    client.native_media.assert_not_called()


@pytest.mark.asyncio
async def test_typed_unresolved_has_only_owner_evidence_and_no_synthetic_message_ref():
    worker, client, store, _ = setup_source()
    client.native_changes.return_value['items'] = [
        {
            'event_id': 'owner-unresolved',
            'kind': 'unresolved',
            'source_sequence': 101,
            'event_cursor': 'raw-wx-cursor',
            'reason': 'native_unresolved',
            'source': 'existing-wechat',
            'scope': 'existing-scope',
        }
    ]
    await worker.initialize()
    await worker.poll_once()
    page = store.accept_page.call_args.args[0]
    assert page.events == () and len(page.unresolved) == 1
    assert page.unresolved[0].event_id == 'owner-unresolved'
    assert page.unresolved[0].source_sequence == 101
    assert not hasattr(page.unresolved[0], 'record') and not hasattr(page.unresolved[0], 'reference')
    assert worker.last_sequence == 101
    client.ack.assert_not_called()


@pytest.mark.asyncio
async def test_restart_uses_owner_sequence_checkpoint_not_core_position_or_wx_cursor():
    worker, _, store, _ = setup_source()
    store.source_sequence_checkpoint.return_value = 9007199254740993
    await worker.initialize()
    assert worker.last_sequence == 9007199254740993
    store.accepted_events.assert_not_called()


@pytest.mark.asyncio
async def test_connected_callback_requires_a_valid_completed_poll_not_task_start():
    worker, _, store, _ = setup_source()
    calls = []
    worker.on_valid_page = lambda: calls.append('valid-poll')
    await worker.initialize()
    assert calls == []
    store.accept_page.side_effect = RuntimeError('not committed')
    with pytest.raises(RuntimeError):
        await worker.poll_once()
    assert calls == []
    store.accept_page.side_effect = None
    await worker.poll_once()
    assert calls == ['valid-poll']


def test_runtime_state_projection_never_returns_arbitrary_error_text():
    from tests.unit_tests.platform.test_agent_wechat_source import adapter_fixture

    adapter = adapter_fixture()
    assert adapter.native_runtime_state() == {'state': 'needs_authorization'}
    adapter.client = adapter.store = adapter.authorize = object()
    adapter.state = 'error'
    adapter.error_code = 'private-url-or-token-never-return-this'
    assert adapter.native_runtime_state() == {'state': 'error', 'error_code': 'source_unavailable'}


@pytest.mark.asyncio
async def test_runtime_state_is_pure_and_uses_current_collector_proof_not_cached_connected(owner_server):
    from datetime import datetime, timezone
    from tests.unit_tests.platform.test_agent_wechat_source import adapter_fixture
    from langbot.pkg.platform.sources.agent_wechat import AgentWeChatAdapter

    client, _, authority, _ = owner_server
    base = adapter_fixture()
    config = dict(
        base.config, source_id=client.source, scope_id=client.scope, source_flavor='personal-wechat-native-v1'
    )
    config.pop('activation_at')
    config.pop('consumer_id')
    adapter = AgentWeChatAdapter(config, base.logger)
    adapter.attach_native_client(client)
    adapter.attach_native_ingress(object(), object())
    adapter.owner_poll_validated = True
    adapter.state, adapter.error_code = 'unknown', 'upstream_state_unverified'
    collector = {
        'account': 'account-1',
        'authenticated': True,
        'connection': 'online',
        'incoming_transport': 'native_push',
        'contact_sync_ready': True,
        'observed_at': datetime.now(timezone.utc).isoformat(),
    }
    authority['collector'] = collector
    await client.native_authority()
    assert adapter.native_runtime_state() == {'state': 'connected', 'collector_verified': True}
    assert (adapter.state, adapter.error_code) == ('unknown', 'upstream_state_unverified')
    authority['collector'] = dict(
        collector,
        authenticated=False,
        connection='disconnected',
        incoming_transport='disconnected',
        contact_sync_ready=False,
    )
    await client.native_authority()
    assert adapter.native_runtime_state() == {
        'state': 'awaiting_upstream',
        'error_code': 'upstream_disconnected',
        'collector_verified': False,
    }
    assert (adapter.state, adapter.error_code) == ('unknown', 'upstream_state_unverified')


@pytest.mark.asyncio
async def test_metadata_manifest_uses_descriptor_and_rechecks_current_source_access():
    worker, client, _, authorize = setup_source()
    descriptor = {'reference': 'ref', 'version': 'version', 'original_available': False}
    client.native_descriptor.return_value = descriptor
    assert await worker.native_original_manifest('ref', 'version') == descriptor
    client.native_descriptor.assert_awaited_once_with('ref', 'version')
    client.native_descriptor.reset_mock()
    authorize.side_effect = NativeAccessDenied('revoked')
    with pytest.raises(NativeAccessDenied, match='revoked'):
        await worker.native_original_manifest('ref', 'version')
    client.native_descriptor.assert_not_called()
