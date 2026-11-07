"""Read the owner source through its actual HTTP contract in an isolated test."""

import json
from hashlib import sha256
from contextlib import contextmanager
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from langbot.pkg.platform.adapters.wecom_archive.reader import HttpSource, SourceUnavailable


class SavedSourceHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        time.sleep(getattr(self.server, 'response_delay', 0))
        query = parse_qs(urlsplit(self.path).query)
        if self.headers.get('Authorization') != 'Bearer fixture-reader-token-000000':
            self.send_error(401)
            return
        if self.headers.get('X-Operator') != 'fixture-reader':
            self.send_error(403)
            return
        body = {
            'source': getattr(self.server, 'source', 'wecom-fixture'),
            'scope': getattr(self.server, 'scope', 'test-chat'),
            'principal': 'fixture-reader',
            'workspace': getattr(self.server, 'workspace', 'workspace-test'),
            'agent': query.get('agent', [None])[0],
            'revision': 'grant-v2' if getattr(self.server, 'changed', False) else 'grant-v1',
            'active': True,
            'read_only': True,
            'workspace_allowed': True,
        }
        if urlsplit(self.path).path.endswith('/changes'):
            body = {
                'source': 'wecom-fixture',
                'scope': 'test-chat',
                'complete_page': True,
                'previous_cursor': query.get('cursor', [None])[0],
                'next_cursor': 'page-v1',
                'coverage': 'success_zero',
                'gaps': [],
                'items': [],
            }
            body.update(getattr(self.server, 'page_overrides', {}))
            if getattr(self.server, 'change_grant_during_read', False):
                self.server.changed = True
        reference = 'wecom:message/fixture/wr-1'
        text = 'a' * 8191 + '汉b'
        chunks = ['a' * 8191, '汉b']
        if urlsplit(self.path).path.endswith('/original-manifest'):
            body = {
                'reference': reference,
                'version': 'wr-1',
                'parser_version': 'fixture-v1',
                'result_sha256': 'f' * 64,
                'chunk_bytes': 8192,
                'streams': {
                    'text': {'bytes': len(text.encode()), 'sha256': sha256(text.encode()).hexdigest(), 'chunks': 2},
                    'parts': {'bytes': 2, 'sha256': sha256(b'[]').hexdigest(), 'chunks': 1},
                },
            }
        elif urlsplit(self.path).path.endswith('/original-chunk'):
            stream, index = query['stream'][0], int(query['index'][0])
            content = chunks[index] if stream == 'text' else '[]'
            more = stream == 'text' and index == 0
            body = {
                'reference': reference,
                'version': 'wr-1',
                'stream': stream,
                'index': index,
                'content': content,
                'bytes': len(content.encode()),
                'sha256': sha256(content.encode()).hexdigest(),
                'has_more': more,
                'next_index': index + 1 if more else None,
            }
            if getattr(self.server, 'corrupt_chunk', False):
                body['sha256'] = '0' * 64
            if getattr(self.server, 'revoke_during_chunk', False):
                self.server.changed = True
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


