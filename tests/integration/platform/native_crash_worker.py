"""Child process used only inside the isolated test container."""
import asyncio
import logging
import os
import sys
from types import SimpleNamespace

import sqlalchemy as sa
from langbot.pkg.core import app as _app  # noqa: F401
from langbot.pkg.persistence.databases.sqlite import SQLiteDatabaseManager
from langbot.pkg.persistence.mgr import PersistenceManager
from langbot.pkg.platform.native_contract import SourceBinding, SourceEnvelope, SourcePage
from langbot.pkg.platform.native_ingress import NativeIngressStore


async def main():
    path, phase = sys.argv[1:]
    ap = SimpleNamespace(instance_config=SimpleNamespace(data={'database':{'sqlite':{'path':path}}}),
                         logger=logging.getLogger('crash-worker'))
    manager = PersistenceManager(ap)
    manager.db = SQLiteDatabaseManager(ap)
    await manager.db.initialize()
    store = NativeIngressStore(manager, workspace_uuid='workspace-a', bot_uuid='bot-workspace-a',
                               binding=SourceBinding('test-source','account-a','chat-a','grant-v1',False))
    async def allowed():
        pass
    if phase == 'sending':
        class Provider:
            async def send(self, *args):
                os._exit(33)
            async def lookup(self, key):
                return None
        await store.send('crashed-host-action','group','chat-a',{'text':'test'},provider=Provider(),authorize=allowed)
    else:
        dialect = manager.get_db_engine().sync_engine.dialect
        original_commit = dialect.do_commit
        def crash_at_database_commit(connection):
            if phase == 'before':
                os._exit(31)
            original_commit(connection)
            os._exit(32)
        dialect.do_commit = crash_at_database_commit
        event = SourceEnvelope('native-crash','v1','source:message/native-crash/v1','created',
                               {'native_id':'native-crash','dispatch_eligible':True})
        await store.accept_page(SourcePage('10','11',(event,)),authorize=allowed)
    await manager.shutdown()


asyncio.run(main())
