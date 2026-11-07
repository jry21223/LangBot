"""Exact accepted originals delegated to the live owning native adapter.

Text/bytes are returned only to the registered reader and never persisted in
LangBot event metadata or copied to public monitoring events.
"""
from dataclasses import asdict
from hashlib import sha256

from .native_contract import NativeAccessDenied
from .native_runtime import authorized_native_store


async def read_native_original(ap,context,bot_uuid,kind,reference,version,*,part_index=None,asset_id=None):
    if kind not in {'authority','original','manifest','chunk','media'}:
        raise ValueError('native_original_operation_invalid')
    if kind in {'original','chunk','media'}:
        raise NativeAccessDenied('native_metadata_reader_originals_forbidden')
    store,authorize,runtime=await authorized_native_store(ap,context,bot_uuid)
    await store.require_current_original(reference,version,authorize=authorize)
    names={'authority':'native_authority','original':'native_original',
           'manifest':'native_original_manifest','chunk':'native_original_chunk','media':'native_media'}
    method=getattr(runtime.adapter,names[kind],None)
    if not callable(method):
        raise NativeAccessDenied('native_original_operation_not_supported')
    args=[reference,version]
    if kind=='chunk':
        if type(part_index) is not int or part_index<0:
            raise ValueError('native_original_part_invalid')
        args.append(part_index)
    if kind=='media':
        if not isinstance(asset_id,str) or not asset_id or len(asset_id)>512:
            raise ValueError('native_media_id_invalid')
        args.append(asset_id)
    result=await method(*args)
    if not isinstance(result,dict) or result.get('reference')!=reference or result.get('version')!=version:
        raise ValueError('native_original_provider_binding_invalid')
    if kind=='media':
        data=result.get('bytes')
        if (not isinstance(data,bytes) or result.get('asset_id')!=asset_id
                or result.get('byte_length')!=len(data) or result.get('sha256')!=sha256(data).hexdigest()
                or not isinstance(result.get('content_type'),str)):
            raise ValueError('native_media_integrity_invalid')
    elif kind in {'original','chunk'}:
        text=result.get('text')
        if not isinstance(text,str) or len(text.encode('utf-8'))>(1_000_000 if kind=='original' else 16_384):
            raise ValueError('native_original_size_invalid')
    await store.require_current_original(reference,version,authorize=authorize)
    # Source methods must perform their own current native version/recall checks
    # around I/O. The Core rechecks its current durable identity and grant too.
    if kind=='authority':
        fence=store.authorization_fence
        result=dict(binding=dict(asdict(store.binding),workspace_uuid=store.workspace_uuid,
            receiver_id=store.receiver_id,source_revision=fence.source_revision,
            receiver_revision=fence.receiver_revision),original=result)
    return result
