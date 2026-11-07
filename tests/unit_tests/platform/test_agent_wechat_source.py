"""Native lifecycle contracts use synthetic sources, never a logged-in account."""

from unittest.mock import AsyncMock
import pytest

from langbot.pkg.platform.adapters.agent_wechat.source import WxNativeSource
from langbot.pkg.platform.adapters.agent_wechat.projection import project_message
from langbot.pkg.platform.adapters.agent_wechat.client import WxProtocolError
from tests.unit_tests.platform.test_agent_wechat_projection import message


def source():
    client, store, authorize = AsyncMock(), AsyncMock(), AsyncMock()
    client.message.return_value = message()
    client.media.return_value = {'items': []}
    store.checkpoint.return_value = '9'
    store.accepted_events.return_value = {'items': [], 'next_position': 0, 'has_more': False}
    store.accept_page.return_value.cursor = '10'
    worker = WxNativeSource(
        client,
        store,
        authorize,
        account='account-1',
        threads={'group-1'},
        activation_at='2026-11-08T00:00:00Z',
        initial_cursor='9',
        consumer='consumer',
        source='fixture',
        scope='scope',
    )
    return worker, client, store, authorize


def event():
    return {
        'cursor': '10',
        'kind': 'message.committed',
        'createdAt': '2026-11-08T00:00:01Z',
        'data': {'threadId': 'group-1', 'messageId': 'msg-1', 'direction': 'incoming', 'origin': 'live'},
    }


@pytest.mark.asyncio
async def test_ack_requires_committed_receipt_and_authority():
    worker, client, store, authorize = source()
    store.accept_page.side_effect = RuntimeError('database unavailable')
    with pytest.raises(RuntimeError):
        await worker.accept_event(event())
    client.ack.assert_not_called()
    store.accept_page.side_effect = None
    await worker.accept_event(event())
    client.ack.assert_awaited_once_with('consumer', '10')
    assert authorize.await_count >= 4


@pytest.mark.asyncio
async def test_lost_ack_retries_same_cursor_without_duplicate_original():
    worker, client, store, _ = source()
    client.ack.side_effect = WxProtocolError('transport')
    with pytest.raises(WxProtocolError):
        await worker.accept_event(event())
    store.checkpoint.return_value = '10'
    client.ack.side_effect = None
    await worker.accept_event(event())
    assert store.accept_page.await_count == 1
    assert client.message.await_count == 1


@pytest.mark.asyncio
async def test_ambiguous_recall_and_unknown_asset_quarantine_without_reading():
    worker, client, _, _ = source()
    for kind, data in [
        ('message.recalled', {'threadId': 'group-1', 'serverId': 'missing'}),
        ('media.ready', {'assetId': 'other-scope'}),
    ]:
        await worker.accept_event(dict(event(), kind=kind, data=data))
    client.message.assert_not_called()
    client.asset.assert_not_called()
    assert client.ack.await_count == 2
    await worker.accept_event(event())
    assert client.message.await_count == 1


@pytest.mark.asyncio
async def test_restart_rebuilds_exact_indexes_and_original_is_checked_fresh():
    worker, client, store, _ = source()
    envelope = project_message(
        message(),
        cursor='10',
        origin='live',
        account='account-1',
        allowed_threads={'group-1'},
        activation_at='2026-11-08T00:00:00Z',
        source='fixture',
        scope='scope',
    )
    store.accepted_events.return_value['items'] = [{'envelope': envelope}]
    await worker.initialize()
    result = await worker.native_original(envelope.reference, envelope.version)
    assert result['text'] == 'private fixture text'
    client.message.return_value = message(recall={'observedAt': '2026-11-08T00:00:05Z'})
    with pytest.raises(WxProtocolError, match='changed_or_recalled'):
        await worker.native_original(envelope.reference, envelope.version)


def test_utf8_chunks_reassemble_without_split_character():
    text = '中文' * 20000
    parts = WxNativeSource._parts(text)
    assert ''.join(parts) == text
    assert all(len(part.encode()) <= 16384 for part in parts)


