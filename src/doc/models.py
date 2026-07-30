"""B-DOC：数据模型（SQLAlchemy ORM）。

独占数据：
- knowledge_bases
- documents
- document_kb_mounts
- directories
- outbox（B-DOC 分区）
"""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    Column, String, Text, Boolean, DateTime, Integer, ForeignKey,
    UniqueConstraint, Index, JSON,
)
from sqlalchemy.dialects.postgresql import UUID, ARRAY
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def _uuid():
    return uuid4()


def _now():
    return datetime.utcnow()


# ═══════════════════════════════════════════════════════════════

class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"

    id = Column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id = Column(String(64), nullable=False)
    name = Column(String(128), nullable=False)
    description = Column(Text, default="")
    owner_id = Column(String(64), nullable=False)
    status = Column(String(16), default="active")
    created_at = Column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        UniqueConstraint("tenant_id", "name"),
    )


class Document(Base):
    __tablename__ = "documents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id = Column(String(64), nullable=False)
    filename = Column(String(256), nullable=False)
    content_fingerprint = Column(String(64), nullable=False)
    storage_path = Column(String(512), nullable=False)
    file_size = Column(Integer, default=0)
    mime_type = Column(String(64), default="")
    uploaded_by = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        UniqueConstraint("tenant_id", "content_fingerprint"),
    )


class DocumentKBMount(Base):
    __tablename__ = "document_kb_mounts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False)
    kb_id = Column(UUID(as_uuid=True), ForeignKey("knowledge_bases.id"), nullable=False)
    is_enabled = Column(Boolean, default=True)
    mounted_by = Column(String(64), nullable=False)
    mounted_at = Column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        UniqueConstraint("document_id", "kb_id"),
    )


class OutboxEntry(Base):
    __tablename__ = "outbox"

    id = Column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    event_type = Column(String(64), nullable=False)
    payload = Column(JSON, nullable=False)
    tenant_id = Column(String(64), nullable=False)
    trace_id = Column(String(64), default="")
    status = Column(String(16), default="pending")
    created_at = Column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        Index("idx_outbox_pending", "status", "created_at",
              postgresql_where=Column("status") == "pending"),
    )
