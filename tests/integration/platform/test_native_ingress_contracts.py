"""Native ingress contracts against LangBot's real scoped SQLite transactions."""

from types import SimpleNamespace
import logging

import pytest
import pytest_asyncio
import sqlalchemy as sa

from langbot.pkg.core import app as _app  # noqa: F401
from langbot.pkg.entity.persistence.base import Base
from langbot.pkg.entity.persistence.bot import Bot
from langbot.pkg.entity.persistence.apikey import ApiKey
from langbot.pkg.entity.persistence.user import User
from langbot.pkg.entity.persistence.workspace import Workspace
from langbot.pkg.entity.persistence.native_ingress import NATIVE_TABLES
from langbot.pkg.persistence.databases.sqlite import SQLiteDatabaseManager
from langbot.pkg.persistence.mgr import PersistenceManager
from langbot.pkg.platform.native_contract import SourceBinding, SourceEnvelope, SourcePage
from langbot.pkg.platform.native_ingress import NativeIngressStore, _hash


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
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[User.__table__, Workspace.__table__, Bot.__table__, ApiKey.__table__, *NATIVE_TABLES]))
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
    return router_event(record,version=version)


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
                        version='v1', status='accepted', receipt_id=_hash(['langbot-router-receipt-v1','test-source','native-1','v1']))
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
    changed = router_event()
    changed.payload['origin'] = 'catchup'
    with pytest.raises(ValueError, match='version_conflict'):
        await ingress.accept_page(SourcePage('11','12',(event('native-2'),changed)),authorize=allowed)
    assert await ingress.checkpoint() == '11'
    assert len((await ingress.accepted_events(authorize=allowed))['items']) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('phase,committed', [('before',False),('after',True)])
async def test_real_process_death_at_transaction_boundary(manager, phase, committed):
    import asyncio
    import pathlib
    import subprocess
    import sys
    ingress = store(manager)
    await ingress.initialize('10')
    path = manager.ap.instance_config.data['database']['sqlite']['path']
    result = await asyncio.to_thread(subprocess.run, [sys.executable,
        str(pathlib.Path(__file__).with_name('native_crash_worker.py')),path,phase], capture_output=True, timeout=30)
    assert result.returncode == (32 if committed else 31), result.stderr.decode()
    assert await ingress.checkpoint() == ('11' if committed else '10')
    assert len((await ingress.accepted_events(authorize=allowed))['items']) == int(committed)


@pytest.mark.asyncio
async def test_process_death_during_send_never_reissues_unknown_operation(manager):
    import asyncio
    import pathlib
    import subprocess
    import sys
    ingress = store(manager)
    await ingress.initialize('10')
    path = manager.ap.instance_config.data['database']['sqlite']['path']
    result = await asyncio.to_thread(subprocess.run, [sys.executable,
        str(pathlib.Path(__file__).with_name('native_crash_worker.py')),path,'sending'],capture_output=True,timeout=30)
    assert result.returncode == 33, result.stderr.decode()
    provider = Provider()
    unknown = await ingress.send('crashed-host-action','group','chat-a',{'text':'test'},provider=provider,authorize=allowed)
    assert unknown['status'] == 'unknown'
    assert provider.calls == [] and len(provider.lookups) == 1


@pytest.mark.asyncio
async def test_router_initial_null_cursor_and_empty_poll_do_not_pin_feed(manager):
    ingress = store(manager)
    await ingress.initialize('10')
    empty = await ingress.read_router_page(None, authorize=allowed)
    assert empty['previous_cursor'] is None and empty['next_cursor'] == '0' and empty['items'] == []
    await ingress.accept_page(SourcePage('10','11',(router_event(),)),authorize=allowed)
    page = await ingress.read_router_page(None, authorize=allowed)
    assert page['previous_cursor'] is None and page['next_cursor'] == '1'
    assert await ingress.read_router_page(None, authorize=allowed) == page
    receipt = dict(event_id=page['items'][0]['event_id'],source='test-source',record='native-1',version='v1',
                   status='accepted',receipt_id=_hash(['langbot-router-receipt-v1','test-source','native-1','v1']))
    ack = await ingress.ack_router_page(page['page_id'],page['page_sha256'],[receipt],authorize=allowed)
    assert ack == dict(status='accepted',page_id=page['page_id'],next_cursor='1')
    assert (await ingress.read_router_page('1',authorize=allowed))['items'] == []
    assert await ingress.lookup_router_receipt('unknown-event',authorize=allowed) is None


