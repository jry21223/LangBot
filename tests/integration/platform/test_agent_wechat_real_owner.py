"""Real frozen source HTTP -> default registry -> real Core SQLite."""

from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa

from langbot.pkg.entity.persistence.native_ingress import NativeGap

from langbot.pkg.platform.adapters.agent_wechat.owner_client import OwnerProtocolError
from langbot.pkg.platform.native_access import register_native_access
from langbot.pkg.platform.native_clients import NativeClientRegistry
from langbot.pkg.platform.native_ingress import _hash
from langbot.pkg.platform.native_runtime import attach_native_runtime
from langbot.pkg.platform.sources.agent_wechat import AgentWeChatAdapter
from tests.integration.platform.test_agent_wechat_native import owner_expectation
from tests.integration.platform.test_native_ingress_contracts import context
from tests.integration.platform.wechat_owner_public_support import real_wechat_owner as _real_wechat_owner
from tests.unit_tests.platform.test_agent_wechat_source import adapter_fixture

pytest_plugins = ['tests.integration.platform.test_native_ingress_contracts']
real_wechat_owner = _real_wechat_owner


@pytest.mark.asyncio
async def test_actual_scoped_source_to_registry_sqlite_restart_and_revocation(manager, real_wechat_owner, tmp_path):
    get, owner, change, config, path = real_wechat_owner
    code, raw, _ = get('/native/authority', extra={'stage': 'ingest', 'audience_workspace': 'core-workspace'})
    assert code == 200
    proof = json.loads(raw)
    base = adapter_fixture()
    adapter_config = dict(
        base.config,
        source_id='wx-test',
        scope_id='test-only',
        account_id='account',
        allowed_threads=['test@chatroom'],
        initial_cursor='0',
        source_flavor='personal-wechat-native-v1',
    )
    adapter_config.pop('activation_at')
    adapter_config.pop('consumer_id')
    adapter = AgentWeChatAdapter(adapter_config, base.logger)
    binding = adapter.native_source_binding()
    ap = SimpleNamespace(persistence_mgr=manager)
    # Independent controlled fixture approval, not a production TOFU registration.
    expectation = {
        'kind': 'wechat-owner-v1',
        'grant': {k: proof['grant'][k] for k in ('source', 'scope', 'principal', 'workspace', 'agent', 'revision')},
        'binding': proof['binding'],
    }
    await register_native_access(
        ap, context(), 'bot-workspace-a', binding, source_enabled=True, source_authority=expectation
    )
    reader = config['native']['readers'][0]
    credential = tmp_path / 'registry-secret'
    credential.write_text(Path(reader['token_file']).read_text())
    credential.chmod(0o600)
    entries = [
        dict(
            credential_ref=adapter_config['credential_ref'],
            workspace_uuid='workspace-a',
            bot_uuid='bot-workspace-a',
            binding_sha256=_hash(asdict(binding)),
            enabled=True,
            provider='agent-wechat-owner-v1',
            secret_file=credential.name,
            endpoint=f'http://127.0.0.1:{config["listen_port"]}',
        )
    ]
    ap.native_source_client_factory = NativeClientRegistry(ap, entries, secret_root=str(tmp_path))
    runtime = SimpleNamespace(
        ap=ap,
        adapter=adapter,
        execution_context=context(),
        bot_entity=SimpleNamespace(uuid='bot-workspace-a'),
        workspace_uuid='workspace-a',
        assert_execution_active=AsyncMock(),
    )
    try:
        store = await attach_native_runtime(runtime)
        assert owner_expectation(adapter.client) == expectation
        assert (await adapter.client.native_binding())['thread'] == 'test@chatroom'
        source_page = await adapter.client.native_changes(cursor='0')
        assert source_page['items'][0]['change']['original_available'] is True
        assert source_page['gaps'] == ['history_before_activation_unverified']
        assert owner.calls == []  # Metadata permission does not rewrite canonical readiness or read body.
        worker = adapter._worker()
        await worker.initialize()
        await worker.poll_once()
        rows = await store.accepted_events(authorize=runtime.native_source_authorize)
        assert len(rows['items']) == 1
        assert rows['items'][0]['envelope']['reference'] == change['original']
        assert rows['items'][0]['envelope']['version'] == change['version']
        assert 'PRIVATE ORIGINAL' not in str(rows)
        descriptor = await worker.native_original_manifest(change['original'], change['version'])
        assert set(descriptor) == {'reference', 'version', 'projection_hash', 'kind', 'original_available'}
        checkpoint = await store.checkpoint()

        async def gaps_count():
            async with manager.tenant_uow('workspace-a') as uow:
                return await uow.session.scalar(
                    sa.select(sa.func.count()).select_from(NativeGap).where(NativeGap.source_id == store.source_id)
                )

        assert await gaps_count() == 1
        from langbot.pkg.platform.adapters.agent_wechat.owner_source import OwnerNativeSource

        restarted = OwnerNativeSource(adapter.client, store, runtime.native_source_authorize, initial_cursor='0')
        await restarted.initialize()
        await restarted.poll_once()  # Persistent coverage gaps remain observable even with no new records.
        assert await store.checkpoint() == checkpoint
        assert await gaps_count() == 1
        assert len((await store.accepted_events(authorize=runtime.native_source_authorize))['items']) == 1
        from copy import deepcopy

        native_store = get.service._store()
        try:
            baseline = deepcopy(owner.message_data)
            baseline.update(message_id='baseline-2', server_id='server-2')
            native_store.commit_native(baseline, event_cursor='2', observed_at='2026-11-08T00:00:02Z', baseline=True)
            recalled = deepcopy(owner.message_data)
            recalled.update(recalled=True, body=None, sender_id=None, direction=None)
            native_store.commit_native(recalled, event_cursor='3', observed_at='2026-11-08T00:00:03Z')
            native_store.commit_unresolved('unresolved-4', '4')
        finally:
            native_store.close()
        await restarted.poll_once()
        accepted = await store.accepted_events(authorize=runtime.native_source_authorize)
        assert len(accepted['items']) == 3
        new_facts = [row['envelope'] for row in accepted['items'][1:]]
        assert {fact['kind'] for fact in new_facts} == {'baseline', 'recalled'}
        assert all(not fact['payload']['dispatch_eligible'] for fact in new_facts)
        assert await store.source_sequence_checkpoint() == 4
        assert 'PRIVATE ORIGINAL' not in str(accepted)
        checkpoint = await store.checkpoint()
        assert adapter.native_runtime_state()['state'] != 'connected'  # fixture observation is intentionally stale
        config['native']['readers'][0]['sharing_approved'] = False
        path.write_text(json.dumps(config))
        with pytest.raises(OwnerProtocolError):
            await restarted.poll_once()
        assert await store.checkpoint() == checkpoint
    finally:
        await adapter.kill()
