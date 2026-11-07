"""Stable-key wx sends. An accepted intent is not a delivered-message receipt."""

from .client import WxProtocolError


def receipt(value):
    if value is None:
        return None
    state = value['state']
    result = {
        'status': {
            'accepted': 'accepted',
            'failed': 'failed',
            'cancelled': 'failed',
            'needs_review': 'needs_review',
            'queued': 'pending',
            'submitting': 'pending',
        }[state],
        'id': value['id'],
    }
    if value.get('serverId') is not None:
        result['server_id'] = value['serverId']
    return result


class WxSendProvider:
    def __init__(self, client, allowed_threads):
        self.client, self.allowed_threads = client, frozenset(allowed_threads)

    async def send(self, operation_key, target_type, target_id, payload):
        if target_type != 'group' or target_id not in self.allowed_threads or set(payload) != {'text'}:
            raise WxProtocolError('send_scope_rejected')
        return receipt(await self.client.send_text(target_id, payload['text'], operation_key))

    async def lookup(self, operation_key):
        value = await self.client.lookup_send(operation_key)
        if value is not None and value.get('destination') not in self.allowed_threads:
            raise WxProtocolError('send_scope_rejected')
        return receipt(value)