@pytest.mark.asyncio
async def test_native_runtime_has_no_unregistered_authority_or_legacy_egress():
    from langbot.pkg.platform.native_runtime import NativeAdapterGuard, attach_native_runtime
    from langbot.pkg.api.http.controller.groups.native_ingress import NativeIngressRouterGroup
    assert NativeIngressRouterGroup.path == '/api/v1/native-ingress'
    adapter = SimpleNamespace(native_source_binding=lambda: SourceBinding('source','account','scope','r1'))
    guard = NativeAdapterGuard(adapter)
    for method in ('send_message','reply_message','send_streaming_message','reply_streaming_message','reply_message_chunk','create_message_card'):
        with pytest.raises(PermissionError,match='host_operation_required'):
            await getattr(guard, method)('anything')
    assert guard.get_supported_apis()==[]
    assert await guard.is_stream_output_supported() is False
    runtime = SimpleNamespace(ap=SimpleNamespace(native_ingress_authorizer_factory=42))
    with pytest.raises(PermissionError,match='authorizer_not_configured'):
        await attach_native_runtime(runtime)


def context(*, workspace='workspace-a', principal_type='account', key=None, permissions=None):
    from langbot.pkg.api.http.context import RequestContext, PrincipalContext, WorkspaceContext, PrincipalType
    return RequestContext(instance_uuid='test-'+workspace,placement_generation=1,request_id='synthetic-request',
        auth_type='test',principal=PrincipalContext(PrincipalType(principal_type),account_uuid='operator' if principal_type=='account' else None,
                                                  api_key_uuid=key),
        workspace=WorkspaceContext(workspace,None,'owner',frozenset(permissions if permissions is not None else ['api_key.manage','resource.manage'])))


async def key_fixture(manager, *, uuid='dedicated-key', scopes=None, status='active', expires_at=None):
    async with manager.tenant_uow('workspace-a') as uow:
        uow.session.add(ApiKey(uuid=uuid,workspace_uuid='workspace-a',name='synthetic',key_hash=_hash(uuid),
                              scopes=[] if scopes is None else scopes,status=status,expires_at=expires_at))


