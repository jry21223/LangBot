"""Real source HTTP and LangBot scoped SQLite; no external credentials or traffic."""

# Ruff sees pytest fixture injection as a redefinition of the imported fixture.
# ruff: noqa: F811
import threading
from contextlib import contextmanager
from http.server import ThreadingHTTPServer

import pytest

from langbot.pkg.platform.adapters.wecom_archive.adapter import WecomArchiveAdapter
from langbot.pkg.platform.native_ingress import NativeIngressStore
from tests.integration.platform.test_native_ingress_contracts import manager  # noqa: F401
from tests.unit_tests.platform.test_wecom_archive_adapter import FixtureLogger
from tests.unit_tests.platform.test_wecom_archive_projection import archived_page
from tests.unit_tests.platform.test_wecom_archive_reader import SavedSourceHandler


@contextmanager
def archive_adapter():
    server = ThreadingHTTPServer(('127.0.0.1', 0), SavedSourceHandler)
    server.source, server.scope, server.workspace = 'fixture-wecom', 'fixture-chat', 'workspace-a'
    server.page_overrides = archived_page()
    server.page_overrides.pop('previous_cursor')
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    from langbot.pkg.platform.adapters.wecom_archive.reader import HttpSource

    config = {
        'source': 'fixture-wecom',
        'account': 'fixture-corp',
        'scope': 'fixture-chat',
        'credential_ref': 'fixture-metadata-reader',
        'grant_revision': 'grant-v1',
    }
    client = HttpSource(
        f'http://127.0.0.1:{server.server_port}/saved',
        source='fixture-wecom',
        scope='fixture-chat',
        workspace='workspace-a',
        principal='fixture-reader',
        token='fixture-reader-token-000000',
    )
    adapter = WecomArchiveAdapter(config, FixtureLogger())
    adapter.attach_native_client(client)
    try:
        yield server, adapter
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_native_poll_persists_owner_revision_and_router_shape(manager):
    with archive_adapter() as (_, adapter):
        store = NativeIngressStore(
            manager, workspace_uuid='workspace-a', bot_uuid='bot-workspace-a', binding=adapter.native_source_binding()
        )
        await store.initialize()

        async def authorized():
            await adapter.authorize_native_source('workspace-a')

        adapter.attach_native_ingress(store, authorized)
        receipt = await adapter.poll_once()
        assert receipt.cursor == 'cursor-one'
        assert await store.checkpoint() == 'cursor-one'
        page = await store.read_router_page('0', authorize=authorized)
        assert len(page['items']) == 1
        change = page['items'][0]['envelope']
        assert change['record'] == 'message-one'
        assert change['version'] == 'wr-1'
        assert change['saved']['message']['source_seq'] == 19
        assert 'dispatch_eligible' not in change


@pytest.mark.asyncio
async def test_replayed_source_revision_does_not_duplicate_after_adapter_restart(manager):
    with archive_adapter() as (_, adapter):

        async def authorized():
            await adapter.authorize_native_source('workspace-a')

        first = NativeIngressStore(
            manager, workspace_uuid='workspace-a', bot_uuid='bot-workspace-a', binding=adapter.native_source_binding()
        )
        await first.initialize()
        adapter.attach_native_ingress(first, authorized)
        await adapter.poll_once()
        restarted = WecomArchiveAdapter(adapter.config, FixtureLogger())
        restarted.attach_native_client(adapter._source_reader())
        second = NativeIngressStore(
            manager, workspace_uuid='workspace-a', bot_uuid='bot-workspace-a', binding=restarted.native_source_binding()
        )
        await second.initialize()
        restarted.attach_native_ingress(second, authorized)
        await restarted.poll_once()
        events = await second.accepted_events(authorize=authorized)
        assert len(events['items']) == 1
        assert await second.checkpoint() == 'cursor-one'


