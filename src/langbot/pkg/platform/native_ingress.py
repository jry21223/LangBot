"""Durable native receipts using the owning LangBot Workspace transaction seam.

Source authorization is supplied by the trusted owner and rechecked per action.
No body, credential, daemon lifecycle or external receiver is configured here.
"""

from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
import re
from datetime import datetime

import sqlalchemy as sa

from ..entity.persistence.native_ingress import NativeEvent, NativeGap, NativePage, NativeSend, NativeSource
from .native_contract import NativeAccessDenied, AcceptanceReceipt, Authorize, SendProvider, SourceBinding, SourcePage, SourceUnresolved


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


_METADATA_FIELDS = {
    'payload': {'change','native','owner_event_id','native_version','event_cursor','business_at','origin','direction',
                'message_type','body_sha256','media','recalled','dispatch_eligible','qualification','native_unresolved'},
    'change': {'source','scope','record','version','sequence','kind','business_at','observed_at','original',
               'projection_hash','goal_reference','saved','original_available'},
    'saved': {'version','sequence','observed_at','original','attachments','message','account_id','thread_id',
              'message_id','sender_id','server_id','direction','message_type','body_available'},
    'message': {'source_seq','message_id','chat_id','chat_type','sender_user_id','sender_identity_type',
                'sender_access_type','message_type','sent_at','action','media_evidence','recalled_message_id',
                'content_materialization'},
    'content_materialization': {'status','evidence_fingerprint','parser_version','input_hash','result_sha256',
                                'text_bytes','parts'},
    'media_evidence': {'mediaId','mediaType','sourceMd5','contentSha256','sourceFileSize','contentSize'},
    'attachments': {'reference','status','type','sha256','size'},
    'native': {'account_id','thread_id','message_id','server_id','sender_id'},
    'media': {'asset_id','kind','state','sha256','byte_length','content_type'},
    'native_unresolved': {'event_kind','event_cursor','reason','asset_id','thread_id','server_id','message_id'},
}


def _metadata(value, field='payload'):
    """Bounded supported source schemas; unknown/free-form containers are refused.

    Source-specific validators additionally enforce exact nested shape, hashes,
    enums and native identity relationships before a source ACK can escape.
    """
    if isinstance(value,dict):
        if field not in _METADATA_FIELDS or not set(value) <= _METADATA_FIELDS[field]:
            raise ValueError('native_metadata_only')
        if field=='media_evidence':
            for key,item in value.items():
                if key in {'sourceFileSize','contentSize'}:
                    valid=type(item) is int and 0<=item<=2**63-1
                elif key=='contentSha256':
                    valid=isinstance(item,str) and re.fullmatch(r'[0-9a-f]{64}',item)
                elif key=='sourceMd5':
                    valid=isinstance(item,str) and re.fullmatch(r'[0-9A-Fa-f]{32}',item)
                else:
                    valid=isinstance(item,str) and 1<=len(item)<=512 and not any(ord(c)<32 for c in item)
                if not valid:
                    raise ValueError('native_metadata_only')
        for key,item in value.items():
            _metadata(item,key)
    elif isinstance(value,list):
        if len(value)>100:
            raise ValueError('native_metadata_only')
        for item in value:
            _metadata(item,field)
    elif isinstance(value,str):
        if len(value)>512 or any(ord(c)<32 for c in value):
            raise ValueError('native_metadata_only')
    elif value is not None and (type(value) not in {bool,int} or (type(value) is int and abs(value)>2**63-1)):
        raise ValueError('native_metadata_only')