@pytest.mark.asyncio
async def test_receiver_registration_is_exact_current_and_rejects_privileged_or_self_reported_identity(manager):
    from datetime import datetime, timedelta
    from langbot.pkg.platform.native_access import NativeAuthorization, register_native_access
    await key_fixture(manager)
    ingress = store(manager)
    ap = SimpleNamespace(persistence_mgr=manager)
    registration = dict(receiver_id='unique-host',receiver_api_key_uuid='dedicated-key')
    disabled = await register_native_access(ap,context(),'bot-workspace-a',ingress.binding,**registration)
    assert disabled['source_enabled'] is False and disabled['receiver_enabled'] is False
    auth = NativeAuthorization(ap)
    receiver = context(principal_type='api_key',key='dedicated-key',permissions=[])
    with pytest.raises(PermissionError):
        await auth.receiver_factory(receiver,'bot-workspace-a',ingress.binding)
    await register_native_access(ap,context(),'bot-workspace-a',ingress.binding,**registration,source_enabled=True,receiver_enabled=True,expected_revision=1)
    authorize = await auth.receiver_factory(receiver,'bot-workspace-a',ingress.binding)
    await authorize()
    for bad_context in [context(),context(principal_type='api_key',key='global-oss-api-key'),
                        context(principal_type='api_key',key='legacy-oss-api-key'),
                        context(principal_type='api_key',key='other-key'),
                        context(workspace='workspace-b',principal_type='api_key',key='dedicated-key')]:
        with pytest.raises(PermissionError):
            await auth.receiver_factory(bad_context,'bot-workspace-a',ingress.binding)
    for binding in [SourceBinding('other','account-a','chat-a','grant-v1',False),
                    SourceBinding('test-source','other','chat-a','grant-v1',False),
                    SourceBinding('test-source','account-a','other','grant-v1',False),
                    SourceBinding('test-source','account-a','chat-a','new-revision',False),
                    SourceBinding('test-source','account-a','chat-a','grant-v1',True)]:
        with pytest.raises(PermissionError):
            await auth.receiver_factory(receiver,'bot-workspace-a',binding)
    with pytest.raises(PermissionError):
        await auth.receiver_factory(receiver,'bot-workspace-b',ingress.binding)
    for values in [dict(status='revoked'),dict(status='active',expires_at=datetime.now()-timedelta(seconds=1)),
                   dict(status='active',expires_at=None,scopes=['runtime.operate'])]:
        async with manager.tenant_uow('workspace-a') as uow:
            await uow.session.execute(sa.update(ApiKey).where(ApiKey.uuid=='dedicated-key').values(**values))
        with pytest.raises(PermissionError):
            await authorize()


@pytest.mark.asyncio
async def test_source_factory_and_transaction_fence_block_revoke_after_preflight(manager):
    from langbot.pkg.platform.native_access import NativeAuthorization, register_native_access
    ap = SimpleNamespace(persistence_mgr=manager)
    ingress = store(manager)
    await register_native_access(ap,context(),'bot-workspace-a',ingress.binding,source_enabled=True)
    async def facts():
        return dict(source='test-source',account='account-a',scope='chat-a',revision='grant-v1')
    authorize = await NativeAuthorization(ap).source_factory(context(),'bot-workspace-a',ingress.binding,
                                                            SimpleNamespace(native_connection_facts=facts,native_authority_kind="account-session"))
    await authorize()
    fenced = NativeIngressStore(manager,workspace_uuid='workspace-a',bot_uuid='bot-workspace-a',binding=ingress.binding,
                                authorization_fence=authorize.native_fence)
    await fenced.initialize('10')
    async def revoke_after_preflight():
        await authorize()
        await register_native_access(ap,context(),'bot-workspace-a',ingress.binding,source_enabled=False,expected_revision=1)
    with pytest.raises(PermissionError,match='registration_revoked'):
        await fenced.accept_page(SourcePage('10','11',(event(),)),authorize=revoke_after_preflight)
    assert await ingress.checkpoint() == '10'
    assert (await ingress.accepted_events(authorize=allowed))['items'] == []


@pytest.mark.asyncio
async def test_registration_requires_actual_account_and_both_management_permissions(manager):
    from langbot.pkg.platform.native_access import register_native_access
    ap = SimpleNamespace(persistence_mgr=manager)
    for bad in [context(principal_type='api_key',key='an-owner-key'),context(permissions=['api_key.manage']),
                context(permissions=['resource.manage'])]:
        with pytest.raises(Exception):
            await register_native_access(ap,bad,'bot-workspace-a',store(manager).binding)


