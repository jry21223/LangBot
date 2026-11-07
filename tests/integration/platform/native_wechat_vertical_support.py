"""Real scoped Source -> default registry/runtime -> protected Core HTTP fixture.

Only the frozen Source's raw wx owner and deployment placement are synthetic.
The metadata and OpenAgents original-reader profiles remain separate identities.
"""

from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import sqlalchemy as sa

from langbot.pkg.entity.persistence.bot import Bot
from langbot.pkg.platform.native_clients import NativeClientRegistry
from langbot.pkg.platform.native_ingress import _hash
from langbot.pkg.platform.native_runtime import attach_native_runtime
from langbot.pkg.platform.sources.agent_wechat import AgentWeChatAdapter
from tests.integration.platform.native_http_support import native_http_fixture
from tests.unit_tests.platform.test_agent_wechat_source import adapter_fixture

BOT_UUID = '4f8736ca-57d8-450a-859e-a7ce9e3d34a8'


async def actual_wechat_core_fixture(manager, source_fixture, tmp_path):
    get, owner, change, config, path = source_fixture
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
    # Controlled synthetic-source approval; not a production TOFU registration.
    expected = dict(
        kind='wechat-owner-v1',
        grant={key: proof['grant'][key] for key in ('source', 'scope', 'principal', 'workspace', 'agent', 'revision')},
        binding=proof['binding'],
    )
    async with manager.tenant_uow('workspace-a') as uow:
        await uow.session.execute(
            sa.insert(Bot).values(
                uuid=BOT_UUID,
                workspace_uuid='workspace-a',
                name='vertical',
                description='',
                adapter='agent-wechat',
                adapter_config=adapter_config,
                enable=True,
            )
        )
    reader = next(item for item in config['native']['readers'] if item['principal'] == 'metadata')
    secret = tmp_path / 'metadata-registry-secret'
    secret.write_text(Path(reader['token_file']).read_text())
    secret.chmod(0o600)

    async def runtime_factory(ap, context, bot_uuid, observed_binding):
        assert observed_binding == binding
        entries = [
            dict(
                credential_ref=adapter_config['credential_ref'],
                workspace_uuid='workspace-a',
                bot_uuid=bot_uuid,
                binding_sha256=_hash(asdict(binding)),
                enabled=True,
                provider='agent-wechat-owner-v1',
                secret_file=secret.name,
                endpoint=f'http://127.0.0.1:{config["listen_port"]}',
            )
        ]
        ap.native_source_client_factory = NativeClientRegistry(ap, entries, secret_root=str(tmp_path))
        runtime = SimpleNamespace(
            ap=ap,
            adapter=adapter,
            execution_context=context,
            workspace_uuid='workspace-a',
            bot_entity=SimpleNamespace(uuid=bot_uuid),
            assert_execution_active=AsyncMock(),
        )
        await attach_native_runtime(runtime)
        return runtime

    fixture = await native_http_fixture(
        manager,
        binding=binding,
        source_authority=expected,
        flavor='native_wechat',
        bot_uuid=BOT_UUID,
        runtime_factory=runtime_factory,
    )
    fixture.owner_source_fixture = source_fixture
    fixture.source_adapter = adapter
    return fixture
