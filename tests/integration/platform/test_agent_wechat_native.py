"""Actual SQLite acceptance and source restart in the isolated LangBot runtime."""

from unittest.mock import AsyncMock
import pytest

from tests.unit_tests.platform.test_agent_wechat_projection import message
from tests.unit_tests.platform.test_agent_wechat_source import event
from langbot.pkg.platform.native_contract import SourceBinding
from langbot.pkg.platform.native_ingress import NativeIngressStore
from langbot.pkg.platform.adapters.agent_wechat.source import WxNativeSource
from langbot.pkg.platform.adapters.agent_wechat.client import WxProtocolError


pytest_plugins = ['tests.integration.platform.test_native_ingress_contracts']


async def allowed():
    pass


def setup(manager):
    store = NativeIngressStore(
        manager,
        workspace_uuid='workspace-a',
        bot_uuid='bot-workspace-a',
        binding=SourceBinding('fixture', 'account-1', 'scope', 'v1'),
    )
    client = AsyncMock()
    client.message.return_value = message()
    client.media.return_value = {'items': []}
    source = WxNativeSource(
        client,
        store,
        allowed,
        account='account-1',
        threads={'group-1'},
        activation_at='2026-11-08T00:00:00Z',
        initial_cursor='9',
        consumer='consumer',
        source='fixture',
        scope='scope',
    )
    return source, client, store


@pytest.mark.asyncio
async def test_sqlite_acceptance_ack_loss_restart_and_exact_handoff(manager):
    source, client, store = setup(manager)
    await source.initialize()
    client.ack.side_effect = WxProtocolError('lost_ack')
    with pytest.raises(WxProtocolError):
        await source.accept_event(event())
    assert await store.checkpoint() == '10'
    restarted, second_client, second_store = setup(manager)
    await restarted.initialize()
    await restarted.accept_event(event())
    second_client.message.assert_not_called()
    rows = await second_store.accepted_events(authorize=allowed)
    assert len(rows['items']) == 1
    envelope = rows['items'][0]['envelope']
    result = await restarted.native_original(envelope['reference'], envelope['version'])
    assert result['text'] == 'private fixture text'
    page = await second_store.read_router_page('0', authorize=allowed)
    assert page['items'][0]['envelope']['original'] == envelope['reference']
    assert 'private fixture text' not in str(page)


@pytest.mark.asyncio
async def test_post_activation_catchup_survives_but_old_baseline_does_not_dispatch(manager):
    source, client, store = setup(manager)
    await source.initialize()
    client.message.return_value = message(createdAt='2026-11-07T00:00:00Z')
    old = event()
    old['data']['origin'] = 'catchup'
    await source.accept_event(old)
    client.message.return_value = message(id='msg-2', createdAt='2026-11-08T00:00:10Z')
    newer = event()
    newer['cursor'] = '11'
    newer['data']['messageId'] = 'msg-2'
    newer['data']['origin'] = 'catchup'
    await source.accept_event(newer)
    rows = await store.accepted_events(authorize=allowed)
    assert len(rows['items']) == 2
    pending = await store.pending_events()
    assert len(pending) == 1
    assert pending[0]['envelope']['payload']['native']['message_id'] == 'msg-2'


@pytest.mark.asyncio
async def test_stable_send_key_unknown_and_needs_review_do_not_resend(manager):
    from langbot.pkg.platform.adapters.agent_wechat.sender import WxSendProvider

    binding = SourceBinding('fixture', 'account-1', 'scope', 'sender-v1', False)
    store = NativeIngressStore(manager, workspace_uuid='workspace-a', bot_uuid='bot-workspace-a', binding=binding)
    await store.initialize('9')
    client = AsyncMock()
    provider = WxSendProvider(client, {'group-1'})
    client.send_text.side_effect = TimeoutError('unknown outcome')
    first = await store.send(
        'host-op-1', 'group', 'group-1', {'text': 'same content'}, provider=provider, authorize=allowed
    )
    assert first['status'] == 'unknown'
    client.lookup_send.return_value = None
    result = await store.send(
        'host-op-1', 'group', 'group-1', {'text': 'same content'}, provider=provider, authorize=allowed
    )
    assert result['status'] == 'unknown'
    assert client.send_text.await_count == 1
    client.send_text.side_effect = None
    client.send_text.return_value = {'state': 'needs_review', 'id': 'intent-2', 'serverId': None}
    review = await store.send(
        'host-op-2', 'group', 'group-1', {'text': 'same content'}, provider=provider, authorize=allowed
    )
    assert review['status'] == 'needs_review'
    again = await store.send(
        'host-op-2', 'group', 'group-1', {'text': 'same content'}, provider=provider, authorize=allowed
    )
    assert again == review
    assert client.send_text.await_count == 2
