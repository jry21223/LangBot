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

_TABLES = ('native_ingress_sources', 'native_ingress_pages', 'native_ingress_events', 'native_send_operations', 'native_ingress_access')


def upgrade():
    op.create_table(_TABLES[0],
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('workspace_uuid', sa.String(36), sa.ForeignKey('workspaces.uuid'), nullable=False),
        sa.Column('bot_uuid', sa.String(255), nullable=False),
        sa.Column('binding', sa.JSON, nullable=False),
        sa.Column('binding_sha256', sa.String(64), nullable=False),
        sa.Column('cursor', sa.Text, nullable=True),
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
        op.create_table(name, *columns)
    op.create_table('native_ingress_access',
        sa.Column('id',sa.String(64),primary_key=True),
        sa.Column('workspace_uuid',sa.String(36),nullable=False),
        sa.Column('bot_uuid',sa.String(255),nullable=False),
        sa.Column('binding_sha256',sa.String(64),nullable=False),
        sa.Column('registered_by_account_uuid',sa.String(36),nullable=False),
        sa.Column('source_enabled',sa.Boolean,nullable=False,server_default=sa.false()),
        sa.Column('source_revision',sa.Integer,nullable=False,server_default='1'),
        sa.Column('receiver_id',sa.String(255),nullable=True),
        sa.Column('receiver_api_key_uuid',sa.String(36),nullable=True),
        sa.Column('receiver_enabled',sa.Boolean,nullable=False,server_default=sa.false()),
        sa.Column('receiver_revision',sa.Integer,nullable=False,server_default='1'),
        sa.UniqueConstraint('workspace_uuid','bot_uuid'),
        sa.ForeignKeyConstraint(['workspace_uuid','bot_uuid'],['bots.workspace_uuid','bots.uuid']))
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
