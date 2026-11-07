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
    config = {
        'source_endpoint': f'http://127.0.0.1:{server.server_port}/saved',
        'source': 'fixture-wecom',
        'account': 'fixture-corp',
        'scope': 'fixture-chat',
        'source_workspace': 'workspace-a',
        'source_principal': 'fixture-reader',
        'source_token': 'fixture-reader-token-000000',
        'grant_revision': 'grant-v1',
    }
    try:
        yield server, WecomArchiveAdapter(config, FixtureLogger())
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
async def test_native_connection_facts_do_not_invent_account_proof():
    with archive_adapter() as (_, adapter):
        facts = await adapter.native_connection_facts()
        assert facts == {
            'source': 'fixture-wecom',
            'scope': 'fixture-chat',
            'revision': 'grant-v1',
            'workspace': 'workspace-a',
        }
        assert 'account' not in facts
