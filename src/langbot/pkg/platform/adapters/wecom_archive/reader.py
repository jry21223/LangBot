"""Owner-authenticated saved archive reads; no SDK ingestion or business tools.

Credentials stay in these adapters. Neither source records nor model output can
choose a URL, tool, principal, scope, or Workspace.
"""

from __future__ import annotations

import http.client
import json
import re
from urllib.parse import urlencode, urlsplit

from .transport import open_http, read_budget


OWNER_GAP_CODES = frozenset({'history_before_feed_unverified', 'original_unavailable'})


def checked_owner_gaps(page):
    gaps = page.get('gaps')
    if (
        not isinstance(gaps, list)
        or len(gaps) > 2
        or any(not isinstance(code, str) or code not in OWNER_GAP_CODES for code in gaps)
        or len(set(gaps)) != len(gaps)
    ):
        raise SourceUnavailable('missing_page')
    return tuple(gaps)


class SourceUnavailable(ValueError):
    def __init__(self, status='sync_failed'):
        self.status = status
        super().__init__('source_unavailable')


def _endpoint(value):
    parsed = urlsplit(value)
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', '::1'}))
    ):
        raise ValueError('invalid_read_endpoint')
    return parsed


def _token(value):
    if not isinstance(value, str) or not 16 <= len(value) <= 4096 or any(c.isspace() for c in value):
        raise ValueError('read_credential_required')
    return value


