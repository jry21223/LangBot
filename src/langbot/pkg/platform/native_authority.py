"""Compare separately approved source audience references to current owner proof."""
from copy import deepcopy

from .native_contract import NativeAccessDenied

_OWNER_GRANT={'source','scope','principal','workspace','agent','revision'}
_OWNER_BINDING={'account','thread','revision'}
_FINANCE_BINDING={'source','scope','principal','workspace','revision','corp_id','origin'}


def _identifiers(value,keys,nullable=frozenset()):
    if not isinstance(value,dict) or set(value)!=keys:
        raise ValueError('native_source_authority_schema_invalid')
    for key,item in value.items():
        if item is None and key in nullable: continue
        if not isinstance(item,str) or not 1<=len(item)<=512 or any(ord(c)<32 for c in item):
            raise ValueError('native_source_authority_identity_invalid')


def validate_expectation(value,binding):
    if value is None: return None
    if not isinstance(value,dict): raise ValueError('native_source_authority_schema_invalid')
    kind=value.get('kind')
    if kind=='wechat-owner-v1':
        if set(value)!={'kind','grant','binding'}: raise ValueError('native_source_authority_schema_invalid')
        _identifiers(value['grant'],_OWNER_GRANT,{'agent'})
        _identifiers(value['binding'],_OWNER_BINDING)
        if (value['grant']['source']!=binding.source or value['grant']['scope']!=binding.scope
                or value['binding']['account']!=binding.account):
            raise ValueError('native_source_authority_binding_invalid')
    elif kind=='wecom-source-binding-v1':
        if set(value)!={'kind','binding'}: raise ValueError('native_source_authority_schema_invalid')
        _identifiers(value['binding'],_FINANCE_BINDING)
        proof=value['binding']
        if (proof['source']!=binding.source or proof['scope']!=binding.scope or proof['corp_id']!=binding.account
                or proof['origin']!='owner_source_binding'):
            raise ValueError('native_source_authority_binding_invalid')
    else: raise ValueError('native_source_authority_kind_invalid')
    return deepcopy(value)


def verify_authority(expected,facts,binding):
    """An expectation constrains an authenticated proof; it cannot grant access.

    Source profile Workspace IDs are compared to their explicit approved mapping,
    never silently replaced with local LangBot or OpenAgents Workspace IDs.
    """
    expected=validate_expectation(expected,binding)
    if expected is None: raise NativeAccessDenied('native_source_audience_not_registered')
    proof=facts.get('authority_proof') if isinstance(facts,dict) else None
    if not isinstance(proof,dict): raise NativeAccessDenied('native_source_audience_proof_missing')
    if expected['kind']=='wechat-owner-v1':
        if set(proof)-{'grant','binding','original','collector'} or not {'grant','binding'}<=set(proof):
            raise NativeAccessDenied('native_source_audience_proof_invalid')
        grant=proof['grant']
        if (not isinstance(grant,dict) or set(grant)!=_OWNER_GRANT|{'active','read_only','workspace_allowed'}
                or any(grant.get(k)!=v for k,v in expected['grant'].items())
                or any(grant.get(k) is not True for k in ('active','read_only','workspace_allowed'))
                or proof.get('binding')!=expected['binding']):
            raise NativeAccessDenied('native_source_audience_proof_invalid')
    else:
        if set(proof)!={'binding'} or proof['binding']!=expected['binding']:
            raise NativeAccessDenied('native_source_audience_proof_invalid')
    return deepcopy(proof)
