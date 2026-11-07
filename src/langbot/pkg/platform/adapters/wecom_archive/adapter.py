"""Native, receive-only owner-archive adapter; SDK collection stays with its owner."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import pydantic
from langbot_plugin.api.definition.abstract.platform.adapter import AbstractPlatformAdapter
from langbot_plugin.api.entities.builtin.platform.errors import NotSupportedError

from langbot.pkg.platform.native_contract import SourceBinding
from .reader import HttpSource, SourceUnavailable
from .projection import project_page


class WecomArchiveAdapter(AbstractPlatformAdapter):
    config: dict = pydantic.Field(exclude=True, repr=False)
    listeners: dict = pydantic.Field(default_factory=dict, exclude=True)
    _reader: HttpSource = pydantic.PrivateAttr()
    _store: Any = pydantic.PrivateAttr(default=None)
    _authorize: Callable[[], Awaitable[None]] | None = pydantic.PrivateAttr(default=None)
    _stop: asyncio.Event = pydantic.PrivateAttr(default_factory=asyncio.Event)
    _last_receipt: str | None = pydantic.PrivateAttr(default=None)

    def __init__(self, config: dict, logger):
        super().__init__(config=dict(config), logger=logger, bot_account_id='')
        self._reader = HttpSource(
            config['source_endpoint'],
            source=config['source'],
            scope=config['scope'],
            workspace=config['source_workspace'],
            principal=config['source_principal'],
            token=config['source_token'],
            timeout=config.get('source_timeout', 10),
        )
        self.native_source_binding()
        interval = config.get('poll_interval', 5)
        if type(interval) not in (int, float) or not 1 <= interval <= 300:
            raise ValueError('archive_poll_interval_invalid')

    def native_source_binding(self) -> SourceBinding:
        return SourceBinding(
            self.config['source'],
            self.config['account'],
            self.config['scope'],
            self.config['grant_revision'],
            read_only=True,
        )

    def attach_native_ingress(self, store, authorize: Callable[[], Awaitable[None]]) -> None:
        if not callable(authorize):
            raise ValueError('native_source_authorizer_required')
        self._store, self._authorize = store, authorize

    async def native_connection_facts(self) -> dict:
        """Read authority facts; an absent owner account proof stays absent."""
        grant = await asyncio.to_thread(self._reader.authorize, self.config['source'], 'ingest')
        facts = {key: grant[key] for key in ('source', 'scope', 'revision', 'workspace')}
        # The frozen owner API does not attest account. The trusted Host must
        # reject this absence until an explicit owner contract or mapping exists.
        if isinstance(grant.get('account'), str) and grant['account']:
            facts['account'] = grant['account']
        return facts

    async def authorize_native_source(self, workspace_uuid: str) -> None:
        if workspace_uuid != self.config['source_workspace']:
            raise SourceUnavailable('permission_denied')
        grant = await asyncio.to_thread(self._reader.authorize, self.config['source'], 'ingest')
        if grant['workspace'] != workspace_uuid or grant['revision'] != self.config['grant_revision']:
            raise SourceUnavailable('permission_denied')

    def get_supported_events(self) -> list[str]:
        return ['message.received', 'message.edited', 'message.deleted']

    def get_supported_apis(self) -> list[str]:
        return []

    async def send_message(self, target_type: str, target_id: str, message):
        raise NotSupportedError('send_message')

    async def reply_message(self, message_source, message, quote_origin: bool = False):
        raise NotSupportedError('reply_message')

    async def call_platform_api(self, action: str, params: dict | None = None):
        raise NotSupportedError('call_platform_api')

    def register_listener(self, event_type, callback):
        self.listeners[event_type] = callback

    def unregister_listener(self, event_type, callback):
        if self.listeners.get(event_type) is callback:
            self.listeners.pop(event_type, None)

    async def poll_once(self):
        if self._store is None or self._authorize is None:
            raise RuntimeError('native_source_authorizer_required')
        await self._authorize()
        cursor = await self._store.checkpoint()
        page = await asyncio.to_thread(self._reader.read_changes, cursor, self.config.get('page_limit', 100))
        projected = project_page(page, self.native_source_binding())
        return await self._store.accept_page(projected, authorize=self._authorize)

    async def run_async(self):
        if self._store is None or self._authorize is None:
            raise RuntimeError('native_source_authorizer_required')
        self._stop.clear()
        await self._store.initialize()
        await self.logger.info('WeCom archive source starting in receive-only mode')
        try:
            while not self._stop.is_set():
                try:
                    receipt = await self.poll_once()
                except SourceUnavailable as error:
                    await self.logger.warning('WeCom archive source unavailable: ' + error.status)
                    if error.status in {
                        'permission_denied',
                        'credential_expired',
                        'disabled',
                        'missing_page',
                        'invalid_page',
                    }:
                        raise RuntimeError('archive_source_' + error.status) from None
                else:
                    if receipt.page_id != self._last_receipt:
                        await self.logger.info('WeCom archive page durably accepted: ' + str(len(receipt.event_ids)))
                        self._last_receipt = receipt.page_id
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.config.get('poll_interval', 5))
                except TimeoutError:
                    pass
        finally:
            await self.logger.info('WeCom archive source stopped')

    async def kill(self) -> bool:
        self._stop.set()
        return True
