"""Real Quart/API-key/default NativeAccess/SQLite handoff fixture.

All identities, data, keys and placement/source proofs are synthetic and confined
in the caller's scratch database/container. API-key hashing/authentication and
native source/receiver policies are the real production classes, not patched.
No production key, registration, port, source or transport is used.
"""
from types import SimpleNamespace

import quart
import sqlalchemy as sa

from langbot.pkg.api.http.controller.groups.native_ingress import NativeIngressRouterGroup
from langbot.pkg.api.http.service.apikey import ApiKeyService
from langbot.pkg.entity.persistence.apikey import ApiKey
from langbot.pkg.entity.persistence.native_ingress import NativeSource
from langbot.pkg.platform.native_access import NativeAuthorization,register_native_access
from langbot.pkg.platform.native_ingress import NativeIngressStore
from tests.integration.platform.test_native_ingress_contracts import context,authority_expectation
from tests.integration.platform.test_native_ui_audience import member


class NativeHttpFixture:
    def __init__(self,ap,web,store,keys,domain,facts,hooks):
        self.ap,self.web,self.store,self.keys,self.domain,self.facts=ap,web,store,keys,domain,facts
        self.hooks=hooks
        self.client=web.test_client()

    def consumer_binding(self):
        return dict(self.domain)

    async def request(self,method,path,*,key='narrow',json=None,headers=None):
        request_headers={'X-API-Key':self.keys[key]['key'],**(headers or {})}
        return await self.client.open(path,method=method,headers=request_headers,json=json)

    async def router_cursor(self):
        async with self.ap.persistence_mgr.tenant_uow(self.store.workspace_uuid) as uow:
            return (await uow.session.get(NativeSource,self.store.source_id)).router_cursor


async def native_http_fixture(manager,*,binding,first_page=None,source_authority=None,
                              owner_frame_sequences=False,flavor='personal-wechat-native-v1'):
    """Use only in an isolated test. Caller may supply canonical synthetic pages."""
    await member(manager)
    workspace='workspace-a'
    bot_uuid='bot-workspace-a'
    authority=source_authority or authority_expectation(binding)
    source_proof={'binding':authority['binding']} if authority['kind']=='wecom-source-binding-v1' else {
        'grant':dict(authority['grant'],active=True,read_only=True,workspace_allowed=True),
        'binding':authority['binding'],
    }
    facts=dict(source=binding.source,account=binding.account,scope=binding.scope,revision=binding.revision,
               authority_proof=source_proof)
    hooks={'calls':0,'callback':None}
    async def source_facts():
        hooks['calls']+=1
        if hooks['callback'] is not None:
            await hooks['callback'](facts,hooks['calls'])
        return facts
    async def execution(requested_workspace,**kwargs):
        if requested_workspace not in {'workspace-a','workspace-b'}:
            raise PermissionError('synthetic_workspace_unavailable')
        return SimpleNamespace(instance_uuid='test-'+requested_workspace,workspace_uuid=requested_workspace,placement_generation=1)
    ap=SimpleNamespace(persistence_mgr=manager,instance_config=SimpleNamespace(data={'api':{'global_api_key':'lbk_synthetic_global'}}),
        workspace_service=SimpleNamespace(get_execution_binding=execution,policy=SimpleNamespace(multi_workspace_enabled=False)))
    async def local_execution(): return await execution(workspace)
    ap.workspace_service.get_local_execution_binding=local_execution
    ap.apikey_service=ApiKeyService(ap)
    admin=context(permissions=['api_key.manage','resource.manage','runtime.operate'])
    async with manager.tenant_scope(workspace):
        keys={}
        for name,scopes in [('narrow',[]),('unregistered',[]),('broad',['runtime.operate'])]:
            keys[name]=await ap.apikey_service.create_api_key(admin,'synthetic-'+name,scopes=scopes)
        keys['legacy']=await ap.apikey_service.create_api_key(admin,'synthetic-legacy',scopes=[])
        await manager.execute_async(sa.update(ApiKey).where(ApiKey.uuid==keys['legacy']['uuid']).values(uuid='legacy-oss-api-key'))
    keys['global']={'key':'lbk_synthetic_global'}
    async with manager.tenant_scope('workspace-b'):
        keys['other_workspace']=await ap.apikey_service.create_api_key(context(workspace='workspace-b',permissions=['api_key.manage']),
                                                                      'synthetic-other-workspace',scopes=[])
    await register_native_access(ap,admin,bot_uuid,binding,receiver_id='synthetic-host',
        receiver_api_key_uuid=keys['narrow']['uuid'],source_enabled=True,receiver_enabled=True,source_authority=authority)
    source_authorize=await NativeAuthorization(ap).source_factory(admin,bot_uuid,binding,
        SimpleNamespace(native_connection_facts=source_facts))
    store=NativeIngressStore(manager,workspace_uuid=workspace,bot_uuid=bot_uuid,binding=binding,
        authorization_fence=source_authorize.native_fence,owner_frame_sequences=owner_frame_sequences)
    initial=first_page.previous_cursor if first_page is not None else 'source-before'
    await store.initialize(initial)
    if first_page is not None: await store.accept_page(first_page,authorize=source_authorize)
    async def active(): await execution(workspace)
    runtime=SimpleNamespace(native_ingress_store=store,native_source_authorize=source_authorize,
        assert_execution_active=active,workspace_uuid=workspace,bot_entity=SimpleNamespace(uuid=bot_uuid))
    async def get_bot(ctx,requested):
        return runtime if ctx.workspace_uuid==workspace and requested==bot_uuid else None
    ap.platform_mgr=SimpleNamespace(get_bot_by_uuid=get_bot)
    web=quart.Quart('native-real-http-fixture')
    group=NativeIngressRouterGroup(ap,web)
    await group.initialize()
    domain=dict(endpoint='https://core-fixture.invalid',workspace=workspace,bot_uuid=bot_uuid,
                source=binding.source,scope=binding.scope,flavor=flavor)
    return NativeHttpFixture(ap,web,store,keys,domain,facts,hooks)
