"""Workspace-owned native source receipts, checkpoints and send operations."""

import sqlalchemy as sa

from .base import Base


class NativeSource(Base):
    __tablename__ = 'native_ingress_sources'
    id = sa.Column(sa.String(64), primary_key=True)
    workspace_uuid = sa.Column(sa.String(36), sa.ForeignKey('workspaces.uuid'), nullable=False)
    bot_uuid = sa.Column(sa.String(255), nullable=False)
    binding = sa.Column(sa.JSON, nullable=False)
    binding_sha256 = sa.Column(sa.String(64), nullable=False)
    cursor = sa.Column(sa.Text, nullable=True)
    owner_frame_sequences = sa.Column(sa.Boolean,nullable=False,default=False,server_default=sa.false())
    last_source_sequence = sa.Column(sa.BigInteger,nullable=True)
    position = sa.Column(sa.Integer, nullable=False, default=0, server_default="0")
    router_cursor = sa.Column(sa.Integer, nullable=False, default=0, server_default="0")
    router_pending_page = sa.Column(sa.String(64), nullable=True)
    __table_args__ = (
        sa.UniqueConstraint('workspace_uuid', 'id'),
        sa.UniqueConstraint('workspace_uuid', 'bot_uuid'),
        sa.ForeignKeyConstraint(['workspace_uuid', 'bot_uuid'], ['bots.workspace_uuid', 'bots.uuid']),
    )


class NativePage(Base):
    __tablename__ = 'native_ingress_pages'
    id = sa.Column(sa.String(64), primary_key=True)
    workspace_uuid = sa.Column(sa.String(36), nullable=False)
    source_id = sa.Column(sa.String(64), nullable=False)
    receiver_id = sa.Column(sa.String(255), nullable=True)
    direction = sa.Column(sa.String(12), nullable=False, default="source")
    previous_cursor = sa.Column(sa.Text, nullable=True)
    fingerprint = sa.Column(sa.String(64), nullable=False)
    cursor = sa.Column(sa.Text, nullable=False)
    event_ids = sa.Column(sa.JSON, nullable=False)
    __table_args__ = (
        sa.ForeignKeyConstraint(['workspace_uuid', 'source_id'], ['native_ingress_sources.workspace_uuid', 'native_ingress_sources.id']),
    )


class NativeEvent(Base):
    __tablename__ = 'native_ingress_events'
    id = sa.Column(sa.String(64), primary_key=True)
    workspace_uuid = sa.Column(sa.String(36), nullable=False)
    source_id = sa.Column(sa.String(64), nullable=False)
    position = sa.Column(sa.Integer, nullable=False)
    envelope = sa.Column(sa.JSON, nullable=False)
    fingerprint = sa.Column(sa.String(64), nullable=False)
    status = sa.Column(sa.String(24), nullable=False, server_default='pending')
    receiver_receipt = sa.Column(sa.JSON, nullable=True)
    __table_args__ = (
        sa.ForeignKeyConstraint(['workspace_uuid', 'source_id'], ['native_ingress_sources.workspace_uuid', 'native_ingress_sources.id']),
        sa.UniqueConstraint('source_id', 'position'),
        sa.Index('ix_native_ingress_events_pending', 'workspace_uuid', 'source_id', 'status', 'id'),
    )


class NativeSend(Base):
    __tablename__ = 'native_send_operations'
    id = sa.Column(sa.String(64), primary_key=True)
    workspace_uuid = sa.Column(sa.String(36), nullable=False)
    source_id = sa.Column(sa.String(64), nullable=False)
    operation_id = sa.Column(sa.String(255), nullable=False)
    provider_key = sa.Column(sa.String(96), nullable=False)
    fingerprint = sa.Column(sa.String(64), nullable=False)
    status = sa.Column(sa.String(24), nullable=False)
    receipt = sa.Column(sa.JSON, nullable=True)
    __table_args__ = (
        sa.ForeignKeyConstraint(['workspace_uuid', 'source_id'], ['native_ingress_sources.workspace_uuid', 'native_ingress_sources.id']),
    )


