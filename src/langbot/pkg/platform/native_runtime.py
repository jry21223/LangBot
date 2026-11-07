"""Default-closed runtime plumbing; this module grants no source access."""

from .native_contract import NativeAccessDenied
from .native_ingress import NativeIngressStore


class NativeAdapterGuard:
    """Block legacy SDK egress for ingress-only adapters at every caller path.

    A future explicitly approved Host sender uses NativeIngressStore.send with
    its persisted operation ID. Generic pipeline/plugin/HTTP send cannot invent
    that identity or turn a receive-only source into a sender.
    """

    def __init__(self, adapter):
        self._adapter = adapter

    def __getattr__(self, name):
        return getattr(self._adapter, name)

    async def send_with_context(self, *args, **kwargs):
        raise NativeAccessDenied("native_host_sender_not_registered")

    async def send_message(self, *args, **kwargs):
        raise NativeAccessDenied('native_host_operation_required')

    async def reply_message(self, *args, **kwargs):
        raise NativeAccessDenied('native_host_operation_required')

    async def send_streaming_message(self, *args, **kwargs):
        raise NativeAccessDenied('native_host_operation_required')

    async def reply_streaming_message(self, *args, **kwargs):
        raise NativeAccessDenied('native_host_operation_required')


async def attach_native_runtime(runtime):
    """Attach only an owner-validated capability, never config-as-authorization.

    The embedding application must provide a trusted factory after the precise
    source principal/Workspace audience mapping has been approved. The factory
    returns a callable that rechecks that mapping and the current source grant.
    Its absence leaves the native source disabled, including on restarts.
    """
    from .native_access import NativeAuthorization
    factory = getattr(runtime.ap, "native_ingress_authorizer_factory", None)
    if factory is None:
        factory = NativeAuthorization(runtime.ap).source_factory
    if not callable(factory):
        raise NativeAccessDenied("native_source_authorizer_not_configured")
    binding = runtime.adapter.native_source_binding()
    authorize_source = await factory(runtime.execution_context, runtime.bot_entity.uuid, binding, runtime.adapter)
    if not callable(authorize_source):
        raise NativeAccessDenied('native_source_authorizer_invalid')

    async def authorize():
        await runtime.assert_execution_active()
        await authorize_source()

    await authorize()
    store = NativeIngressStore(runtime.ap.persistence_mgr, workspace_uuid=runtime.workspace_uuid,
                               bot_uuid=runtime.bot_entity.uuid, binding=binding,
                               authorization_fence=getattr(authorize_source,"native_fence",None))
    # initialize is adapter-owned because its initial source cursor must be
    # explicit, persisted, and included in the immutable configuration revision.
    runtime.adapter.attach_native_ingress(store, authorize)
    runtime.native_source_authorize = authorize
    runtime.native_ingress_store = store
    return store


async def authorized_native_store(ap, request_context, bot_uuid):
    """Resolve the current runtime and a specifically registered receiver."""
    runtime = await ap.platform_mgr.get_bot_by_uuid(request_context, bot_uuid)
    if runtime is None or runtime.native_ingress_store is None:
        raise NativeAccessDenied('native_source_not_active')
    from .native_access import NativeAuthorization
    factory = getattr(ap, 'native_router_authorizer_factory', None)
    if factory is None:
        factory = NativeAuthorization(ap).receiver_factory
    if not callable(factory):
        raise NativeAccessDenied('native_receiver_not_configured')
    authorize_receiver = await factory(request_context, bot_uuid, runtime.native_ingress_store.binding)
    if not callable(authorize_receiver):
        raise NativeAccessDenied('native_receiver_authorizer_invalid')

    async def authorize():
        await runtime.assert_execution_active()
        # The source grant and owning audience can be revoked independently
        # from the Host principal, so each action must recheck both.
        await runtime.native_source_authorize()
        await authorize_receiver()

    await authorize()
    receiver_store = NativeIngressStore(ap.persistence_mgr,workspace_uuid=runtime.workspace_uuid,
        bot_uuid=runtime.bot_entity.uuid,binding=runtime.native_ingress_store.binding,
        authorization_fence=getattr(authorize_receiver,"native_fence",None))
    return receiver_store, authorize, runtime
