"""True API-key authentication and the default native receiver policy over HTTP."""
from datetime import datetime,timedelta

import pytest
import sqlalchemy as sa

from langbot.pkg.entity.persistence.apikey import ApiKey
from langbot.pkg.platform.native_contract import SourcePage
from langbot.pkg.platform.native_ingress import _hash
from tests.integration.platform.test_native_ingress_contracts import manager,store,router_event  # noqa: F401
from tests.integration.platform.native_http_support import native_http_fixture

_PATH='/api/v1/native-ingress/bot-workspace-a/router'


@pytest.mark.asyncio
async def test_narrow_db_key_actual_http_page_ack_lookup_and_restart_receipt(manager):
    fixture=await native_http_fixture(manager,binding=store(manager).binding,
        first_page=SourcePage('10','11',(router_event(),)))
    response=await fixture.request('GET',_PATH+'/changes',headers={'X-Workspace-Id':'workspace-b'})
    assert response.status_code==200,await response.get_json()
    page=(await response.get_json())['data']
    assert page['previous_cursor'] is None and page['next_cursor']=='1'
    receipt=dict(event_id=page['items'][0]['event_id'],source='test-source',record='native-1',version='v1',
        status='accepted',receipt_id=_hash(['langbot-router-receipt-v1','test-source','native-1','v1']))
    response=await fixture.request('POST',_PATH+'/ack',json=dict(page_id=page['page_id'],page_sha256=page['page_sha256'],receipts=[receipt]))
    assert response.status_code==200,await response.get_json()
    assert (await response.get_json())['data']==dict(status='accepted',page_id=page['page_id'],next_cursor='1')
    response=await fixture.request('GET',_PATH+'/receipts/'+receipt['event_id'])
    assert response.status_code==200 and (await response.get_json())['data']==receipt
    assert await fixture.router_cursor()==1


@pytest.mark.asyncio
@pytest.mark.parametrize('key',['unregistered','broad','global','legacy','other_workspace'])
async def test_unregistered_and_wide_scope_keys_never_reserve_or_ack_native_pages(manager,key):
    fixture=await native_http_fixture(manager,binding=store(manager).binding,
        first_page=SourcePage('10','11',(router_event(),)))
    response=await fixture.request('GET',_PATH+'/changes',key=key)
    assert response.status_code==403,await response.get_json()
    assert await fixture.router_cursor()==0


@pytest.mark.asyncio
@pytest.mark.parametrize('change',['revoke','expire','remote_source'])
async def test_actual_authentication_and_remote_grant_revocation_deny_receipts(manager,change):
    fixture=await native_http_fixture(manager,binding=store(manager).binding,
        first_page=SourcePage('10','11',(router_event(),)))
    if change=='remote_source':
        fixture.facts['authority_proof']['binding']['revision']='not-approved'
    else:
        values={'status':'revoked'} if change=='revoke' else {'expires_at':datetime.now()-timedelta(seconds=1)}
        async with manager.tenant_uow('workspace-a') as uow:
            await uow.session.execute(sa.update(ApiKey).where(ApiKey.uuid==fixture.keys['narrow']['uuid']).values(**values))
    response=await fixture.request('GET',_PATH+'/changes')
    assert response.status_code in {401,403},await response.get_json()
    assert await fixture.router_cursor()==0


@pytest.mark.asyncio
async def test_remote_source_revoke_after_page_reservation_withholds_metadata_but_replays_same_page(manager):
    from langbot.pkg.entity.persistence.native_ingress import NativeSource
    fixture=await native_http_fixture(manager,binding=store(manager).binding,
        first_page=SourcePage('10','11',(router_event(),)))
    original=dict(fixture.facts['authority_proof']['binding'])
    fixture.hooks['calls']=0
    async def revoke_on_post(facts,calls):
        if calls==3: facts['authority_proof']={'binding':dict(original,revision='revoked')}
    fixture.hooks['callback']=revoke_on_post
    response=await fixture.request('GET',_PATH+'/changes')
    assert response.status_code==403
    async with manager.tenant_uow('workspace-a') as uow:
        pending=(await uow.session.get(NativeSource,fixture.store.source_id)).router_pending_page
    assert pending is not None and await fixture.router_cursor()==0
    fixture.hooks['callback']=None
    fixture.facts['authority_proof']={'binding':original}
    response=await fixture.request('GET',_PATH+'/changes')
    assert response.status_code==200
    assert (await response.get_json())['data']['page_id']==pending


@pytest.mark.asyncio
async def test_ack_post_revoke_does_not_claim_rollback_or_reissue_a_different_receipt(manager):
    fixture=await native_http_fixture(manager,binding=store(manager).binding,
        first_page=SourcePage('10','11',(router_event(),)))
    page=(await (await fixture.request('GET',_PATH+'/changes')).get_json())['data']
    receipt=dict(event_id=page['items'][0]['event_id'],source='test-source',record='native-1',version='v1',
        status='accepted',receipt_id=_hash(['langbot-router-receipt-v1','test-source','native-1','v1']))
    original=dict(fixture.facts['authority_proof']['binding'])
    fixture.hooks['calls']=0
    async def revoke_on_post(facts,calls):
        if calls==3: facts['authority_proof']={'binding':dict(original,revision='revoked')}
    fixture.hooks['callback']=revoke_on_post
    response=await fixture.request('POST',_PATH+'/ack',json=dict(page_id=page['page_id'],page_sha256=page['page_sha256'],receipts=[receipt]))
    assert response.status_code==403
    assert await fixture.router_cursor()==1
    fixture.hooks['callback']=None
    fixture.facts['authority_proof']={'binding':original}
    response=await fixture.request('GET',_PATH+'/receipts/'+receipt['event_id'])
    assert response.status_code==200 and (await response.get_json())['data']==receipt
