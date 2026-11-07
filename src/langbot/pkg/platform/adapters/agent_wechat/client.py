"""Bounded client for the existing wx v1 API; no account lifecycle ownership."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import json
from urllib.parse import quote, urlsplit

import aiohttp


class WxProtocolError(RuntimeError):
    """A safe error code that never includes credentials or remote payloads."""


def _identifier(value: str, code: str = 'invalid_identifier') -> str:
    if not isinstance(value, str) or not value or len(value) > 1024 or any(ord(c) < 32 for c in value):
        raise WxProtocolError(code)
    return value


class WxClient:
    """Use one existing daemon account with no login, token creation or daemon control."""

    def __init__(self, base_url: str, token: str, account_id: str, *, timeout: float = 20):
        endpoint = urlsplit(base_url)
        if endpoint.scheme not in {'http', 'https'} or not endpoint.hostname or endpoint.username or endpoint.password:
            raise WxProtocolError('invalid_endpoint')
        if endpoint.query or endpoint.fragment or endpoint.path.rstrip('/') != '/v1':
            raise WxProtocolError('invalid_endpoint')
        if endpoint.scheme == 'http' and endpoint.hostname not in {
            '127.0.0.1',
            'localhost',
            '::1',
            'host.docker.internal',
        }:
            raise WxProtocolError('cleartext_remote_endpoint')
        self.base_url = base_url.rstrip('/')
        self.account_id = _identifier(account_id)
        self._token = _identifier(token, 'invalid_credential')
        self._timeout = timeout
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                headers={'Authorization': 'Bearer ' + self._token},
                timeout=aiohttp.ClientTimeout(total=self._timeout),
                trust_env=False,
            )
        return self._session

    def _url(self, path: str) -> str:
        return self.base_url + '/accounts/' + quote(self.account_id, safe='') + path

    async def _json(
        self, method: str, path: str, *, payload=None, params=None, key: str | None = None, missing: bool = False
    ) -> dict | None:
        headers = {'Idempotency-Key': _identifier(key, 'invalid_operation_id')} if key is not None else {}
        try:
            async with self._http().request(
                method, self._url(path), json=payload, params=params, headers=headers, allow_redirects=False
            ) as response:
                if response.status == 404 and missing:
                    return None
                if response.status in {401, 403}:
                    raise WxProtocolError('authorization_rejected')
                if response.status == 409:
                    raise WxProtocolError('source_or_operation_conflict')
                if not 200 <= response.status < 300:
                    raise WxProtocolError('daemon_request_rejected')
                data = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    data.extend(chunk)
                    if len(data) > 1024 * 1024:
                        raise WxProtocolError('response_too_large')
                result = json.loads(data)
                if not isinstance(result, dict):
                    raise WxProtocolError('invalid_daemon_response')
                return result
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise WxProtocolError('daemon_transport_unavailable') from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WxProtocolError('invalid_daemon_response') from exc

    async def status(self) -> dict:
        return await self._json('GET', '')

    async def capabilities(self) -> dict:
        return await self._json('GET', '/capabilities')

    async def message(self, message_id: str) -> dict:
        message = await self._json('GET', '/messages/' + quote(_identifier(message_id), safe=''))
        if message.get('accountId') != self.account_id or message.get('id') != message_id:
            raise WxProtocolError('message_binding_mismatch')
        return message

    async def media(self, message_id: str) -> dict:
        return await self._json('GET', '/messages/' + quote(_identifier(message_id), safe='') + '/media')

    async def asset(self, asset_id: str) -> dict:
        result = await self._json('GET', '/media/' + quote(_identifier(asset_id), safe=''))
        if result.get('id') != asset_id or result.get('accountId') != self.account_id:
            raise WxProtocolError('asset_binding_mismatch')
        return result

    async def media_content(
        self, asset_id: str, *, sha256_hex: str, byte_length: int, content_type: str, max_bytes: int = 32 * 1024 * 1024
    ) -> bytes:
        """Read exact daemon bytes, never a caller-provided URL or redirect."""
        from hashlib import sha256

        if type(byte_length) is not int or not 0 <= byte_length <= max_bytes:
            raise WxProtocolError('media_size_rejected')
        try:
            async with self._http().get(
                self._url('/media/' + quote(_identifier(asset_id), safe='') + '/content'), allow_redirects=False
            ) as response:
                if response.status != 200 or response.content_type != content_type.split(';', 1)[0]:
                    raise WxProtocolError('media_content_rejected')
                data = bytearray()
                async for chunk in response.content.iter_chunked(16384):
                    data.extend(chunk)
                    if len(data) > byte_length:
                        raise WxProtocolError('media_integrity_mismatch')
                result = bytes(data)
                if len(result) != byte_length or sha256(result).hexdigest() != sha256_hex:
                    raise WxProtocolError('media_integrity_mismatch')
                return result
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise WxProtocolError('media_transport_unavailable') from exc

    async def ack(self, consumer: str, cursor: str) -> dict:
        result = await self._json(
            'POST',
            '/consumers/' + quote(_identifier(consumer), safe='') + '/ack',
            payload={'cursor': _identifier(cursor)},
        )
        if result.get('cursor') != cursor:
            raise WxProtocolError('ack_cursor_mismatch')
        return result

    async def send_text(
        self, thread_id: str, text: str, operation_id: str, *, quote_reference: dict | None = None
    ) -> dict:
        payload = {'threadId': _identifier(thread_id), 'text': text}
        if not isinstance(text, str) or not text or len(text.encode()) > 1024 * 1024:
            raise WxProtocolError('invalid_send_content')
        if quote_reference is not None:
            payload['quote'] = quote_reference
        result = await self._json('POST', '/sends', payload=payload, key=operation_id)
        self._verify_send(result, thread_id)
        return result

    async def lookup_send(self, operation_id: str) -> dict | None:
        result = await self._json(
            'GET',
            '/sends/by-idempotency-key',
            params={'key': _identifier(operation_id, 'invalid_operation_id')},
            missing=True,
        )
        if result is not None:
            self._verify_send(result)
        return result

    def _verify_send(self, result: dict, thread_id: str | None = None) -> None:
        if (
            result.get('accountId') != self.account_id
            or not isinstance(result.get('id'), str)
            or result.get('state') not in {'queued', 'submitting', 'accepted', 'failed', 'needs_review', 'cancelled'}
            or (thread_id is not None and result.get('destination') != thread_id)
        ):
            raise WxProtocolError('send_binding_mismatch')

    async def events(self, after: str | None) -> AsyncIterator[dict]:
        """Yield only complete frames. No cursor is acknowledged by this transport."""
        headers = {'Last-Event-ID': _identifier(after)} if after is not None else {}
        timeout = aiohttp.ClientTimeout(total=None, connect=self._timeout, sock_read=60)
        try:
            async with self._http().get(
                self._url('/events/stream'), headers=headers, timeout=timeout, allow_redirects=False
            ) as response:
                if response.status in {401, 403}:
                    raise WxProtocolError('authorization_rejected')
                if response.status != 200 or response.content_type != 'text/event-stream':
                    raise WxProtocolError('event_stream_rejected')
                buffer = b''
                fields: list[str] = []
                frame_bytes = 0
                async for chunk in response.content.iter_chunked(16384):
                    buffer += chunk
                    if len(buffer) > 1024 * 1024:
                        raise WxProtocolError('event_frame_too_large')
                    while b'\n' in buffer:
                        raw, buffer = buffer.split(b'\n', 1)
                        line = raw.rstrip(b'\r').decode('utf-8')
                        frame_bytes += len(raw)
                        if frame_bytes > 1024 * 1024:
                            raise WxProtocolError('event_frame_too_large')
                        if not line:
                            event = self._parse_frame(fields)
                            fields, frame_bytes = [], 0
                            if event is not None:
                                yield event
                        elif not line.startswith(':'):
                            fields.append(line)
                # A truncated frame is intentionally not delivered. Reconnect uses the last accepted cursor.
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise WxProtocolError('event_stream_disconnected') from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WxProtocolError('invalid_event_frame') from exc

    @staticmethod
    def _parse_frame(fields: list[str]) -> dict | None:
        data = []
        event_id = None
        event_type = None
        for line in fields:
            name, _, value = line.partition(':')
            value = value.removeprefix(' ')
            if name == 'data':
                data.append(value)
            elif name == 'id':
                event_id = _identifier(value)
            elif name == 'event':
                event_type = value
        if not data:
            return None
        event = json.loads('\n'.join(data))
        if event_type == 'error':
            if (
                isinstance(event, dict)
                and isinstance(event.get('error'), dict)
                and event['error'].get('code') == 'event_cursor_expired'
            ):
                raise WxProtocolError('event_cursor_expired')
            raise WxProtocolError('event_stream_error')
        if not isinstance(event, dict) or not isinstance(event.get('kind'), str):
            raise WxProtocolError('invalid_event_frame')
        cursor = _identifier(event.get('cursor'))
        if event_id is not None and event_id != cursor:
            raise WxProtocolError('event_cursor_mismatch')
        if not isinstance(event.get('data'), dict):
            raise WxProtocolError('invalid_event_frame')
        return event
