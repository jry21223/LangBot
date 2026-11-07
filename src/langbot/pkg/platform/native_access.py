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
from ..entity.persistence.workspace import WorkspaceMembership
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
                                 receiver_api_key_uuid=None, source_enabled=False, receiver_enabled=False, expected_revision=None, source_authority=None, ui_observer_accounts=None):
    """Prepare or revise exact access under api_key.manage, disabled by default.

    No secret is accepted or returned. Enabling is an explicit operational action,
    separate from installing code or creating a disabled adapter configuration.
    """
    if request_context.principal.principal_type != PrincipalType.ACCOUNT or not request_context.principal.account_uuid:
        raise NativeAccessDenied("native_registration_requires_account")
    require_permission(request_context, Permission.RESOURCE_MANAGE)
    require_permission(request_context, Permission.API_KEY_MANAGE)
    for value in (bot_uuid, receiver_id, receiver_api_key_uuid):
        _identifier(value, optional=True)
    if type(source_enabled) is not bool or type(receiver_enabled) is not bool:
        raise ValueError('native_registration_flag_invalid')
    if receiver_id is not None and len(receiver_id)>255:
        raise ValueError('native_receiver_id_invalid')
    from .native_authority import validate_expectation
    source_authority=validate_expectation(source_authority,binding)
    observers=[] if ui_observer_accounts is None else ui_observer_accounts
    if (not isinstance(observers,list) or len(observers)>32 or any(not isinstance(a,str) or not 1<=len(a)<=36 for a in observers)
            or len(set(observers))!=len(observers)):
        raise ValueError('native_observer_list_invalid')
    workspace = request_context.workspace_uuid
    source_id = _hash([workspace, bot_uuid])
    binding_hash = _hash(asdict(binding))
    async with ap.persistence_mgr.tenant_uow(workspace) as uow:
        session = uow.session
        bot = await session.get(Bot, bot_uuid)
        if bot is None or bot.workspace_uuid != workspace:
            raise NativeAccessDenied('native_bot_workspace_mismatch')
        for account in observers:
            member=(await session.execute(sa.select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_uuid==workspace,WorkspaceMembership.account_uuid==account,
                WorkspaceMembership.status=='active'))).scalar_one_or_none()
            if member is None:
                raise NativeAccessDenied('native_observer_not_current_member')
        if receiver_api_key_uuid:
            key = (await session.execute(sa.select(ApiKey).where(ApiKey.uuid == receiver_api_key_uuid))).scalar_one_or_none()
            if key is None or key.workspace_uuid != workspace or key.status != 'active' or key.scopes != []:
                raise NativeAccessDenied('native_receiver_requires_narrow_workspace_key')
        if receiver_enabled and (not receiver_id or not receiver_api_key_uuid or not source_enabled):
            raise ValueError('native_receiver_registration_incomplete')
        row = (await session.execute(sa.select(NativeAccess).where(
            NativeAccess.id == source_id).with_for_update())).scalar_one_or_none()
        values=dict(workspace_uuid=workspace,bot_uuid=bot_uuid,binding_sha256=binding_hash,
            registration_request_id=request_context.request_id,registered_by_account_uuid=request_context.principal.account_uuid,
            source_authority=source_authority,ui_observer_accounts=list(observers),source_enabled=source_enabled,
            receiver_id=receiver_id,receiver_api_key_uuid=receiver_api_key_uuid,receiver_enabled=receiver_enabled)
        if row is None:
            if expected_revision is not None:
                raise ValueError('native_registration_revision_conflict')
            source_revision=receiver_revision=1
            session.add(NativeAccess(id=source_id,**values,source_revision=1,receiver_revision=1))
        else:
            if type(expected_revision) is not int or expected_revision!=row.source_revision:
                raise ValueError('native_registration_revision_conflict')
            source_revision=row.source_revision+1
            receiver_revision=row.receiver_revision+1
            changed=await session.execute(sa.update(NativeAccess).where(
                NativeAccess.id==source_id,NativeAccess.source_revision==row.source_revision,
                NativeAccess.receiver_revision==row.receiver_revision,
            ).values(**values,source_revision=source_revision,receiver_revision=receiver_revision))
            if changed.rowcount!=1:
                raise ValueError('native_registration_revision_conflict')
        result=dict(bot_uuid=bot_uuid,binding_sha256=binding_hash,source_enabled=source_enabled,
            source_revision=source_revision,source_authority=source_authority,ui_observer_accounts=list(observers),
            receiver_id=receiver_id,receiver_api_key_uuid=receiver_api_key_uuid,receiver_enabled=receiver_enabled,
            receiver_revision=receiver_revision)

    return result


