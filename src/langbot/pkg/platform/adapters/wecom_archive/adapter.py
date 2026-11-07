"""Native, receive-only owner-archive adapter; SDK collection stays with its owner."""

from __future__ import annotations

import asyncio
import re
from typing import Any, Awaitable, Callable, ClassVar

import pydantic
from langbot_plugin.api.definition.abstract.platform.adapter import AbstractPlatformAdapter
from langbot_plugin.api.entities.builtin.platform.errors import NotSupportedError

from langbot.pkg.platform.native_contract import SourceBinding
from .reader import HttpSource, SourceUnavailable, OWNER_GAP_CODES
from .projection import project_page, validate_change


class WecomArchiveAdapter(AbstractPlatformAdapter):
    native_authority_kind: ClassVar[str] = 'workspace-grant'
    native_client_required: ClassVar[bool] = True
    native_owner_gap_codes: ClassVar[frozenset[str]] = OWNER_GAP_CODES
    config: dict = pydantic.Field(exclude=True, repr=False)
    listeners: dict = pydantic.Field(default_factory=dict, exclude=True)
    _reader: HttpSource | None = pydantic.PrivateAttr(default=None)
    _store: Any = pydantic.PrivateAttr(default=None)
    _authorize: Callable[[], Awaitable[None]] | None = pydantic.PrivateAttr(default=None)
    _stop: asyncio.Event = pydantic.PrivateAttr(default_factory=asyncio.Event)
    _native_state: str = pydantic.PrivateAttr(default='unknown')
    _native_error: str | None = pydantic.PrivateAttr(default=None)
    _last_receipt: str | None = pydantic.PrivateAttr(default=None)

    def __init__(self, config: dict, logger):
        super().__init__(config=dict(config), logger=logger, bot_account_id='')
        self.native_validate_config(config)

    @staticmethod
    def native_validate_config(config: dict) -> None:
        """A pure pre-persistence hook; no client or credentials are resolved."""
        allowed = {'source', 'account', 'scope', 'grant_revision', 'credential_ref', 'poll_interval', 'page_limit'}
        if not isinstance(config, dict) or set(config) - allowed:
            raise ValueError('archive_service_config_not_allowed')
        for field in ('source', 'account', 'scope', 'grant_revision'):
            value = config.get(field)
            if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,200}', value):
                raise ValueError('archive_source_binding_invalid')
        reference = config.get('credential_ref')
        if not isinstance(reference, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', reference):
            raise ValueError('native_credential_reference_invalid')
        interval = config.get('poll_interval', 5)
        if type(interval) not in (int, float) or not 1 <= interval <= 300:
            raise ValueError('archive_poll_interval_invalid')
        limit = config.get('page_limit', 100)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('archive_page_limit_invalid')

    def native_credential_ref(self) -> str:
        value = self.config.get('credential_ref')
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', value):
            raise ValueError('native_credential_reference_invalid')
        return value

    def attach_native_client(self, client: HttpSource) -> None:
        if (
            not isinstance(client, HttpSource)
            or client.source != self.config['source']
            or client.scope != self.config['scope']
        ):
            raise ValueError('archive_source_client_binding_invalid')
        self._reader = client

    def _source_reader(self) -> HttpSource:
        if self._reader is None:
            raise RuntimeError('native_source_client_not_configured')
        return self._reader

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

    def native_validate_change(self, change: dict) -> None:
        validate_change(change, self.native_source_binding())

    async def native_connection_facts(self) -> dict:
        """Use only the owner's fresh, protected source-binding proof."""
        proof = await asyncio.to_thread(self._source_reader().read_source_binding)
        return {key: proof[key] for key in ('source', 'scope', 'revision', 'workspace')} | {
            'account': proof['corp_id'],
            'origin': proof['origin'],
            'authority_proof': {'binding': proof},
        }

    async def authorize_native_source(self, workspace_uuid: str) -> None:
        reader = self._source_reader()
        if workspace_uuid != reader.workspace:
            raise SourceUnavailable('permission_denied')
        grant = await asyncio.to_thread(reader.authorize, self.config['source'], 'ingest')
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

    def native_runtime_state(self) -> dict:
        state = {'state': self._native_state}
        if self._native_error is not None:
            state['error_code'] = self._native_error
        return state

    async def poll_once(self):
        if self._store is None or self._authorize is None:
            self._native_state, self._native_error = 'needs_authorization', 'native_source_authorizer_required'
            raise RuntimeError('native_source_authorizer_required')
        try:
            await self._authorize()
            cursor = await self._store.checkpoint()
            page = await asyncio.to_thread(
                self._source_reader().read_changes, cursor, self.config.get('page_limit', 100)
            )
            projected = project_page(page, self.native_source_binding())
            receipt = await self._store.accept_page(projected, authorize=self._authorize)
        except SourceUnavailable as error:
            errors = {
                'permission_denied': ('needs_authorization', 'source_permission_denied'),
                'credential_expired': ('needs_authorization', 'source_credential_expired'),
                'disabled': ('needs_authorization', 'source_disabled'),
                'missing_page': ('error', 'source_page_missing'),
                'invalid_page': ('error', 'source_page_invalid'),
            }
            self._native_state, self._native_error = errors.get(error.status, ('disconnected', 'source_sync_failed'))
            raise
        except PermissionError:
            self._native_state, self._native_error = 'needs_authorization', 'source_registration_unavailable'
            raise
        except Exception:
            self._native_state, self._native_error = 'error', 'source_ingress_failed'
            raise
        self._native_state, self._native_error = ('disabled' if self._stop.is_set() else 'source_available'), None
        return receipt

    async def run_async(self):
        if self._store is None or self._authorize is None:
            raise RuntimeError('native_source_authorizer_required')
        self._stop.clear()
        self._native_state, self._native_error = 'connecting', None
        try:
            await self._store.initialize()
        except Exception:
            self._native_state, self._native_error = 'error', 'source_ingress_failed'
            raise
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
        self._native_state, self._native_error = 'disabled', None
        self._stop.set()
        return True