def test_native_adapter_can_be_loaded_without_secret_and_sends_fail_closed():
    from langbot.pkg.platform.sources.agent_wechat import AgentWeChatAdapter
    from langbot_plugin.api.definition.abstract.platform.event_logger import AbstractEventLogger

    adapter = AgentWeChatAdapter(
        {
            'account_id': 'account-1',
            'allowed_threads': ['group-1'],
            'source_id': 'fixture',
            'scope_id': 'scope',
            'credential_ref': 'existing',
            'consumer_id': 'consumer',
            'initial_cursor': '9',
            'activation_at': '2026-11-08T00:00:00Z',
        },
        AsyncMock(spec=AbstractEventLogger),
    )
    assert adapter.native_source_binding().read_only
    with pytest.raises(WxProtocolError, match='trusted_source_binding'):
        adapter._worker()


@pytest.mark.asyncio
async def test_read_revocation_between_original_checks_returns_nothing():
    worker, client, store, authorize = source()
    await worker.accept_event(event())
    envelope = store.accept_page.call_args.args[0].events[0]
    authorize.side_effect = [None, None, PermissionError('revoked')]
    with pytest.raises(PermissionError):
        await worker.native_original(envelope.reference, envelope.version)


@pytest.mark.asyncio
async def test_raw_send_cannot_use_read_grant_as_send_grant():
    from langbot.pkg.platform.adapters.agent_wechat.sender import WxSendProvider

    worker, client, _, _ = source()
    provider = WxSendProvider(client, {'group-1'})
    with pytest.raises(WxProtocolError, match='scope'):
        await provider.send('host-operation', 'group', 'unapproved', {'text': 'synthetic'})
    client.send_text.assert_not_called()


def test_native_manifest_is_discoverable_by_existing_core_ui_contract():
    from langbot.pkg.discover.engine import ComponentDiscoveryEngine

    discovery = ComponentDiscoveryEngine(None)
    component = discovery.load_component_manifest('pkg/platform/sources/agent_wechat.yaml')
    assert component.kind == 'MessagePlatformAdapter'
    assert component.get_python_component_class().__name__ == 'AgentWeChatAdapter'
    info = component.to_plain_dict()
    assert info['label']['zh_Hans'] == '个人微信'
    assert info['label']['en_US'] == 'Personal WeChat'
    fields = {field['name']: field for field in info['spec']['config']}
    assert fields['allowed_threads']['type'] == 'array[string]'
    assert 'token' not in fields


@pytest.mark.asyncio
async def test_trusted_factory_can_check_connection_before_ingress_attachment():
    from langbot.pkg.platform.sources.agent_wechat import AgentWeChatAdapter
    from langbot_plugin.api.definition.abstract.platform.event_logger import AbstractEventLogger

    adapter = AgentWeChatAdapter(
        {
            'account_id': 'account-1',
            'allowed_threads': ['group-1'],
            'source_id': 'fixture',
            'scope_id': 'scope',
            'credential_ref': 'existing',
            'consumer_id': 'consumer',
            'initial_cursor': '9',
            'activation_at': '2026-11-08T00:00:00Z',
        },
        AsyncMock(spec=AbstractEventLogger),
    )
    client = AsyncMock()
    client.account_id = 'account-1'
    client.status.return_value = {
        'id': 'account-1',
        'authenticated': True,
        'contactSyncReady': True,
        'connection': 'online',
        'incomingTransport': 'native_push',
    }
    adapter.attach_native_client(client)
    assert adapter.store is None
    facts = await adapter.native_connection_facts()
    assert facts['revision'] == adapter.native_source_binding().revision
    assert facts['account'] == 'account-1'


@pytest.mark.asyncio
async def test_connection_facts_never_invent_missing_account_or_online_state():
    from langbot.pkg.platform.adapters.agent_wechat.source import connection_facts

    client = AsyncMock()
    for state in [
        {'authenticated': True, 'contactSyncReady': True},
        {
            'accountId': 'account-1',
            'authenticated': True,
            'contactSyncReady': True,
            'connection': 'disconnected',
            'incomingTransport': 'disconnected',
        },
    ]:
        client.status.return_value = state
        with pytest.raises(WxProtocolError):
            await connection_facts(client, 'account-1', 'fixture', 'scope')


