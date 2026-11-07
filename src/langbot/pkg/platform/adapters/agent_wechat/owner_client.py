"""Metadata-only client for the existing owner's fixed scoped native v1 API.

It has no wx account token and exposes no original, media, materialization or
send operation. Endpoint and narrow reader identity must come from the trusted
service registry. Installing this class neither creates a reader nor opens a
network route.
"""

from copy import deepcopy
from datetime import datetime, timezone
import asyncio
import json
import re
from urllib.parse import urlsplit

import aiohttp

from .owner_contract import OwnerContract, NATIVE_V1
from .projection import identity
from .client import WxProtocolError


class OwnerProtocolError(WxProtocolError):
    """Bounded local error codes only, never remote content or credentials."""


def _id(value):
    try:
        identity(value)
        if len(value) > 512:
            raise ValueError()
    except (TypeError, ValueError):
        raise OwnerProtocolError('owner_identity_invalid') from None
    return value


def _hash(value, *, optional=False):
    if optional and value is None:
        return
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise OwnerProtocolError('owner_hash_invalid')


def _media(items):
    if not isinstance(items, list) or len(items) > 100:
        raise OwnerProtocolError('owner_media_metadata_invalid')
    ids = set()
    for item in items:
        if not isinstance(item, dict) or set(item) != {
            'asset_id',
            'kind',
            'state',
            'sha256',
            'byte_length',
            'content_type',
        }:
            raise OwnerProtocolError('owner_media_metadata_invalid')
        asset_id = _id(item['asset_id'])
        if asset_id in ids:
            raise OwnerProtocolError('owner_media_duplicate')
        ids.add(asset_id)
        if not isinstance(item['kind'], str) or item['kind'] not in {'image', 'voice', 'file', 'video', 'sticker'}:
            raise OwnerProtocolError('owner_media_kind_invalid')
        if not isinstance(item['state'], str) or item['state'] not in {
            'pending',
            'available',
            'expired',
            'unsupported',
            'failed',
            'cancelled',
            'needs_review',
        }:
            raise OwnerProtocolError('owner_media_state_invalid')
        _hash(item['sha256'], optional=True)
        length, mime = item['byte_length'], item['content_type']
        if length is not None and (type(length) is not int or not 0 <= length <= 9223372036854775807):
            raise OwnerProtocolError('owner_media_length_invalid')
        if mime is not None and (
            not isinstance(mime, str)
            or len(mime) > 255
            or re.fullmatch(r'[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+', mime) is None
        ):
            raise OwnerProtocolError('owner_media_type_invalid')
        if item['state'] == 'available' and (item['sha256'] is None or length is None or mime is None):
            raise OwnerProtocolError('owner_available_media_integrity_required')


