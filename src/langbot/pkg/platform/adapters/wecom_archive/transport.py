"""Absolute transport deadlines shared by the host's existing HTTP adapters."""

from contextlib import contextmanager
from contextvars import ContextVar
import http.client
import socket
from threading import Timer
from urllib.parse import urlsplit
import time

current_deadline = ContextVar('enterprise_agent_deadline', default=None)


@contextmanager
def read_budget(timeout):
    """One absolute budget for a complete multi-request owner read."""
    token = current_deadline.set(min(time.monotonic() + timeout, current_deadline.get() or float('inf')))
    try:
        yield
    finally:
        current_deadline.reset(token)


def request_deadline(timeout):
    deadline = min(time.monotonic() + timeout, current_deadline.get() or float('inf'))
    if deadline <= time.monotonic():
        raise TimeoutError('budget_expired')
    return deadline


def read_body(response, *, limit, deadline):
    """Bound the entire body, including a peer which never stops slow-dripping."""
    chunks, length = [], 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        # HTTPResponse retains the socket here even when Connection: close has
        # detached it from HTTPConnection. read1 performs one buffered read.
        sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
        if sock is not None:
            sock.settimeout(remaining)
        chunk = response.read1(min(65536, limit + 1 - length))
        if not chunk:
            return b''.join(chunks)
        length += len(chunk)
        if length > limit:
            raise ValueError('read_result_too_large')
        chunks.append(chunk)


class Response:
    def __init__(self, response, connection, timer, deadline, limit):
        self.response, self.connection, self.timer = response, connection, timer
        self.deadline, self.limit = deadline, limit
        self.headers, self.status = response.headers, response.status

    def close(self):
        self.timer.cancel()
        self.response.close()
        self.connection.close()

    def read(self):
        try:
            return read_body(self.response, limit=self.limit, deadline=self.deadline)
        finally:
            self.close()


def open_http(url, *, method='GET', data=None, headers=None, timeout=10, limit=1024 * 1024):
    """Fixed-owner HTTP only; no proxy, redirect, ambient credentials or retries.

    A socket shutdown watchdog covers header and chunk framing as well as body
    bytes. DNS resolution remains the platform resolver's responsibility.
    """
    deadline = request_deadline(timeout)
    parsed = urlsplit(url)
    cls = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
    connection = cls(parsed.hostname, parsed.port, timeout=max(0.001, deadline - time.monotonic()))
    timer = None
    try:
        connection.connect()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('budget_expired')
        sock = connection.sock

        def abort():
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        timer = Timer(remaining, abort)
        timer.daemon = True
        timer.start()
        path = parsed.path or '/'
        if parsed.query:
            path += '?' + parsed.query
        connection.request(method, path, body=data, headers=headers or {})
        response = connection.getresponse()
        return Response(response, connection, timer, deadline, limit)
    except BaseException:
        if timer is not None:
            timer.cancel()
        connection.close()
        raise