class HttpSource:
    def __init__(self, endpoint, *, source, scope, workspace, principal, token, timeout=10):
        self.endpoint = _endpoint(endpoint)
        if any(
            not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,200}', v)
            for v in (source, scope, workspace, principal)
        ):
            raise ValueError('invalid_source_binding')
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 30:
            raise ValueError('invalid_read_timeout')
        self.source, self.scope, self.workspace, self.principal = source, scope, workspace, principal
        self._token = _token(token)
        self.timeout = timeout

    def _request(self, route, **params):
        params.update(source=self.source, scope=self.scope, workspace=self.workspace)
        path = (
            self.endpoint.path.rstrip('/')
            + route
            + '?'
            + urlencode({key: value for key, value in params.items() if value is not None})
        )
        url = self.endpoint._replace(path=path, query='').geturl()
        response = None
        try:
            response = open_http(
                url,
                timeout=self.timeout,
                limit=4 * 1024 * 1024,
                headers={
                    'Authorization': 'Bearer ' + self._token,
                    'X-Operator': self.principal,
                    'Accept': 'application/json',
                },
            )
            if response.status != 200:
                status = {401: 'credential_expired', 403: 'permission_denied'}.get(response.status, 'sync_failed')
                if response.status in {409, 503}:
                    response.limit = min(response.limit, 4096)
                    failure = json.loads(response.read())
                    if isinstance(failure, dict):
                        code = failure.get('code')
                        if isinstance(failure.get('error'), dict):
                            code = failure['error'].get('code')
                        elif isinstance(failure.get('detail'), str):
                            code = failure['detail']
                        if response.status == 503 and code in {'ENTERPRISE_SOURCE_DISABLED', 'SOURCE_DISABLED'}:
                            status = 'disabled'
                        elif response.status == 409 and code in {'ENTERPRISE_SOURCE_CURSOR_INVALID', 'CURSOR_INVALID'}:
                            status = 'missing_page'
                raise SourceUnavailable(status)
            result = json.loads(response.read())
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except SourceUnavailable:
            raise
        except (OSError, ValueError, TypeError, http.client.HTTPException):
            raise SourceUnavailable() from None
        finally:
            if response is not None:
                response.close()

    def authorize(self, source, stage, agent=None):
        if source != self.source or stage not in {'ingest', 'dispatch', 'result', 'display'}:
            raise SourceUnavailable('permission_denied')
        grant = self._request('/authority', stage=stage, agent=agent)
        return self._checked_grant(grant, agent)

    def _checked_grant(self, grant, agent):
        if (
            grant.get('source') != self.source
            or grant.get('scope') != self.scope
            or grant.get('principal') != self.principal
            or grant.get('workspace') != self.workspace
            or grant.get('agent') != agent
            or grant.get('active') is not True
            or grant.get('read_only') is not True
            or grant.get('workspace_allowed') is not True
            or not isinstance(grant.get('revision'), str)
            or not grant['revision']
        ):
            raise SourceUnavailable('permission_denied')
        return grant

    def authorize_evidence(self, event, stage, agent=None):
        if event['source'] != self.source or stage not in {'ingest', 'dispatch', 'result', 'display'}:
            raise SourceUnavailable('permission_denied')
        grant = self._request('/authority', stage=stage, agent=agent, reference=event['evidence'])
        return self._checked_grant(grant, agent)

    def read_source_binding(self):
        """Fetch owner-attested metadata without borrowing a caller account label."""
        with read_budget(self.timeout):
            before = self.authorize(self.source, 'ingest')
            proof = self._request('/source-binding')
            expected = {'source', 'scope', 'principal', 'workspace', 'revision', 'corp_id', 'origin'}
            if (
                set(proof) != expected
                or proof['origin'] != 'owner_source_binding'
                or any(proof[key] != before[key] for key in ('source', 'scope', 'principal', 'workspace', 'revision'))
                or not isinstance(proof['corp_id'], str)
                or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', proof['corp_id'])
                or self.authorize(self.source, 'ingest') != before
            ):
                raise SourceUnavailable('permission_denied')
            return proof

    def read_changes(self, cursor=None, limit=100):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('invalid_page_limit')
        with read_budget(self.timeout):
            before = self.authorize(self.source, 'ingest')
            page = self._request('/changes', cursor=cursor, limit=limit)
            if (
                page.get('complete_page') is not True
                or page.get('source') != self.source
                or page.get('scope') != self.scope
                or page.get('previous_cursor') != cursor
                or page.get('coverage') not in {'success', 'success_zero'}
            ):
                raise SourceUnavailable('missing_page')
            checked_owner_gaps(page)
            if self.authorize(self.source, 'ingest') != before:
                raise SourceUnavailable('permission_denied')
            return page

    def read_original(self, event, *, agent):
        """Exact owner original, current explicit agent grant before and after read."""
        if (
            not isinstance(event, dict)
            or event.get('source') != self.source
            or event.get('scope') != self.scope
            or not isinstance(agent, str)
            or not agent
            or not isinstance(event.get('evidence'), str)
            or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,512}', event['evidence'])
            or not isinstance(event.get('version'), str)
        ):
            raise SourceUnavailable('permission_denied')
        before = self.authorize_evidence(event, 'dispatch', agent)
        value = self._request('/original', reference=event['evidence'])
        if (
            set(value) != {'reference', 'version', 'content'}
            or value['reference'] != event['evidence']
            or value['version'] != event['version']
            or not isinstance(value['content'], str)
            or len(value['content'].encode()) > 16384
            or self.authorize_evidence(event, 'dispatch', agent) != before
        ):
            raise SourceUnavailable()
        return value

    def read_manifest(self, event, *, agent):
        from .documents import read_manifest

        return read_manifest(self, event, agent=agent)

    def read_chunk(self, event, *, agent, manifest, stream, index):
        from .documents import read_chunk

        return read_chunk(self, event, agent=agent, manifest=manifest, stream=stream, index=index)

    def read_window(self, event, *, agent):
        from .documents import read_window

        return read_window(self, event, agent=agent)

    def read_stream(self, event, *, agent, manifest, stream, max_chunks=100, max_bytes=1_000_000):
        from .documents import read_stream

        return read_stream(
            self, event, agent=agent, manifest=manifest, stream=stream, max_chunks=max_chunks, max_bytes=max_bytes
        )