@pytest.mark.asyncio
async def test_permission_change_prevents_page_commit_and_keeps_watermark(manager):
    from langbot.pkg.platform.adapters.wecom_archive.reader import SourceUnavailable

    with archive_adapter() as (server, adapter):
        server.change_grant_during_read = True
        store = NativeIngressStore(
            manager, workspace_uuid='workspace-a', bot_uuid='bot-workspace-a', binding=adapter.native_source_binding()
        )
        await store.initialize()

        async def authorized():
            await adapter.authorize_native_source('workspace-a')

        adapter.attach_native_ingress(store, authorized)
        with pytest.raises(SourceUnavailable):
            await adapter.poll_once()
        assert await store.checkpoint() is None


@pytest.mark.asyncio
async def test_incomplete_page_keeps_committed_source_watermark(manager):
    from langbot.pkg.platform.adapters.wecom_archive.reader import SourceUnavailable

    with archive_adapter() as (server, adapter):
        server.page_overrides['complete_page'] = False
        store = NativeIngressStore(
            manager, workspace_uuid='workspace-a', bot_uuid='bot-workspace-a', binding=adapter.native_source_binding()
        )
        await store.initialize()

        async def authorized():
            await adapter.authorize_native_source('workspace-a')

        adapter.attach_native_ingress(store, authorized)
        with pytest.raises(SourceUnavailable):
            await adapter.poll_once()
        assert await store.checkpoint() is None
        assert (await store.accepted_events(authorize=authorized))['items'] == []


@pytest.mark.asyncio
async def test_archive_lifecycle_accepts_then_stops_without_a_second_pipeline_path(manager):
    import asyncio

    with archive_adapter() as (_, adapter):
        store = NativeIngressStore(
            manager, workspace_uuid='workspace-a', bot_uuid='bot-workspace-a', binding=adapter.native_source_binding()
        )

        async def authorized():
            await adapter.authorize_native_source('workspace-a')

        adapter.attach_native_ingress(store, authorized)
        await store.initialize()
        task = asyncio.create_task(adapter.run_async())
        try:
            for _ in range(100):
                await asyncio.sleep(0.02)
                if await store.checkpoint() == 'cursor-one':
                    break
            else:
                raise AssertionError('native page was not durably accepted')
            assert await adapter.kill() is True
            await asyncio.wait_for(task, 2)
            assert len((await store.accepted_events(authorize=authorized))['items']) == 1
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_disconnected_owner_keeps_existing_checkpoint(manager):
    from langbot.pkg.platform.adapters.wecom_archive.reader import SourceUnavailable

    with archive_adapter() as (server, adapter):
        store = NativeIngressStore(
            manager, workspace_uuid='workspace-a', bot_uuid='bot-workspace-a', binding=adapter.native_source_binding()
        )
        await store.initialize(initial_cursor='existing-checkpoint')

        async def authorized():
            await adapter.authorize_native_source('workspace-a')

        adapter.attach_native_ingress(store, authorized)
        server.shutdown()
        server.server_close()
        with pytest.raises(SourceUnavailable):
            await adapter.poll_once()
        assert await store.checkpoint() == 'existing-checkpoint'


@pytest.mark.asyncio
async def test_native_connection_facts_require_owner_account_proof():
    from langbot.pkg.platform.adapters.wecom_archive.reader import SourceUnavailable

    with archive_adapter() as (server, adapter):
        with pytest.raises(SourceUnavailable):
            await adapter.native_connection_facts()
        proof = {
            'source': 'fixture-wecom',
            'scope': 'fixture-chat',
            'principal': 'fixture-reader',
            'workspace': 'workspace-a',
            'revision': 'grant-v1',
            'corp_id': 'owner-attested-corp',
            'origin': 'owner_source_binding',
        }
        server.binding_proof = proof
        facts = await adapter.native_connection_facts()
        assert facts['account'] == 'owner-attested-corp'
        assert facts['account'] != adapter.config['account']
        for field, value in [
            ('workspace', 'another-workspace'),
            ('revision', 'old-grant'),
            ('origin', 'caller_config'),
            ('corp_id', ''),
        ]:
            server.binding_proof = proof | {field: value}
            with pytest.raises(SourceUnavailable):
                await adapter.native_connection_facts()
        server.binding_proof = proof
        server.revoke_during_proof = True
        with pytest.raises(SourceUnavailable):
            await adapter.native_connection_facts()


