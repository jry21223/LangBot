"""Exact MCP transport allowlists supplied by trusted instance configuration."""
from urllib.parse import urlsplit

from mcp.server.transport_security import TransportSecuritySettings

_LOCAL_HOSTS=['127.0.0.1:*','localhost:*','[::1]:*']
_LOCAL_ORIGINS=['http://127.0.0.1:*','http://localhost:*','http://[::1]:*']


def transport_security(ap):
    data=getattr(getattr(ap,'instance_config',None),'data',{})
    configured=data.get('api',{}).get('mcp_transport_security',{})
    if not isinstance(configured,dict) or set(configured)-{'allowed_hosts','allowed_origins'}:
        raise ValueError('invalid_mcp_transport_security')
    hosts=configured.get('allowed_hosts',_LOCAL_HOSTS)
    origins=configured.get('allowed_origins',_LOCAL_ORIGINS)
    for values in (hosts,origins):
        if not isinstance(values,list) or not 1<=len(values)<=64 or len(set(values))!=len(values):
            raise ValueError('invalid_mcp_transport_allowlist')
        if any(not isinstance(v,str) or len(v)>512 or any(ord(c)<33 for c in v) for v in values):
            raise ValueError('invalid_mcp_transport_allowlist')
    for host in hosts:
        if host in _LOCAL_HOSTS:
            continue
        parsed=urlsplit('https://'+host)
        if ('*' in host or parsed.username or parsed.password or not parsed.hostname or parsed.path
                or parsed.query or parsed.fragment or parsed.port is None):
            raise ValueError('mcp_host_must_be_exact')
    for origin in origins:
        if origin in _LOCAL_ORIGINS:
            continue
        parsed=urlsplit(origin)
        if ('*' in origin or parsed.scheme!='https' or parsed.username or parsed.password
                or not parsed.hostname or parsed.path or parsed.query or parsed.fragment):
            raise ValueError('mcp_origin_must_be_exact_https')
    return TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                     allowed_hosts=list(hosts),allowed_origins=list(origins))
