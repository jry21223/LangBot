"""Frozen v3 Source -> default Core runtime/HTTP -> formal run_host receipt.

Raw wx owner, placement and OA capability responses are synthetic. Formal Host
admission is unchanged: producer acceptance and needs_review are not analysis.
"""

import pytest
from tests.integration.platform.native_wechat_vertical_support import actual_wechat_core_fixture, BOT_UUID
from tests.integration.platform.wechat_owner_public_support import real_wechat_owner as _source

pytest_plugins = ['tests.integration.platform.test_native_ingress_contracts']
real_wechat_owner = _source


@pytest.mark.asyncio
async def test_actual_source_registry_runtime_and_core_http_no_body_or_shadow_identity(
    manager, real_wechat_owner, tmp_path
):
    fixture = await actual_wechat_core_fixture(manager, real_wechat_owner, tmp_path)
    _, owner, change, _, _ = real_wechat_owner
    try:
        worker = fixture.source_adapter._worker()
        await worker.initialize()
        await worker.poll_once()
        response = await fixture.request('GET', f'/api/v1/native-ingress/{BOT_UUID}/router/changes')
        assert response.status_code == 200, await response.get_json()
        page = (await response.get_json())['data']
        assert len(page['items']) == 1 and len(page['gaps']) == 1
        actual = page['items'][0]['envelope']
        assert {key: actual[key] for key in ('source', 'scope', 'record', 'version', 'original')} == {
            key: change[key] for key in ('source', 'scope', 'record', 'version', 'original')
        }
        assert page['gaps'][0]['code'] == 'history_before_activation_unverified'
        assert 'PRIVATE ORIGINAL' not in str(page) and owner.calls == []
        assert fixture.runtime.native_ingress_store is fixture.store
    finally:
        await fixture.source_adapter.kill()


