"""Add workspace-owned native source receipts without altering upstream revisions.

Downgrade refuses nonempty tables: source/Host checkpoints must not be discarded
as an incidental rollback. Roll back the application with adapters disabled and
retain this additive schema; export and explicitly retire receipts separately.
"""

from alembic import op
import sqlalchemy as sa

revision = 'zimu_native_ingress_v1'
down_revision = '0037_metadata_text'
branch_labels = None
depends_on = None

_TABLES = ('native_ingress_sources', 'native_ingress_pages', 'native_ingress_events', 'native_send_operations', 'native_ingress_access','native_ingress_gaps')


_UNIQUES = {
    'native_ingress_sources': {('workspace_uuid','id'),('workspace_uuid','bot_uuid')},
    'native_ingress_pages': set(),
    'native_ingress_events': {('source_id','position')},
    'native_send_operations': set(),
    'native_ingress_access': {('workspace_uuid','bot_uuid')},
    'native_ingress_gaps': {('source_id','owner_event_id'),('source_id','position')},
}


def _default(value,column_type):
    if value is None:
        return None
    value=str(value).split('::')[0].strip("()' ").lower()
    if isinstance(column_type,sa.Boolean):
        return {'0':'false','1':'true'}.get(value,value)
    return value


def _create_table(name,*columns):
    inspector=sa.inspect(op.get_bind())
    if not inspector.has_table(name):
        op.create_table(name,*columns)
        return
    expected={column.name:column for column in columns if isinstance(column,sa.Column)}
    actual={column['name']:column for column in inspector.get_columns(name)}
    valid=set(actual)==set(expected)
    for key,column in expected.items():
        old=actual.get(key,{})
        old_type=old.get('type')
        valid=valid and (old_type is not None and old_type._type_affinity==column.type._type_affinity
            and getattr(old_type,'length',None)==getattr(column.type,'length',None)
            and old.get('nullable')==column.nullable
            and _default(old.get('default'),column.type)==_default(
                column.server_default.arg if column.server_default is not None else None,column.type))
    pk=set(inspector.get_pk_constraint(name)['constrained_columns'])
    valid=valid and pk=={key for key,column in expected.items() if column.primary_key}
    uniques={tuple(row['column_names']) for row in inspector.get_unique_constraints(name)}
    valid=valid and uniques==_UNIQUES[name]
    foreign_keys=inspector.get_foreign_keys(name)
    for foreign_key in foreign_keys:
        options=foreign_key.get('options',{})
        valid=valid and all(options.get(action) in {None,'NO ACTION'} for action in ('ondelete','onupdate'))
        valid=valid and options.get('deferrable') in {None,False} and options.get('initially') in {None,'IMMEDIATE'}
        valid=valid and options.get('match') in {None,'SIMPLE'}
        valid=valid and foreign_key.get('referred_schema') in {None,inspector.default_schema_name}
    fks={(tuple(row['constrained_columns']),row['referred_table'],tuple(row['referred_columns']))
         for row in foreign_keys}
    expected_fks={(('workspace_uuid','source_id'),'native_ingress_sources',('workspace_uuid','id'))}
    if name=='native_ingress_sources':
        expected_fks={(('workspace_uuid',),'workspaces',('uuid',)),
                      (('workspace_uuid','bot_uuid'),'bots',('workspace_uuid','uuid'))}
    elif name=='native_ingress_access':
        expected_fks={(('workspace_uuid','bot_uuid'),'bots',('workspace_uuid','uuid'))}
    valid=valid and fks==expected_fks
    if not valid:
        raise RuntimeError('Existing native table lacks the exact type, nullability, default, unique or foreign-key contract')


