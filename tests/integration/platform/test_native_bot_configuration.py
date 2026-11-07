"""Real BotService/database checks: invalid native config never reaches storage."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa

from langbot.pkg.api.http.service.bot import BotService
from langbot.pkg.entity.persistence.bot import Bot
from langbot.pkg.platform.adapters.wecom_archive.adapter import WecomArchiveAdapter
from langbot.pkg.platform.sources.agent_wechat import AgentWeChatAdapter
from tests.integration.platform.test_native_ingress_contracts import manager, context  # noqa: F401


def config():
    return dict(source='finance-fixture',account='corp-fixture',scope='chat-fixture',
                grant_revision='approved-v1',credential_ref='registered-reader')


def service(manager):
    ap=SimpleNamespace(persistence_mgr=manager,instance_config=SimpleNamespace(data={}),
        platform_mgr=SimpleNamespace(adapter_dict={'wecom-archive':WecomArchiveAdapter,'agent-wechat':AgentWeChatAdapter},
            remove_bot=AsyncMock(),get_bot_by_uuid=AsyncMock(return_value=None),load_bot=AsyncMock()),
        sess_mgr=SimpleNamespace(session_list=[]))
    return BotService(ap)


@pytest.mark.asyncio
@pytest.mark.parametrize('field',['source_token','secret','secret_file','endpoint','principal'])
async def test_disabled_native_create_rejects_unexpected_credentials_before_insert(manager,field):
    bot_service=service(manager)
    payload=dict(name='native draft',description='',adapter='wecom-archive',enable=False,
                 adapter_config=dict(config(),**{field:'synthetic-rejected-value'}))
    async with manager.tenant_scope('workspace-a'):
        with pytest.raises(ValueError,match='service_config_not_allowed'):
            await bot_service.create_bot(context(),payload)
    async with manager.tenant_uow('workspace-a') as uow:
        rows=(await uow.session.execute(sa.select(Bot).where(Bot.name=='native draft'))).scalars().all()
        assert rows==[]
    bot_service.ap.platform_mgr.load_bot.assert_not_called()


@pytest.mark.asyncio
async def test_config_only_update_and_adapter_switch_validate_before_mutation(manager):
    bot_service=service(manager)
    async with manager.tenant_scope('workspace-a'):
        bot_uuid=await bot_service.create_bot(context(),dict(name='native',description='',adapter='wecom-archive',
                                                            enable=False,adapter_config=config()))
        bad=dict(config(),source_token='synthetic-rejected-value')
        with pytest.raises(ValueError):
            await bot_service.update_bot(context(),bot_uuid,{'adapter_config':bad})
        assert (await bot_service.get_bot(context(),bot_uuid,include_secret=True))['adapter_config']==config()
        with pytest.raises(ValueError):
            await bot_service.update_bot(context(),'bot-workspace-a',{'adapter':'wecom-archive','adapter_config':bad})
        assert (await bot_service.get_bot(context(),'bot-workspace-a',include_secret=True))['adapter']=='test'
        with pytest.raises(ValueError,match='explicit_config'):
            await bot_service.update_bot(context(),bot_uuid,{'adapter':'test'})
    bot_service.ap.platform_mgr.remove_bot.assert_not_called()


@pytest.mark.asyncio
async def test_old_native_secret_is_redacted_on_read_and_requires_explicit_clean_replacement(manager):
    bot_service=service(manager)
    async with manager.tenant_uow('workspace-a') as uow:
        await uow.session.execute(sa.update(Bot).where(Bot.uuid=='bot-workspace-a').values(adapter='wecom-archive',
            adapter_config=dict(config(),source_token='synthetic-old-value')))
    async with manager.tenant_scope('workspace-a'):
        value=await bot_service.get_bot(context(),'bot-workspace-a',include_secret=True)
        assert value['adapter_config']=={} and value['native_configuration_invalid'] is True
        assert 'synthetic-old-value' not in str(value)
        with pytest.raises(ValueError):
            await bot_service.update_bot(context(),'bot-workspace-a',{'name':'must not persist'})
        await bot_service.update_bot(context(),'bot-workspace-a',{'adapter_config':config()})
        clean=await bot_service.get_bot(context(),'bot-workspace-a',include_secret=True)
        assert clean['adapter_config']==config() and 'native_configuration_invalid' not in clean


@pytest.mark.asyncio
async def test_concurrent_adapter_change_cannot_bypass_native_config_validation(manager):
    import asyncio
    bot_service=service(manager)
    stale_read=asyncio.Event()
    continue_stale=asyncio.Event()
    original_get=bot_service.get_bot
    async def paused_get(context,bot_uuid,include_secret=False):
        value=await original_get(context,bot_uuid,include_secret=include_secret)
        if asyncio.current_task().get_name()=='stale-config-writer' and not stale_read.is_set():
            stale_read.set()
            await continue_stale.wait()
        return value
    bot_service.get_bot=paused_get
    async def stale_update():
        async with manager.tenant_scope('workspace-a'):
            await bot_service.update_bot(context(),'bot-workspace-a',{'adapter_config':{'source_token':'synthetic-rejected-value'}})
    task=asyncio.create_task(stale_update(),name='stale-config-writer')
    await stale_read.wait()
    async with manager.tenant_scope('workspace-a'):
        await bot_service.update_bot(context(),'bot-workspace-a',{'adapter':'wecom-archive','adapter_config':config()})
    continue_stale.set()
    with pytest.raises(ValueError,match='configuration_changed_retry'):
        await task
    async with manager.tenant_uow('workspace-a') as uow:
        row=await uow.session.get(Bot,'bot-workspace-a')
        assert row.adapter=='wecom-archive' and row.adapter_config==config()


@pytest.mark.asyncio
async def test_switch_into_native_always_requires_explicit_validated_complete_config(manager):
    bot_service=service(manager)
    # Even a currently clean legacy config cannot be borrowed for a later
    # adapter-only switch: another legacy writer may edit it concurrently.
    async with manager.tenant_uow('workspace-a') as uow:
        await uow.session.execute(sa.update(Bot).where(Bot.uuid=='bot-workspace-a').values(adapter_config=config()))
    async with manager.tenant_scope('workspace-a'):
        with pytest.raises(ValueError,match='explicit_config'):
            await bot_service.update_bot(context(),'bot-workspace-a',{'adapter':'wecom-archive'})
    async with manager.tenant_uow('workspace-a') as uow:
        row=await uow.session.get(Bot,'bot-workspace-a')
        assert row.adapter=='test' and row.adapter_config==config()


@pytest.mark.asyncio
async def test_official_native_draft_save_registration_enable_validation_sequence(manager):
    from langbot.pkg.platform.native_access import register_native_access,registration_binding
    from tests.integration.platform.test_native_ingress_contracts import authority_expectation
    bot_service=service(manager)
    async with manager.tenant_scope('workspace-a'):
        bot_uuid=await bot_service.create_bot(context(),dict(name='draft',description='',adapter='wecom-archive',
                                                            adapter_config={},enable=False))
        draft=await bot_service.get_bot(context(),bot_uuid,include_secret=True)
        assert draft['enable'] is False and draft['native_configuration_incomplete'] is True
        with pytest.raises(ValueError):
            await bot_service.update_bot(context(),bot_uuid,{'enable':True})
        bot_service.ap.platform_mgr.load_bot.assert_not_called()
        await bot_service.update_bot(context(),bot_uuid,{'adapter_config':config(),'enable':False})
        saved=await bot_service.get_bot(context(),bot_uuid,include_secret=True)
        assert saved['enable'] is False and saved['adapter_config']==config()
        bot_service.ap.platform_mgr.load_bot.assert_not_called()
        binding=await registration_binding(bot_service.ap,context(),bot_uuid)
        await register_native_access(bot_service.ap,context(),bot_uuid,binding,source_enabled=True,
                                     source_authority=authority_expectation(binding))
        await bot_service.update_bot(context(),bot_uuid,{'enable':True})
        # The runtime is deliberately a fixture here: this proves the real
        # database/API order, not a production source connection or grant.
        assert (await bot_service.get_bot(context(),bot_uuid,include_secret=True))['enable'] is True
        bot_service.ap.platform_mgr.load_bot.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_disabled_empty_config_cannot_overwrite_concurrently_enabled_native(manager):
    import asyncio
    bot_service=service(manager)
    async with manager.tenant_scope('workspace-a'):
        bot_uuid=await bot_service.create_bot(context(),dict(name='draft',description='',adapter='wecom-archive',
                                                            adapter_config={},enable=False))
    paused=asyncio.Event()
    resume=asyncio.Event()
    original_get=bot_service.get_bot
    async def paused_get(ctx,uuid,include_secret=False):
        value=await original_get(ctx,uuid,include_secret=include_secret)
        if asyncio.current_task().get_name()=='stale-disabled-draft' and not paused.is_set():
            paused.set()
            await resume.wait()
        return value
    bot_service.get_bot=paused_get
    async def stale():
        async with manager.tenant_scope('workspace-a'):
            await bot_service.update_bot(context(),bot_uuid,{'adapter_config':{}})
    task=asyncio.create_task(stale(),name='stale-disabled-draft')
    await paused.wait()
    async with manager.tenant_scope('workspace-a'):
        await bot_service.update_bot(context(),bot_uuid,{'adapter_config':config(),'enable':True})
    resume.set()
    with pytest.raises(ValueError,match='configuration_changed_retry'):
        await task
    async with manager.tenant_uow('workspace-a') as uow:
        row=await uow.session.get(Bot,bot_uuid)
        assert row.enable is True and row.adapter_config==config()


@pytest.mark.asyncio
async def test_native_source_rejects_local_route_configuration_and_existing_debug_binding(manager,monkeypatch):
    import quart
    from langbot.pkg.api.http.controller.groups.platform.bots import BotsRouterGroup
    bot_service=service(manager)
    binding=[dict(event_pattern='message.received',target_type='discard',target_uuid='',enabled=True)]
    async with manager.tenant_scope('workspace-a'):
        with pytest.raises(ValueError,match='routes_managed_externally'):
            await bot_service.create_bot(context(),dict(name='must not persist',description='',adapter='wecom-archive',
                adapter_config=config(),enable=False,event_bindings=binding))
        bot_uuid=await bot_service.create_bot(context(),dict(name='native',description='',adapter='wecom-archive',
                adapter_config=config(),enable=False))
        with pytest.raises(ValueError,match='routes_managed_externally'):
            await bot_service.update_bot(context(),bot_uuid,{'event_bindings':binding})
    async with manager.tenant_uow('workspace-a') as uow:
        await uow.session.execute(sa.update(Bot).where(Bot.uuid==bot_uuid).values(event_bindings=binding))
    bot_service.ap.bot_service=bot_service
    web=quart.Quart('native-routing-guard')
    group=BotsRouterGroup(bot_service.ap,web)
    async def none(*args): return None
    async def account(*args): return ({},'operator')
    async def account_context(*args): return context(permissions=['resource.view'])
    monkeypatch.setattr(group,'_authenticate_support_admin',none)
    monkeypatch.setattr(group,'_authenticate_account',account)
    monkeypatch.setattr(group,'_resolve_account_context',account_context)
    await group.initialize()
    response=await web.test_client().post('/api/v1/platform/bots/'+bot_uuid+'/event-routes/dry-run',
        headers={'Authorization':'Bearer synthetic-identity','Accept-Language':'zh-Hans'},
        json={'event_type':'message.received','event_data':{'message_text':'must not become native source'}})
    assert response.status_code==409,await response.get_json()
    assert (await response.get_json())['code']=='native_ingress_debug_unavailable'
