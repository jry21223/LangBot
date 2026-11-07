"""Frozen real Host/workflow -> real Core HTTP authentication/policy/SQLite.

Source grants/descriptors and admission/model are synthetic boundaries. The
Host lock, workflow transaction, Core auth, receiver ACL and ACK are real code.
"""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from langbot.pkg.entity.persistence.bot import Bot

from langbot.pkg.platform.native_contract import SourceBinding, SourceEnvelope, SourcePage
from tests.integration.platform.native_http_support import native_http_fixture
from tests.integration.platform.wechat_owner_public_support import real_wechat_owner as _real_wechat_owner

pytest_plugins = ['tests.integration.platform.test_native_ingress_contracts']
real_wechat_owner = _real_wechat_owner


class CoreASGITransport:
    def __init__(self, fixture, loop, *, lose_ack=False, key='narrow'):
        self.fixture, self.loop, self.lose_ack, self.key = fixture, loop, lose_ack, key
        self.calls = 0

    def consumer_binding(self):
        return self.fixture.consumer_binding()

    def request(self, method, suffix, payload=None):
        async def invoke():
            response = await self.fixture.request(method,
                '/api/v1/native-ingress/' + self.fixture.domain['bot_uuid'] + '/router/' + suffix,
                key=self.key, json=payload)
            if response.status_code != 200:
                raise OSError('core_http_' + str(response.status_code))
            return (await response.get_json())['data']
        self.calls += 1
        return asyncio.run_coroutine_threadsafe(invoke(), self.loop).result(timeout=20)

    def read_router_page(self, cursor, limit, *, authorize):
        query = '?limit=' + str(limit)
        if cursor is not None:
            query += '&cursor=' + cursor
        return self.request('GET', 'changes' + query)

    def ack_router_page(self, page_id, page_sha256, receipts, *, authorize):
        proof = self.request('POST', 'ack', dict(page_id=page_id, page_sha256=page_sha256, receipts=receipts))
        if self.lose_ack:
            self.lose_ack = False
            raise OSError('synthetic_ack_response_lost_after_real_commit')
        return proof

    def lookup_router_receipt(self, event_id, *, authorize):
        return self.request('GET', 'receipts/' + event_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(('lose_ack', 'key'), [(False, 'narrow'), (True, 'narrow'), (False, 'unregistered')])
async def test_real_host_core_commit_ack_restart_and_consumer_fence(manager, real_wechat_owner, tmp_path, lose_ack, key):
    # The shared frozen-source fixture verifies every exported file before import.
    import sys
    from pathlib import Path
    import enterprise_agent.host as frozen_host
    sys.path.insert(0, str(Path(frozen_host.__file__).resolve().parents[1] / "tests"))
    from enterprise_agent.host import Host
    from enterprise_agent.producer import Producer
    from enterprise_agent.workflow import Workflow
    from enterprise_agent.native_ingress import NativeIngress
    from test_enterprise_agent_host_1389 import saved_change
    from test_enterprise_agent_1324 import grant

    change = saved_change()
    binding = SourceBinding(change['source'], 'synthetic-corp', change['scope'], 'grant-v1', True)
    envelope = SourceEnvelope(change['record'], change['version'], change['original'], change['kind'],
                              {'change': change, 'dispatch_eligible': True})
    async with manager.tenant_uow("workspace-a") as uow:
        await uow.session.execute(sa.insert(Bot).values(uuid="4f8736ca-57d8-450a-859e-a7ce9e3d34a8",
            workspace_uuid="workspace-a",name="synthetic-host-core",description="",adapter="test",adapter_config={},enable=False))
    fixture = await native_http_fixture(manager, binding=binding,
        bot_uuid='4f8736ca-57d8-450a-859e-a7ce9e3d34a8', flavor='finance',
        first_page=SourcePage('source-before', 'source-after', (envelope,)))
    transport = CoreASGITransport(fixture, asyncio.get_running_loop(), lose_ack=lose_ack, key=key)

    class Owner:
        source, scope = binding.source, binding.scope
        def authorize(self, source, stage, agent=None): return grant(source, stage, agent)
        def authorize_evidence(self, event, stage, agent=None): return grant(event['source'], stage, agent)
        def read_descriptor(self, event, *, include_history=False):
            return dict(reference=change['original'], version=change['version'],
                projection_hash=change['projection_hash'], kind=change['kind'], original_available=True)

    def cycle():
        workflow = Workflow(tmp_path / 'host.sqlite', workspace='test-workspace', coordinator='main-agent',
                            assistants=['finance-assistant'], authorize=grant)
        producer = Producer(workflow, rule_version='fixture', model_version='none')
        host = Host(producer, sources=[NativeIngress(Owner(), transport=transport)],
                    workspace=SimpleNamespace(supports_idempotency=lambda: True), admit=lambda candidate: None)
        try:
            report = host.step(now='2026-11-08T00:00:00Z')
            return report, producer.coverage(binding.source), deepcopy(workflow._read())
        finally:
            host.close()
            workflow.close()

    _, coverage, state = await asyncio.to_thread(cycle)
    if key != 'narrow':
        assert not state['producer']['candidates']
        assert coverage.get('cursor') is None
        assert await fixture.router_cursor() == 0
        assert not coverage['native_ingress'].get('ack_pending', False)
        return
    assert len(state['producer']['candidates']) == 1
    assert coverage['native_ingress']['ack_pending'] is lose_ack
    assert await fixture.router_cursor() == 1  # Even the lost ACK committed exactly once.
    receipt = coverage['native_ingress']['receipts'][0]
    _, coverage, state = await asyncio.to_thread(cycle)
    assert coverage['native_ingress'].get('ack_pending', False) is False
    assert len(state['producer']['candidates']) == 1
    actual = await fixture.request('GET', '/api/v1/native-ingress/' + fixture.domain['bot_uuid'] + '/router/receipts/' + receipt['event_id'])
    assert (await actual.get_json())['data'] == receipt
    before_calls = transport.calls
    transport.fixture.domain['bot_uuid'] = 'f2366bf9-3038-40da-abbb-4274afc2d705'
    report, _, after = await asyncio.to_thread(cycle)
    assert report['sources'][binding.source] == 'native_consumer_conflict'
    assert transport.calls == before_calls and after == state