def upgrade():
    _create_table(_TABLES[0],
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('workspace_uuid', sa.String(36), sa.ForeignKey('workspaces.uuid'), nullable=False),
        sa.Column('bot_uuid', sa.String(255), nullable=False),
        sa.Column('binding', sa.JSON, nullable=False),
        sa.Column('binding_sha256', sa.String(64), nullable=False),
        sa.Column('cursor', sa.Text, nullable=True),
        sa.Column('last_source_sequence',sa.BigInteger,nullable=True),
        sa.Column('position', sa.Integer, nullable=False, server_default='0'),
        sa.Column('router_cursor', sa.Integer, nullable=False, server_default='0'),
        sa.Column('router_pending_page', sa.String(64), nullable=True),
        sa.UniqueConstraint('workspace_uuid', 'id'),
        sa.UniqueConstraint('workspace_uuid', 'bot_uuid'),
        sa.ForeignKeyConstraint(['workspace_uuid','bot_uuid'], ['bots.workspace_uuid','bots.uuid']))
    for name in _TABLES[1:4]:
        columns = [
            sa.Column('id', sa.String(64), primary_key=True),
            sa.Column('workspace_uuid', sa.String(36), nullable=False),
            sa.Column('source_id', sa.String(64), nullable=False),
        ]
        if name == 'native_ingress_pages':
            columns += [sa.Column('receiver_id', sa.String(255), nullable=True),
                sa.Column('direction', sa.String(12), nullable=False),
                sa.Column('previous_cursor', sa.Text, nullable=True),
                sa.Column('fingerprint', sa.String(64), nullable=False),
                sa.Column('cursor', sa.Text, nullable=False), sa.Column('event_ids', sa.JSON, nullable=False)]
        elif name == 'native_ingress_events':
            columns += [sa.Column('position', sa.Integer, nullable=False),
                sa.Column('envelope', sa.JSON, nullable=False),
                sa.Column('fingerprint', sa.String(64), nullable=False),
                sa.Column('status', sa.String(24), nullable=False, server_default='pending'),
                sa.Column('receiver_receipt', sa.JSON, nullable=True),
                sa.UniqueConstraint('source_id','position')]
        else:
            columns += [sa.Column('operation_id', sa.String(255), nullable=False),
                sa.Column('provider_key', sa.String(96), nullable=False),
                sa.Column('fingerprint', sa.String(64), nullable=False),
                sa.Column('status', sa.String(24), nullable=False), sa.Column('receipt', sa.JSON, nullable=True)]
        columns.append(sa.ForeignKeyConstraint(['workspace_uuid','source_id'],
            ['native_ingress_sources.workspace_uuid','native_ingress_sources.id']))
        _create_table(name, *columns)
    _create_table('native_ingress_access',
        sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('workspace_uuid',sa.String(36),nullable=False),
        sa.Column('bot_uuid',sa.String(255),nullable=False),
        sa.Column('binding_sha256',sa.String(64),nullable=False),
        sa.Column('registration_request_id',sa.String(255),nullable=False),
        sa.Column('registered_by_account_uuid',sa.String(36),nullable=False),
        sa.Column('source_enabled',sa.Boolean,nullable=False,server_default=sa.false()),
        sa.Column('source_revision',sa.Integer,nullable=False,server_default='1'),
        sa.Column('receiver_id',sa.String(255),nullable=True),
        sa.Column('receiver_api_key_uuid',sa.String(36),nullable=True),
        sa.Column('receiver_enabled',sa.Boolean,nullable=False,server_default=sa.false()),
        sa.Column('receiver_revision',sa.Integer,nullable=False,server_default='1'),
        sa.UniqueConstraint('workspace_uuid','bot_uuid'),
        sa.ForeignKeyConstraint(['workspace_uuid','bot_uuid'],['bots.workspace_uuid','bots.uuid']))
    _create_table('native_ingress_gaps',
        sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('workspace_uuid',sa.String(36),nullable=False),
        sa.Column('source_id',sa.String(64),nullable=False),
        sa.Column('code',sa.String(64),nullable=False),
        sa.Column('reason',sa.String(32),nullable=False,server_default='owner_source_gap'),
        sa.Column('owner_event_id',sa.String(512),nullable=True),
        sa.Column('source_sequence',sa.BigInteger,nullable=True),
        sa.Column('position',sa.Integer,nullable=False),
        sa.Column('upstream_cursor',sa.Text,nullable=False),
        sa.UniqueConstraint('source_id','owner_event_id'),sa.UniqueConstraint('source_id','position'),
        sa.ForeignKeyConstraint(['workspace_uuid','source_id'],['native_ingress_sources.workspace_uuid','native_ingress_sources.id']))
    if 'ix_native_ingress_events_pending' not in {i['name'] for i in sa.inspect(op.get_bind()).get_indexes('native_ingress_events')}:
        op.create_index('ix_native_ingress_events_pending', 'native_ingress_events',
                        ['workspace_uuid','source_id','status','id'])
    if op.get_bind().dialect.name == 'postgresql':
        for table in _TABLES:
            op.execute(sa.text(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY'))
            op.execute(sa.text(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY'))
            expression = "workspace_uuid::text = NULLIF(current_setting('langbot.workspace_uuid', true), '')"
            op.execute(sa.text(f'CREATE POLICY langbot_workspace_isolation ON {table} FOR ALL USING ({expression}) WITH CHECK ({expression})'))


def downgrade():
    bind = op.get_bind()
    for table in reversed(_TABLES):
        if bind.execute(sa.text(f'SELECT COUNT(*) FROM {table}')).scalar():
            raise RuntimeError('Native receipts exist; retain schema during application rollback')
    for table in reversed(_TABLES):
        op.drop_table(table)
