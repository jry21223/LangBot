"""Actual SQLite acceptance and source restart in the isolated LangBot runtime."""

from unittest.mock import AsyncMock
import pytest

from tests.unit_tests.platform.test_agent_wechat_projection import message
from tests.unit_tests.platform.test_agent_wechat_source import event
from langbot.pkg.platform.native_contract import SourceBinding
from langbot.pkg.platform.native_ingress import NativeIngressStore, _hash
from langbot.pkg.platform.adapters.agent_wechat.source import WxNativeSource
from langbot.pkg.platform.adapters.agent_wechat.client import WxProtocolError
from tests.unit_tests.platform.test_agent_wechat_owner_client import owner_server as _owner_server_fixture


pytest_plugins = ['tests.integration.platform.test_native_ingress_contracts']
owner_server = _owner_server_fixture


def owner_expectation(client):
    return {
        'kind': 'wechat-owner-v1',
        'grant': {
            'source': client.source,
            'scope': client.scope,
            'principal': client.operator,
            'workspace': client.workspace,
            'agent': client.agent,
            'revision': client.grant_revision,
        },
        'binding': {'account': client.account_id, 'thread': client.thread, 'revision': client.binding_revision},
    }


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
    provider = WxSendProvider(client, {'group-1'}, target_id='group-1')
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


@pytest.mark.asyncio
async def test_default_source_factory_rejects_direct_account_facts_without_owner_audience(manager):
    from types import SimpleNamespace
    from tests.unit_tests.platform.test_agent_wechat_source import adapter_fixture
    from langbot.pkg.platform.native_access import register_native_access
    from langbot.pkg.platform.native_runtime import attach_native_runtime
    from tests.integration.platform.test_native_ingress_contracts import context

    adapter = adapter_fixture()
    client = AsyncMock()
    client.account_id = 'account-1'
    client.status.return_value = {
        'id': 'account-1',
        'authenticated': True,
        'contactSyncReady': True,
        'connection': 'online',
        'incomingTransport': 'native_push',
    }
    ap = SimpleNamespace(persistence_mgr=manager, native_source_client_factory=AsyncMock(return_value=client))
    await register_native_access(ap, context(), 'bot-workspace-a', adapter.native_source_binding(), source_enabled=True)
    runtime = SimpleNamespace(
        ap=ap,
        adapter=adapter,
        execution_context=context(),
        bot_entity=SimpleNamespace(uuid='bot-workspace-a'),
        workspace_uuid='workspace-a',
        assert_execution_active=AsyncMock(),
    )
    with pytest.raises(PermissionError, match='source_audience_not_registered'):
        await attach_native_runtime(runtime)
    assert adapter.store is None
    client.message.assert_not_called()


@pytest.mark.asyncio
async def test_restart_reconciles_lost_ack_even_without_another_source_event(manager):
    source, client, store = setup(manager)
    await source.initialize()
    client.ack.side_effect = WxProtocolError('lost_ack')
    with pytest.raises(WxProtocolError):
        await source.accept_event(event())
    restarted, replacement, _ = setup(manager)
    await restarted.initialize()
    replacement.ack.assert_awaited_once_with('consumer', '10')
    replacement.message.assert_not_called()
    assert await store.checkpoint() == '10'


@pytest.mark.asyncio
async def test_actual_wx_quarantine_does_not_starve_next_message_or_forge_host_receipt(manager):
    source, client, store = setup(manager)
    await source.initialize()
    unresolved = dict(event(), kind='media.ready', data={'assetId': 'unapproved-or-not-yet-known'})
    await source.accept_event(unresolved)
    next_event = event()
    next_event['cursor'] = '11'
    await source.accept_event(next_event)
    assert client.message.await_count == 1
    assert await store.checkpoint() == '11'
    page = await store.read_router_page('0', authorize=allowed)
    assert len(page['gaps']) == 1
    assert len(page['items']) == 1
    assert page['gaps'][0]['reason'] == 'native_unresolved'
    rows = await store.accepted_events(authorize=allowed)
    real = next(item for item in rows['items'] if item['envelope']['kind'] != 'unresolved')
    envelope = real['envelope']
    receipt = {
        'event_id': real['event_id'],
        'source': 'fixture',
        'record': envelope['record'],
        'version': envelope['version'],
        'status': 'accepted',
        'receipt_id': _hash(['langbot-router-receipt-v1', 'fixture', envelope['record'], envelope['version']]),
    }
    await store.ack_router_page(page['page_id'], page['page_sha256'], [receipt], authorize=allowed)
    assert await store.lookup_router_receipt(page['gaps'][0]['event_id'], authorize=allowed) is None


