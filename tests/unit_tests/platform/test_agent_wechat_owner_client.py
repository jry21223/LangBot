"""Fixed owner v1 metadata HTTP contract against a synthetic local server."""

import pytest
from aiohttp import web

from langbot.pkg.platform.adapters.agent_wechat.owner_client import OwnerNativeClient, OwnerProtocolError
from tests.unit_tests.platform.test_agent_wechat_owner_contract import owner_change
from langbot.pkg.platform.adapters.agent_wechat.owner_contract import NATIVE_V1


@pytest.fixture
async def owner_server():
    item = owner_change()
    item.change['original'] = item.change['original'].replace('owner-fixture:', 'wechat-native:')
    page = {
        'source': 'existing-wechat',
        'scope': 'existing-scope',
        'previous_cursor': 'owner-before',
        'next_cursor': 'owner-after',
        'complete_page': True,
        'gaps': [],
        'items': [
            {
                'event_id': 'owner-event-1',
                'change': item.change,
                'native_version': 'wx-version',
                'event_cursor': 'raw-wx-cursor',
                'media': [],
                'qualification': 'eligible',
                'dispatch_eligible': True,
            }
        ],
    }
    authority = {
        'grant': {
            'source': 'existing-wechat',
            'scope': 'existing-scope',
            'principal': 'metadata-reader',
            'workspace': 'workspace-a',
            'agent': 'metadata-agent',
            'active': True,
            'read_only': True,
            'workspace_allowed': True,
            'revision': 'grant-r1',
        },
        'binding': {'account': 'account-1', 'thread': 'group-1', 'revision': 'binding-r1'},
    }
    requests = []

    async def handle(request):
        requests.append((request.method, request.path, dict(request.query), dict(request.headers)))
        assert request.headers['Authorization'] == 'Bearer synthetic-narrow-reader'
        assert request.headers['X-Operator'] == 'metadata-reader'
        assert request.query['source'] == 'existing-wechat'
        assert request.query['scope'] == 'existing-scope'
        assert request.query['workspace'] == client.workspace
        if request.path == '/native/changes':
            return web.json_response(page)
        if request.path == '/native/authority':
            return web.json_response(authority)
        return web.json_response({'error': {'code': 'forbidden'}}, status=403)

    app = web.Application()
    app.router.add_route('*', '/{tail:.*}', handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    client = OwnerNativeClient(
        f'http://127.0.0.1:{port}',
        'synthetic-narrow-reader',
        operator='metadata-reader',
        source='existing-wechat',
        scope='existing-scope',
        workspace='workspace-a',
        account='account-1',
        thread='group-1',
        stage='ingest',
        agent='metadata-agent',
        grant_revision='grant-r1',
        binding_revision='binding-r1',
        contract=NATIVE_V1,
    )
    yield client, page, authority, requests
    await client.close()
    await runner.cleanup()


@pytest.mark.asyncio
async def test_exact_owner_paths_queries_metadata_identity_and_no_raw_wx_operations(owner_server):
    client, page, _, requests = owner_server
    result = await client.native_changes(cursor='owner-before', limit=100)
    assert result['items'][0]['change'] == page['items'][0]['change']
    assert result['items'][0]['change']['version'] == 'b' * 64
    assert result['items'][0]['event_cursor'] == 'raw-wx-cursor'
    assert result['items'][0]['change']['sequence'] == 101
    assert {entry[1] for entry in requests} == {'/native/authority', '/native/changes'}
    assert all(entry[0] == 'GET' for entry in requests)
    assert not hasattr(client, 'message') and not hasattr(client, 'media_content')
    assert not hasattr(client, 'ack')  # The existing owner retains wx ACK and collection progress.


@pytest.mark.asyncio
async def test_scope_principal_workspace_and_revision_cannot_be_self_reported(owner_server):
    client, _, authority, _ = owner_server
    for field, value in [
        ('scope', 'other'),
        ('principal', 'other'),
        ('workspace', 'other'),
        ('active', False),
        ('read_only', False),
        ('workspace_allowed', False),
        ('revision', 'other'),
    ]:
        previous = authority['grant'][field]
        authority['grant'][field] = value
        with pytest.raises(OwnerProtocolError):
            await client.native_changes(cursor='owner-before')
        authority['grant'][field] = previous


@pytest.mark.asyncio
async def test_unknown_current_version_is_rejected_and_original_is_not_a_client_capability(owner_server):
    client, page, authority, _ = owner_server
    change = page['items'][0]['change']
    authority['original'] = {
        'reference': change['original'],
        'version': 'e' * 64,
        'native_version': 'wx-version',
        'recalled': False,
        'available': True,
        'dispatch_eligible': True,
        'input_hash': 'c' * 64,
        'parser_version': 'parser-v1',
        'result_sha256': 'd' * 64,
    }
    with pytest.raises(OwnerProtocolError, match='version'):
        await client.native_authority(reference=change['original'], version=change['version'])
    assert not hasattr(client, 'native_original') and not hasattr(client, 'native_media')


@pytest.mark.asyncio
async def test_partial_and_nested_metadata_fail_before_admission(owner_server):
    client, page, _, _ = owner_server
    page['complete_page'] = False
    with pytest.raises(OwnerProtocolError):
        await client.native_changes(cursor='owner-before')
    page['complete_page'] = True
    page['items'][0]['media'] = [
        {
            'asset_id': 'asset',
            'kind': 'image',
            'state': 'pending',
            'sha256': None,
            'byte_length': None,
            'content_type': {'token': 'no'},
        }
    ]
    with pytest.raises(OwnerProtocolError):
        await client.native_changes(cursor='owner-before')


@pytest.mark.asyncio
async def test_oneof_unresolved_has_no_fake_message_identity_and_uses_owner_sequence(owner_server):
    client, page, _, _ = owner_server
    original = page['items'][0]
    unresolved = {
        'event_id': 'owner-unresolved-1',
        'kind': 'unresolved',
        'source_sequence': 100,
        'event_cursor': 'raw-wx-token',
        'reason': 'native_unresolved',
        'source': 'existing-wechat',
        'scope': 'existing-scope',
    }
    page['items'] = [unresolved, original]
    page['gaps'] = ['history_before_activation_unverified']
    result = await client.native_changes(cursor='owner-before')
    assert result['items'][0] == unresolved
    assert 'record' not in result['items'][0] and 'change' not in result['items'][0]
    page['items'][0]['change'] = None
    with pytest.raises(OwnerProtocolError):
        await client.native_changes(cursor='owner-before')


@pytest.mark.asyncio
async def test_old_source_gap_labels_are_not_silently_inherited_by_native_flavor(owner_server):
    client, page, _, _ = owner_server
    for gap in ['non_text_not_projected', 'recall_not_qualified', {'token': 'no'}]:
        page['gaps'] = [gap]
        with pytest.raises(OwnerProtocolError):
            await client.native_changes(cursor='owner-before')


@pytest.mark.asyncio
async def test_optional_collector_is_fresh_evidence_not_grant_or_http_success(owner_server):
    from datetime import datetime, timezone, timedelta

    client, _, authority, _ = owner_server
    await client.native_authority()
    assert client.upstream_state() == 'unknown'
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
    assert client.upstream_state() == 'connected'
    for update in [
        {
            'authenticated': False,
            'connection': 'disconnected',
            'incoming_transport': 'disconnected',
            'contact_sync_ready': False,
        },
        {'connection': 'reconnecting'},
        {'incoming_transport': 'heartbeat_polling'},
        {'contact_sync_ready': False},
        {'incoming_transport': 'simulator_push'},
        {'account': 'other'},
        {'observed_at': (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()},
        {'observed_at': 'no-timezone'},
        {'authenticated': {'token': 'no'}},
    ]:
        authority['collector'] = dict(collector, **update)
        await client.native_authority()
        assert client.upstream_state() != 'connected'
    authority['collector'] = {'token': 'must-not-escape'}
    proof = await client.native_authority()
    assert 'collector' not in proof and client.upstream_state() == 'unknown'


@pytest.mark.asyncio
async def test_global_generation_change_is_not_an_owner_version_or_fresh_read_barrier(owner_server):
    client, page, authority, _ = owner_server
    historical = await client.native_changes(cursor='owner-before')
    change = page['items'][0]['change']
    authority['original'] = {
        'reference': change['original'],
        'version': change['version'],
        'native_version': 'different-global-generation-from-fresh-snapshot',
        'recalled': False,
        'available': True,
        'dispatch_eligible': True,
        'input_hash': 'c' * 64,
        'parser_version': 'parser-v1',
        'result_sha256': 'd' * 64,
    }
    current = await client.native_authority(reference=change['original'], version=change['version'])
    assert current['original']['version'] == historical['items'][0]['change']['version']
    assert current['original']['native_version'] != historical['items'][0]['native_version']
    assert page['items'][0]['native_version'] == 'wx-version'
    assert historical['items'][0]['native_version'] == 'wx-version'
