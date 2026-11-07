"""Durable native receipts using the owning LangBot Workspace transaction seam.

Source authorization is supplied by the trusted owner and rechecked per action.
No body, credential, daemon lifecycle or external receiver is configured here.
"""

from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json

import sqlalchemy as sa

from ..entity.persistence.native_ingress import NativeEvent, NativePage, NativeSend, NativeSource
from .native_contract import AcceptanceReceipt, Authorize, SendProvider, SourceBinding, SourcePage


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def _hash(value):
    return sha256(_json(value).encode()).hexdigest()


def _identifier(value, *, optional=False):
    if optional and value is None:
        return value
    if not isinstance(value, str) or not value or len(value) > 512 or any(ord(c) < 32 for c in value):
        raise ValueError('native_identifier_invalid')
    return value


def _metadata(value):
    """Reject common secret/body containers; originals stay at their source."""
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or key.lower() in {
                'body', 'content', 'text', 'raw_payload', 'raw', 'credentials', 'token',
                'authorization', 'cookie', 'password', 'api_key', 'secret',
            }:
                raise ValueError('native_metadata_only')
            _metadata(item)
    elif isinstance(value, list):
        for item in value:
            _metadata(item)


class NativeIngressStore:
    def __init__(self, manager, *, workspace_uuid: str, bot_uuid: str, binding: SourceBinding):
        self.manager = manager
        self.workspace_uuid = _identifier(workspace_uuid)
        self.bot_uuid = _identifier(bot_uuid)
        self.binding = binding
        if not isinstance(binding, SourceBinding) or type(binding.read_only) is not bool:
            raise ValueError('native_binding_invalid')
        for value in (binding.source, binding.account, binding.scope, binding.revision):
            _identifier(value)
        self.source_id = _hash([workspace_uuid, bot_uuid])
        self.binding_hash = _hash(asdict(binding))

    def _own_transaction(self):
        if self.manager.current_session() is not None:
            raise ValueError('native_own_transaction_required')
        return self.manager.tenant_uow(self.workspace_uuid)

    async def _source(self, session):
        row = await session.get(NativeSource, self.source_id)
        if row is None or row.workspace_uuid != self.workspace_uuid or row.binding_sha256 != self.binding_hash:
            raise ValueError('native_binding_missing_or_changed')
        return row

    async def initialize(self, initial_cursor: str | None = None):
        _identifier(initial_cursor, optional=True)
        async with self._own_transaction() as uow:
            source = await uow.session.get(NativeSource, self.source_id)
            if source is None:
                uow.session.add(NativeSource(id=self.source_id, workspace_uuid=self.workspace_uuid,
                    bot_uuid=self.bot_uuid, binding=asdict(self.binding), binding_sha256=self.binding_hash,
                    cursor=initial_cursor))
            else:
                await self._source(uow.session)

    async def checkpoint(self):
        async with self._own_transaction() as uow:
            return (await self._source(uow.session)).cursor

    async def accept_page(self, page: SourcePage, *, authorize: Authorize) -> AcceptanceReceipt:
        if not isinstance(page, SourcePage) or page.complete is not True or len(page.events) > 100:
            raise ValueError('native_complete_page_required')
        _identifier(page.previous_cursor, optional=True)
        _identifier(page.next_cursor)
        envelopes = []
        for event in page.events:
            value = asdict(event)
            for key in ('record', 'version', 'reference', 'kind'):
                _identifier(value[key])
            if value['kind'] not in {'created', 'updated', 'media', 'recall', 'baseline', 'baseline_snapshot', 'changed', 'attachments_changed', 'recalled'}:
                raise ValueError('native_event_kind_invalid')
            if not isinstance(value['payload'], dict):
                raise ValueError('native_metadata_invalid')
            _metadata(value['payload'])
            envelopes.append(value)
        if len(_json(envelopes).encode()) > 1_000_000:
            raise ValueError('native_page_too_large')
        await authorize()
        page_id = _hash([self.source_id, "source", page.previous_cursor, page.next_cursor])
        fingerprint = _hash(envelopes)
        receipt = None
        async with self._own_transaction() as uow:
            session = uow.session
            source = await self._source(session)
            prior = await session.get(NativePage, page_id)
            if prior is not None:
                if prior.fingerprint != fingerprint:
                    raise ValueError('native_page_conflict')
                receipt = AcceptanceReceipt(prior.id, prior.cursor, tuple(prior.event_ids))
            else:
                if source.cursor != page.previous_cursor:
                    raise ValueError('native_cursor_conflict')
                event_ids = []
                for value in envelopes:
                    event_id = _hash([self.source_id, value['record'], value['version']])
                    old = await session.get(NativeEvent, event_id)
                    if old is not None:
                        if old.fingerprint != _hash(value):
                            raise ValueError('native_source_version_conflict')
                    else:
                        source.position += 1
                        eligible = value['payload'].get('dispatch_eligible') is True
                        session.add(NativeEvent(id=event_id, workspace_uuid=self.workspace_uuid,
                            source_id=self.source_id, position=source.position, envelope=value, fingerprint=_hash(value),
                            status='pending' if eligible else 'recorded'))
                    event_ids.append(event_id)
                await session.flush()
                changed = await session.execute(sa.update(NativeSource).where(
                    NativeSource.id == self.source_id,
                    NativeSource.cursor.is_(None) if page.previous_cursor is None else NativeSource.cursor == page.previous_cursor,
                ).values(cursor=page.next_cursor))
                if changed.rowcount != 1:
                    raise ValueError('native_cursor_conflict')
                session.add(NativePage(id=page_id, workspace_uuid=self.workspace_uuid, source_id=self.source_id,
                    direction="source", previous_cursor=page.previous_cursor,
                    fingerprint=fingerprint, cursor=page.next_cursor, event_ids=event_ids))
                receipt = AcceptanceReceipt(page_id, page.next_cursor, tuple(event_ids))
        # The UoW has committed before an acknowledgement-capable receipt escapes.
        return receipt

    async def pending_events(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('native_limit_invalid')
        async with self._own_transaction() as uow:
            await self._source(uow.session)
            rows = (await uow.session.execute(sa.select(NativeEvent).where(
                NativeEvent.source_id == self.source_id, NativeEvent.status == 'pending',
            ).order_by(NativeEvent.id).limit(limit))).scalars().all()
            return [dict(event_id=r.id, envelope=r.envelope, status=r.status) for r in rows]

    async def accepted_events(self, *, after_position=0, limit=100, authorize: Authorize):
        """Restore exact source metadata, including non-dispatchable versions."""
        self._position(after_position)
        self._limit(limit)
        await authorize()
        async with self._own_transaction() as uow:
            await self._source(uow.session)
            rows = (await uow.session.execute(sa.select(NativeEvent).where(
                NativeEvent.source_id == self.source_id, NativeEvent.position > after_position,
            ).order_by(NativeEvent.position).limit(limit + 1))).scalars().all()
            items = [dict(event_id=r.id, position=r.position, envelope=r.envelope, status=r.status)
                     for r in rows[:limit]]
            return dict(items=items, next_position=items[-1]['position'] if items else after_position,
                        has_more=len(rows) > limit)

    @staticmethod
    def _position(value):
        if type(value) is not int or value < 0:
            raise ValueError('native_position_invalid')

    @staticmethod
    def _limit(value):
        if type(value) is not int or not 1 <= value <= 100:
            raise ValueError('native_limit_invalid')

    def _router_change(self, event):
        change = event.envelope['payload'].get('change')
        required = {'source', 'scope', 'record', 'version', 'sequence', 'kind', 'business_at',
                    'observed_at', 'original', 'projection_hash', 'goal_reference', 'saved'}
        if not isinstance(change, dict) or set(change) not in (required, required | {'original_available'}):
            raise ValueError('native_router_change_contract_invalid')
        expected = dict(source=self.binding.source, scope=self.binding.scope,
                        record=event.envelope['record'], version=event.envelope['version'])
        if any(change[key] != value for key, value in expected.items()):
            raise ValueError('native_router_identity_mismatch')
        if change['original'] != event.envelope['reference'] or change['kind'] != event.envelope['kind']:
            raise ValueError('native_router_original_mismatch')
        return change

    async def _router_page_value(self, session, page):
        items = []
        for event_id in page.event_ids:
            event = await session.get(NativeEvent, event_id)
            if event is None or event.source_id != self.source_id:
                raise ValueError('native_router_event_missing')
            items.append(dict(event_id=event.id, envelope=self._router_change(event)))
        value = dict(page_id=page.id, source=self.binding.source, scope=self.binding.scope,
                     complete_page=True, previous_cursor=page.previous_cursor, next_cursor=page.cursor,
                     gaps=[], items=items)
        if _hash(value) != page.fingerprint:
            raise ValueError('native_router_page_changed')
        return dict(value, page_sha256=page.fingerprint)

    async def read_router_page(self, cursor: str, limit=100, *, authorize: Authorize):
        """Reserve one immutable metadata page for the existing unique Host.

        Cursor belongs only to this downstream feed. Source consumer watermarks
        are never reused here. All source states travel, including tombstones.
        """
        self._limit(limit)
        if not isinstance(cursor, str) or not cursor.isascii() or not cursor.isdigit() or str(int(cursor)) != cursor:
            raise ValueError('native_router_cursor_invalid')
        position = int(cursor)
        await authorize()
        async with self._own_transaction() as uow:
            session = uow.session
            source = await self._source(session)
            if source.router_pending_page:
                page = await session.get(NativePage, source.router_pending_page)
                if page.previous_cursor != cursor:
                    raise ValueError('native_router_ack_pending')
                return await self._router_page_value(session, page)
            if source.router_cursor != position:
                raise ValueError('native_router_cursor_conflict')
            rows = (await session.execute(sa.select(NativeEvent).where(
                NativeEvent.source_id == self.source_id, NativeEvent.position > position,
            ).order_by(NativeEvent.position).limit(limit))).scalars().all()
            next_cursor = str(rows[-1].position) if rows else cursor
            page_id = _hash([self.source_id, 'router', cursor, next_cursor])
            value = dict(page_id=page_id, source=self.binding.source, scope=self.binding.scope,
                         complete_page=True, previous_cursor=cursor, next_cursor=next_cursor,
                         gaps=[], items=[dict(event_id=r.id, envelope=self._router_change(r)) for r in rows])
            fingerprint = _hash(value)
            # An empty poll has no acknowledgement and must not pin the feed.
            if rows:
                changed = await session.execute(sa.update(NativeSource).where(
                    NativeSource.id == self.source_id, NativeSource.router_cursor == position,
                    NativeSource.router_pending_page.is_(None),
                ).values(router_pending_page=page_id))
                if changed.rowcount != 1:
                    raise ValueError('native_router_page_race')
                session.add(NativePage(id=page_id, workspace_uuid=self.workspace_uuid,
                    source_id=self.source_id, direction='router', previous_cursor=cursor,
                    cursor=next_cursor, fingerprint=fingerprint, event_ids=[r.id for r in rows]))
        return dict(value, page_sha256=fingerprint)

    async def ack_router_page(self, page_id, page_sha256, receipts, *, authorize: Authorize):
        """Persist only the Host's committed producer receipts, never success claims."""
        _identifier(page_id)
        _identifier(page_sha256)
        if not isinstance(receipts, list) or len(receipts) > 100:
            raise ValueError('native_router_receipts_invalid')
        await authorize()
        async with self._own_transaction() as uow:
            session = uow.session
            source = await self._source(session)
            page = await session.get(NativePage, page_id)
            if page is None or page.source_id != self.source_id or page.direction != 'router' or page.fingerprint != page_sha256:
                raise ValueError('native_router_page_unknown')
            if len(receipts) != len(page.event_ids) or len({r.get('event_id') for r in receipts if isinstance(r, dict)}) != len(receipts):
                raise ValueError('native_router_receipts_incomplete')
            by_id = {r['event_id']: r for r in receipts}
            if set(by_id) != set(page.event_ids):
                raise ValueError('native_router_receipts_incomplete')
            for event_id in page.event_ids:
                event = await session.get(NativeEvent, event_id)
                receipt = by_id[event_id]
                expected = dict(event_id=event_id, source=self.binding.source,
                                record=event.envelope['record'], version=event.envelope['version'], status='accepted')
                if set(receipt) != set(expected) | {'receipt_id'} or any(receipt[k] != v for k, v in expected.items()):
                    raise ValueError('native_router_receipt_identity_invalid')
                _identifier(receipt['receipt_id'])
                if event.receiver_receipt is not None and event.receiver_receipt != receipt:
                    raise ValueError('native_router_receipt_conflict')
                event.receiver_receipt = receipt
            if source.router_pending_page == page_id and source.router_cursor == int(page.previous_cursor):
                source.router_cursor = int(page.cursor)
                source.router_pending_page = None
            elif source.router_cursor < int(page.cursor):
                raise ValueError('native_router_ack_out_of_order')
        return dict(status='accepted', page_id=page_id, next_cursor=page.cursor)

    async def lookup_router_receipt(self, event_id, *, authorize: Authorize):
        _identifier(event_id)
        await authorize()
        async with self._own_transaction() as uow:
            await self._source(uow.session)
            event = await uow.session.get(NativeEvent, event_id)
            if event is None or event.source_id != self.source_id:
                return dict(status='unknown', event_id=event_id)
            return event.receiver_receipt or dict(status='pending', event_id=event_id)

    async def send(self, operation_id, target_type, target_id, payload, *, provider: SendProvider, authorize: Authorize):
        """A host-owned operation ID survives crashes and UNKNOWN lookup.

        The caller must not generate this ID from message content. No original
        outgoing body is stored. An absent lookup never authorizes a resend.
        """
        if self.binding.read_only:
            raise ValueError('native_receive_only')
        for value in (operation_id, target_type, target_id):
            _identifier(value)
        await authorize()
        operation_key = _hash([self.source_id, operation_id])
        fingerprint = _hash([target_type, target_id, payload])
        should_send = False
        async with self._own_transaction() as uow:
            session = uow.session
            await self._source(session)
            operation = await session.get(NativeSend, operation_key)
            if operation is None:
                operation = NativeSend(id=operation_key, workspace_uuid=self.workspace_uuid,
                    source_id=self.source_id, operation_id=operation_id, provider_key=operation_key,
                    fingerprint=fingerprint, status='prepared')
                session.add(operation)
                await session.flush()
            elif operation.fingerprint != fingerprint:
                raise ValueError('native_operation_conflict')
            if operation.status in {'accepted', 'failed', 'needs_review'}:
                return operation.receipt
            changed = await session.execute(sa.update(NativeSend).where(
                NativeSend.id == operation_key, NativeSend.status == 'prepared',
            ).values(status='unknown'))
            should_send = changed.rowcount == 1
        # UNKNOWN is committed before external I/O. A crash now resumes lookup.
        try:
            result = (await provider.send(operation_key, target_type, target_id, payload)
                      if should_send else await provider.lookup(operation_key))
        except Exception:
            result = None
        if not isinstance(result, dict) or result.get('status') not in {'accepted', 'failed', 'needs_review', 'pending'}:
            result = dict(status='unknown', operation_key=operation_key)
        else:
            # Retain only provider receipt metadata, not arbitrary echoed bodies.
            result = {k: v for k, v in result.items() if k in {'status', 'id', 'server_id', 'error_code'}}
            result['operation_key'] = operation_key
            for key in ('id', 'server_id', 'error_code'):
                if key in result and not isinstance(result[key], str):
                    raise ValueError('native_provider_receipt_invalid')
        await authorize()
        async with self._own_transaction() as uow:
            session = uow.session
            await self._source(session)
            operation = await session.get(NativeSend, operation_key)
            if operation.status not in {'accepted', 'failed', 'needs_review'}:
                operation.status = result['status']
                operation.receipt = result
            return operation.receipt or result
