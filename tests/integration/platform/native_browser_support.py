"""Disposable real native API fixture for same-container browser verification.

Never run on a production host. SQLite, accounts, approval registrations and all
records are synthetic. Login/placement and source lifecycle are explicit mocked
boundaries; native route handlers, BotService, metadata manifests, observer ACL,
NativeAccess and durable record projection are the actual candidate code.
"""
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio
import logging
import tempfile

import quart
import sqlalchemy as sa
import yaml

from langbot.pkg.api.http.controller.groups.platform.adapters import AdaptersRouterGroup
from langbot.pkg.api.http.controller.groups.platform.bots import BotsRouterGroup
from langbot.pkg.api.http.service.bot import BotService
from langbot.pkg.discover.engine import Component
from langbot.pkg.entity.persistence.native_ingress import NativeAccess
from langbot.pkg.platform.adapters.wecom_archive.adapter import WecomArchiveAdapter
from langbot.pkg.platform.botmgr import PlatformManager
from langbot.pkg.platform.native_access import NativeAuthorization, register_native_access, registration_binding
from langbot.pkg.platform.native_contract import SourceEnvelope, SourcePage
from langbot.pkg.platform.native_ingress import NativeIngressStore
from langbot.pkg.platform.sources.agent_wechat import AgentWeChatAdapter
from tests.integration.platform.test_native_ingress_contracts import context, manager as _manager
from tests.integration.platform.test_native_ui_audience import member

TOKEN = 'synthetic-browser-user'
CONFIGS = {
    'wecom-archive': dict(source='finance-fixture', account='corp-fixture', scope='chat-fixture',
                         grant_revision='approved-v1', credential_ref='registered-reader', poll_interval=5, page_limit=100),
    'agent-wechat': dict(source_id='wx-fixture', account_id='account-fixture', scope_id='chat-fixture',
                         allowed_threads=['test@chatroom'], initial_cursor='0',
                         credential_ref='registered-reader', source_flavor='personal-wechat-native-v1'),
}