@pytest.mark.asyncio
async def test_owner_gap_cannot_advance_persistent_watermark(manager):
    from langbot.pkg.platform.adapters.wecom_archive.reader import SourceUnavailable

    with archive_adapter() as (server, adapter):
        server.page_overrides['gaps'] = ['history_before_feed_unverified']
        store = NativeIngressStore(
            manager, workspace_uuid='workspace-a', bot_uuid='bot-workspace-a', binding=adapter.native_source_binding()
        )
        await store.initialize()

        async def authorized():
            await adapter.authorize_native_source('workspace-a')

        adapter.attach_native_ingress(store, authorized)
        with pytest.raises(SourceUnavailable):
            await adapter.poll_once()
        assert await store.checkpoint() is None
        assert (await store.accepted_events(authorize=authorized))['items'] == []


@pytest.mark.asyncio
async def test_persisted_admission_rejects_poisoned_saved_metadata(manager):
    from langbot.pkg.platform.adapters.wecom_archive.projection import project_page

    with archive_adapter() as (_, adapter):
        store = NativeIngressStore(
            manager,
            workspace_uuid='workspace-a',
            bot_uuid='bot-workspace-a',
            binding=adapter.native_source_binding(),
            change_validator=adapter.native_validate_change,
        )
        await store.initialize()

        async def authorized():
            await adapter.authorize_native_source('workspace-a')

        page = project_page(archived_page(), adapter.native_source_binding())
        page.events[0].payload['change']['saved']['message']['summary'] = 'unapproved original'
        with pytest.raises(ValueError):
            await store.accept_page(page, authorize=authorized)
        assert await store.checkpoint() is None
        assert (await store.accepted_events(authorize=authorized))['items'] == []


@pytest.mark.asyncio
async def test_runtime_workspace_cannot_borrow_another_owner_grant():
    from langbot.pkg.platform.adapters.wecom_archive.reader import SourceUnavailable

    with archive_adapter() as (_, adapter):
        with pytest.raises(SourceUnavailable) as caught:
            await adapter.authorize_native_source('another-workspace')
        assert caught.value.status == 'permission_denied'


@pytest.mark.asyncio
@pytest.mark.parametrize('media_type', ['image', 'voice', 'file'])
async def test_legal_media_evidence_is_durably_accepted_with_six_field_schema(manager, media_type):
    with archive_adapter() as (server, adapter):
        item = server.page_overrides['items'][0]
        item['kind'] = 'attachments_changed'
        item['saved']['message'].update(
            message_type=media_type,
            media_evidence=[
                {
                    'mediaId': 'fixture-media',
                    'mediaType': media_type,
                    'sourceMd5': 'a' * 32,
                    'contentSha256': 'b' * 64,
                    'sourceFileSize': 7,
                    'contentSize': 7,
                }
            ],
            content_materialization={
                'status': 'complete',
                'parser_version': 'fixture-v1',
                'input_hash': 'c' * 64,
                'result_sha256': 'd' * 64,
                'text_bytes': 7,
                'parts': 1,
            },
        )
        item['saved']['attachments'] = [
            {'reference': 'wecom:media/fixture', 'status': 'saved', 'type': media_type, 'sha256': 'b' * 64, 'size': 7}
        ]
        store = NativeIngressStore(
            manager,
            workspace_uuid='workspace-a',
            bot_uuid='bot-workspace-a',
            binding=adapter.native_source_binding(),
            change_validator=adapter.native_validate_change,
        )
        await store.initialize()

        async def authorized():
            await adapter.authorize_native_source('workspace-a')

        adapter.attach_native_ingress(store, authorized)
        await adapter.poll_once()
        accepted = (await store.accepted_events(authorize=authorized))['items']
        assert len(accepted) == 1
        assert await store.checkpoint() == 'cursor-one'
