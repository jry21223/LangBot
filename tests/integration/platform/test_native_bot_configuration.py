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
