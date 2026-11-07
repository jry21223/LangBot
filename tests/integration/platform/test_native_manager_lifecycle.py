"""Real Manager/RuntimeBot/task/placement cold starts in disposable SQLite.

Application wiring is explicit, not the entire boot graph: storage deletion is a
no-op and no plugin runtime, model or production profile is started. Discovery,
WorkspaceService (including placement reads), PlatformManager, RuntimeBot,
AsyncTaskManager, both native classes and the debug WebSocket are real.
"""

import asyncio
from dataclasses import asdict
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa

from langbot.pkg.core.app import Application
from langbot.pkg.core.taskmgr import AsyncTaskManager
from langbot.pkg.discover.engine import ComponentDiscoveryEngine
from langbot.pkg.entity.persistence.base import Base
from langbot.pkg.entity.persistence.bot import Bot
from langbot.pkg.entity.persistence.workspace import WorkspaceExecutionState
from langbot.pkg.platform.botmgr import PlatformManager, RuntimeBot
from langbot.pkg.platform.native_access import register_native_access
from langbot.pkg.platform.native_clients import NativeClientRegistry
from langbot.pkg.platform.native_ingress import _hash
from langbot.pkg.platform.sources.agent_wechat import AgentWeChatAdapter
from langbot.pkg.workspace.service import WorkspaceService
from tests.integration.platform.test_native_ingress_contracts import context
from tests.integration.platform.wechat_owner_public_support import real_wechat_owner as _owner
from tests.unit_tests.platform.test_agent_wechat_source import adapter_fixture

pytest_plugins = ['tests.integration.platform.test_native_ingress_contracts']
real_wechat_owner = _owner


async def application(manager):
    ap = Application()
    ap.logger = logging.getLogger('manager-lifecycle')
    ap.persistence_mgr = manager
    ap.instance_config = SimpleNamespace(data={'system': {}, 'api': {}})
    # Images are not involved in these lifecycle tests. No disk or service IO.
    ap.storage_mgr = SimpleNamespace(storage_provider=SimpleNamespace(delete_dir_recursive=AsyncMock()))
    ap.workspace_service = WorkspaceService(ap, instance_uuid='test-workspace-a')
    async with manager.get_db_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            sa.insert(WorkspaceExecutionState).values(
                workspace_uuid='workspace-a', instance_uuid='test-workspace-a', active_generation=1
            )
        )
    ap.discover = ComponentDiscoveryEngine(ap)
    for name in ('websocket', 'agent_wechat'):
        ap.discover.load_component_manifest(f'pkg/platform/sources/{name}.yaml')
    ap.discover.load_component_manifest('pkg/platform/adapters/wecom_archive/manifest.yaml')
    ap.task_mgr = AsyncTaskManager(ap)
    ap.platform_mgr = PlatformManager(ap)
    return ap


async def put_bot(manager, uuid, adapter, config, enabled):
    async with manager.tenant_uow('workspace-a') as uow:
        await uow.session.execute(
            sa.insert(Bot).values(
                uuid=uuid,
                workspace_uuid='workspace-a',
                name=uuid,
                description='',
                adapter=adapter,
                adapter_config=config,
                enable=enabled,
            )
        )


async def stop(ap):
    wrappers = list(ap.task_mgr.tasks)
    await ap.platform_mgr.shutdown()
    await asyncio.gather(*(w.task for w in wrappers), return_exceptions=True)
    assert not ap.platform_mgr.bots and not ap.platform_mgr.websocket_proxy_bots
    assert all(w.task.done() for w in wrappers)


@pytest.mark.asyncio
async def test_disabled_native_drafts_cold_start_real_manager_and_websocket(manager):
    ap = await application(manager)
    await put_bot(manager, 'disabled-wx', 'agent-wechat', {}, False)
    await put_bot(manager, 'disabled-wecom', 'wecom-archive', {}, False)
    await ap.platform_mgr.initialize()
    try:
        assert {'agent-wechat', 'wecom-archive', 'websocket'} <= set(ap.platform_mgr.adapter_dict)
        assert not ap.platform_mgr.bots
        proxy = ap.platform_mgr.websocket_proxy_bot
        assert type(proxy) is RuntimeBot
        assert proxy.execution_context.workspace_uuid == 'workspace-a'
        assert proxy.placement_generation == 1
        await ap.platform_mgr.run()
        await asyncio.sleep(0)
        assert proxy.task_wrapper is not None and not proxy.task_wrapper.task.done()
    finally:
        await stop(ap)


