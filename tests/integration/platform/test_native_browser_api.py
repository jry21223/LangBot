"""Same real backend fixture consumed by the isolated browser, not fulfilled API JSON."""
import pytest

from tests.integration.platform.native_browser_support import native_browser_fixture, TOKEN, CONFIGS


@pytest.mark.asyncio
@pytest.mark.parametrize('adapter',['wecom-archive','agent-wechat'])
async def test_actual_native_create_edit_register_enable_record_and_revoke(tmp_path,adapter):
    async with native_browser_fixture(tmp_path) as web:
        client=web.test_client()
        headers={'Authorization':'Bearer '+TOKEN}
        async def request(method,path,body=None):
            response=await client.open(path,method=method,headers=headers,json=body)
            assert response.status_code==200,await response.get_data()
            return (await response.get_json())['data']
        manifest=await request('GET','/api/v1/platform/adapters')
        assert {item['name'] for item in manifest['adapters']}==set(CONFIGS)
        icon=await client.get('/api/v1/platform/adapters/'+adapter+'/icon')
        assert icon.status_code==200 and icon.content_type=='image/png'
        created=await request('POST','/api/v1/platform/bots',dict(name='real-api-fixture',description='',
            adapter=adapter,adapter_config={},enable=False))
        path='/api/v1/platform/bots/'+created['uuid']
        detail=await request('GET',path)
        assert detail['bot']['adapter_runtime_values']['native_ingress']['state']=='needs_configuration'
        await request('PUT',path,dict(adapter_config=CONFIGS[adapter],enable=False))
        seeded=await client.post('/__fixture/register-seed/'+created['uuid'])
        assert seeded.status_code==200,await seeded.get_data()
        await request('PUT',path,{'enable':True})
        snapshot=(await request('GET',path))['bot']['adapter_runtime_values']['native_ingress']
        assert snapshot['state']=='source_available'
        assert snapshot['counts']['source_events']==1 and snapshot['counts']['router_accepted']==0
        assert snapshot['records'][0]['stage']=='source_accepted'
        await client.post('/__fixture/revoke-observer/'+created['uuid'])
        snapshot=(await request('GET',path))['bot']['adapter_runtime_values']['native_ingress']
        assert snapshot['state']=='needs_authorization' and 'records' not in snapshot and 'counts' not in snapshot
        logs=await client.post(path+'/logs',headers=headers,json={'from_index':-1,'max_count':10})
        assert logs.status_code==403


@pytest.mark.asyncio
@pytest.mark.parametrize('revoke',['observer','source'])
async def test_native_legacy_logs_recheck_audience_after_logger_read(tmp_path,revoke):
    from types import SimpleNamespace
    import sqlalchemy as sa
    from langbot.pkg.entity.persistence.native_ingress import NativeAccess
    async with native_browser_fixture(tmp_path) as web:
        client=web.test_client()
        headers={'Authorization':'Bearer '+TOKEN}
        response=await client.post('/api/v1/platform/bots',headers=headers,json=dict(name='logs',description='',
            adapter='wecom-archive',adapter_config=CONFIGS['wecom-archive'],enable=False))
        bot_uuid=(await response.get_json())['data']['uuid']
        assert (await client.post('/__fixture/register-seed/'+bot_uuid)).status_code==200
        path='/api/v1/platform/bots/'+bot_uuid
        assert (await client.put(path,headers=headers,json={'enable':True})).status_code==200
        runtime=web.native_fixture.prepared[bot_uuid]
        calls=[]
        async def delayed_logs(*args):
            calls.append(1)
            if revoke=='observer':
                async with web.native_fixture.ap.persistence_mgr.tenant_uow('workspace-a') as uow:
                    await uow.session.execute(sa.update(NativeAccess).where(NativeAccess.bot_uuid==bot_uuid).values(ui_observer_accounts=[]))
            else:
                runtime.fixture_source_facts['authority_proof']['binding']['revision']='revoked-after-read'
            return [SimpleNamespace(to_json=lambda: {'message':'SOURCE_ACTIVITY_COUNT_19','seq_id':19})],19
        runtime.logger.get_logs=delayed_logs
        response=await client.post(path+'/logs',headers=headers,json={'from_index':-1,'max_count':10})
        assert response.status_code==403 and b'SOURCE_ACTIVITY' not in await response.get_data()
        assert calls==[1]
        # Current revocation also stops the next request before logger access.
        assert (await client.post(path+'/logs',headers=headers,json={})).status_code==403
        assert calls==[1]
        assert (await client.get(path+'/event-routes/status',headers=headers)).status_code==403


@pytest.mark.asyncio
async def test_browser_backend_fixture_serves_only_container_loopback(tmp_path):
    import asyncio
    import aiohttp
    from hypercorn.asyncio import serve
    from hypercorn.config import Config
    async with native_browser_fixture(tmp_path) as web:
        stopped=asyncio.Event()
        config=Config()
        config.bind=['127.0.0.1:5300']
        task=asyncio.create_task(serve(web,config,shutdown_trigger=stopped.wait))
        try:
            async with aiohttp.ClientSession() as client:
                for attempt in range(50):
                    try:
                        async with client.get('http://127.0.0.1:5300/__fixture/health') as response:
                            payload=await response.json()
                            assert payload['ready'] is True and payload['token']==TOKEN
                            break
                    except aiohttp.ClientConnectorError:
                        if attempt==49:
                            raise
                        await asyncio.sleep(0.02)
        finally:
            stopped.set()
            await task
