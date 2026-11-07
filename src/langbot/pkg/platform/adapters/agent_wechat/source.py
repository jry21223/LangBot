"""Durable native wx input and fresh original reads; no daemon ownership."""

from __future__ import annotations

from hashlib import sha256

from langbot.pkg.platform.native_contract import SourcePage, SourceEnvelope
from .client import WxProtocolError
from .projection import identity, project_message, digest, event_sequence, validate_change


async def connection_facts(client, account, source, scope):
    state = await client.status()
    if state.get('id') != account:
        raise WxProtocolError('account_binding_mismatch')
    if (
        state.get('authenticated') is not True
        or state.get('contactSyncReady') is not True
        or state.get('connection') != 'online'
        or state.get('incomingTransport') != 'native_push'
    ):
        raise WxProtocolError('account_not_ready')
    return {'source': source, 'account': account, 'scope': scope, 'connected': True}


class WxNativeSource:
    """Acknowledge the wx consumer only after the Core transaction commits."""

    def __init__(
        self, client, store, authorize, *, account, threads, activation_at, initial_cursor, consumer, source, scope
    ):
        if not threads or initial_cursor is None:
            raise ValueError('explicit_scope_and_cursor_required')
        self.client, self.store, self.authorize = client, store, authorize
        self.account, self.threads = identity(account), frozenset(threads)
        self.activation_at, self.initial_cursor = activation_at, identity(initial_cursor)
        self.consumer, self.source, self.scope = identity(consumer), identity(source), identity(scope)
        self.records = {}
        self.assets = {}
        self.servers = {}
        self.origins = {}
        self.versions = set()

    def _remember(self, envelope):
        payload = envelope['payload'] if isinstance(envelope, dict) else envelope.payload
        ref = envelope['reference'] if isinstance(envelope, dict) else envelope.reference
        version = envelope['version'] if isinstance(envelope, dict) else envelope.version
        if 'native' not in payload:
            return
        native = payload['native']
        self.records[ref] = (version, payload)
        self.versions.add((envelope['record'] if isinstance(envelope, dict) else envelope.record, version))
        self.origins[native['message_id']] = payload['origin']
        for asset in payload['media']:
            self.assets[asset['asset_id']] = native['message_id']
        server = native.get('server_id')
        if server:
            key = (native['thread_id'], server)
            previous = self.servers.get(key)
            self.servers[key] = native['message_id'] if previous in {None, native['message_id']} else False

    async def initialize(self):
        await self.authorize()
        await self.store.initialize(initial_cursor=self.initial_cursor)
        position = 0
        while True:
            page = await self.store.accepted_events(after_position=position, limit=100, authorize=self.authorize)
            for item in page['items']:
                self._remember(item['envelope'])
            position = page['next_position']
            if not page['has_more']:
                break
        checkpoint = await self.store.checkpoint()
        if checkpoint != self.initial_cursor:
            await self.authorize()
            await self.client.ack(self.consumer, checkpoint)

    async def native_source_authority(self):
        await self.authorize()
        return await self.native_connection_facts()

    async def native_connection_facts(self):
        return await connection_facts(self.client, self.account, self.source, self.scope)

    async def _project(self, message_id, event, origin):
        await self.authorize()
        original = await self.client.message(message_id)
        if event.get('data', {}).get('threadId') is not None and event['data']['threadId'] != original.get('threadId'):
            raise WxProtocolError('event_message_binding_mismatch')
        if original.get('threadId') not in self.threads or original.get('direction') != 'incoming':
            return None
        media = await self.client.media(message_id)
        return project_message(
            original,
            cursor=event['cursor'],
            origin=origin,
            account=self.account,
            allowed_threads=set(self.threads),
            activation_at=self.activation_at,
            source=self.source,
            scope=self.scope,
            assets=media['items'],
            observed_at=event['createdAt'],
        )

    async def accept_event(self, event):
        """Unknown event kinds advance only the transport cursor, never create business."""
        await self.authorize()
        event_sequence(event['cursor'])
        previous = await self.store.checkpoint()
        if previous is not None and event_sequence(event['cursor']) < int(previous):
            raise WxProtocolError('event_cursor_regressed')
        if previous == event['cursor']:
            await self.client.ack(self.consumer, previous)
            return
        data, kind = event['data'], event['kind']
        envelope = None
        if kind == 'message.committed' and data.get('threadId') in self.threads and data.get('direction') == 'incoming':
            envelope = await self._project(identity(data.get('messageId')), event, data.get('origin', 'unknown'))
        elif kind == 'media.ready':
            message_id = self.assets.get(data.get('assetId'))
            if message_id is None:
                message_id = await self._find_known_asset(data.get('assetId'))
            if message_id:
                envelope = await self._project(message_id, event, self.origins[message_id])
            # An unknown asset may belong to another scope. Never follow it speculatively.
            else:
                envelope = self._unresolved(
                    event, 'media_identity_unresolved', {'asset_id': identity(data.get('assetId'))}
                )
        elif kind == 'message.recalled' and data.get('threadId') in self.threads:
            message_id = data.get('messageId') or self.servers.get((data['threadId'], data.get('serverId')))
            if not message_id:
                envelope = self._unresolved(
                    event,
                    'recall_identity_unresolved',
                    {'thread_id': identity(data['threadId']), 'server_id': identity(data.get('serverId'))},
                )
            else:
                envelope = await self._project(
                    message_id, event, self.origins.get(message_id, data.get('origin', 'unknown'))
                )
                if envelope is not None and not envelope.payload['recalled']:
                    raise WxProtocolError('recall_not_materialized')
        if envelope is not None and envelope.kind != 'unresolved':
            validate_change(envelope.payload['change'])
        events = () if envelope is None or (envelope.record, envelope.version) in self.versions else (envelope,)
        receipt = await self.store.accept_page(SourcePage(previous, event['cursor'], events), authorize=self.authorize)
        if events:
            self._remember(envelope)
        await self.authorize()
        await self.client.ack(self.consumer, receipt.cursor)

    async def _find_known_asset(self, asset_id):
        """Refresh only exact accepted pending-message IDs, never an unknown asset route."""
        candidates = {
            payload['native']['message_id']
            for _, payload in self.records.values()
            if payload['message_type'] in {3, 34, 43, 47, 49}
            and not payload['recalled']
            and not payload['dispatch_eligible']
        }
        # Work is bounded; larger backlogs remain durable unresolved entries for explicit reconciliation.
        for message_id in sorted(candidates)[:100]:
            await self.authorize()
            result = await self.client.media(message_id)
            for asset in result['items']:
                if (
                    asset.get('id') == asset_id
                    and asset.get('accountId') == self.account
                    and asset.get('messageId') == message_id
                ):
                    return message_id
        return None

    def _unresolved(self, event, reason, native_ids):
        metadata = {
            'event_kind': identity(event['kind']),
            'event_cursor': identity(event['cursor']),
            'reason': reason,
            **native_ids,
        }
        record = digest([self.account, metadata])
        return SourceEnvelope(
            record,
            record,
            'wechat-native:unresolved/' + record,
            'unresolved',
            {'native_unresolved': metadata, 'dispatch_eligible': False},
        )

    async def receive(self):
        await self.initialize()
        await self.native_source_authority()
        async for event in self.client.events(await self.store.checkpoint()):
            await self.accept_event(event)

    async def _fresh(self, reference, version):
        await self.authorize()
        if reference not in self.records or self.records[reference][0] != version:
            raise WxProtocolError('original_not_accepted')
        payload = self.records[reference][1]
        native = payload['native']
        original = await self.client.message(native['message_id'])
        media = await self.client.media(native['message_id'])
        current = project_message(
            original,
            cursor=payload['event_cursor'],
            origin=payload['origin'],
            account=self.account,
            allowed_threads=set(self.threads),
            activation_at=self.activation_at,
            source=self.source,
            scope=self.scope,
            assets=media['items'],
            observed_at=payload['change']['observed_at'],
        )
        await self.authorize()
        if (
            current is None
            or current.reference != reference
            or current.version != version
            or current.payload['recalled']
        ):
            raise WxProtocolError('original_changed_or_recalled')
        return original, current

    async def native_authority(self, reference, version):
        _, envelope = await self._fresh(reference, version)
        return {
            'reference': reference,
            'version': version,
            'available': envelope.payload['change']['original_available'],
            'recalled': False,
            'native_version': envelope.payload['native_version'],
            'dispatch_eligible': envelope.payload['dispatch_eligible'],
        }

    async def native_original(self, reference, version):
        original, _ = await self._fresh(reference, version)
        if original.get('type') != 1 or not isinstance(original.get('body'), str):
            raise WxProtocolError('media_materialization_required')
        result = {'reference': reference, 'version': version, 'text': original['body']}
        await self._fresh(reference, version)
        return result

    async def native_original_manifest(self, reference, version):
        result = await self.native_original(reference, version)
        data = result['text'].encode('utf-8')
        return {
            'reference': reference,
            'version': version,
            'byte_length': len(data),
            'sha256': sha256(data).hexdigest(),
            'parts': len(self._parts(result['text'])),
        }

    @staticmethod
    def _parts(text):
        parts, part, size = [], '', 0
        for character in text:
            width = len(character.encode('utf-8'))
            if size + width > 16384:
                parts.append(part)
                part, size = '', 0
            part += character
            size += width
        if part or not parts:
            parts.append(part)
        return parts

    async def native_original_chunk(self, reference, version, part_index):
        result = await self.native_original(reference, version)
        parts = self._parts(result['text'])
        if type(part_index) is not int or not 0 <= part_index < len(parts):
            raise WxProtocolError('invalid_part_index')
        return {
            'reference': reference,
            'version': version,
            'part_index': part_index,
            'text': parts[part_index],
            'has_more': part_index + 1 < len(parts),
        }

    async def native_media(self, reference, version, asset_id):
        _, envelope = await self._fresh(reference, version)
        assets = [item for item in envelope.payload['media'] if item['asset_id'] == asset_id]
        if len(assets) != 1 or assets[0]['state'] != 'available':
            raise WxProtocolError('media_not_available')
        asset = assets[0]
        if not isinstance(asset['content_type'], str):
            raise WxProtocolError('media_content_type_required')
        data = await self.client.media_content(
            asset_id, sha256_hex=asset['sha256'], byte_length=asset['byte_length'], content_type=asset['content_type']
        )
        await self._fresh(reference, version)
        return {
            'reference': reference,
            'version': version,
            'asset_id': asset_id,
            'bytes': data,
            'sha256': asset['sha256'],
            'byte_length': asset['byte_length'],
            'content_type': asset['content_type'],
        }