@asynccontextmanager
async def native_browser_fixture(tmp_path):
    database = _manager.__wrapped__(tmp_path)
    manager = await anext(database)
    await member(manager)
    persona = context(permissions=['resource.view','resource.manage','api_key.manage','runtime.operate'])
    ap = SimpleNamespace(persistence_mgr=manager, instance_config=SimpleNamespace(data={}),
                         logger=logging.getLogger('native-browser-fixture'),
                         sess_mgr=SimpleNamespace(session_list=[]))
    platform = PlatformManager(ap)
    ap.platform_mgr = platform
    platform.adapter_dict = {'wecom-archive': WecomArchiveAdapter, 'agent-wechat': AgentWeChatAdapter}
    for relative in ('pkg/platform/adapters/wecom_archive/manifest.yaml', 'pkg/platform/sources/agent_wechat.yaml'):
        path = Path('src/langbot') / relative
        platform.adapter_components.append(Component(owner='system', manifest=yaml.safe_load(path.read_text()), rel_path=relative))
    ap.discover = SimpleNamespace(get_components_by_kind=lambda kind: platform.adapter_components)
    prepared, active = {}, {}
    async def get_bot(ctx, bot_uuid):
        return active.get(bot_uuid) if ctx.workspace_uuid == 'workspace-a' else None
    async def load_bot(ctx, bot):
        if bot['uuid'] not in prepared:
            raise ValueError('synthetic_fixture_registration_required')
        active[bot['uuid']] = prepared[bot['uuid']]
        return active[bot['uuid']]
    async def remove_bot(ctx, bot_uuid):
        active.pop(bot_uuid, None)
    platform.get_bot_by_uuid, platform.load_bot, platform.remove_bot = get_bot, load_bot, remove_bot
    ap.bot_service = BotService(ap)
    web = quart.Quart('native-browser-api-fixture')
    for group_type in (AdaptersRouterGroup, BotsRouterGroup):
        group = group_type(ap, web)
        async def authenticate(token):
            if token != TOKEN:
                raise ValueError('synthetic_login_required')
            return SimpleNamespace(uuid='operator', user='operator@fixture.invalid'), 'operator@fixture.invalid'
        async def resolve(account, auth_type):
            # Login is synthetic; downstream observer checks re-read actual membership.
            return persona
        group._authenticate_account, group._resolve_account_context = authenticate, resolve
        await group.initialize()

    @web.get('/__fixture/health')
    async def health():
        return quart.jsonify(ready=True, boundary='synthetic-login-source-lifecycle',
                             token=TOKEN, workspace='workspace-a', configs=CONFIGS)

    @web.post('/__fixture/register-seed/<bot_uuid>')
    async def register_seed(bot_uuid):
        async with manager.tenant_scope('workspace-a'):
            bot = await ap.bot_service.get_bot(persona, bot_uuid, include_secret=True)
            if bot is None or bot['adapter'] not in CONFIGS or bot['adapter_config'] != CONFIGS[bot['adapter']] or bot['enable']:
                return quart.jsonify(error='fixture_requires_exact_disabled_config'), 409
            binding = await registration_binding(ap, persona, bot_uuid)
            if bot['adapter'] == 'wecom-archive':
                expected = dict(kind='wecom-source-binding-v1', binding=dict(source='finance-fixture', scope='chat-fixture',
                    principal='metadata-browser-fixture', workspace='source-fixture-audience', revision='approved-v1',
                    corp_id='corp-fixture', origin='owner_source_binding'))
                proof = {'binding': dict(expected['binding'])}
            else:
                expected = dict(kind='wechat-owner-v1', grant=dict(source='wx-fixture', scope='chat-fixture',
                    principal='metadata-browser-fixture', workspace='source-fixture-audience', agent='metadata-only', revision='approved-v1'),
                    binding=dict(account='account-fixture', thread='test@chatroom', revision='binding-fixture-v1'))
                proof = dict(grant=dict(expected['grant'], active=True, read_only=True, workspace_allowed=True), binding=expected['binding'])
            await register_native_access(ap, persona, bot_uuid, binding, source_enabled=True,
                                         source_authority=expected, ui_observer_accounts=['operator'])
            facts = dict(source=binding.source, account=binding.account, scope=binding.scope, revision=binding.revision,
                         authority_proof=proof)
            async def current_facts(): return facts
            adapter = SimpleNamespace(native_connection_facts=current_facts,
                native_runtime_state=lambda: dict(state='source_available', collector_verified=False))
            authorize = await NativeAuthorization(ap).source_factory(persona, bot_uuid, binding, adapter)
            store = NativeIngressStore(manager, workspace_uuid='workspace-a', bot_uuid=bot_uuid,
                                       binding=binding, authorization_fence=authorize.native_fence)
            await store.initialize('fixture-before')
            change = dict(source=binding.source, scope=binding.scope, record='fixture-record', version='fixture-v1', sequence=1,
                kind='created', business_at='2026-11-08T00:00:00Z', observed_at='2026-11-08T00:00:01Z',
                original='source:message/fixture-record/fixture-v1', projection_hash='a'*64, goal_reference=None, saved={})
            await store.accept_page(SourcePage('fixture-before','fixture-after',(
                SourceEnvelope(change['record'],change['version'],change['original'],'created',{'change':change,'dispatch_eligible':True}),
            )),authorize=authorize)
            runtime = SimpleNamespace(workspace_uuid='workspace-a', native_ingress_store=store,
                native_source_authorize=authorize, adapter=adapter, run=AsyncMock(),
                logger=SimpleNamespace(get_logs=AsyncMock(return_value=([],0))), fixture_source_facts=facts)
            prepared[bot_uuid] = runtime
        return quart.jsonify(registered=True, records=1, business_execution=False)

    @web.post('/__fixture/revoke-observer/<bot_uuid>')
    async def revoke(bot_uuid):
        async with manager.tenant_uow('workspace-a') as uow:
            await uow.session.execute(sa.update(NativeAccess).where(NativeAccess.workspace_uuid=='workspace-a',
                NativeAccess.bot_uuid==bot_uuid).values(ui_observer_accounts=[]))
        return quart.jsonify(revoked=True)

    web.native_fixture = SimpleNamespace(ap=ap,prepared=prepared,active=active,persona=persona)
    try:
        yield web
    finally:
        await manager.shutdown()
        await database.aclose()


async def main():
    from hypercorn.asyncio import serve
    from hypercorn.config import Config
    with tempfile.TemporaryDirectory(prefix='native-browser-scratch-') as path:
        async with native_browser_fixture(Path(path)) as web:
            configuration = Config()
            configuration.bind = ['127.0.0.1:5300']
            print('NATIVE_BROWSER_FIXTURE_READY', flush=True)
            await serve(web, configuration)


if __name__ == '__main__':
    asyncio.run(main())