@pytest.mark.asyncio
async def test_native_schema_upgrade_from_0037_and_non_destructive_application_rollback(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine
    from langbot.pkg.persistence.alembic_runner import run_alembic_upgrade,run_alembic_stamp,run_alembic_downgrade,get_alembic_current
    engine = create_async_engine('sqlite+aiosqlite:///'+str(tmp_path/'migration.db'))
    try:
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.create_all(c,tables=[User.__table__,Workspace.__table__,Bot.__table__]))
        await run_alembic_stamp(engine,'0037_metadata_text')
        await run_alembic_upgrade(engine)
        assert await get_alembic_current(engine) == 'zimu_native_ingress_v1'
        async with engine.begin() as conn:
            names = await conn.run_sync(lambda c: set(sa.inspect(c).get_table_names()))
            assert {table.name for table in NATIVE_TABLES} <= names
        await run_alembic_downgrade(engine,'0037_metadata_text')
        assert await get_alembic_current(engine) == '0037_metadata_text'
        await run_alembic_upgrade(engine)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_registration_compare_and_set_prevents_stale_admin_overwrite(manager):
    from langbot.pkg.platform.native_access import register_native_access
    ap = SimpleNamespace(persistence_mgr=manager)
    binding = store(manager).binding
    first = await register_native_access(ap,context(),'bot-workspace-a',binding)
    second = await register_native_access(ap,context(),'bot-workspace-a',binding,expected_revision=first['source_revision'])
    assert second['source_revision'] == 2
    with pytest.raises(ValueError,match='revision_conflict'):
        await register_native_access(ap,context(),'bot-workspace-a',binding,expected_revision=1,source_enabled=True)


@pytest.mark.asyncio
async def test_first_registration_uses_persistent_disabled_bot_without_runtime_or_credentials(manager):
    from langbot.pkg.platform.native_access import registration_binding, register_native_access
    class Adapter:
        def __init__(self,config,logger):
            assert config == {}
        def native_source_binding(self):
            return SourceBinding('test-source','account-a','chat-a','grant-v1',False)
    ap = SimpleNamespace(persistence_mgr=manager,platform_mgr=SimpleNamespace(adapter_dict={'test':Adapter}))
    binding = await registration_binding(ap,context(),'bot-workspace-a')
    result = await register_native_access(ap,context(),'bot-workspace-a',binding)
    assert result['source_enabled'] is False
    assert result['binding_sha256'] == store(manager).binding_hash


@pytest.mark.asyncio
async def test_poison_router_change_is_rejected_before_source_ack_and_following_page_survives(manager):
    ingress = store(manager)
    await ingress.initialize('10')
    for poison in [SourceEnvelope('native-1','v1','source:message/native-1/v1','created',{}),
                   SourceEnvelope('native-1','v1','source:message/native-1/v1','created',{'change':{}})]:
        with pytest.raises(ValueError,match='contract_invalid'):
            await ingress.accept_page(SourcePage('10','11',(poison,)),authorize=allowed)
        assert await ingress.checkpoint() == '10'
        assert (await ingress.accepted_events(authorize=allowed))['items'] == []
    await ingress.accept_page(SourcePage('10','11',(event(),)),authorize=allowed)
    assert (await ingress.read_router_page(None,authorize=allowed))['items'][0]['envelope']['record']=='native-1'


@pytest.mark.asyncio
async def test_disabled_bot_registration_http_bootstrap_does_not_need_running_adapter(manager, monkeypatch):
    import quart
    from langbot.pkg.api.http.controller.groups.native_ingress import NativeIngressRouterGroup
    class Adapter:
        def __init__(self,config,logger):
            pass
        def native_source_binding(self):
            return SourceBinding('test-source','account-a','chat-a','grant-v1',False)
    ap=SimpleNamespace(persistence_mgr=manager,platform_mgr=SimpleNamespace(adapter_dict={'test':Adapter}))
    web=quart.Quart('native-bootstrap')
    group=NativeIngressRouterGroup(ap,web)
    async def no_support(*args): return None
    async def account(*args): return ({},'operator')
    async def account_context(*args): return context()
    monkeypatch.setattr(group,'_authenticate_support_admin',no_support)
    monkeypatch.setattr(group,'_authenticate_account',account)
    monkeypatch.setattr(group,'_resolve_account_context',account_context)
    await group.initialize()
    response=await web.test_client().put('/api/v1/native-ingress/bot-workspace-a/registration',
        headers={'Authorization':'Bearer synthetic-test-identity'},json={})
    assert response.status_code==200,await response.get_json()
    body=await response.get_json()
    assert body['data']['source_enabled'] is False and body['data']['receiver_enabled'] is False


