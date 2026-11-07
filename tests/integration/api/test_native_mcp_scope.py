"""Preserve task-owned MCP isolation and exact transport security configuration."""
import asyncio
import contextvars
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from mcp.server.transport_security import TransportSecurityMiddleware

from langbot.pkg.api.mcp.context import bind_request_context,reset_request_context
from langbot.pkg.api.mcp.server import LangBotMCPServer
from langbot.pkg.api.mcp.security import transport_security
from langbot.pkg.persistence.tenant_uow import PersistenceScope,PersistenceScopeBoundary


def server_fixture():
    scope=contextvars.ContextVar('mcp-test-scope',default=None)
    transaction=contextvars.ContextVar('mcp-test-transaction',default=None)
    def tenant_scope(workspace):
        return PersistenceScopeBoundary(PersistenceScope.workspace(workspace),active_scope=scope,active_transaction=transaction)
    configured=dict(allowed_hosts=['localhost:*','127.0.0.1:*','[::1]:*','approved.example:8443'],
                    allowed_origins=['http://localhost:*','http://127.0.0.1:*','http://[::1]:*','https://approved.example:8443'])
    ap=SimpleNamespace(persistence_mgr=SimpleNamespace(tenant_scope=tenant_scope),
                       instance_config=SimpleNamespace(data={'api':{'mcp_transport_security':configured}}))
    server=LangBotMCPServer(ap)
    @server.mcp.tool()
    async def inspect_scope(workspace:str,fail:bool=False)->str:
        value=scope.get()
        assert value.owner_task is asyncio.current_task()
        assert value.scope==PersistenceScope.workspace(workspace)
        await asyncio.sleep(0)
        if fail:
            raise ValueError('synthetic failure')
        return workspace
    return server,scope


@pytest.mark.asyncio
async def test_mcp_preserves_exact_configured_host_origin_and_dns_rebinding_protection():
    server,_=server_fixture()
    middleware=TransportSecurityMiddleware(server.mcp.settings.transport_security)
    assert middleware.settings.enable_dns_rebinding_protection is True
    async def status(host,origin=None):
        headers=[(b'host',host.encode()),(b'content-type',b'application/json')]
        if origin:
            headers.append((b'origin',origin.encode()))
        response=await middleware.validate_request(Request({'type':'http','headers':headers}),is_post=True)
        return None if response is None else response.status_code
    assert await status('approved.example:8443','https://approved.example:8443') is None
    for host in ['approved.example:8444','approved.example.evil:8443','unapproved.example:8443']:
        assert await status(host)==421
    assert await status('approved.example:8443','https://unapproved.example:8443')==403


@pytest.mark.asyncio
async def test_mcp_child_tasks_open_independent_real_persistence_scopes_and_clean_failure():
    server,scope=server_fixture()
    async def invoke(workspace,fail=False):
        token=bind_request_context(SimpleNamespace(workspace=SimpleNamespace(workspace_uuid=workspace)))
        try:
            task=asyncio.create_task(server.mcp.call_tool('inspect_scope',{'workspace':workspace,'fail':fail}))
            if fail:
                with pytest.raises(Exception):
                    await task
            else:
                await task
            assert scope.get() is None
        finally:
            reset_request_context(token)
    await asyncio.gather(invoke('workspace-a'),invoke('workspace-b'),invoke('workspace-c',True))


@pytest.mark.asyncio
async def test_mcp_missing_authenticated_context_is_denied_before_tool_scope():
    server,scope=server_fixture()
    with pytest.raises(RuntimeError,match='Workspace context is unavailable'):
        await server.mcp.call_tool('inspect_scope',{'workspace':'untrusted'})
    assert scope.get() is None


@pytest.mark.parametrize('configuration',[
    {'allowed_hosts':['*.example:8443']},
    {'allowed_origins':['http://approved.example:8443']},
    {'enable_dns_rebinding_protection':False},
])
def test_mcp_instance_allowlist_cannot_disable_protection_or_enable_remote_wildcards(configuration):
    ap=SimpleNamespace(instance_config=SimpleNamespace(data={'api':{'mcp_transport_security':configuration}}))
    with pytest.raises(ValueError):
        transport_security(ap)


@pytest.mark.asyncio
async def test_native_machine_handoff_is_not_exposed_as_user_mcp_tools():
    server, _ = server_fixture()
    names = {tool.name for tool in await server.mcp.list_tools()}
    assert not names & {'read_native_router_page', 'ack_native_router_page', 'lookup_native_router_receipt'}
    assert 'read_native_source_original' not in names
    assert 'get_system_info' in names
