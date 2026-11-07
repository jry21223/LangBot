"""Native ingress contracts against LangBot's real scoped SQLite transactions."""

from types import SimpleNamespace
import logging

import pytest
import pytest_asyncio
import sqlalchemy as sa

from langbot.pkg.core import app as _app  # noqa: F401
from langbot.pkg.entity.persistence.base import Base
from langbot.pkg.entity.persistence.bot import Bot
from langbot.pkg.entity.persistence.user import User
from langbot.pkg.entity.persistence.workspace import Workspace
from langbot.pkg.entity.persistence.native_ingress import NATIVE_TABLES
from langbot.pkg.persistence.databases.sqlite import SQLiteDatabaseManager
from langbot.pkg.persistence.mgr import PersistenceManager
from langbot.pkg.platform.native_contract import SourceBinding, SourceEnvelope, SourcePage
from langbot.pkg.platform.native_ingress import NativeIngressStore


@pytest_asyncio.fixture
async def manager(tmp_path):
    ap = SimpleNamespace(
        instance_config=SimpleNamespace(data={'database': {'sqlite': {'path': str(tmp_path / 'native.db')}}}),
        logger=logging.getLogger('native-test'),
    )
    manager = PersistenceManager(ap)
    manager.db = SQLiteDatabaseManager(ap)
    await manager.db.initialize()
    async with manager.get_db_engine().begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[User.__table__, Workspace.__table__, Bot.__table__, *NATIVE_TABLES]))
        for workspace in ['workspace-a', 'workspace-b']:
            await conn.execute(sa.insert(Workspace).values(uuid=workspace, instance_uuid='test-'+workspace, name=workspace, slug=workspace))
            await conn.execute(sa.insert(Bot).values(uuid='bot-'+workspace, workspace_uuid=workspace, name='test', description='', adapter='test', adapter_config={}, enable=False))
    yield manager
    await manager.shutdown()


def store(manager, *, read_only=False, workspace='workspace-a'):
    return NativeIngressStore(manager, workspace_uuid=workspace, bot_uuid='bot-'+workspace,
        binding=SourceBinding('test-source', 'account-a', 'chat-a', 'grant-v1', read_only))


async def allowed():
    return None


def event(record='native-1', *, version='v1'):
    return SourceEnvelope(record, version, 'source:message/'+record+'/'+version, 'created',
                          {'native_id': record, 'dispatch_eligible': True})


@pytest.mark.asyncio
async def test_page_receipt_replays_after_restart_without_duplicate_events(manager):
    ingress = store(manager)
    await ingress.initialize(initial_cursor='10')
    page = SourcePage('10', '11', (event(),))
    first = await ingress.accept_page(page, authorize=allowed)
    recreated = store(manager)
    await recreated.initialize(initial_cursor='10')
    second = await recreated.accept_page(page, authorize=allowed)
    assert second == first
    assert await recreated.checkpoint() == '11'
    assert [e['envelope']['record'] for e in await recreated.pending_events()] == ['native-1']


def router_event(record='native-1', *, version='v1', kind='created', eligible=True):
    change = dict(source='test-source', scope='chat-a', record=record, version=version,
                  sequence=1, kind=kind, business_at='2026-11-08T00:00:00Z',
                  observed_at='2026-11-08T00:00:01Z', original='source:message/'+record+'/'+version,
                  projection_hash='a'*64, goal_reference=None, saved={})
    return SourceEnvelope(record, version, change['original'], kind,
                          {'change': change, 'dispatch_eligible': eligible})


@pytest.mark.asyncio
async def test_router_page_is_frozen_until_exact_host_receipts_commit(manager):
    ingress = store(manager)
    await ingress.initialize('10')
    receipt = await ingress.accept_page(SourcePage('10', '11', (
        router_event(), router_event('native-2', kind='recalled', eligible=False),
    )), authorize=allowed)
    page = await ingress.read_router_page('0', 1, authorize=allowed)
    assert page['next_cursor'] == '1'
    assert len(page['items']) == 1
    assert await ingress.read_router_page('0', 100, authorize=allowed) == page
    with pytest.raises(ValueError, match='ack_pending'):
        await ingress.read_router_page('1', authorize=allowed)
    with pytest.raises(ValueError, match='incomplete'):
        await ingress.ack_router_page(page['page_id'], page['page_sha256'], [], authorize=allowed)
    host_receipt = dict(event_id=receipt.event_ids[0], source='test-source', record='native-1',
                        version='v1', status='accepted', receipt_id='host-committed-1')
    result = await ingress.ack_router_page(page['page_id'], page['page_sha256'], [host_receipt], authorize=allowed)
    restarted = store(manager)
    assert await restarted.ack_router_page(page['page_id'], page['page_sha256'], [host_receipt], authorize=allowed) == result
    assert await restarted.lookup_router_receipt(receipt.event_ids[0], authorize=allowed) == host_receipt
    next_page = await restarted.read_router_page('1', authorize=allowed)
    assert next_page['items'][0]['envelope']['kind'] == 'recalled'
    assert 'dispatch_eligible' not in next_page['items'][0]['envelope']
    assert (await restarted.accepted_events(authorize=allowed))['next_position'] == 2