def adapter_fixture():
    from langbot.pkg.platform.sources.agent_wechat import AgentWeChatAdapter
    from langbot_plugin.api.definition.abstract.platform.event_logger import AbstractEventLogger

    return AgentWeChatAdapter(
        {
            'account_id': 'account-1',
            'allowed_threads': ['group-1'],
            'source_id': 'fixture',
            'scope_id': 'scope',
            'credential_ref': 'existing',
            'consumer_id': 'consumer',
            'initial_cursor': '9',
            'activation_at': '2026-11-08T00:00:00Z',
        },
        AsyncMock(spec=AbstractEventLogger),
    )


def test_sdk_two_argument_unregistration_removes_only_matching_callback():
    import inspect
    from langbot_plugin.api.definition.abstract.platform.adapter import AbstractMessagePlatformAdapter

    adapter = adapter_fixture()
    first, second = lambda *_: None, lambda *_: None
    adapter.register_listener('event', first)
    adapter.register_listener('event', second)
    inspect.signature(AbstractMessagePlatformAdapter.unregister_listener).bind(adapter, 'event', first)
    adapter.unregister_listener('event', first)
    assert adapter.listeners['event'] == [second]
    adapter.unregister_listener('event', first)
    assert adapter.listeners['event'] == [second]
    adapter.unregister_listener('event', second)
    assert 'event' not in adapter.listeners


@pytest.mark.asyncio
async def test_run_stays_owned_by_calling_task_and_kill_joins_it():
    import asyncio

    adapter = adapter_fixture()
    entered, stopped = asyncio.Event(), asyncio.Event()

    class Worker:
        async def initialize(self):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    adapter.worker = Worker()
    adapter.client = AsyncMock()
    caller = asyncio.create_task(adapter.run_async())
    await asyncio.wait_for(entered.wait(), 1)
    assert not caller.done()
    assert adapter.task is caller
    await asyncio.wait_for(adapter.kill(), 1)
    assert caller.done() and stopped.is_set()
    adapter.client.close.assert_awaited()
    assert adapter.state == 'disconnected'


@pytest.mark.asyncio
async def test_fatal_error_reaches_runtime_task_owner_and_closes_client():
    adapter = adapter_fixture()
    adapter.worker = AsyncMock()
    adapter.worker.initialize.side_effect = WxProtocolError('event_cursor_expired')
    adapter.client = AsyncMock()
    with pytest.raises(WxProtocolError, match='event_cursor_expired'):
        await adapter.run_async()
    assert adapter.error_code == 'event_cursor_expired'
    adapter.client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_source_revocation_is_not_swallowed_by_reconnect_loop():
    adapter = adapter_fixture()
    adapter.worker = AsyncMock()
    adapter.worker.initialize.side_effect = PermissionError('source revoked')
    adapter.client = AsyncMock()
    with pytest.raises(PermissionError):
        await adapter.run_async()
    adapter.client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_media_manifest_exposes_only_exact_fresh_asset_metadata_for_existing_parser():
    worker, client, store, _ = source()
    client.message.return_value = message(type=3, body=None)
    client.media.return_value = {
        'items': [
            {
                'id': 'asset-1',
                'accountId': 'account-1',
                'messageId': 'msg-1',
                'kind': 'image',
                'state': 'available',
                'sha256': 'a' * 64,
                'byteLength': 4,
                'contentType': 'image/png',
                'contentUrl': 'https://not-returned.invalid',
                'filename': 'not-returned',
            }
        ]
    }
    await worker.accept_event(event())
    envelope = store.accept_page.call_args.args[0].events[0]
    manifest = await worker.native_original_manifest(envelope.reference, envelope.version)
    assert manifest['kind'] == 'media'
    assert manifest['assets'] == [
        {
            'asset_id': 'asset-1',
            'kind': 'image',
            'state': 'available',
            'sha256': 'a' * 64,
            'byte_length': 4,
            'content_type': 'image/png',
        }
    ]
    assert 'filename' not in str(manifest) and 'not-returned' not in str(manifest)
    client.message.return_value = message(type=3, body=None, recall={'observedAt': 'now'})
    with pytest.raises(WxProtocolError):
        await worker.native_original_manifest(envelope.reference, envelope.version)