@pytest.mark.asyncio
async def test_core_metadata_reader_cannot_reach_any_original_or_media_provider():
    from types import SimpleNamespace
    from tests.integration.platform.test_native_ingress_contracts import context
    from langbot.pkg.platform.native_contract import NativeAccessDenied
    from langbot.pkg.platform.native_originals import read_native_original

    lookup = AsyncMock(side_effect=AssertionError('provider must not be reached'))
    ap = SimpleNamespace(platform_mgr=SimpleNamespace(get_bot_by_uuid=lookup))
    for kind, options in [('original', {}), ('chunk', {'part_index': 0}), ('media', {'asset_id': 'asset'})]:
        with pytest.raises(NativeAccessDenied, match='metadata_reader_originals_forbidden'):
            await read_native_original(ap, context(), 'bot-workspace-a', kind, 'reference', 'version', **options)
    lookup.assert_not_called()


@pytest.mark.asyncio
async def test_owner_canonical_pages_persist_legacy_refs_and_derived_versions_without_shadow_identity(manager):
    from langbot.pkg.platform.adapters.agent_wechat.owner_contract import OwnerPage, accept_owner_page
    from tests.unit_tests.platform.test_agent_wechat_owner_contract import owner_change, FIXTURE_CONTRACT

    binding = SourceBinding('existing-wechat', 'account-1', 'existing-scope', 'owner-protocol-candidate')
    store = NativeIngressStore(manager, workspace_uuid='workspace-a', bot_uuid='bot-workspace-a', binding=binding)
    await store.initialize('owner-before')
    pending = owner_change(sequence=101, state='PENDING')
    complete = owner_change(sequence=102, version='e' * 64)
    page = OwnerPage('owner-before', 'owner-after', (pending, complete), True)
    await accept_owner_page(
        store,
        page,
        authorize=allowed,
        previous_sequence=100,
        source='existing-wechat',
        scope='existing-scope',
        account='account-1',
        thread='group-1',
        contract=FIXTURE_CONTRACT,
    )
    rows = await store.accepted_events(authorize=allowed)
    assert [row['envelope']['version'] for row in rows['items']] == ['b' * 64, 'e' * 64]
    assert [row['envelope']['reference'] for row in rows['items']] == [
        pending.change['original'],
        complete.change['original'],
    ]
    assert all(row['envelope']['payload']['event_cursor'] == 'opaque-wx-cursor' for row in rows['items'])
    assert [row['envelope']['payload']['change']['sequence'] for row in rows['items']] == [101, 102]
    assert await store.checkpoint() == 'owner-after'


@pytest.mark.asyncio
async def test_scoped_owner_http_runtime_persists_typed_gaps_and_preserves_independent_owner_progress(
    manager, owner_server
):
    from types import SimpleNamespace
    from tests.integration.platform.test_native_ingress_contracts import context
    from tests.unit_tests.platform.test_agent_wechat_source import adapter_fixture
    from langbot.pkg.platform.sources.agent_wechat import AgentWeChatAdapter
    from langbot.pkg.platform.native_access import register_native_access
    from langbot.pkg.platform.native_runtime import attach_native_runtime
    from langbot.pkg.platform.native_contract import NativeAccessDenied
    from langbot.pkg.platform.adapters.agent_wechat.owner_client import OwnerProtocolError
    from langbot.pkg.platform.adapters.agent_wechat.owner_source import OwnerNativeSource

    client, page, authority, requests = owner_server
    # The owner profile audience is explicit; it is not fabricated from Core's Workspace ID.
    client.workspace = 'actual-source-profile-audience'
    authority['grant']['workspace'] = client.workspace
    initial = adapter_fixture()
    config = dict(
        initial.config,
        source_id='existing-wechat',
        scope_id='existing-scope',
        source_flavor='personal-wechat-native-v1',
        initial_cursor='owner-before',
    )
    config.pop('activation_at')
    config.pop('consumer_id')
    adapter = AgentWeChatAdapter(config, initial.logger)
    ap = SimpleNamespace(persistence_mgr=manager, native_source_client_factory=AsyncMock(return_value=client))
    binding = adapter.native_source_binding()
    await register_native_access(
        ap, context(), 'bot-workspace-a', binding, source_enabled=True, source_authority=owner_expectation(client)
    )
    runtime = SimpleNamespace(
        ap=ap,
        adapter=adapter,
        execution_context=context(),
        bot_entity=SimpleNamespace(uuid='bot-workspace-a'),
        workspace_uuid='workspace-a',
        assert_execution_active=AsyncMock(),
    )
    store = await attach_native_runtime(runtime)
    worker = adapter._worker()
    assert isinstance(worker, OwnerNativeSource)
    assert binding.read_only is True
    await worker.initialize()
    assert adapter.native_runtime_state()['state'] != 'connected'
    unresolved = {
        'event_id': 'owner-unresolved-1',
        'kind': 'unresolved',
        'source_sequence': 100,
        'event_cursor': 'raw-wx-cursor',
        'reason': 'native_unresolved',
        'source': 'existing-wechat',
        'scope': 'existing-scope',
    }
    page['items'].insert(0, unresolved)
    page['gaps'] = ['original_unavailable']
    await worker.poll_once()
    assert adapter.native_runtime_state() == {
        'state': 'unknown',
        'error_code': 'upstream_state_unverified',
        'collector_verified': False,
    }
    assert await store.checkpoint() == 'owner-after'
    assert await store.source_sequence_checkpoint() == 101
    rows = await store.accepted_events(authorize=runtime.native_source_authorize)
    assert len(rows['items']) == 1
    envelope = rows['items'][0]['envelope']
    assert envelope['payload']['owner_event_id'] == 'owner-event-1'
    assert envelope['reference'] == page['items'][1]['change']['original']
    feed = await store.read_router_page('0', authorize=runtime.native_source_authorize)
    assert len(feed['items']) == 1 and {gap['reason'] for gap in feed['gaps']} == {
        'native_unresolved',
        'owner_source_gap',
    }
    for gap in feed['gaps']:
        required = {'event_id', 'reason', 'source', 'scope'}
        if gap['reason'] == 'owner_source_gap':
            assert gap['code'] == 'original_unavailable'
            required.add('code')
        assert set(gap) == required
    # Reopen using only the persisted owner sequence/cursor, with no wx read or ACK.
    restarted = OwnerNativeSource(client, store, runtime.native_source_authorize, initial_cursor='owner-before')
    await restarted.initialize()
    assert restarted.last_sequence == 101
    page.update(previous_cursor='owner-after', next_cursor='owner-after', items=[], gaps=[])
    assert await restarted.poll_once() is None
    for method in ('native_original', 'native_original_manifest', 'native_media'):
        with pytest.raises(NativeAccessDenied, match='metadata_reader_only'):
            await getattr(adapter, method)(
                *(['reference', 'version', 'asset'] if method == 'native_media' else ['reference', 'version'])
            )
    assert all(request[0] == 'GET' for request in requests)
    assert {request[1] for request in requests} == {'/native/authority', '/native/changes'}
    authority['grant']['active'] = False
    with pytest.raises(OwnerProtocolError):
        await restarted.poll_once()
    assert await store.checkpoint() == 'owner-after'