@pytest.mark.asyncio
async def test_binding_workspace_and_revoked_authority_cannot_read_receipts(manager):
    ingress = store(manager)
    await ingress.initialize('10')
    await ingress.accept_page(SourcePage('10','11',(router_event(),)), authorize=allowed)
    async def denied():
        raise PermissionError('revoked')
    for operation in (
        ingress.accepted_events(authorize=denied),
        ingress.read_router_page('0', authorize=denied),
        ingress.lookup_router_receipt('unknown', authorize=denied),
    ):
        with pytest.raises(PermissionError, match='revoked'):
            await operation
    other = store(manager, workspace='workspace-b')
    await other.initialize('10')
    assert (await other.accepted_events(authorize=allowed))['items'] == []
    changed = NativeIngressStore(manager, workspace_uuid='workspace-a', bot_uuid='bot-workspace-a',
        binding=SourceBinding('test-source','account-a','chat-a','grant-v2',False))
    with pytest.raises(ValueError, match='binding_missing_or_changed'):
        await changed.initialize('10')


class Provider:
    def __init__(self):
        self.calls = []
        self.lookups = []
        self.result = None

    async def send(self, key, target_type, target_id, payload):
        self.calls.append((key, target_type, target_id, payload))
        # Simulate acceptance followed by a lost response.
        self.result = dict(status='accepted', id='send-1', server_id='9007199254740993')
        raise ConnectionError('response lost')

    async def lookup(self, key):
        self.lookups.append(key)
        return self.result


@pytest.mark.asyncio
async def test_unknown_send_resumes_same_key_without_resend_and_new_operation_is_distinct(manager):
    ingress = store(manager)
    await ingress.initialize('10')
    provider = Provider()
    first = await ingress.send('host-action-1', 'group', 'chat-a', {'text':'same'}, provider=provider, authorize=allowed)
    assert first['status'] == 'unknown'
    restarted = store(manager)
    result = await restarted.send('host-action-1', 'group', 'chat-a', {'text':'same'}, provider=provider, authorize=allowed)
    assert result['status'] == 'accepted'
    assert result['server_id'] == '9007199254740993'
    assert len(provider.calls) == 1
    assert provider.lookups == [provider.calls[0][0]]
    with pytest.raises(ValueError, match='operation_conflict'):
        await restarted.send('host-action-1', 'group', 'chat-a', {'text':'different'}, provider=provider, authorize=allowed)
    await restarted.send('host-action-2', 'group', 'chat-a', {'text':'same'}, provider=provider, authorize=allowed)
    assert len(provider.calls) == 2
    assert provider.calls[0][0] != provider.calls[1][0]


@pytest.mark.asyncio
async def test_archive_readonly_gate_blocks_send_before_provider_or_authorization(manager):
    ingress = store(manager, read_only=True)
    await ingress.initialize('10')
    provider = Provider()
    with pytest.raises(ValueError, match='receive_only'):
        await ingress.send('host-action-1','group','chat-a',{'text':'x'},provider=provider,authorize=allowed)
    assert not provider.calls and not provider.lookups


@pytest.mark.asyncio
async def test_invalid_page_rolls_back_events_and_cursor(manager):
    ingress = store(manager)
    await ingress.initialize('10')
    invalid = SourceEnvelope('private','v1','source:private','created',{'content':'must stay at source'})
    with pytest.raises(ValueError, match='metadata_only'):
        await ingress.accept_page(SourcePage('10','11',(event(),invalid)),authorize=allowed)
    assert await ingress.checkpoint() == '10'
    assert (await ingress.accepted_events(authorize=allowed))['items'] == []
    await ingress.accept_page(SourcePage('10','11',(event(),)),authorize=allowed)
    changed = SourceEnvelope('native-1','v1','source:message/native-1/v1','created',{'native_id':'changed'})
    with pytest.raises(ValueError, match='version_conflict'):
        await ingress.accept_page(SourcePage('11','12',(event('native-2'),changed)),authorize=allowed)
    assert await ingress.checkpoint() == '11'
    assert len((await ingress.accepted_events(authorize=allowed))['items']) == 1