@pytest.mark.asyncio
async def test_quarantine_gap_only_page_is_durable_ackable_and_does_not_starve_later_change(manager):
    ingress = store(manager)
    await ingress.initialize('10')
    unresolved = SourceEnvelope('unknown-event','v1','source:unresolved/unknown-event','unresolved',
        {'native_unresolved':{'event_kind':'media.ready','event_cursor':'11','reason':'unknown_asset','asset_id':'asset-1'},
         'dispatch_eligible':False})
    await ingress.accept_page(SourcePage('10','11',(unresolved,)),authorize=allowed)
    page=await ingress.read_router_page(None,authorize=allowed)
    assert page['items']==[] and page['gaps']==[dict(event_id=_hash([ingress.source_id,'unknown-event','v1']),
        reason='native_unresolved',source='test-source',scope='chat-a')]
    assert await ingress.read_router_page(None,authorize=allowed)==page
    ack=await ingress.ack_router_page(page['page_id'],page['page_sha256'],[],authorize=allowed)
    assert ack['next_cursor']=='1'
    assert await ingress.lookup_router_receipt(page['gaps'][0]['event_id'],authorize=allowed) is None
    await ingress.accept_page(SourcePage('11','12',(event(),)),authorize=allowed)
    next_page=await ingress.read_router_page('1',authorize=allowed)
    assert len(next_page['items'])==1 and next_page['gaps']==[]


@pytest.mark.asyncio
@pytest.mark.parametrize('location', ['payload','saved','native'])
async def test_unrecognized_metadata_containers_are_not_persisted(manager,location):
    ingress=store(manager)
    await ingress.initialize('10')
    candidate=router_event()
    if location=='payload': candidate.payload['summary']='untrusted arbitrary text'
    elif location=='saved': candidate.payload['change']['saved']['source_token']='not-a-real-secret'
    else: candidate.payload['native']={'note':'untrusted arbitrary text'}
    with pytest.raises(ValueError,match='metadata_only'):
        await ingress.accept_page(SourcePage('10','11',(candidate,)),authorize=allowed)
    assert await ingress.checkpoint()=='10'
    assert (await ingress.accepted_events(authorize=allowed))['items']==[]


@pytest.mark.asyncio
async def test_empty_polls_need_no_ack_and_do_not_accumulate_pages(manager):
    from langbot.pkg.entity.persistence.native_ingress import NativePage
    ingress=store(manager)
    await ingress.initialize('10')
    for cursor in [None,'0','0']:
        page=await ingress.read_router_page(cursor,authorize=allowed)
        assert page['items']==[] and page['gaps']==[] and page['next_cursor']=='0'
    async with manager.tenant_uow('workspace-a') as uow:
        assert (await uow.session.execute(sa.select(sa.func.count()).select_from(NativePage))).scalar()==0
    await ingress.accept_page(SourcePage('10','11',(event(),)),authorize=allowed)
    assert len((await ingress.read_router_page('0',authorize=allowed))['items'])==1


