"""Human metadata observers do not inherit service/Host source privileges."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from langbot.pkg.api.http.context import PrincipalContext,PrincipalType
from langbot.pkg.entity.persistence.user import User
from langbot.pkg.entity.persistence.workspace import WorkspaceMembership
from langbot.pkg.platform.native_access import NativeAuthorization,register_native_access
from langbot.pkg.platform.native_ui import native_ui_status
from langbot.pkg.platform.native_ingress import NativeIngressStore
from langbot.pkg.platform.native_contract import SourcePage
from tests.integration.platform.test_native_ingress_contracts import manager,store,context,router_event,authority_expectation  # noqa: F401


async def member(manager,account='operator'):
    async with manager.get_db_engine().begin() as connection:
        await connection.execute(sa.insert(User).values(uuid=account,user=account+'@fixture.invalid',
            normalized_email=account+'@fixture.invalid',password='synthetic-not-a-login-hash'))
        await connection.execute(sa.insert(WorkspaceMembership).values(workspace_uuid='workspace-a',account_uuid=account,
            role='owner' if account=='operator' else 'viewer',status='active'))


async def prepared(manager):
    await member(manager)
    binding=store(manager).binding
    expected=authority_expectation(binding)
    ap=SimpleNamespace(persistence_mgr=manager)
    await register_native_access(ap,context(),'bot-workspace-a',binding,source_enabled=True,
        source_authority=expected,ui_observer_accounts=['operator'])
    state=dict(source=binding.source,scope=binding.scope,account=binding.account,revision=binding.revision,
               authority_proof={'binding':expected['binding']})
    calls=[]
    async def facts():
        calls.append(1)
        return state
    adapter=SimpleNamespace(native_connection_facts=facts,
        native_runtime_state=lambda:dict(state='connected',collector_verified=True))
    authorize=await NativeAuthorization(ap).source_factory(context(),'bot-workspace-a',binding,adapter)
    ingress=NativeIngressStore(manager,workspace_uuid='workspace-a',bot_uuid='bot-workspace-a',binding=binding,
                               authorization_fence=authorize.native_fence)
    await ingress.initialize('10')
    await ingress.accept_page(SourcePage('10','11',(router_event(),)),authorize=authorize)
    runtime=SimpleNamespace(workspace_uuid='workspace-a',native_ingress_store=ingress,adapter=adapter,
                            native_source_authorize=authorize)
    viewer=context(permissions=['resource.view'])
    bot=dict(uuid='bot-workspace-a',adapter='test',enable=True)
    return ap,runtime,viewer,bot,state,calls


@pytest.mark.asyncio
async def test_approved_observer_reads_durable_metadata_but_no_upstream_health_is_invented(manager):
    ap,runtime,viewer,bot,state,calls=await prepared(manager)
    result=await native_ui_status(ap,viewer,bot,runtime)
    assert result['state']=='awaiting_upstream' and result['error_code']=='upstream_state_unverified'
    assert result['counts']==dict(source_events=1,router_accepted=0,pending_host=1,quarantines=0,owner_gaps=0)
    assert result['records'][0]['stage']=='source_accepted'
    assert result['records'][0]['record']=='native-1'
    assert 'payload' not in str(result) and 'cursor' not in str(result)
    assert result['has_more'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('principal',['other','api_key','support_admin','wrong_workspace'])
async def test_resource_view_and_host_identity_do_not_confer_ui_observer_access(manager,principal):
    ap,runtime,viewer,bot,state,calls=await prepared(manager)
    if principal=='other':
        await member(manager,'other')
        viewer=replace(viewer,principal=PrincipalContext(PrincipalType.ACCOUNT,account_uuid='other'))
    elif principal=='api_key':
        viewer=replace(viewer,principal=PrincipalContext(PrincipalType.API_KEY,api_key_uuid='dedicated-host'))
    elif principal=='support_admin':
        viewer=replace(viewer,principal=PrincipalContext(PrincipalType.SUPPORT_ADMIN,actor_account_uuid='operator'))
    else:
        viewer=context(workspace='workspace-b',permissions=['resource.view'])
    result=await native_ui_status(ap,viewer,bot,runtime)
    assert result['state']=='needs_authorization'
    assert 'records' not in result and 'counts' not in result


@pytest.mark.asyncio
@pytest.mark.parametrize('revoke',['membership','observer','source'])
async def test_ui_rechecks_current_viewer_and_source_after_delayed_authenticated_proof(manager,revoke):
    ap,runtime,viewer,bot,state,calls=await prepared(manager)
    paused=asyncio.Event()
    resume=asyncio.Event()
    original=runtime.adapter.native_connection_facts
    async def delayed():
        paused.set()
        await resume.wait()
        return await original()
    runtime.adapter.native_connection_facts=delayed
    # The factory captures the callable when attached, so rebuild this real
    # authorizer with the delayed owning-source proof, not a fake allowed flag.
    runtime.native_source_authorize=await NativeAuthorization(ap).source_factory(context(),'bot-workspace-a',
        runtime.native_ingress_store.binding,runtime.adapter)
    task=asyncio.create_task(native_ui_status(ap,viewer,bot,runtime))
    await paused.wait()
    if revoke=='membership':
        async with manager.tenant_uow('workspace-a') as uow:
            await uow.session.execute(sa.update(WorkspaceMembership).where(WorkspaceMembership.account_uuid=='operator').values(status='removed'))
    elif revoke=='observer':
        await register_native_access(ap,context(),'bot-workspace-a',runtime.native_ingress_store.binding,
            source_enabled=True,source_authority=authority_expectation(runtime.native_ingress_store.binding),
            expected_revision=1,ui_observer_accounts=[])
    else:
        state['authority_proof']={'binding':dict(state['authority_proof']['binding'],revision='revoked-revision')}
    resume.set()
    result=await task
    assert result['state']=='needs_authorization'
    assert 'counts' not in result and 'records' not in result


@pytest.mark.asyncio
async def test_missing_or_wrong_source_audience_proof_never_reuses_service_facts(manager):
    ap,runtime,viewer,bot,state,calls=await prepared(manager)
    state.pop('authority_proof')
    result=await native_ui_status(ap,viewer,bot,runtime)
    assert result['state']=='needs_authorization' and 'records' not in result and 'counts' not in result


@pytest.mark.asyncio
async def test_service_audience_expectation_cannot_be_forged_by_config_or_callback_labels(manager):
    from langbot.pkg.platform.native_authority import verify_authority
    expected=dict(kind='wechat-owner-v1',grant=dict(source='test-source',scope='chat-a',principal='metadata-reader',
        workspace='approved-source-profile',agent=None,revision='approved-revision'),
        binding=dict(account='account-a',thread='approved-thread',revision='owner-binding-revision'))
    grant=dict(expected['grant'],active=True,read_only=True,workspace_allowed=True)
    facts=dict(authority_proof={'grant':grant,'binding':expected['binding']})
    assert verify_authority(expected,facts,store(manager).binding)['grant']['active'] is True
    for key,value in [('principal','self-reported'),('workspace','workspace-a'),('revision','new-unapproved'),
                      ('active',False),('read_only',False),('workspace_allowed',False)]:
        bad=dict(authority_proof={'grant':dict(grant,**{key:value}),'binding':expected['binding']})
        with pytest.raises(PermissionError):
            verify_authority(expected,bad,store(manager).binding)


@pytest.mark.asyncio
async def test_outer_bot_detail_does_not_leak_source_account_outside_observer_gate(manager):
    from langbot.pkg.api.http.service.bot import BotService
    ap,runtime,viewer,bot,state,calls=await prepared(manager)
    await member(manager,'other')
    class NativeAdapter:
        def native_source_binding(self): return runtime.native_ingress_store.binding
    async def get_bot(ctx,bot_uuid): return runtime
    ap.platform_mgr=SimpleNamespace(adapter_dict={'test':NativeAdapter},get_bot_by_uuid=get_bot)
    ap.discover=SimpleNamespace(get_components_by_kind=lambda kind:[])
    runtime.adapter.bot_account_id='synthetic-sensitive-source-account'
    from langbot.pkg.entity.persistence.bot import Bot
    async with manager.tenant_uow('workspace-a') as uow:
        await uow.session.execute(sa.update(Bot).where(Bot.uuid=='bot-workspace-a').values(enable=True))
    viewer=replace(viewer,principal=PrincipalContext(PrincipalType.ACCOUNT,account_uuid='other'))
    async with manager.tenant_scope('workspace-a'):
        result=await BotService(ap).get_runtime_bot_info(viewer,'bot-workspace-a',include_secret=False)
    values=result['adapter_runtime_values']
    assert values['native_ingress']['state']=='needs_authorization'
    assert 'bot_account_id' not in values
    assert 'synthetic-sensitive-source-account' not in str(result)
