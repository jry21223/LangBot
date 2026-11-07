"""Public receive-only adapter capabilities; no production source is contacted."""

import json

import pytest
from langbot_plugin.api.definition.abstract.platform.event_logger import AbstractEventLogger
from langbot_plugin.api.entities.builtin.platform.errors import NotSupportedError

from langbot.pkg.platform.adapters.wecom_archive.adapter import WecomArchiveAdapter


class FixtureLogger(AbstractEventLogger):
    async def info(self, *args, **kwargs):
        pass

    async def debug(self, *args, **kwargs):
        pass

    async def warning(self, *args, **kwargs):
        pass

    async def error(self, *args, **kwargs):
        pass


def source_config():
    return {
        'source': 'fixture-wecom',
        'account': 'fixture-corp',
        'scope': 'fixture-chat',
        'credential_ref': 'fixture-metadata-reader',
        'grant_revision': 'grant-v1',
    }


@pytest.mark.asyncio
async def test_archive_source_has_no_send_reply_or_platform_api():
    adapter = WecomArchiveAdapter(source_config(), FixtureLogger())
    assert adapter.get_supported_apis() == []
    assert adapter.native_authority_kind == 'workspace-grant'
    with pytest.raises(NotSupportedError):
        await adapter.send_message('group', 'fixture-chat', [])
    with pytest.raises(NotSupportedError):
        await adapter.reply_message(None, [])
    with pytest.raises(NotSupportedError):
        await adapter.call_platform_api('send_to_all', {})


def test_native_manifest_uses_existing_component_loader_and_has_no_send_api():
    from pathlib import Path
    import yaml
    from langbot.pkg.discover.engine import Component

    path = Path('src/langbot/pkg/platform/adapters/wecom_archive/manifest.yaml')
    manifest = yaml.safe_load(path.read_text())
    component = Component(
        owner='system', manifest=manifest, rel_path='pkg/platform/adapters/wecom_archive/manifest.yaml'
    )
    assert component.get_python_component_class() is WecomArchiveAdapter
    assert manifest['spec']['supported_apis'] == {'required': [], 'optional': []}
    assert manifest['metadata']['name'] == 'wecom-archive'
    assert {row['name'] for row in manifest['spec']['config']} == {
        'source',
        'account',
        'scope',
        'grant_revision',
        'credential_ref',
        'poll_interval',
        'page_limit',
    }


def test_adapter_representation_does_not_disclose_source_credentials():
    adapter = WecomArchiveAdapter(source_config(), FixtureLogger())
    assert 'source_token' not in repr(adapter)
    assert 'source_token' not in json.dumps(adapter.model_dump())
    with pytest.raises(ValueError, match='archive_service_config_not_allowed'):
        WecomArchiveAdapter(source_config() | {'source_token': 'not-accepted-in-bot-config'}, FixtureLogger())


@pytest.mark.asyncio
async def test_native_source_requires_host_authorizer_before_poll_or_start():
    adapter = WecomArchiveAdapter(source_config(), FixtureLogger())
    with pytest.raises(RuntimeError, match='native_source_authorizer_required'):
        await adapter.poll_once()
    with pytest.raises(RuntimeError, match='native_source_authorizer_required'):
        await adapter.run_async()


@pytest.mark.asyncio
async def test_native_source_requires_trusted_client_before_contacting_source():

    adapter = WecomArchiveAdapter(source_config(), FixtureLogger())
    with pytest.raises(RuntimeError, match='native_source_client_not_configured'):
        await adapter.authorize_native_source('another-workspace')


def test_pure_pre_persistence_validation_rejects_service_fields_and_invalid_binding():
    for field in ('source_token', 'endpoint', 'source_endpoint', 'secret_file', 'source_principal'):
        with pytest.raises(ValueError):
            WecomArchiveAdapter.native_validate_config(source_config() | {field: 'forbidden'})
    for field, value in [
        ('page_limit', True),
        ('page_limit', 101),
        ('poll_interval', 0),
        ('account', ''),
        ('credential_ref', '/arbitrary/path'),
    ]:
        with pytest.raises(ValueError):
            WecomArchiveAdapter.native_validate_config(source_config() | {field: value})