NATIVE_TABLES = [NativeSource.__table__, NativePage.__table__, NativeEvent.__table__, NativeSend.__table__]


class NativeAccess(Base):
    """Operator-managed exact source audience and machine receiver registration."""
    __tablename__ = 'native_ingress_access'
    id = sa.Column(sa.String(64), primary_key=True)
    workspace_uuid = sa.Column(sa.String(36), nullable=False)
    bot_uuid = sa.Column(sa.String(255), nullable=False)
    binding_sha256 = sa.Column(sa.String(64), nullable=False)
    registration_request_id = sa.Column(sa.String(255), nullable=False)
    registered_by_account_uuid = sa.Column(sa.String(36), nullable=False)
    source_enabled = sa.Column(sa.Boolean, nullable=False, default=False, server_default=sa.false())
    source_revision = sa.Column(sa.Integer, nullable=False, default=1, server_default="1")
    receiver_id = sa.Column(sa.String(255), nullable=True)
    receiver_api_key_uuid = sa.Column(sa.String(36), nullable=True)
    receiver_enabled = sa.Column(sa.Boolean, nullable=False, default=False, server_default=sa.false())
    receiver_revision = sa.Column(sa.Integer, nullable=False, default=1, server_default="1")
    __table_args__ = (
        sa.UniqueConstraint('workspace_uuid','bot_uuid'),
        sa.ForeignKeyConstraint(['workspace_uuid','bot_uuid'], ['bots.workspace_uuid','bots.uuid']),
    )


NATIVE_TABLES.append(NativeAccess.__table__)


class NativeGap(Base):
    """Owner coverage code, with no fabricated message record or original."""
    __tablename__ = 'native_ingress_gaps'
    id = sa.Column(sa.String(64),primary_key=True)
    workspace_uuid = sa.Column(sa.String(36),nullable=False)
    source_id = sa.Column(sa.String(64),nullable=False)
    code = sa.Column(sa.String(64),nullable=False)
    reason = sa.Column(sa.String(32),nullable=False,default='owner_source_gap',server_default='owner_source_gap')
    owner_event_id = sa.Column(sa.String(512),nullable=True)
    source_sequence = sa.Column(sa.BigInteger,nullable=True)
    position = sa.Column(sa.Integer,nullable=False)
    upstream_cursor = sa.Column(sa.Text,nullable=False)
    __table_args__ = (
        sa.UniqueConstraint('source_id','owner_event_id'),
        sa.UniqueConstraint('source_id','position'),
        sa.ForeignKeyConstraint(['workspace_uuid','source_id'],['native_ingress_sources.workspace_uuid','native_ingress_sources.id']),
    )


NATIVE_TABLES.append(NativeGap.__table__)


class NativeFrame(Base):
    """One immutable owner sequence identity across messages and gaps."""
    __tablename__ = 'native_ingress_frames'
    id = sa.Column(sa.String(64),primary_key=True)
    workspace_uuid = sa.Column(sa.String(36),nullable=False)
    source_id = sa.Column(sa.String(64),nullable=False)
    sequence = sa.Column(sa.BigInteger,nullable=False)
    owner_event_id = sa.Column(sa.String(512),nullable=True)
    frame_id = sa.Column(sa.String(64),nullable=False)
    fingerprint = sa.Column(sa.String(64),nullable=False)
    __table_args__ = (
        sa.UniqueConstraint('source_id','sequence'),
        sa.UniqueConstraint('source_id','owner_event_id'),
        sa.UniqueConstraint('source_id','frame_id'),
        sa.ForeignKeyConstraint(['workspace_uuid','source_id'],['native_ingress_sources.workspace_uuid','native_ingress_sources.id']),
    )


NATIVE_TABLES.append(NativeFrame.__table__)