@pytest.mark.asyncio
async def test_registered_original_reader_checks_latest_revision_and_recall_before_provider(manager):
    from langbot.pkg.platform.native_access import register_native_access
    from langbot.pkg.platform.native_originals import read_native_original
    await key_fixture(manager)
    ingress=store(manager)
    await ingress.initialize('10')
    await ingress.accept_page(SourcePage('10','11',(event(),)),authorize=allowed)
    class Adapter:
        calls=0
        async def native_original(self,ref,version):
            self.calls+=1
            return dict(reference=ref,version=version,text='你好\nsource original')
    adapter=Adapter()
    runtime=SimpleNamespace(native_ingress_store=ingress,adapter=adapter,workspace_uuid='workspace-a',
        bot_entity=SimpleNamespace(uuid='bot-workspace-a'),assert_execution_active=allowed,native_source_authorize=allowed)
    async def get_bot(ctx,bot_uuid):
        return runtime if ctx.workspace_uuid=='workspace-a' and bot_uuid=='bot-workspace-a' else None
    ap=SimpleNamespace(persistence_mgr=manager,platform_mgr=SimpleNamespace(get_bot_by_uuid=get_bot))
    await register_native_access(ap,context(),'bot-workspace-a',ingress.binding,
        receiver_id='unique-host',receiver_api_key_uuid='dedicated-key',source_enabled=True,receiver_enabled=True)
    receiver=context(principal_type='api_key',key='dedicated-key',permissions=[])
    result=await read_native_original(ap,receiver,'bot-workspace-a','original','source:message/native-1/v1','v1')
    assert result['text']=='你好\nsource original' and adapter.calls==1
    recalled=router_event(version='v2',kind='recalled',eligible=False)
    await ingress.accept_page(SourcePage('11','12',(recalled,)),authorize=allowed)
    with pytest.raises(PermissionError,match='changed_or_recalled'):
        await read_native_original(ap,receiver,'bot-workspace-a','original','source:message/native-1/v1','v1')
    assert adapter.calls==1


@pytest.mark.asyncio
async def test_client_reference_is_exact_and_never_allows_arbitrary_files_or_disabled_grants(manager,tmp_path):
    import dataclasses
    from langbot.pkg.platform.native_access import register_native_access
    from langbot.pkg.platform.native_clients import NativeClientRegistry
    binding=store(manager).binding
    ap=SimpleNamespace(persistence_mgr=manager)
    await register_native_access(ap,context(),'bot-workspace-a',binding,source_enabled=True)
    secret=tmp_path/'wx-read'
    secret.write_text('synthetic-test-only-value')
    secret.chmod(0o400)
    entry=dict(credential_ref='read-fixture',enabled=True,workspace_uuid='workspace-a',bot_uuid='bot-workspace-a',
        binding_sha256=_hash(dataclasses.asdict(binding)),provider='fixture',endpoint='https://fixture.invalid/v1',secret_file='wx-read')
    calls=[]
    def constructor(endpoint,credential,account):
        calls.append((endpoint,account))
        assert credential=='synthetic-test-only-value'
        return SimpleNamespace(account_id=account)
    registry=NativeClientRegistry(ap,[entry],constructors={'fixture':constructor},secret_root=str(tmp_path))
    result=await registry(context(),'bot-workspace-a',binding,'read-fixture')
    assert result.account_id=='account-a' and len(calls)==1
    for modification in [dict(enabled=False),dict(bot_uuid='other-bot'),dict(binding_sha256='0'*64),
                         dict(workspace_uuid='workspace-b'),dict(secret_file='../wx-read')]:
        invalid=NativeClientRegistry(ap,[dict(entry,**modification)],constructors={'fixture':constructor},secret_root=str(tmp_path))
        with pytest.raises(PermissionError):
            await invalid(context(),'bot-workspace-a',binding,'read-fixture')
    secret.chmod(0o444)
    with pytest.raises(PermissionError,match='permissions_invalid'):
        await registry(context(),'bot-workspace-a',binding,'read-fixture')
    secret.unlink()
    (tmp_path/'other-file').write_text('not-read')
    secret.symlink_to(tmp_path/'other-file')
    with pytest.raises(PermissionError,match='mount_unavailable'):
        await registry(context(),'bot-workspace-a',binding,'read-fixture')
    assert len(calls)==1


@pytest.mark.asyncio
async def test_default_registry_does_not_move_account_wide_wx_credentials_into_langbot(manager):
    from langbot.pkg.platform.native_access import register_native_access
    from langbot.pkg.platform.native_clients import NativeClientRegistry
    binding=store(manager).binding
    ap=SimpleNamespace(persistence_mgr=manager)
    await register_native_access(ap,context(),'bot-workspace-a',binding,source_enabled=True)
    entry=dict(credential_ref='unscoped-wx',enabled=True,workspace_uuid='workspace-a',bot_uuid='bot-workspace-a',
               binding_sha256=store(manager).binding_hash,provider='agent-wechat-v1',
               endpoint='https://fixture.invalid/v1',secret_file='does-not-exist')
    registry=NativeClientRegistry(ap,[entry])
    with pytest.raises(PermissionError,match='thread_scoped_transport_required'):
        await registry(context(),'bot-workspace-a',binding,'unscoped-wx')


