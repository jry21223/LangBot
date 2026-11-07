"""Fixed service-side credential references; no Bot DB secrets or arbitrary paths.

Installing this module does not configure a credential, mount, network route or
source grant. Each runtime entry must match an operator-approved exact binding.
The actual read-only secret mount and endpoint are separate deployment changes.
"""
from dataclasses import asdict
import os
import re
import stat

from .native_access import NativeAuthorization
from .native_contract import NativeAccessDenied
from .native_ingress import _hash

_SECRET_ROOT = '/run/secrets/langbot-native'


class NativeClientRegistry:
    def __init__(self,ap,entries,*,constructors=None,secret_root=_SECRET_ROOT):
        self.ap=ap
        self.entries=entries
        self.constructors=constructors
        self.secret_root=secret_root

    async def __call__(self,context,bot_uuid,binding,credential_ref):
        await NativeAuthorization(self.ap).source_registration(context,bot_uuid,binding)
        if not isinstance(credential_ref,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}',credential_ref):
            raise NativeAccessDenied('native_credential_reference_invalid')
        matches=[entry for entry in self.entries if entry.get('credential_ref')==credential_ref]
        if len(matches)!=1:
            raise NativeAccessDenied('native_credential_not_registered')
        entry=matches[0]
        if (entry.get('enabled') is not True or entry.get('workspace_uuid')!=context.workspace_uuid
                or entry.get('bot_uuid')!=bot_uuid or entry.get('binding_sha256')!=_hash(asdict(binding))):
            raise NativeAccessDenied('native_credential_binding_mismatch')
        name=entry.get('secret_file')
        if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}',name):
            raise NativeAccessDenied('native_secret_file_invalid')
        provider=entry.get('provider')
        if self.constructors is None:
            if provider!='agent-wechat-v1':
                raise NativeAccessDenied('native_client_provider_not_supported')
            from .adapters.agent_wechat.client import WxClient
            constructor=WxClient
        else:
            constructor=self.constructors.get(provider)
            if not callable(constructor):
                raise NativeAccessDenied('native_client_provider_not_supported')
        # No user-supplied path/env/module lookup. O_NOFOLLOW also refuses a
        # secret entry replaced with a symlink. Never print the credential.
        root_fd=None
        secret_fd=None
        try:
            root_fd=os.open(self.secret_root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            secret_fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=root_fd)
            info=os.fstat(secret_fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_size>8192
                    or info.st_uid not in {0,os.geteuid()} or info.st_mode & 0o077):
                raise NativeAccessDenied('native_secret_mount_permissions_invalid')
            secret=os.read(secret_fd,8193).decode('utf-8').strip()
            if not secret or len(secret)>4096 or any(c.isspace() for c in secret):
                raise NativeAccessDenied('native_secret_format_invalid')
        except NativeAccessDenied:
            raise
        except (OSError,UnicodeError):
            raise NativeAccessDenied('native_secret_mount_unavailable') from None
        finally:
            if secret_fd is not None: os.close(secret_fd)
            if root_fd is not None: os.close(root_fd)
        await NativeAuthorization(self.ap).source_registration(context,bot_uuid,binding)
        return constructor(entry.get('endpoint'),secret,binding.account)


def configured_native_client_factory(ap):
    settings=ap.instance_config.data.get('native_ingress',{})
    entries=settings.get('credential_bindings',[]) if isinstance(settings,dict) else []
    if not isinstance(entries,list) or any(not isinstance(entry,dict) for entry in entries):
        raise NativeAccessDenied('native_credential_registry_invalid')
    return NativeClientRegistry(ap,entries)