@pytest.mark.asyncio
async def test_registered_owner_provider_builds_real_metadata_client_with_default_factory(
    manager, owner_server, tmp_path
):
    from dataclasses import asdict
    from types import SimpleNamespace
    from tests.integration.platform.test_native_ingress_contracts import context
    from tests.unit_tests.platform.test_agent_wechat_source import adapter_fixture
    from langbot.pkg.platform.sources.agent_wechat import AgentWeChatAdapter
    from langbot.pkg.platform.native_access import register_native_access
    from langbot.pkg.platform.native_runtime import attach_native_runtime
    from langbot.pkg.platform.native_clients import NativeClientRegistry
    from langbot.pkg.platform.adapters.agent_wechat.owner_client import OwnerNativeClient

    profile, _, _, _ = owner_server
    base = adapter_fixture()
    config = dict(
        base.config,
        source_id=profile.source,
        scope_id=profile.scope,
        source_flavor='personal-wechat-native-v1',
        initial_cursor='owner-before',
    )
    config.pop('activation_at')
    config.pop('consumer_id')
    adapter = AgentWeChatAdapter(config, base.logger)
    ap = SimpleNamespace(persistence_mgr=manager)
    binding = adapter.native_source_binding()
    await register_native_access(
        ap, context(), 'bot-workspace-a', binding, source_enabled=True, source_authority=owner_expectation(profile)
    )
    credential = tmp_path / 'metadata-reader-key'
    credential.write_text('synthetic-narrow-reader')
    credential.chmod(0o600)
    entries = [
        {
            'credential_ref': config['credential_ref'],
            'workspace_uuid': 'workspace-a',
            'bot_uuid': 'bot-workspace-a',
            'binding_sha256': _hash(asdict(binding)),
            'enabled': True,
            'provider': 'agent-wechat-owner-v1',
            'secret_file': credential.name,
            'endpoint': profile.endpoint,
        }
    ]
    ap.native_source_client_factory = NativeClientRegistry(ap, entries, secret_root=str(tmp_path))
    runtime = SimpleNamespace(
        ap=ap,
        adapter=adapter,
        execution_context=context(),
        bot_entity=SimpleNamespace(uuid='bot-workspace-a'),
        workspace_uuid='workspace-a',
        assert_execution_active=AsyncMock(),
    )
    try:
        store = await attach_native_runtime(runtime)
        assert isinstance(adapter.client, OwnerNativeClient) and adapter.client.stage == 'ingest'
        assert not hasattr(adapter.client, 'native_original') and not hasattr(adapter.client, 'native_media')
        await adapter._worker().initialize()
        await adapter.worker.poll_once()
        assert await store.checkpoint() == 'owner-after'
    finally:
        await adapter.kill()
