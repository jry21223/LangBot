"""Read-only native state projection for the existing Bot detail view."""

from .native_contract import NativeAccessDenied

_STATES = {
    'disabled',
    'needs_authorization',
    'connecting',
    'connected',
    'disconnected',
    'error',
    'unknown',
    'source_available',
    'awaiting_upstream',
    'needs_configuration',
}
_ERRORS = {
    'upstream_disconnected',
    'upstream_not_ready',
    'upstream_unverified',
    'upstream_state_unverified',
    'source_unavailable',
    'source_not_ready',
    'source_disconnected',
    'source_authorization_required',
    'configuration_invalid',
    'source_not_active',
    'source_not_authenticated',
}


def is_native_adapter(ap, adapter_name):
    registry = getattr(getattr(ap, 'platform_mgr', None), 'adapter_dict', {})
    adapter_type = registry.get(adapter_name) if isinstance(registry, dict) else None
    return callable(getattr(adapter_type, 'native_source_binding', None))


def _collector_verified(runtime):
    facts = getattr(runtime, 'native_source_facts', None) or getattr(
        runtime.native_source_authorize, 'last_facts', None
    )
    proof = facts.get('authority_proof') if isinstance(facts, dict) else None
    collector = proof.get('collector') if isinstance(proof, dict) else None
    try:
        from .adapters.agent_wechat.owner_client import collector_connection_state
    except ImportError:
        return False
    return collector_connection_state(collector, runtime.native_ingress_store.binding.account) == 'connected'


async def native_ui_status(ap, context, bot, runtime):
    """RESOURCE_VIEW alone gives no source facts, counts, IDs or original access."""
    result = dict(state='unknown', has_more=False)
    if bot.get('native_configuration_incomplete'):
        return dict(result, state='needs_configuration')
    if bot.get('native_configuration_invalid'):
        return dict(result, state='error', error_code='configuration_invalid')
    if not bot.get('enable'):
        return dict(result, state='disabled')
    if (
        runtime is None
        or getattr(runtime, 'workspace_uuid', None) != context.workspace_uuid
        or getattr(runtime, 'native_ingress_store', None) is None
        or not callable(getattr(runtime, 'native_source_authorize', None))
    ):
        return dict(result, state='needs_authorization', error_code='source_not_active')
    from .native_access import authorize_ui_observer

    async def authorize():
        await authorize_ui_observer(ap, context, bot['uuid'], runtime.native_ingress_store.binding)
        await runtime.native_source_authorize()
        await authorize_ui_observer(ap, context, bot['uuid'], runtime.native_ingress_store.binding)

    try:
        # The trusted source factory checks the registered source audience and
        # its current owner grant. No Host key is exposed to this human route.
        snapshot = await runtime.native_ingress_store.ui_snapshot(authorize=authorize)
        getter = getattr(runtime.adapter, 'native_runtime_state', None)
        state = getter() if callable(getter) else {}
        if not isinstance(state, dict):
            state = {}
        observed = state.get('state', 'unknown')
        if observed not in _STATES:
            observed = 'unknown'
        error = state.get('error_code')
        if error not in _ERRORS:
            error = None
        if observed == 'connected' and (
            state.get('collector_verified') is not True or not _collector_verified(runtime)
        ):
            observed = 'awaiting_upstream'
            error = 'upstream_state_unverified'
        result.update(snapshot, state=observed)
        if error is not None:
            result['error_code'] = error
        await authorize()
        if result['state'] == 'connected' and not _collector_verified(runtime):
            result.update(state='awaiting_upstream', error_code='upstream_state_unverified')
        return result
    except (NativeAccessDenied, PermissionError):
        return dict(state='needs_authorization', error_code='source_authorization_required', has_more=False)
    except Exception:
        # No exception/provider response text or cached source rows on failure.
        return dict(state='unknown', error_code='source_unavailable', has_more=False)