@pytest.mark.asyncio
async def test_saved_media_evidence_six_supported_fields_survive_core_without_silent_narrowing(manager):
    ingress=store(manager)
    await ingress.initialize('10')
    candidate=router_event()
    evidence=dict(mediaId='owner-media-1',mediaType='image',sourceMd5='A'*32,
                  contentSha256='b'*64,sourceFileSize=123,contentSize=120)
    candidate.payload['change']['saved']={'message':{'media_evidence':[evidence]}}
    await ingress.accept_page(SourcePage('10','11',(candidate,)),authorize=allowed)
    page=await ingress.read_router_page(None,authorize=allowed)
    assert page['items'][0]['envelope']['saved']['message']['media_evidence']==[evidence]


@pytest.mark.asyncio
@pytest.mark.parametrize('key,value',[('sourceMd5','z'*32),('contentSha256','wrong'),
    ('sourceFileSize',True),('contentSize',-1),('mediaId',{'content':'untrusted'}),('mediaType','a'*513)])
async def test_malformed_media_evidence_is_rejected_before_source_cursor_advance(manager,key,value):
    ingress=store(manager)
    await ingress.initialize('10')
    candidate=router_event()
    candidate.payload['change']['saved']={'message':{'media_evidence':[{key:value}]}}
    with pytest.raises(ValueError,match='metadata_only'):
        await ingress.accept_page(SourcePage('10','11',(candidate,)),authorize=allowed)
    assert await ingress.checkpoint()=='10'


@pytest.mark.asyncio
async def test_owner_gap_has_no_fake_original_and_is_preserved_once_across_good_pages(manager):
    from langbot.pkg.entity.persistence.native_ingress import NativeGap
    ingress=NativeIngressStore(manager,workspace_uuid='workspace-a',bot_uuid='bot-workspace-a',
        binding=store(manager).binding,owner_gap_codes={'history_before_feed_unverified','original_unavailable'})
    await ingress.initialize('10')
    source_receipt=await ingress.accept_page(SourcePage('10','11',(),gaps=('history_before_feed_unverified',)),authorize=allowed)
    page=await ingress.read_router_page(None,authorize=allowed)
    assert page['items']==[] and page['next_cursor']=='1'
    assert page['gaps']==[dict(event_id=source_receipt.event_ids[0],reason='owner_source_gap',source='test-source',
                               scope='chat-a',code='history_before_feed_unverified')]
    assert 'record' not in page['gaps'][0] and 'original' not in page['gaps'][0]
    await ingress.ack_router_page(page['page_id'],page['page_sha256'],[],authorize=allowed)
    await ingress.accept_page(SourcePage('11','12',(event(),),gaps=('history_before_feed_unverified',)),authorize=allowed)
    good_page=await ingress.read_router_page('1',authorize=allowed)
    assert len(good_page['items'])==1 and good_page['gaps']==[]
    async with manager.tenant_uow('workspace-a') as uow:
        unresolved=(await uow.session.execute(sa.select(NativeGap))).scalars().all()
        assert [row.code for row in unresolved]==['history_before_feed_unverified']
    with pytest.raises(ValueError,match='gap_not_supported'):
        await ingress.accept_page(SourcePage('12','13',(),gaps=('arbitrary_external_text',)),authorize=allowed)
    assert await ingress.checkpoint()=='12'


