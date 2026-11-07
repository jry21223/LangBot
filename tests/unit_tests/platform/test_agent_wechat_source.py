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
