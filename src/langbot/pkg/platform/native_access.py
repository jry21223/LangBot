"""Trusted, default-disabled audience and receiver registration.

This code creates no credentials and broadens no existing key. Operator changes
use the existing API-key management permission. Bot config, HTTP JSON identities
and source-returned labels cannot authorize access by themselves.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import sqlalchemy as sa

from ..api.http.authz import Permission, require_permission
from ..api.http.context import PrincipalType
from ..entity.persistence.apikey import ApiKey
from ..entity.persistence.bot import Bot
from ..entity.persistence.native_ingress import NativeAccess
from .native_contract import NativeAccessDenied
from .native_ingress import _hash, _identifier


@dataclass(frozen=True)
class NativeFence:
    source_revision: int
    receiver_revision: int | None = None
    receiver_id: str | None = None
    api_key_uuid: str | None = None


async def check_fence(session, store, fence):
    row = (await session.execute(sa.select(NativeAccess).where(
        NativeAccess.id == store.source_id).with_for_update())).scalar_one_or_none()
    if (row is None or row.workspace_uuid != store.workspace_uuid or row.bot_uuid != store.bot_uuid
            or row.binding_sha256 != store.binding_hash or row.source_enabled is not True
            or row.source_revision != fence.source_revision):
        raise NativeAccessDenied('native_source_registration_revoked')
    if fence.receiver_id is not None:
        if (not row.receiver_enabled or row.receiver_id != fence.receiver_id
                or row.receiver_api_key_uuid != fence.api_key_uuid or row.receiver_revision != fence.receiver_revision):
            raise NativeAccessDenied('native_receiver_registration_revoked')
        key = (await session.execute(sa.select(ApiKey).where(ApiKey.uuid == fence.api_key_uuid))).scalar_one_or_none()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if (key is None or key.workspace_uuid != store.workspace_uuid or key.status != 'active'
                or key.scopes != [] or (key.expires_at is not None and key.expires_at <= now)):
            raise NativeAccessDenied('native_receiver_key_revoked_or_overprivileged')
    return row


async def register_native_access(ap, request_context, bot_uuid, binding, *, receiver_id=None,
                                 receiver_api_key_uuid=None, source_enabled=False, receiver_enabled=False, expected_revision=None):
    """Prepare or revise exact access under api_key.manage, disabled by default.

    No secret is accepted or returned. Enabling is an explicit operational action,
    separate from installing code or creating a disabled adapter configuration.
    """
    if request_context.principal.principal_type != PrincipalType.ACCOUNT:
        raise NativeAccessDenied("native_registration_requires_account")
    require_permission(request_context, Permission.RESOURCE_MANAGE)
    require_permission(request_context, Permission.API_KEY_MANAGE)
    for value in (bot_uuid, receiver_id, receiver_api_key_uuid):
        _identifier(value, optional=True)
    if type(source_enabled) is not bool or type(receiver_enabled) is not bool:
        raise ValueError('native_registration_flag_invalid')
    workspace = request_context.workspace_uuid
    source_id = _hash([workspace, bot_uuid])
    binding_hash = _hash(asdict(binding))
    async with ap.persistence_mgr.tenant_uow(workspace) as uow:
        session = uow.session
        bot = await session.get(Bot, bot_uuid)
        if bot is None or bot.workspace_uuid != workspace:
            raise NativeAccessDenied('native_bot_workspace_mismatch')
        if receiver_api_key_uuid:
            key = (await session.execute(sa.select(ApiKey).where(ApiKey.uuid == receiver_api_key_uuid))).scalar_one_or_none()
            if key is None or key.workspace_uuid != workspace or key.status != 'active' or key.scopes != []:
                raise NativeAccessDenied('native_receiver_requires_narrow_workspace_key')
        if receiver_enabled and (not receiver_id or not receiver_api_key_uuid or not source_enabled):
            raise ValueError('native_receiver_registration_incomplete')
        row = (await session.execute(sa.select(NativeAccess).where(
            NativeAccess.id == source_id).with_for_update())).scalar_one_or_none()
        if row is None:
            row = NativeAccess(id=source_id, workspace_uuid=workspace, bot_uuid=bot_uuid,
                binding_sha256=binding_hash, source_revision=1, receiver_revision=1)
            session.add(row)
        else:
            if type(expected_revision) is not int or expected_revision != row.source_revision:
                raise ValueError('native_registration_revision_conflict')
            # Any administrative change fences in-flight operations. A running
            # adapter must explicitly restart under the new registration.
            row.source_revision += 1
            row.receiver_revision += 1
        row.registration_request_id = request_context.request_id
        row.registered_by_account_uuid = request_context.principal.account_uuid
        row.binding_sha256 = binding_hash
        row.source_enabled = source_enabled
        row.receiver_id = receiver_id
        row.receiver_api_key_uuid = receiver_api_key_uuid
        row.receiver_enabled = receiver_enabled
        result = dict(bot_uuid=bot_uuid, binding_sha256=binding_hash, source_enabled=source_enabled,
            source_revision=row.source_revision, receiver_id=receiver_id,
            receiver_api_key_uuid=receiver_api_key_uuid, receiver_enabled=receiver_enabled,
            receiver_revision=row.receiver_revision)
    return result


class NativeAuthorization:
    def __init__(self, ap):
        self.ap = ap

    async def source_factory(self, context, bot_uuid, binding, adapter):
        from .native_ingress import NativeIngressStore
        store = NativeIngressStore(self.ap.persistence_mgr,workspace_uuid=context.workspace_uuid,
                                   bot_uuid=bot_uuid,binding=binding)
        async with self.ap.persistence_mgr.tenant_uow(context.workspace_uuid) as uow:
            row = await uow.session.get(NativeAccess,store.source_id)
            if row is None:
                raise NativeAccessDenied('native_source_not_registered')
            fence = NativeFence(row.source_revision)
            await check_fence(uow.session,store,fence)
        facts_reader = getattr(adapter,'native_connection_facts',None)
        if not callable(facts_reader):
            raise NativeAccessDenied('native_connection_authority_unavailable')
        async def authorize():
            async with self.ap.persistence_mgr.tenant_uow(context.workspace_uuid) as uow:
                await check_fence(uow.session,store,fence)
            # Network calls stay outside database transactions. These are fresh
            # source connection facts, not a substitute for the registry grant.
            facts = await facts_reader()
            authority_kind = getattr(adapter,'native_authority_kind','workspace-grant')
            if callable(authority_kind):
                authority_kind = authority_kind()
            if authority_kind == 'workspace-grant':
                if not isinstance(facts,dict) or facts.get('workspace') != context.workspace_uuid:
                    raise NativeAccessDenied('native_source_workspace_audience_mismatch')
            elif authority_kind != 'account-session':
                raise NativeAccessDenied('native_source_authority_kind_invalid')
            if not isinstance(facts,dict) or any(facts.get(k) != v for k,v in (
                ('source',binding.source),('account',binding.account),('scope',binding.scope),('revision',binding.revision))):
                raise NativeAccessDenied('native_source_connection_mismatch')
            async with self.ap.persistence_mgr.tenant_uow(context.workspace_uuid) as uow:
                await check_fence(uow.session,store,fence)
        authorize.native_fence = fence
        return authorize

    async def receiver_factory(self, context, bot_uuid, binding):
        from .native_ingress import NativeIngressStore
        principal = context.principal
        if (principal.principal_type != PrincipalType.API_KEY or not principal.api_key_uuid
                or principal.api_key_uuid in {'global-oss-api-key','legacy-oss-api-key'}):
            raise NativeAccessDenied('native_receiver_requires_dedicated_key')
        store = NativeIngressStore(self.ap.persistence_mgr,workspace_uuid=context.workspace_uuid,
                                   bot_uuid=bot_uuid,binding=binding)
        async with self.ap.persistence_mgr.tenant_uow(context.workspace_uuid) as uow:
            row = await uow.session.get(NativeAccess,store.source_id)
            if row is None or row.receiver_api_key_uuid != principal.api_key_uuid or not row.receiver_id:
                raise NativeAccessDenied('native_receiver_not_registered')
            fence = NativeFence(row.source_revision,row.receiver_revision,row.receiver_id,principal.api_key_uuid)
            await check_fence(uow.session,store,fence)
        async def authorize():
            async with self.ap.persistence_mgr.tenant_uow(context.workspace_uuid) as uow:
                await check_fence(uow.session,store,fence)
        authorize.native_fence = fence
        return authorize


async def registration_binding(ap, request_context, bot_uuid):
    """Resolve a disabled persistent bot without opening its source or secrets."""
    if request_context.principal.principal_type != PrincipalType.ACCOUNT:
        raise NativeAccessDenied("native_registration_requires_account")
    require_permission(request_context,Permission.API_KEY_MANAGE)
    require_permission(request_context,Permission.RESOURCE_MANAGE)
    import copy
    from .adapter_names import canonical_adapter_name
    from .logger import EventLogger
    from ..api.http.context import ExecutionContext
    async with ap.persistence_mgr.tenant_uow(request_context.workspace_uuid) as uow:
        bot = await uow.session.get(Bot,bot_uuid)
        if bot is None or bot.workspace_uuid != request_context.workspace_uuid:
            raise NativeAccessDenied('native_bot_workspace_mismatch')
        adapter_name = canonical_adapter_name(bot.adapter)
        config = copy.deepcopy(bot.adapter_config)
    adapter_type = ap.platform_mgr.adapter_dict.get(adapter_name)
    if adapter_type is None or not callable(getattr(adapter_type,'native_source_binding',None)):
        raise ValueError('native_adapter_not_registered')
    logger = EventLogger(name='native-registration',ap=ap,
                        execution_context=ExecutionContext.from_request(request_context,bot_uuid=bot_uuid),owner=bot_uuid)
    # Native adapter constructors parse configuration only. Connection setup and
    # credential resolution are explicit runtime hooks, never called here.
    adapter = adapter_type(config,logger)
    return adapter.native_source_binding()