@pytest.mark.asyncio
async def test_migration_rejects_same_columns_without_required_constraints(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine
    from langbot.pkg.persistence.alembic_runner import run_alembic_stamp,run_alembic_upgrade
    engine=create_async_engine('sqlite+aiosqlite:///'+str(tmp_path/'weak.db'))
    try:
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.create_all(c,tables=[User.__table__,Workspace.__table__,Bot.__table__]))
            await conn.exec_driver_sql('''CREATE TABLE native_ingress_sources (
                id VARCHAR(64) PRIMARY KEY NOT NULL,workspace_uuid VARCHAR(36) NOT NULL,bot_uuid VARCHAR(255) NOT NULL,
                binding JSON NOT NULL,binding_sha256 VARCHAR(64) NOT NULL,cursor TEXT,last_source_sequence BIGINT,
                position INTEGER NOT NULL DEFAULT 0,router_cursor INTEGER NOT NULL DEFAULT 0,router_pending_page VARCHAR(64))''')
        await run_alembic_stamp(engine,'0037_metadata_text')
        with pytest.raises(RuntimeError,match='exact type, nullability'):
            await run_alembic_upgrade(engine)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_rejects_unapproved_cascade_on_otherwise_matching_native_source(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine
    from langbot.pkg.persistence.alembic_runner import run_alembic_stamp,run_alembic_upgrade
    engine=create_async_engine('sqlite+aiosqlite:///'+str(tmp_path/'cascade.db'))
    try:
        async with engine.begin() as conn:
            await conn.run_sync(lambda c: Base.metadata.create_all(c,tables=[User.__table__,Workspace.__table__,Bot.__table__]))
            await conn.exec_driver_sql('''CREATE TABLE native_ingress_sources (
                id VARCHAR(64) PRIMARY KEY NOT NULL,workspace_uuid VARCHAR(36) NOT NULL,bot_uuid VARCHAR(255) NOT NULL,
                binding JSON NOT NULL,binding_sha256 VARCHAR(64) NOT NULL,cursor TEXT,last_source_sequence BIGINT,
                position INTEGER NOT NULL DEFAULT 0,router_cursor INTEGER NOT NULL DEFAULT 0,router_pending_page VARCHAR(64),
                UNIQUE(workspace_uuid,id),UNIQUE(workspace_uuid,bot_uuid),
                FOREIGN KEY(workspace_uuid) REFERENCES workspaces(uuid) ON DELETE CASCADE,
                FOREIGN KEY(workspace_uuid,bot_uuid) REFERENCES bots(workspace_uuid,uuid))''')
        await run_alembic_stamp(engine,'0037_metadata_text')
        with pytest.raises(RuntimeError,match='exact type, nullability'):
            await run_alembic_upgrade(engine)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_typed_owner_unresolved_preserves_event_sequence_without_fake_message_reference(manager):
    from langbot.pkg.platform.native_contract import SourceUnresolved
    from langbot.pkg.entity.persistence.native_ingress import NativeGap
    ingress=store(manager)
    await ingress.initialize('owner-before')
    unresolved=SourceUnresolved('owner-event-9',9,'wx-raw-3','test-source','chat-a')
    await ingress.accept_page(SourcePage('owner-before','owner-after',(),unresolved=(unresolved,)),authorize=allowed)
    assert await ingress.source_sequence_checkpoint()==9
    page=await ingress.read_router_page(None,authorize=allowed)
    assert page['items']==[] and page['gaps']==[dict(event_id=_hash([ingress.source_id,'native-unresolved','owner-event-9']),
        reason='native_unresolved',source='test-source',scope='chat-a')]
    await ingress.ack_router_page(page['page_id'],page['page_sha256'],[],authorize=allowed)
    restarted=store(manager)
    assert await restarted.checkpoint()=='owner-after'
    assert await restarted.source_sequence_checkpoint()==9
    async with manager.tenant_uow('workspace-a') as uow:
        row=(await uow.session.execute(sa.select(NativeGap))).scalar_one()
        assert row.owner_event_id=='owner-event-9' and row.source_sequence==9 and row.upstream_cursor=='wx-raw-3'
        assert not hasattr(row,'record') and not hasattr(row,'reference')