def validate_collector_proof(value, account):
    """Return exact fresh collector facts, or no evidence; this grants no data access."""
    fields = {'account', 'authenticated', 'connection', 'incoming_transport', 'contact_sync_ready', 'observed_at'}
    if not isinstance(value, dict) or set(value) != fields or value['account'] != account:
        return None
    if type(value['authenticated']) is not bool or type(value['contact_sync_ready']) is not bool:
        return None
    if (
        not isinstance(value['connection'], str)
        or value['connection'] not in {'online', 'reconnecting', 'disconnected'}
        or not isinstance(value['incoming_transport'], str)
        or value['incoming_transport'] not in {'native_push', 'heartbeat_polling', 'disconnected'}
    ):
        return None
    try:
        if not isinstance(value['observed_at'], str):
            return None
        observed = datetime.fromisoformat(value['observed_at'].replace('Z', '+00:00'))
        if observed.tzinfo is None:
            return None
        age = (datetime.now(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()
        if not -5 <= age <= 30:
            return None
    except (ValueError, OverflowError):
        return None
    return deepcopy(value)


def collector_connection_state(value, account):
    """Only authenticated online native-push and sync-ready evidence is connected."""
    current = validate_collector_proof(value, account)
    if current is None:
        return 'unknown'
    if not current['authenticated'] or current['connection'] == 'disconnected':
        return 'disconnected'
    if (
        current['connection'] == 'online'
        and current['incoming_transport'] == 'native_push'
        and current['contact_sync_ready']
    ):
        return 'connected'
    return 'awaiting_upstream'


class OwnerNativeClient:
    metadata_only = True

    def __init__(
        self,
        endpoint,
        credential,
        *,
        operator,
        source,
        scope,
        workspace,
        account,
        thread,
        stage='ingest',
        agent,
        grant_revision,
        binding_revision,
        contract: OwnerContract,
    ):
        url = urlsplit(endpoint)
        if (
            url.scheme not in {'http', 'https'}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {'', '/'}
        ):
            raise OwnerProtocolError('owner_endpoint_invalid')
        if url.scheme == 'http' and url.hostname not in {'127.0.0.1', '::1', 'localhost', 'host.docker.internal'}:
            raise OwnerProtocolError('owner_cleartext_remote_rejected')
        self.endpoint, self._credential = endpoint.rstrip('/'), _id(credential)
        self.operator, self.source, self.scope = _id(operator), _id(source), _id(scope)
        self.workspace, self.account_id, self.thread = _id(workspace), _id(account), _id(thread)
        if stage != 'ingest':
            raise OwnerProtocolError('owner_metadata_ingest_stage_required')
        self.stage, self.agent = 'ingest', None if agent is None else _id(agent)
        self.grant_revision, self.binding_revision = _id(grant_revision), _id(binding_revision)
        if contract is not NATIVE_V1:
            raise OwnerProtocolError('owner_contract_not_registered')
        self.contract = contract
        self._session = None
        self.collector = None

    async def close(self):
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _get(self, operation, params):
        if operation not in {'binding', 'changes', 'authority', 'descriptor'}:
            raise OwnerProtocolError('owner_metadata_operation_forbidden')
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=20),
                trust_env=False,
                headers={'Authorization': 'Bearer ' + self._credential, 'X-Operator': self.operator},
            )
        query = {'source': self.source, 'scope': self.scope, 'workspace': self.workspace, **params}
        try:
            async with self._session.get(
                self.endpoint + '/native/' + operation, params=query, allow_redirects=False
            ) as response:
                if response.status in {403, 401}:
                    raise OwnerProtocolError('owner_authorization_rejected')
                if response.status == 404:
                    raise OwnerProtocolError('owner_exact_identity_unknown')
                if response.status == 409:
                    raise OwnerProtocolError('owner_version_or_materialization_changed')
                if response.status != 200:
                    raise OwnerProtocolError('owner_request_rejected')
                data = bytearray()
                async for part in response.content.iter_chunked(16384):
                    data.extend(part)
                    if len(data) > 1_000_000:
                        raise OwnerProtocolError('owner_response_too_large')
                result = json.loads(data)
                if not isinstance(result, dict):
                    raise OwnerProtocolError('owner_response_invalid')
                return result
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise OwnerProtocolError('owner_transport_unavailable') from None
        except (json.JSONDecodeError, UnicodeError):
            raise OwnerProtocolError('owner_response_invalid') from None

    def upstream_state(self):
        return collector_connection_state(self.collector, self.account_id)

    async def native_authority(self, *, reference=None, version=None):
        self.collector = None
        if (reference is None) != (version is None):
            raise OwnerProtocolError('owner_exact_version_required')
        query = {'stage': self.stage, 'audience_workspace': self.workspace}
        if self.agent is not None:
            query['agent'] = self.agent
        if reference is not None:
            query.update(reference=_id(reference), version=_id(version))
        result = await self._get('authority', query)
        if set(result) - {'grant', 'binding', 'original', 'collector'} or not {'grant', 'binding'} <= set(result):
            raise OwnerProtocolError('owner_authority_schema_invalid')
        grant, binding = result['grant'], result['binding']
        if not isinstance(grant, dict) or set(grant) != {
            'source',
            'scope',
            'principal',
            'workspace',
            'agent',
            'active',
            'read_only',
            'workspace_allowed',
            'revision',
        }:
            raise OwnerProtocolError('owner_grant_schema_invalid')
        expected = {
            'source': self.source,
            'scope': self.scope,
            'principal': self.operator,
            'workspace': self.workspace,
            'agent': self.agent,
            'revision': self.grant_revision,
        }
        if any(grant.get(key) != value for key, value in expected.items()) or any(
            grant.get(key) is not True for key in ('active', 'read_only', 'workspace_allowed')
        ):
            raise OwnerProtocolError('owner_current_grant_rejected')
        if binding != {'account': self.account_id, 'thread': self.thread, 'revision': self.binding_revision}:
            raise OwnerProtocolError('owner_current_binding_rejected')
        if reference is not None:
            original = result.get('original')
            fields = {
                'reference',
                'version',
                'native_version',
                'recalled',
                'available',
                'dispatch_eligible',
                'input_hash',
                'parser_version',
                'result_sha256',
            }
            if not isinstance(original, dict) or set(original) != fields:
                raise OwnerProtocolError('owner_current_original_schema_invalid')
            if original['reference'] != reference or original['version'] != version:
                raise OwnerProtocolError('owner_current_version_mismatch')
            _id(original['native_version'])
            for field in ('recalled', 'available', 'dispatch_eligible'):
                if type(original[field]) is not bool:
                    raise OwnerProtocolError('owner_original_state_invalid')
            _hash(original['input_hash'])
            _hash(original['result_sha256'], optional=True)
            if original['parser_version'] is not None:
                _id(original['parser_version'])
        elif 'original' in result:
            raise OwnerProtocolError('owner_unrequested_original')
        self.collector = validate_collector_proof(result.get('collector'), self.account_id)
        result.pop('collector', None)
        if self.collector is not None:
            result['collector'] = deepcopy(self.collector)
        return deepcopy(result)

    async def native_binding(self):
        await self.native_authority()
        result = await self._get('binding', {})
        expected = {
            'source': self.source,
            'scope': self.scope,
            'principal': self.operator,
            'workspace': self.workspace,
            'revision': self.grant_revision,
            'account': self.account_id,
            'thread': self.thread,
            'origin': 'owner_source_binding',
        }
        if result != expected:
            raise OwnerProtocolError('owner_binding_metadata_invalid')
        await self.native_authority()
        return deepcopy(result)

    async def native_descriptor(self, reference, version, *, include_history=False):
        if type(include_history) is not bool:
            raise OwnerProtocolError('owner_history_flag_invalid')
        await self.native_authority()
        result = await self._get(
            'descriptor',
            {
                'reference': _id(reference),
                'version': _id(version),
                'include_history': 'true' if include_history else 'false',
            },
        )
        fields = {'reference', 'version', 'projection_hash', 'kind', 'original_available'}
        if (
            set(result) != fields
            or result['reference'] != reference
            or result['version'] != version
            or type(result['original_available']) is not bool
            or not isinstance(result['kind'], str)
            or result['kind'] not in {'created', 'baseline', 'changed', 'attachments_changed', 'recalled'}
        ):
            raise OwnerProtocolError('owner_descriptor_metadata_invalid')
        _hash(result['projection_hash'])
        await self.native_authority()
        return deepcopy(result)

    async def native_changes(self, *, cursor=None, limit=100):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise OwnerProtocolError('owner_page_limit_invalid')
        query = {'limit': limit}
        if cursor is not None:
            query['cursor'] = _id(cursor)
        await self.native_authority()
        page = await self._get('changes', query)
        if set(page) != {'source', 'scope', 'previous_cursor', 'next_cursor', 'complete_page', 'items', 'gaps'}:
            raise OwnerProtocolError('owner_page_schema_invalid')
        if page['source'] != self.source or page['scope'] != self.scope or page['previous_cursor'] != cursor:
            raise OwnerProtocolError('owner_page_binding_invalid')
        if page['complete_page'] is not True or not isinstance(page['items'], list) or len(page['items']) > limit:
            raise OwnerProtocolError('owner_complete_page_required')
        _id(page['next_cursor'])
        if (
            not isinstance(page['gaps'], list)
            or len(page['gaps']) > 100
            or any(
                not isinstance(gap, str) or gap not in {'history_before_activation_unverified', 'original_unavailable'}
                for gap in page['gaps']
            )
        ):
            raise OwnerProtocolError('owner_gap_contract_invalid')
        previous_sequence, versions, event_ids = 0, set(), set()
        for item in page['items']:
            normal_fields = {
                'event_id',
                'change',
                'native_version',
                'event_cursor',
                'media',
                'qualification',
                'dispatch_eligible',
            }
            unresolved_fields = {'event_id', 'kind', 'source_sequence', 'event_cursor', 'reason', 'source', 'scope'}
            if not isinstance(item, dict) or set(item) not in (normal_fields, unresolved_fields):
                raise OwnerProtocolError('owner_item_schema_invalid')
            unresolved = set(item) == unresolved_fields
            event_id = _id(item['event_id'])
            if event_id in event_ids:
                raise OwnerProtocolError('owner_event_id_duplicate')
            event_ids.add(event_id)
            if unresolved:
                if (
                    item['kind'] != 'unresolved'
                    or item['reason'] != 'native_unresolved'
                    or item['source'] != self.source
                    or item['scope'] != self.scope
                    or type(item['source_sequence']) is not int
                    or not previous_sequence < item['source_sequence'] <= 9223372036854775807
                ):
                    raise OwnerProtocolError('owner_unresolved_invalid')
                _id(item['event_cursor'])
                previous_sequence = item['source_sequence']
                continue
            _id(item['native_version'])
            _id(item['event_cursor'])
            _id(item['qualification'])
            _media(item['media'])
            if type(item['dispatch_eligible']) is not bool:
                raise OwnerProtocolError('owner_eligibility_invalid')
            change = item['change']
            try:
                self.contract.validate(change)
            except (TypeError, KeyError, ValueError):
                raise OwnerProtocolError('owner_change_contract_invalid') from None
            if (
                change['source'] != self.source
                or change['scope'] != self.scope
                or change['saved']['account_id'] != self.account_id
                or change['saved']['thread_id'] != self.thread
            ):
                raise OwnerProtocolError('owner_change_binding_invalid')
            sequence = change['sequence']
            if sequence <= previous_sequence:
                raise OwnerProtocolError('owner_sequence_not_monotonic')
            previous_sequence = sequence
            key = (change['record'], change['version'])
            if key in versions:
                raise OwnerProtocolError('owner_version_reused')
            versions.add(key)
            if item['dispatch_eligible'] and (
                change['kind'] in {'baseline', 'recalled'} or not change['original_available']
            ):
                raise OwnerProtocolError('owner_ineligible_dispatch')
        await self.native_authority()
        return deepcopy(page)
