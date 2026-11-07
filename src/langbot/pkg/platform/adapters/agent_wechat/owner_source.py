"""Consume the existing owner's append-only metadata feed without wx ownership."""

import asyncio
from copy import deepcopy

from langbot.pkg.platform.native_contract import SourceEnvelope, SourcePage, NativeAccessDenied
from .owner_client import OwnerProtocolError


class OwnerNativeSource:
    def __init__(self, client, store, authorize, *, initial_cursor, on_valid_page=None):
        self.client, self.store, self.authorize = client, store, authorize
        self.initial_cursor, self.last_sequence = initial_cursor, 0
        self.on_valid_page = on_valid_page

    async def initialize(self):
        await self.authorize()
        await self.store.initialize(initial_cursor=self.initial_cursor)
        sequence = await self.store.source_sequence_checkpoint()
        if sequence is not None and (type(sequence) is not int or not 0 <= sequence <= 9223372036854775807):
            raise OwnerProtocolError('owner_persisted_sequence_invalid')
        self.last_sequence = 0 if sequence is None else sequence

    async def native_source_authority(self):
        await self.authorize()
        return await self.client.native_authority()

    async def poll_once(self):
        await self.authorize()
        cursor = await self.store.checkpoint()
        page = await self.client.native_changes(cursor=cursor, limit=100)
        if not page['items'] and not page['gaps'] and page['next_cursor'] == cursor:
            if self.on_valid_page is not None:
                self.on_valid_page()
            return None
        events, unresolved = [], []
        sequence = self.last_sequence
        for item in page['items']:
            current = item['source_sequence'] if item.get('kind') == 'unresolved' else item['change']['sequence']
            if current <= sequence:
                raise OwnerProtocolError('owner_sequence_regressed')
            sequence = current
            if item.get('kind') == 'unresolved':
                try:
                    from langbot.pkg.platform.native_contract import SourceUnresolved
                except ImportError:
                    raise OwnerProtocolError('owner_core_unresolved_contract_required') from None
                unresolved.append(
                    SourceUnresolved(
                        event_id=item['event_id'],
                        source_sequence=current,
                        event_cursor=item['event_cursor'],
                        source=item['source'],
                        scope=item['scope'],
                        reason=item['reason'],
                    )
                )
                continue
            change = item['change']
            events.append(
                SourceEnvelope(
                    change['record'],
                    change['version'],
                    change['original'],
                    change['kind'],
                    {
                        'change': deepcopy(change),
                        'owner_event_id': item['event_id'],
                        'native_version': item['native_version'],
                        'event_cursor': item['event_cursor'],
                        'media': deepcopy(item['media']),
                        'qualification': item['qualification'],
                        'dispatch_eligible': item['dispatch_eligible'],
                    },
                    owner_frame_sequence=current,
                )
            )
        options = {}
        if page['gaps']:
            options['gaps'] = tuple(page['gaps'])
        if unresolved:
            options['unresolved'] = tuple(unresolved)
        try:
            source_page = SourcePage(page['previous_cursor'], page['next_cursor'], tuple(events), **options)
        except TypeError:
            raise OwnerProtocolError('owner_core_page_contract_required') from None
        receipt = await self.store.accept_page(source_page, authorize=self.authorize)
        self.last_sequence = sequence
        if self.on_valid_page is not None:
            self.on_valid_page()
        # This is a saved append-only owner feed. There is no external ACK API:
        # the Core transaction persists next_cursor, not the wx collection cursor.
        return receipt

    async def receive(self):
        while True:
            await self.poll_once()
            await asyncio.sleep(5)

    async def native_authority(self, reference, version):
        await self.authorize()
        result = await self.client.native_authority(reference=reference, version=version)
        return dict(result['original'])

    async def native_original(self, *args):
        raise NativeAccessDenied('native_metadata_reader_only')

    async def native_original_manifest(self, *args):
        raise NativeAccessDenied('native_metadata_reader_only')

    async def native_original_chunk(self, *args):
        raise NativeAccessDenied('native_metadata_reader_only')

    async def native_media(self, *args):
        raise NativeAccessDenied('native_metadata_reader_only')