class TestSavedArchiveReader(unittest.TestCase):
    def test_authorized_saved_source_returns_a_complete_page(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), SavedSourceHandler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
        thread.start()
        try:
            source = HttpSource(
                f'http://127.0.0.1:{server.server_port}/saved',
                source='wecom-fixture',
                scope='test-chat',
                workspace='workspace-test',
                principal='fixture-reader',
                token='fixture-reader-token-000000',
                timeout=2,
            )
            page = source.read_changes(None, 20)
            self.assertEqual(page['items'], [])
            self.assertTrue(page['complete_page'])
            self.assertEqual(page['next_cursor'], 'page-v1')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_incomplete_owner_page_cannot_be_accepted_as_consumable(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), SavedSourceHandler)
        server.page_overrides = {'complete_page': False}
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
        thread.start()
        try:
            source = HttpSource(
                f'http://127.0.0.1:{server.server_port}/saved',
                source='wecom-fixture',
                scope='test-chat',
                workspace='workspace-test',
                principal='fixture-reader',
                token='fixture-reader-token-000000',
                timeout=2,
            )
            with self.assertRaises(SourceUnavailable):
                source.read_changes(None, 20)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_permission_binding_change_during_page_read_is_rejected(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), SavedSourceHandler)
        server.change_grant_during_read = True
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
        thread.start()
        try:
            source = HttpSource(
                f'http://127.0.0.1:{server.server_port}/saved',
                source='wecom-fixture',
                scope='test-chat',
                workspace='workspace-test',
                principal='fixture-reader',
                token='fixture-reader-token-000000',
                timeout=2,
            )
            with self.assertRaises(SourceUnavailable):
                source.read_changes(None, 20)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_page_from_another_source_scope_is_rejected(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), SavedSourceHandler)
        server.page_overrides = {'scope': 'another-chat'}
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
        thread.start()
        try:
            source = HttpSource(
                f'http://127.0.0.1:{server.server_port}/saved',
                source='wecom-fixture',
                scope='test-chat',
                workspace='workspace-test',
                principal='fixture-reader',
                token='fixture-reader-token-000000',
                timeout=2,
            )
            with self.assertRaises(SourceUnavailable):
                source.read_changes(None, 20)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


@contextmanager
def document_source(**behavior):
    server = ThreadingHTTPServer(('127.0.0.1', 0), SavedSourceHandler)
    for name, value in behavior.items():
        setattr(server, name, value)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    try:
        yield HttpSource(
            f'http://127.0.0.1:{server.server_port}/saved',
            source='wecom-fixture',
            scope='test-chat',
            workspace='workspace-test',
            principal='fixture-reader',
            token='fixture-reader-token-000000',
            timeout=2,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


DOCUMENT_EVENT = {
    'source': 'wecom-fixture',
    'scope': 'test-chat',
    'evidence': 'wecom:message/fixture/wr-1',
    'version': 'wr-1',
}


def test_utf8_original_stream_is_complete_only_after_all_verified_chunks():
    with document_source() as source:
        manifest = source.read_manifest(DOCUMENT_EVENT, agent='analysis-test')
        text = source.read_stream(DOCUMENT_EVENT, agent='analysis-test', manifest=manifest, stream='text')
        assert text == 'a' * 8191 + '汉b'
        assert source.read_stream(DOCUMENT_EVENT, agent='analysis-test', manifest=manifest, stream='parts') == []
        window = source.read_window(DOCUMENT_EVENT, agent='analysis-test')
        assert window['complete'] is False
        assert window['coverage']['text']['complete'] is False
        assert window['coverage']['parts']['complete'] is False


def test_corrupt_original_chunk_cannot_become_complete_content():
    import pytest

    with document_source(corrupt_chunk=True) as source:
        manifest = source.read_manifest(DOCUMENT_EVENT, agent='analysis-test')
        with pytest.raises(SourceUnavailable):
            source.read_stream(DOCUMENT_EVENT, agent='analysis-test', manifest=manifest, stream='text')


def test_permission_change_between_original_chunks_returns_no_partial_document():
    import pytest

    with document_source(revoke_during_chunk=True) as source:
        manifest = source.read_manifest(DOCUMENT_EVENT, agent='analysis-test')
        with pytest.raises(SourceUnavailable):
            source.read_stream(DOCUMENT_EVENT, agent='analysis-test', manifest=manifest, stream='text')


def test_source_page_has_one_deadline_including_both_authority_reads():
    import pytest

    with document_source(response_delay=0.1) as endpoint:
        source = HttpSource(
            endpoint.endpoint.geturl(),
            source='wecom-fixture',
            scope='test-chat',
            workspace='workspace-test',
            principal='fixture-reader',
            token='fixture-reader-token-000000',
            timeout=0.15,
        )
        with pytest.raises(SourceUnavailable):
            source.read_changes(None, 20)
