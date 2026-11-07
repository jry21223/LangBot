"""Native Agent WeChat channel; credentials and grants come from trusted factories."""

import asyncio
import re
from typing import Any, ClassVar
from pydantic import Field

from langbot_plugin.api.definition.abstract.platform.adapter import AbstractMessagePlatformAdapter
from langbot.pkg.platform.native_contract import SourceBinding, NativeAccessDenied
from langbot.pkg.platform.adapters.agent_wechat.projection import digest, identity, timestamp, validate_change
from langbot.pkg.platform.adapters.agent_wechat.source import WxNativeSource, connection_facts
from langbot.pkg.platform.adapters.agent_wechat.client import WxProtocolError
from langbot.pkg.platform.adapters.agent_wechat.owner_client import OwnerNativeClient
from langbot.pkg.platform.adapters.agent_wechat.owner_contract import NATIVE_V1
from langbot.pkg.platform.adapters.agent_wechat.owner_source import OwnerNativeSource


class AgentWeChatAdapter(AbstractMessagePlatformAdapter):
    native_authority_kind: ClassVar[str] = 'account-session'
    native_owner_gap_codes: ClassVar[frozenset[str]] = frozenset(
        {'history_before_activation_unverified', 'original_unavailable'}
    )
    native_client_required: bool = True
    client: Any = Field(default=None, exclude=True)
    store: Any = Field(default=None, exclude=True)
    authorize: Any = Field(default=None, exclude=True)
    worker: Any = Field(default=None, exclude=True)
    task: Any = Field(default=None, exclude=True)
    sender_authorize: Any = Field(default=None, exclude=True)
    threads: frozenset[str] = frozenset()
    owner_poll_validated: bool = False
    state: str = 'disconnected'
    error_code: str | None = None
    listeners: dict = Field(default_factory=dict)

    def __init__(self, config, logger):
        self.native_validate_config(config)
        super().__init__(config=dict(config), logger=logger, bot_account_id=config['account_id'])
        self.client = self.store = self.authorize = self.worker = self.task = None
        self.sender_authorize = None
        self.state, self.error_code = 'disconnected', None
        threads = config.get('allowed_threads')
        if not isinstance(threads, list) or not threads or not all(isinstance(item, str) for item in threads):
            raise ValueError('explicit_thread_scope_required')
        self.threads = frozenset(identity(item) for item in threads)

    @staticmethod
    def native_validate_config(config):
        required = {
            'account_id',
            'allowed_threads',
            'source_id',
            'scope_id',
            'credential_ref',
            'initial_cursor',
        }
        if (
            not isinstance(config, dict)
            or not required <= set(config)
            or set(config) - required - {'send_enabled', 'source_flavor', 'consumer_id', 'activation_at'}
        ):
            raise ValueError('native_config_fields_invalid')
        for key in required - {'allowed_threads'}:
            identity(config[key])
            if len(config[key]) > 512:
                raise ValueError('native_config_identifier_too_long')
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', config['credential_ref']):
            raise ValueError('native_credential_reference_invalid')
        threads = config['allowed_threads']
        if (
            not isinstance(threads, list)
            or not threads
            or len(threads) > 100
            or any(not isinstance(item, str) or not item or len(item) > 512 for item in threads)
        ):
            raise ValueError('explicit_thread_scope_required')
        for thread in threads:
            identity(thread)
        if len(set(threads)) != len(threads):
            raise ValueError('native_duplicate_thread')
        if config.get('source_flavor') == NATIVE_V1.name:
            if set(config) & {'consumer_id', 'activation_at'}:
                raise ValueError('owner_metadata_config_excludes_collector_state')
            if config.get('send_enabled', False) is not False:
                raise ValueError('owner_metadata_send_forbidden')
        else:
            identity(config.get('consumer_id'))
            timestamp(config.get('activation_at'))
        if 'source_flavor' in config and (
            not isinstance(config['source_flavor'], str)
            or config['source_flavor']
            not in {
                'personal-wechat-native-v1',
                'personal-wechat-text-v1',
            }
        ):
            raise ValueError('native_source_flavor_invalid')
        if 'send_enabled' in config and type(config['send_enabled']) is not bool:
            raise ValueError('native_send_flag_invalid')

    def native_validate_change(self, change):
        if isinstance(self.client, OwnerNativeClient):
            self.client.contract.validate(change)
        else:
            validate_change(change)

    def native_owner_frame_sequences(self):
        return isinstance(self.client, OwnerNativeClient)

    def native_credential_ref(self):
        return self.config['credential_ref']

    def native_source_binding(self):
        frozen = {
            key: self.config.get(key)
            for key in (
                'account_id',
                'source_id',
                'scope_id',
                'credential_ref',
                'consumer_id',
                'initial_cursor',
                'activation_at',
                'send_enabled',
                'source_flavor',
            )
        }
        frozen['allowed_threads'] = sorted(self.threads)
        return SourceBinding(
            self.config['source_id'],
            self.config['account_id'],
            self.config['scope_id'],
            digest(frozen),
            read_only=self.config.get('source_flavor') == NATIVE_V1.name
            or not bool(self.config.get('send_enabled', False)),
        )

    def attach_native_client(self, client):
        if client.account_id != self.config['account_id']:
            raise WxProtocolError('client_account_mismatch')
        if isinstance(client, OwnerNativeClient):
            if (
                self.config.get('source_flavor') != NATIVE_V1.name
                or client.source != self.config['source_id']
                or client.scope != self.config['scope_id']
                or self.threads != frozenset({client.thread})
            ):
                raise WxProtocolError('owner_native_flavor_and_exact_scope_required')
        self.client = client

    def attach_native_ingress(self, store, authorize):
        self.store, self.authorize = store, authorize

    def _worker(self):
        if self.worker is None:
            if self.client is None or self.store is None or self.authorize is None:
                raise WxProtocolError('trusted_source_binding_required')
            if isinstance(self.client, OwnerNativeClient):
                self.worker = OwnerNativeSource(
                    self.client,
                    self.store,
                    self.authorize,
                    initial_cursor=self.config['initial_cursor'],
                    on_valid_page=self._native_page_validated,
                )
                return self.worker
            self.worker = WxNativeSource(
                self.client,
                self.store,
                self.authorize,
                account=self.config['account_id'],
                threads=self.threads,
                activation_at=self.config['activation_at'],
                initial_cursor=self.config['initial_cursor'],
                consumer=self.config['consumer_id'],
                source=self.config['source_id'],
                scope=self.config['scope_id'],
            )
        return self.worker

    def _native_page_validated(self):
        self.owner_poll_validated = True
        self.state, self.error_code = self._owner_runtime_status()

    def _owner_runtime_status(self):
        state = self.client.upstream_state()
        if state == 'connected':
            return 'connected', None
        if state == 'disconnected':
            return 'awaiting_upstream', 'upstream_disconnected'
        if state == 'awaiting_upstream':
            return 'awaiting_upstream', 'upstream_not_ready'
        return 'unknown', 'upstream_state_unverified'

    def native_runtime_state(self):
        if self.client is None or self.store is None or self.authorize is None:
            return {'state': 'needs_authorization'}
        state, error_code = self.state, self.error_code
        if (
            isinstance(self.client, OwnerNativeClient)
            and self.owner_poll_validated
            and state not in {'error', 'connecting'}
            and error_code in {None, 'upstream_disconnected', 'upstream_not_ready', 'upstream_state_unverified'}
        ):
            state, error_code = self._owner_runtime_status()
        states = {
            'disabled',
            'needs_authorization',
            'connecting',
            'connected',
            'disconnected',
            'error',
            'unknown',
            'awaiting_upstream',
            'source_available',
        }
        result = {'state': state if state in states else 'unknown'}
        safe = {
            'source_unavailable',
            'event_cursor_expired',
            'authorization_rejected',
            'trusted_source_binding_required',
            'account_not_ready',
            'owner_transport_unavailable',
            'owner_authorization_rejected',
            'owner_current_grant_rejected',
            'owner_current_binding_rejected',
            'owner_page_binding_invalid',
            'owner_complete_page_required',
            'owner_change_contract_invalid',
            'owner_item_schema_invalid',
            'owner_gap_contract_invalid',
            'owner_sequence_not_monotonic',
            'owner_sequence_regressed',
            'owner_core_unresolved_contract_required',
            'owner_core_page_contract_required',
            'native_metadata_reader_only',
            'owner_native_flavor_and_exact_scope_required',
            'owner_metadata_ingest_stage_required',
            'upstream_state_unverified',
            'upstream_disconnected',
            'upstream_not_ready',
        }
        if error_code is not None:
            result['error_code'] = error_code if error_code in safe else 'source_unavailable'
        return result

    async def native_connection_facts(self):
        if self.client is None:
            raise WxProtocolError('trusted_client_binding_required')
        if isinstance(self.client, OwnerNativeClient):
            proof = await self.client.native_authority()
            return {
                'source': proof['grant']['source'],
                'account': proof['binding']['account'],
                'scope': proof['grant']['scope'],
                'workspace': proof['grant']['workspace'],
                'revision': self.native_source_binding().revision,
                'authority_proof': proof,
            }
        facts = await connection_facts(
            self.client, self.config['account_id'], self.config['source_id'], self.config['scope_id']
        )
        return dict(facts, revision=self.native_source_binding().revision)

    async def native_source_authority(self):
        return await self._worker().native_source_authority()

    async def native_authority(self, reference, version):
        return await self._worker().native_authority(reference, version)

    async def native_original(self, reference, version):
        return await self._worker().native_original(reference, version)

    async def native_original_manifest(self, reference, version):
        return await self._worker().native_original_manifest(reference, version)

    async def native_original_chunk(self, reference, version, part_index):
        return await self._worker().native_original_chunk(reference, version, part_index)

    async def native_media(self, reference, version, asset_id):
        return await self._worker().native_media(reference, version, asset_id)

    async def send_message(self, *args, **kwargs):
        raise WxProtocolError('trusted_send_context_required')

    async def reply_message(self, *args, **kwargs):
        raise WxProtocolError('trusted_send_context_required')

    def attach_native_sender(self, sender_authorize):
        self.sender_authorize = sender_authorize

    async def send_with_context(self, operation_id, target_type, target_id, message):
        from langbot_plugin.api.entities.builtin.platform.message import Plain
        from langbot.pkg.platform.adapters.agent_wechat.sender import WxSendProvider

        if isinstance(self.client, OwnerNativeClient):
            raise NativeAccessDenied('native_metadata_reader_only')
        if self.sender_authorize is None or not self.config.get('send_enabled', False):
            raise WxProtocolError('trusted_sender_binding_required')
        self._worker()
        if not message or any(not isinstance(component, Plain) for component in message):
            raise WxProtocolError('text_send_only')

        async def authorize_send():
            await self.authorize()
            await self.sender_authorize()

        return await self.store.send(
            operation_id,
            target_type,
            target_id,
            {'text': ''.join(component.text for component in message)},
            provider=WxSendProvider(self.client, self.threads, target_id=target_id),
            authorize=authorize_send,
        )

    def register_listener(self, event_type, callback):
        callbacks = self.listeners.setdefault(event_type, [])
        if callback not in callbacks:
            callbacks.append(callback)

    def unregister_listener(self, event_type, callback):
        callbacks = self.listeners.get(event_type, [])
        if callback in callbacks:
            callbacks.remove(callback)
        if not callbacks:
            self.listeners.pop(event_type, None)

    async def _receive(self):
        retryable = {
            'event_stream_disconnected',
            'daemon_transport_unavailable',
            'account_not_ready',
            'owner_transport_unavailable',
        }
        while True:
            try:
                self.state, self.error_code = 'connecting', None
                worker = self._worker()
                await worker.initialize()
                await worker.native_source_authority()
                if not isinstance(worker, OwnerNativeSource):
                    self.state = 'connected'
                if isinstance(worker, OwnerNativeSource):
                    await worker.receive()
                else:
                    async for event in self.client.events(await self.store.checkpoint()):
                        await worker.accept_event(event)
                self.state = 'disconnected'
            except asyncio.CancelledError:
                raise
            except WxProtocolError as exc:
                self.state, self.error_code = 'disconnected', str(exc)
                await self.logger.warning('Agent WeChat: ' + str(exc))
                if str(exc) not in retryable:
                    self.state = 'error'
                    raise
            except Exception:
                self.state, self.error_code = 'error', 'source_unavailable'
                await self.logger.warning('Agent WeChat: source_unavailable')
                raise
            await asyncio.sleep(5)

    async def run_async(self):
        self._worker()
        if self.task is not None and not self.task.done():
            raise WxProtocolError('source_already_running')
        self.task = asyncio.current_task()
        try:
            await self._receive()
        finally:
            self.owner_poll_validated = False
            if self.state != 'error':
                self.state = 'disconnected'
            self.task = None
            if self.client is not None:
                await self.client.close()

    async def kill(self):
        task = self.task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self.client is not None:
            await self.client.close()
        self.owner_poll_validated = False
        self.state, self.error_code = 'disconnected', None
        return True
