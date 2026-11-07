"""Contract tests against a synthetic daemon, never a live account."""

import asyncio
import json
import unittest

from aiohttp import web

from langbot.pkg.platform.adapters.agent_wechat.client import WxClient, WxProtocolError


class WxClientContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.sends = {}
        self.fragmented = False
        self.ack_cursor = None
        self.stream_error = False
        self.message = {
            'id': 'message-1',
            'accountId': 'account-1',
            'threadId': 'group-1',
            'serverId': '9007199254740993123',
            'senderId': 'member-1',
            'type': 1,
            'direction': 'incoming',
            'body': 'hello',
            'createdAt': '2026-11-08T00:00:00Z',
            'version': 'version-1',
            'mentions': {'state': 'known', 'userIds': [], 'self': False, 'all': False},
            'media': [],
        }

        async def endpoint(request):
            self.requests.append((request.method, request.path, dict(request.headers)))
            if request.headers.get('Authorization') != 'Bearer fixture-only':
                return web.json_response({'error': 'forbidden'}, status=403)
            if request.path.endswith('/events/stream'):
                if self.stream_error:
                    return web.Response(
                        text='event: error\ndata: {"error":{"code":"event_cursor_expired"}}\n\n',
                        content_type='text/event-stream',
                    )
                event = {
                    'cursor': '9007199254740994',
                    'kind': 'message.committed',
                    'data': {'messageId': 'message-1', 'threadId': 'group-1'},
                    'createdAt': '2026-11-08T00:00:00Z',
                }
                return web.Response(
                    text=': heartbeat\n\nid: 9007199254740994\ndata: ' + json.dumps(event) + '\n\n',
                    content_type='text/event-stream',
                )
            if request.path.endswith('/media/asset-1/content'):
                return web.Response(body=b'synthetic-media', content_type='image/png')
            if '/consumers/' in request.path:
                payload = await request.json()
                return web.json_response({'cursor': self.ack_cursor or payload['cursor']})
            if request.path.endswith('/messages/message-1'):
                if self.fragmented:
                    response = web.StreamResponse(headers={'Content-Type': 'application/json'})
                    await response.prepare(request)
                    encoded = json.dumps(self.message).encode()
                    await response.write(encoded[:8])
                    await asyncio.sleep(0.02)
                    await response.write(encoded[8:])
                    await response.write_eof()
                    return response
                return web.json_response(self.message)
            if request.path.endswith('/sends/by-idempotency-key'):
                value = self.sends.get(request.query['key'])
                return web.json_response(value or {'error': 'not_found'}, status=200 if value else 404)
            if request.path.endswith('/sends'):
                key = request.headers['Idempotency-Key']
                payload = await request.json()
                old = self.sends.get(key)
                if old is not None and old['destination'] != payload['threadId']:
                    return web.json_response({'error': 'idempotency_conflict'}, status=409)
                value = old or {
                    'id': 'send-' + key,
                    'accountId': 'account-1',
                    'destination': payload['threadId'],
                    'state': 'needs_review',
                    'serverId': None,
                }
                self.sends[key] = value
                return web.json_response(value, status=202)
            return web.json_response({'error': 'not_found'}, status=404)

        app = web.Application()
        app.router.add_route('*', '/{path:.*}', endpoint)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.client = WxClient(f'http://127.0.0.1:{port}/v1', 'fixture-only', 'account-1')

    async def asyncTearDown(self):
        await self.client.close()
        await self.runner.cleanup()

    async def test_original_preserves_opaque_server_id_and_rejects_wrong_account(self):
        message = await self.client.message('message-1')
        self.assertEqual(message['serverId'], '9007199254740993123')
        self.message['accountId'] = 'another-account'
        with self.assertRaisesRegex(WxProtocolError, 'message_binding_mismatch'):
            await self.client.message('message-1')

    async def test_reconnect_uses_saved_cursor_and_heartbeat_does_not_become_message(self):
        events = [event async for event in self.client.events('9007199254740993')]
        self.assertEqual([event['cursor'] for event in events], ['9007199254740994'])
        self.assertEqual(self.requests[0][2]['Last-Event-ID'], '9007199254740993')
        receipt = await self.client.ack('langbot-fixture', events[0]['cursor'])
        self.assertEqual(receipt, {'cursor': '9007199254740994'})

    async def test_fragmented_original_is_read_completely(self):
        self.fragmented = True
        original = await self.client.message('message-1')
        self.assertEqual(original['version'], 'version-1')

    async def test_wrong_ack_and_expired_cursor_are_explicit_failures(self):
        self.ack_cursor = '99'
        with self.assertRaisesRegex(WxProtocolError, 'ack_cursor_mismatch'):
            await self.client.ack('fixture', '10')
        self.stream_error = True
        with self.assertRaisesRegex(WxProtocolError, 'event_cursor_expired'):
            _ = [event async for event in self.client.events('10')]

    async def test_unknown_send_is_looked_up_by_same_operation_without_resend(self):
        receipt = await self.client.send_text('group-1', 'hello', 'operation-1')
        self.assertEqual(receipt['state'], 'needs_review')
        found = await self.client.lookup_send('operation-1')
        self.assertEqual(found['id'], receipt['id'])
        self.assertEqual(len(self.sends), 1)
        self.assertIsNone(await self.client.lookup_send('missing'))

    async def test_distinct_operations_can_send_identical_content(self):
        first = await self.client.send_text('group-1', 'hello', 'operation-1')
        second = await self.client.send_text('group-1', 'hello', 'operation-2')
        self.assertNotEqual(first['id'], second['id'])

    async def test_invalid_operation_and_remote_cleartext_rejected_before_transport(self):
        with self.assertRaisesRegex(WxProtocolError, 'invalid_operation_id'):
            await self.client.send_text('group-1', 'hello', 'bad\nkey')
        with self.assertRaisesRegex(WxProtocolError, 'cleartext_remote_endpoint'):
            WxClient('http://example.test/v1', 'fixture-only', 'account-1')

    async def test_media_bytes_require_exact_hash_length_and_type(self):
        from hashlib import sha256

        result = await self.client.media_content(
            'asset-1', sha256_hex=sha256(b'synthetic-media').hexdigest(), byte_length=15, content_type='image/png'
        )
        self.assertEqual(result, b'synthetic-media')
        for length, checksum, mime in [
            (14, '0' * 64, 'image/png'),
            (15, '0' * 64, 'image/png'),
            (15, sha256(result).hexdigest(), 'text/plain'),
        ]:
            with self.assertRaises(WxProtocolError):
                await self.client.media_content('asset-1', sha256_hex=checksum, byte_length=length, content_type=mime)


if __name__ == '__main__':
    unittest.main()
