"""B-CHAT：数据模型。

独占数据：conversation / conversation_turn
"""

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    Column, String, Text, Boolean, DateTime, Integer, ForeignKey,
    UniqueConstraint, JSON,
)
from sqlalchemy.dialects.postgresql import UUID, ARRAY
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def _uuid():
    return uuid4()


def _now():
    return datetime.utcnow()


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id = Column(String(64), nullable=False)
    user_id = Column(String(64), nullable=False)
    bound_kb_ids = Column(ARRAY(UUID(as_uuid=True)), default=list)
    created_at = Column(DateTime(timezone=True), default=_now)


class ConversationTurn(Base):
    __tablename__ = "conversation_turns"

    id = Column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    conversation_id = Column(UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False)
    turn_index = Column(Integer, nullable=False)
    user_question = Column(Text, nullable=False)
    resolved_query = Column(Text, nullable=False)
    retrieval_params_snapshot = Column(JSON, nullable=True)
    retrieved_chunk_ids = Column(ARRAY(Text), default=list)
    trace_id = Column(String(64), default="")
    authz_decision_ref = Column(String(64), default="")
    pipeline_yaml_version = Column(String(32), default="v1")
    created_at = Column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        UniqueConstraint("conversation_id", "turn_index"),
    )