class NativeAuthorization:
    def __init__(self, ap):
        self.ap = ap

    async def source_registration(self,context,bot_uuid,binding):
        from .native_ingress import NativeIngressStore
        store=NativeIngressStore(self.ap.persistence_mgr,workspace_uuid=context.workspace_uuid,
                                 bot_uuid=bot_uuid,binding=binding)
        async with self.ap.persistence_mgr.tenant_uow(context.workspace_uuid) as uow:
            row=await uow.session.get(NativeAccess,store.source_id)
            if row is None:
                raise NativeAccessDenied('native_source_not_registered')
            fence=NativeFence(row.source_revision)
            await check_fence(uow.session,store,fence)
            if row.source_authority is None:
                raise NativeAccessDenied('native_source_audience_not_registered')
        return fence

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
        expected=row.source_authority
        if expected is None:
            raise NativeAccessDenied('native_source_audience_not_registered')
        facts_reader = getattr(adapter,'native_connection_facts',None)
        if not callable(facts_reader):
            raise NativeAccessDenied('native_connection_authority_unavailable')
        async def authorize():
            async with self.ap.persistence_mgr.tenant_uow(context.workspace_uuid) as uow:
                await check_fence(uow.session,store,fence)
            # Network calls stay outside database transactions. These are fresh
            # source connection facts, not a substitute for the registry grant.
            facts = await facts_reader()
            from .native_authority import verify_authority
            verified=verify_authority(expected,facts,binding)
            if not isinstance(facts,dict) or any(facts.get(k) != v for k,v in (
                ('source',binding.source),('account',binding.account),('scope',binding.scope),('revision',binding.revision))):
                raise NativeAccessDenied('native_source_connection_mismatch')
            async with self.ap.persistence_mgr.tenant_uow(context.workspace_uuid) as uow:
                await check_fence(uow.session,store,fence)
            authorize.last_facts=facts
            authorize.last_authority_proof=verified
            return facts
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
    if request_context.principal.principal_type != PrincipalType.ACCOUNT or not request_context.principal.account_uuid:
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


async def authorize_ui_observer(ap,context,bot_uuid,binding):
    """Explicit human audience, separate from both resource.view and Host keys."""
    from ..api.http.authz import permissions_for_role
    principal=context.principal
    if principal.principal_type!=PrincipalType.ACCOUNT or not principal.account_uuid:
        raise NativeAccessDenied('native_ui_observer_required')
    require_permission(context,Permission.RESOURCE_VIEW)
    source_id=_hash([context.workspace_uuid,bot_uuid])
    async with ap.persistence_mgr.tenant_uow(context.workspace_uuid) as uow:
        row=await uow.session.get(NativeAccess,source_id)
        if (row is None or row.workspace_uuid!=context.workspace_uuid or row.bot_uuid!=bot_uuid
                or row.binding_sha256!=_hash(asdict(binding)) or not row.source_enabled
                or row.source_authority is None or principal.account_uuid not in row.ui_observer_accounts):
            raise NativeAccessDenied('native_ui_observer_not_approved')
        member=(await uow.session.execute(sa.select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_uuid==context.workspace_uuid,
            WorkspaceMembership.account_uuid==principal.account_uuid,WorkspaceMembership.status=='active',
        ))).scalar_one_or_none()
        if member is None or Permission.RESOURCE_VIEW.value not in permissions_for_role(member.role):
            raise NativeAccessDenied('native_ui_membership_revoked')