@pytest.mark.asyncio
@pytest.mark.parametrize('lose_ack', [False, True])
async def test_formal_run_host_consumes_actual_native_core_http_and_recovers_same_receipt(
    manager,
    real_wechat_owner,
    tmp_path,
    monkeypatch,
    lose_ack,
):
    import asyncio
    from copy import deepcopy
    import json
    import os
    from pathlib import Path
    import socket
    import aiohttp
    import quart
    from hypercorn.asyncio import serve
    from hypercorn.config import Config
    from enterprise_agent.run_host import open_host
    from enterprise_agent.native_ingress import NativeIngress
    from enterprise_agent.wechat_native_projection import SavedNativeWechat

    fixture = await actual_wechat_core_fixture(manager, real_wechat_owner, tmp_path)
    _, owner, change, source_config, source_config_path = real_wechat_owner
    original_profile = next(p for p in source_config['native']['readers'] if p['principal'] == 'original')
    original_token = Path(original_profile['token_file']).read_text()
    oa_token = 'synthetic-oa-token-0123456789abcdef'
    monkeypatch.setenv('VERTICAL_ORIGINAL_TOKEN', original_token)
    monkeypatch.setenv('VERTICAL_OA_TOKEN', oa_token)
    calls = []
    oa_calls = []
    fault = {'remaining': lose_ack, 'lost': 0}
    underlying = fixture.web.asgi_app

    async def transport_fault(scope, receive, send):
        if scope['type'] == 'http' and '/router/' in scope.get('path', ''):
            calls.append((scope['method'], scope['path']))
        if scope['type'] == 'http' and scope.get('path', '').endswith('/router/ack') and fault['remaining']:
            messages = []

            async def capture(message):
                messages.append(message)

            await underlying(scope, receive, capture)  # Real auth/transaction/receipt commit first.
            assert messages[0]['status'] == 200
            fault.update(remaining=False, lost=fault['lost'] + 1)
            await send(
                {'type': 'http.response.start', 'status': 503, 'headers': [(b'content-type', b'application/json')]}
            )
            await send({'type': 'http.response.body', 'body': b'{"error":"synthetic_ack_response_lost_after_commit"}'})
            return
        await underlying(scope, receive, send)

    fixture.web.asgi_app = transport_fault

    @fixture.web.get('/v1/events/delivery-capabilities')
    async def synthetic_oa_capabilities():
        assert quart.request.headers.get('X-Workspace-Token') == oa_token
        oa_calls.append('capabilities')
        return quart.jsonify(idempotent_messages=True)

    @fixture.web.get('/__vertical/ready')
    async def ready():
        return quart.jsonify(ready=True)

    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    origin = f'http://127.0.0.1:{port}'
    server_config = Config()
    server_config.bind = [f'127.0.0.1:{port}']
    stopped = asyncio.Event()
    server_task = asyncio.create_task(serve(fixture.web, server_config, shutdown_trigger=stopped.wait))
    private = tmp_path / 'formal-host'
    private.mkdir(mode=0o700)
    key_file = private / 'core-reader.key'
    key_file.write_text(fixture.keys['narrow']['key'])
    key_file.chmod(0o600)
    connector = private / 'connector.json'
    connector.write_text(json.dumps(dict(workspace='oa-workspace', endpoint=origin, model='no-model-fixture')))
    connector.chmod(0o600)
    source_item = dict(
        endpoint=f'http://127.0.0.1:{source_config["listen_port"]}/native',
        source='wx-test',
        scope='test-only',
        principal='original',
        token_env='VERTICAL_ORIGINAL_TOKEN',
        goal_assignee_ids=[],
        kind='personal-wechat',
        native_ingress=dict(
            enabled=True,
            flavor='native_wechat',
            endpoint=origin,
            bot_uuid=BOT_UUID,
            workspace='workspace-a',
            api_key_file=str(key_file),
        ),
    )
    host_config = dict(
        version=1,
        enabled=True,
        mode='observe',
        owner_uid=os.getuid(),
        state=str(private / 'workflow.sqlite'),
        workspace='oa-workspace',
        workspace_endpoint=origin,
        workspace_token_env='VERTICAL_OA_TOKEN',
        coordinator='router-lab',
        assistants=['analysis-lab'],
        connector_config=str(connector),
        rule_version='native-v1',
        model_version='no-model-fixture',
        sources=[source_item],
        gd=None,
        limits=dict(pages=1, page_size=100, admissions=20, actions=20, checks=20, seconds=15),
    )

    def cycle():
        host, limits = open_host(host_config)  # Formal factory and real admission, not a hand-built Host.
        try:
            ingress = host.sources['wx-test']
            assert isinstance(ingress, NativeIngress) and ingress.flavor == 'native_wechat'
            assert ingress.finance.workspace == 'oa-workspace' and ingress.finance.principal == 'original'
            assert fixture.source_adapter.client.workspace == 'core-workspace'
            assert fixture.source_adapter.client.operator == 'metadata'
            assert isinstance(host.producer.projectors['wx-test'], SavedNativeWechat)
            domain = ingress.transport.consumer_binding()
            assert domain['workspace'] == 'workspace-a' and host.workflow.workspace == 'oa-workspace'
            report = host.step(now='2026-11-08T00:00:05Z', limits=limits)
            return report, host.producer.coverage('wx-test'), deepcopy(host.workflow._read())
        finally:
            host.close()
            host.workflow.close()

    try:
        async with aiohttp.ClientSession() as client:
            for attempt in range(50):
                try:
                    async with client.get(origin + '/__vertical/ready') as response:
                        assert response.status == 200
                        break
                except aiohttp.ClientConnectorError:
                    if attempt == 49:
                        raise
                    await asyncio.sleep(0.02)
        worker = fixture.source_adapter._worker()
        await worker.initialize()
        await worker.poll_once()
        report, coverage, state = await asyncio.to_thread(cycle)
        assert len(state['producer']['candidates']) == 1
        candidate = next(iter(state['producer']['candidates'].values()))
        assert (
            candidate['change']['original'] == change['original']
            and candidate['change']['version'] == change['version']
        )
        assert candidate['status'] == 'needs_review' and not state['outbox']
        assert coverage['native_ingress']['ack_pending'] is lose_ack
        receipt = coverage['native_ingress']['receipts'][0]
        assert receipt['status'] == 'accepted' and receipt['record'] == change['record']
        assert await fixture.router_cursor() == 2  # Event plus persistent owner gap, not an upstream wx cursor.
        report, coverage, restarted = await asyncio.to_thread(cycle)
        assert not coverage['native_ingress'].get('ack_pending', False)
        assert len(restarted['producer']['candidates']) == 1 and not restarted['outbox']
        lookup = await fixture.request(
            'GET', f'/api/v1/native-ingress/{BOT_UUID}/router/receipts/' + receipt['event_id']
        )
        assert lookup.status_code == 200 and (await lookup.get_json())['data'] == receipt
        assert fault['lost'] == int(lose_ack) and oa_calls and set(oa_calls) == {'capabilities'}
        assert owner.calls == [] and 'PRIVATE ORIGINAL' not in str(restarted)
        # Revoke only OA's original-reader approval: deny before any new Core IO.
        original_profile['sharing_approved'] = False
        source_config_path.write_text(json.dumps(source_config))
        before = len(calls)
        _, denied, after = await asyncio.to_thread(cycle)
        assert len(calls) == before and len(after['producer']['candidates']) == 1 and not after['outbox']
        assert await fixture.router_cursor() == 2
        assert denied['cursor'] == coverage['cursor']
    finally:
        stopped.set()
        await server_task
        await fixture.source_adapter.kill()
