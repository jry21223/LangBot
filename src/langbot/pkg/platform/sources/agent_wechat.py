"""Native Agent WeChat channel; credentials and grants come from trusted factories."""

import asyncio
from typing import Any, ClassVar
from pydantic import Field

from langbot_plugin.api.definition.abstract.platform.adapter import AbstractMessagePlatformAdapter
from langbot.pkg.platform.native_contract import SourceBinding
from langbot.pkg.platform.adapters.agent_wechat.projection import digest, identity, timestamp, validate_change
from langbot.pkg.platform.adapters.agent_wechat.source import WxNativeSource, connection_facts
from langbot.pkg.platform.adapters.agent_wechat.client import WxProtocolError


class AgentWeChatAdapter(AbstractMessagePlatformAdapter):
    native_authority_kind: ClassVar[str] = 'account-session'
    native_client_required: bool = True
    client: Any = Field(default=None, exclude=True)
    store: Any = Field(default=None, exclude=True)
    authorize: Any = Field(default=None, exclude=True)
    worker: Any = Field(default=None, exclude=True)
    task: Any = Field(default=None, exclude=True)
    sender_authorize: Any = Field(default=None, exclude=True)
    threads: frozenset[str] = frozenset()
    state: str = 'disconnected'
    error_code: str | None = None
    listeners: dict = Field(default_factory=dict)

    def __init__(self, config, logger):
        super().__init__(config=dict(config), logger=logger, bot_account_id=config['account_id'])
        self.client = self.store = self.authorize = self.worker = self.task = None
        self.sender_authorize = None
        self.state, self.error_code = 'disconnected', None
        threads = config.get('allowed_threads')
        if not isinstance(threads, list) or not threads or not all(isinstance(item, str) for item in threads):
            raise ValueError('explicit_thread_scope_required')
        self.threads = frozenset(identity(item) for item in threads)
        for key in ('account_id', 'source_id', 'scope_id', 'credential_ref', 'consumer_id', 'initial_cursor'):
            identity(config.get(key))
        timestamp(config.get('activation_at'))

    @staticmethod
    def native_validate_change(change):
        validate_change(change)

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
                'endpoint',
                'send_enabled',
            )
        }
        frozen['allowed_threads'] = sorted(self.threads)
        return SourceBinding(
            self.config['source_id'],
            self.config['account_id'],
            self.config['scope_id'],
            digest(frozen),
            read_only=not bool(self.config.get('send_enabled', False)),
        )

    def attach_native_client(self, client):
        if client.account_id != self.config['account_id']:
            raise WxProtocolError('client_account_mismatch')
        self.client = client

    def attach_native_ingress(self, store, authorize):
        self.store, self.authorize = store, authorize

    def _worker(self):
        if self.worker is None:
            if self.client is None or self.store is None or self.authorize is None:
                raise WxProtocolError('trusted_source_binding_required')
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

    async def native_connection_facts(self):
        if self.client is None:
            raise WxProtocolError('trusted_client_binding_required')
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
            provider=WxSendProvider(self.client, self.threads),
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
        retryable = {'event_stream_disconnected', 'daemon_transport_unavailable', 'account_not_ready'}
        while True:
            try:
                self.state, self.error_code = 'connecting', None
                worker = self._worker()
                await worker.initialize()
                await worker.native_source_authority()
                self.state = 'connected'
                async for event in self.client.events(await self.store.checkpoint()):
                    await worker.accept_event(event)
                self.state = 'disconnected'
            except asyncio.CancelledError:
                raise
            except WxProtocolError as exc:
                self.state, self.error_code = 'disconnected', str(exc)
                await self.logger.warning('Agent WeChat: ' + str(exc))
                if str(exc) not in retryable:
                    raise
            except Exception:
                self.state, self.error_code = 'disconnected', 'source_unavailable'
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
        self.state = 'disconnected'
        return True