@pytest.mark.asyncio
async def test_enabled_native_real_manager_runtime_source_registry_and_restart(manager, real_wechat_owner, tmp_path):
    ap = await application(manager)
    get, owner, change, source_config, path = real_wechat_owner
    config = dict(
        adapter_fixture().config,
        source_id='wx-test',
        scope_id='test-only',
        account_id='account',
        allowed_threads=['test@chatroom'],
        initial_cursor='0',
        source_flavor='personal-wechat-native-v1',
    )
    config.pop('activation_at')
    config.pop('consumer_id')
    binding = AgentWeChatAdapter(config, adapter_fixture().logger).native_source_binding()
    await put_bot(manager, 'enabled-wx', 'agent-wechat', config, True)
    code, raw, _ = get('/native/authority', extra={'stage': 'ingest', 'audience_workspace': 'core-workspace'})
    assert code == 200
    proof = json.loads(raw)
    expected = dict(
        kind='wechat-owner-v1',
        binding=proof['binding'],
        grant={k: proof['grant'][k] for k in ('source', 'scope', 'principal', 'workspace', 'agent', 'revision')},
    )
    await register_native_access(ap, context(), 'enabled-wx', binding, source_enabled=True, source_authority=expected)
    reader = next(r for r in source_config['native']['readers'] if r['principal'] == 'metadata')
    secret = tmp_path / 'synthetic-metadata-secret'
    secret.write_text(Path(reader['token_file']).read_text())
    secret.chmod(0o600)
    ap.native_source_client_factory = NativeClientRegistry(
        ap,
        [
            dict(
                credential_ref=config['credential_ref'],
                workspace_uuid='workspace-a',
                bot_uuid='enabled-wx',
                binding_sha256=_hash(asdict(binding)),
                enabled=True,
                provider='agent-wechat-owner-v1',
                secret_file=secret.name,
                endpoint=f'http://127.0.0.1:{source_config["listen_port"]}',
            )
        ],
        secret_root=str(tmp_path),
    )
    previous = None
    checkpoint = None
    for _ in range(2):
        await ap.platform_mgr.initialize()
        try:
            assert len(ap.platform_mgr.bots) == 1
            runtime = ap.platform_mgr.bots[0]
            assert type(runtime) is RuntimeBot and runtime is not previous
            assert runtime.native_ingress_store is not None
            await runtime.assert_execution_active()  # Real DB-backed WorkspaceService.
            await ap.platform_mgr.run()
            for attempt in range(100):
                try:
                    records = await runtime.native_ingress_store.accepted_events(
                        authorize=runtime.native_source_authorize
                    )
                    observed = await runtime.native_ingress_store.checkpoint()
                except ValueError:
                    observed = None
                    records = {'items': []}
                if observed is not None and records['items']:
                    break
                await asyncio.sleep(0.02)
            assert observed is not None
            assert len(records['items']) == 1
            assert records['items'][0]['envelope']['reference'] == change['original']
            if checkpoint is not None:
                assert observed == checkpoint
            checkpoint = observed
            assert runtime.task_wrapper is not None and not runtime.task_wrapper.task.done()
            assert runtime.adapter.get_supported_apis() == []
            assert owner.calls == []  # Metadata lifecycle never fetches original bytes.
            previous = runtime
        finally:
            await stop(ap)


@pytest.mark.asyncio
async def test_unregistered_enabled_native_does_not_abort_debug_runtime(manager, caplog):
    ap = await application(manager)
    config = dict(adapter_fixture().config)
    await put_bot(manager, 'unregistered-wx', 'agent-wechat', config, True)
    with caplog.at_level(logging.ERROR):
        await ap.platform_mgr.initialize()
    try:
        assert not ap.platform_mgr.bots
        assert 'native_source_not_registered' in caplog.text
        assert type(ap.platform_mgr.websocket_proxy_bot) is RuntimeBot
        await ap.platform_mgr.run()
        await asyncio.sleep(0)
        assert not ap.platform_mgr.websocket_proxy_bot.task_wrapper.task.done()
    finally:
        await stop(ap)