class NativeIngressStore:
    def __init__(self, manager, *, workspace_uuid: str, bot_uuid: str, binding: SourceBinding, authorization_fence=None, change_validator=None, owner_gap_codes=frozenset()):
        self.manager = manager
        self.workspace_uuid = _identifier(workspace_uuid)
        self.bot_uuid = _identifier(bot_uuid)
        if len(bot_uuid)>255 or len(workspace_uuid)>36:
            raise ValueError("native_owner_identifier_invalid")
        self.owner_gap_codes = frozenset(owner_gap_codes)
        self.change_validator = change_validator
        self.authorization_fence = authorization_fence
        self.receiver_id = getattr(authorization_fence, "receiver_id", None)
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
        if self.authorization_fence is not None:
            from .native_access import check_fence
            await check_fence(session, self, self.authorization_fence)
        return row

    async def initialize(self, initial_cursor: str | None = None):
        _identifier(initial_cursor, optional=True)
        async with self._own_transaction() as uow:
            if self.authorization_fence is not None:
                from .native_access import check_fence
                await check_fence(uow.session, self, self.authorization_fence)
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

    async def source_sequence_checkpoint(self):
        """The owner's sequence watermark; never inferred from Core position."""
        async with self._own_transaction() as uow:
            return (await self._source(uow.session)).last_source_sequence

    async def accept_page(self, page: SourcePage, *, authorize: Authorize) -> AcceptanceReceipt:
        if not isinstance(page, SourcePage) or page.complete is not True or len(page.events)+len(page.unresolved)+len(page.gaps) > 100:
            raise ValueError('native_complete_page_required')
        if (not isinstance(page.gaps,tuple) or len(page.gaps)>2 or any(not isinstance(code,str) for code in page.gaps) or len(set(page.gaps))!=len(page.gaps)
                or any(code not in self.owner_gap_codes for code in page.gaps)):
            raise ValueError('native_owner_gap_not_supported')
        _identifier(page.previous_cursor, optional=True)
        _identifier(page.next_cursor)
        envelopes = []
        for event in page.events:
            value = asdict(event)
            for key in ('record', 'version', 'reference', 'kind'):
                _identifier(value[key])
            if value['kind'] not in {'created', 'baseline', 'baseline_snapshot', 'changed', 'attachments_changed', 'recalled', 'unresolved'}:
                raise ValueError('native_event_kind_invalid')
            if not isinstance(value['payload'], dict):
                raise ValueError('native_metadata_invalid')
            _metadata(value['payload'])
            if value['kind'] == 'unresolved':
                if 'change' in value['payload'] or not isinstance(value['payload'].get('native_unresolved'), dict):
                    raise ValueError('native_quarantine_invalid')
            else:
                change = self._checked_change(value)
                if self.change_validator is not None:
                    self.change_validator(change)
            envelopes.append(value)
        unresolved=[]
        if not isinstance(page.unresolved,tuple):
            raise ValueError('native_unresolved_invalid')
        for item in page.unresolved:
            if not isinstance(item,SourceUnresolved):
                raise ValueError('native_unresolved_invalid')
            value=asdict(item)
            for key in ('event_id','event_cursor','source','scope'):
                _identifier(value[key])
            if (value['reason']!='native_unresolved' or value['source']!=self.binding.source
                    or value['scope']!=self.binding.scope or type(value['source_sequence']) is not int
                    or not 1<=value['source_sequence']<=2**63-1):
                raise ValueError('native_unresolved_invalid')
            unresolved.append(value)
        frames=[('event',value) for value in envelopes]+[('unresolved',value) for value in unresolved]
        if unresolved:
            def sequence(frame):
                return frame[1]['source_sequence'] if frame[0]=='unresolved' else frame[1]['payload']['change']['sequence']
            if len({sequence(frame) for frame in frames})!=len(frames):
                raise ValueError('native_source_sequence_conflict')
            frames.sort(key=sequence)
        if len(_json(envelopes).encode()) > 1_000_000:
            raise ValueError('native_page_too_large')
        await authorize()
        page_id = _hash([self.source_id, "source", page.previous_cursor, page.next_cursor])
        fingerprint = _hash(dict(events=envelopes,gaps=page.gaps,unresolved=unresolved))
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
                for frame_kind,value in frames:
                    if frame_kind=='unresolved':
                        event_id=_hash([self.source_id,'native-unresolved',value['event_id']])
                        old=await session.get(NativeGap,event_id)
                        if old is None:
                            source.position+=1
                            session.add(NativeGap(id=event_id,workspace_uuid=self.workspace_uuid,source_id=self.source_id,
                                code='native_unresolved',reason='native_unresolved',owner_event_id=value['event_id'],
                                source_sequence=value['source_sequence'],position=source.position,upstream_cursor=value['event_cursor']))
                        elif (old.reason!='native_unresolved' or old.owner_event_id!=value['event_id']
                              or old.source_sequence!=value['source_sequence'] or old.upstream_cursor!=value['event_cursor']):
                            raise ValueError('native_unresolved_conflict')
                        event_ids.append(event_id)
                        continue
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
                for code in page.gaps:
                    gap_id=_hash([self.source_id,'owner-gap',code])
                    old_gap=await session.get(NativeGap,gap_id)
                    if old_gap is None:
                        source.position+=1
                        session.add(NativeGap(id=gap_id,workspace_uuid=self.workspace_uuid,source_id=self.source_id,
                            code=code,position=source.position,upstream_cursor=page.next_cursor))
                    event_ids.append(gap_id)
                sequences=[value['payload']['change']['sequence'] for value in envelopes if 'change' in value['payload']]
                sequences.extend(value['source_sequence'] for value in unresolved)
                if sequences:
                    source.last_source_sequence=max([source.last_source_sequence or 0,*sequences])
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

    def _checked_change(self, envelope):
        change = envelope['payload'].get('change')
        required = {'source', 'scope', 'record', 'version', 'sequence', 'kind', 'business_at',
                    'observed_at', 'original', 'projection_hash', 'goal_reference', 'saved'}
        if not isinstance(change, dict) or set(change) not in (required, required | {'original_available'}):
            raise ValueError('native_router_change_contract_invalid')
        expected = dict(source=self.binding.source, scope=self.binding.scope,
                        record=envelope['record'], version=envelope['version'])
        if any(change[key] != value for key, value in expected.items()):
            raise ValueError('native_router_identity_mismatch')
        if change['original'] != envelope['reference'] or change['kind'] != envelope['kind']:
            raise ValueError('native_router_original_mismatch')
        if (type(change['sequence']) is not int or change['sequence'] < 1
                or not isinstance(change['projection_hash'],str)
                or not re.fullmatch(r'[0-9a-f]{64}',change['projection_hash'])
                or not isinstance(change['saved'],dict)
                or type(change.get('original_available',True)) is not bool):
            raise ValueError('native_router_change_invalid')
        for key in ('business_at','observed_at'):
            try:
                instant = datetime.fromisoformat(change[key].replace('Z','+00:00'))
                if instant.utcoffset() is None:
                    raise ValueError()
            except (TypeError,AttributeError,ValueError):
                raise ValueError('native_router_time_invalid') from None
        code = change['goal_reference']
        if code is not None and (not isinstance(code,str) or not re.fullmatch(r'GD-[A-Za-z0-9-]{1,60}',code)):
            raise ValueError('native_router_goal_invalid')
        return change

    def _router_change(self, event):
        return self._checked_change(event.envelope)

    def _router_parts(self, rows):
        items, gaps = [], []
        for event in rows:
            if isinstance(event,NativeGap):
                value=dict(event_id=event.id,reason=event.reason,source=self.binding.source,scope=self.binding.scope)
                if event.reason=='owner_source_gap':
                    value['code']=event.code
                gaps.append(value)
            elif event.envelope['kind'] == 'unresolved':
                gaps.append(dict(event_id=event.id,reason='native_unresolved',
                                 source=self.binding.source,scope=self.binding.scope))
            else:
                items.append(dict(event_id=event.id,envelope=self._router_change(event)))
        return items,gaps

    async def _router_page_value(self, session, page):
        rows = []
        for event_id in page.event_ids:
            event = await session.get(NativeEvent, event_id)
            if event is None:
                event=await session.get(NativeGap,event_id)
            if event is None or event.source_id != self.source_id:
                raise ValueError('native_router_event_missing')
            rows.append(event)
        items,gaps = self._router_parts(rows)
        value = dict(page_id=page.id, source=self.binding.source, scope=self.binding.scope,
                     complete_page=True, previous_cursor=page.previous_cursor, next_cursor=page.cursor,
                     gaps=gaps, items=items)
        if _hash(value) != page.fingerprint:
            raise ValueError('native_router_page_changed')
        return dict(value, page_sha256=page.fingerprint)

    async def read_router_page(self, cursor: str | None = None, limit=100, *, authorize: Authorize):
        """Reserve one immutable metadata page for the existing unique Host.

        Cursor belongs only to this downstream feed. Source consumer watermarks
        are never reused here. All source states travel, including tombstones.
        """
        self._limit(limit)
        if cursor is not None and (not isinstance(cursor, str) or not cursor.isascii() or not cursor.isdigit() or str(int(cursor)) != cursor):
            raise ValueError('native_router_cursor_invalid')
        position = int(cursor) if cursor is not None else 0
        await authorize()
        async with self._own_transaction() as uow:
            session = uow.session
            source = await self._source(session)
            if source.router_pending_page:
                page = await session.get(NativePage, source.router_pending_page)
                if page.receiver_id != self.receiver_id:
                    raise NativeAccessDenied("native_router_receiver_mismatch")
                if page.previous_cursor != cursor:
                    raise ValueError('native_router_ack_pending')
                return await self._router_page_value(session, page)
            if source.router_cursor != position:
                raise ValueError('native_router_cursor_conflict')
            rows = (await session.execute(sa.select(NativeEvent).where(
                NativeEvent.source_id == self.source_id, NativeEvent.position > position,
            ).order_by(NativeEvent.position).limit(limit))).scalars().all()
            owner_gaps=(await session.execute(sa.select(NativeGap).where(
                NativeGap.source_id==self.source_id,NativeGap.position>position,
            ).order_by(NativeGap.position).limit(limit))).scalars().all()
            rows=sorted([*rows,*owner_gaps],key=lambda row:row.position)[:limit]
            next_cursor = str(rows[-1].position) if rows else str(position)
            page_id = _hash([self.source_id, 'router', self.receiver_id, cursor, next_cursor])
            items,gaps = self._router_parts(rows)
            value = dict(page_id=page_id, source=self.binding.source, scope=self.binding.scope,
                         complete_page=True, previous_cursor=cursor, next_cursor=next_cursor,
                         gaps=gaps, items=items)
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
                    source_id=self.source_id, direction='router', receiver_id=self.receiver_id, previous_cursor=cursor,
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
            if page is None or page.source_id != self.source_id or page.direction != 'router' or page.fingerprint != page_sha256 or page.receiver_id != self.receiver_id:
                raise ValueError('native_router_page_unknown')
            receipt_event_ids = []
            for event_id in page.event_ids:
                event = await session.get(NativeEvent,event_id)
                if event is None:
                    gap=await session.get(NativeGap,event_id)
                    if gap is None or gap.source_id!=self.source_id:
                        raise ValueError('native_router_gap_missing')
                elif event.envelope['kind'] != 'unresolved':
                    receipt_event_ids.append(event_id)
            if len(receipts) != len(receipt_event_ids) or len({r.get('event_id') for r in receipts if isinstance(r, dict)}) != len(receipts):
                raise ValueError('native_router_receipts_incomplete')
            by_id = {r['event_id']: r for r in receipts}
            if set(by_id) != set(receipt_event_ids):
                raise ValueError('native_router_receipts_incomplete')
            for event_id in receipt_event_ids:
                event = await session.get(NativeEvent, event_id)
                receipt = by_id[event_id]
                expected = dict(event_id=event_id, source=self.binding.source,
                                record=event.envelope['record'], version=event.envelope['version'], status='accepted')
                if set(receipt) != set(expected) | {'receipt_id'} or any(receipt[k] != v for k, v in expected.items()):
                    raise ValueError('native_router_receipt_identity_invalid')
                expected_receipt_id = _hash(['langbot-router-receipt-v1', self.binding.source,
                                             event.envelope['record'], event.envelope['version']])
                if receipt['receipt_id'] != expected_receipt_id:
                    raise ValueError('native_router_receipt_id_invalid')
                if event.receiver_receipt is not None and event.receiver_receipt != receipt:
                    raise ValueError('native_router_receipt_conflict')
                event.receiver_receipt = receipt
            if source.router_pending_page == page_id and source.router_cursor == (int(page.previous_cursor) if page.previous_cursor is not None else 0):
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
                return None
            return event.receiver_receipt

    async def send(self, operation_id, target_type, target_id, payload, *, provider: SendProvider, authorize: Authorize):
        """A host-owned operation ID survives crashes and UNKNOWN lookup.

        The caller must not generate this ID from message content. No original
        outgoing body is stored. An absent lookup never authorizes a resend.
        """
        if self.binding.read_only:
            raise ValueError('native_receive_only')
        if isinstance(operation_id,str) and len(operation_id)>255:
            raise ValueError('native_operation_id_invalid')
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

    async def require_current_original(self, reference, version, *, authorize: Authorize):
        """An accepted exact source revision, not a caller-selected remote URL."""
        _identifier(reference)
        _identifier(version)
        await authorize()
        async with self._own_transaction() as uow:
            session=uow.session
            await self._source(session)
            row=(await session.execute(sa.select(NativeEvent).where(
                NativeEvent.source_id==self.source_id,
                NativeEvent.envelope['reference'].as_string()==reference,
                NativeEvent.envelope['version'].as_string()==version,
            ).order_by(NativeEvent.position.desc()).limit(1))).scalar_one_or_none()
            if row is None or row.envelope['kind']=='unresolved':
                raise NativeAccessDenied('native_original_not_accepted')
            record=row.envelope['record']
            latest=(await session.execute(sa.select(NativeEvent).where(
                NativeEvent.source_id==self.source_id,
                NativeEvent.envelope['record'].as_string()==record,
            ).order_by(NativeEvent.position.desc()).limit(1))).scalar_one()
            if latest.id!=row.id or latest.envelope['kind'] in {'recalled','recall'}:
                raise NativeAccessDenied('native_original_changed_or_recalled')
            return row.envelope
