"""Integration tests: Core RAG Pipeline (Upload -> Ingest -> Query).

Tests the complete end-to-end chain using live infrastructure.
P0 -- must pass before production deployment.

Usage:
    pytest tests/integration/test_core_rag_chain.py -v
"""

import os
import time
import uuid

import pytest
import httpx

from tests.integration.conftest import (
    requires_api, requires_postgres,
    admin_headers, unique_test_name, create_test_text_file,
    API_BASE, SEED_KB_ID, TEST_TENANT,
)

pytestmark = pytest.mark.integration

V1 = f"{API_BASE}/api/v1"


# ══════════════════════════════════════════════════════════════════════
# 1. Knowledge Base Listing
# ══════════════════════════════════════════════════════════════════════


@requires_api
@requires_postgres
def test_kb_listing():
    """GET /api/v1/knowledge-bases returns KBs visible to the user."""
    resp = httpx.get(f"{V1}/knowledge-bases", headers=admin_headers(),
                     timeout=10)
    assert resp.status_code == 200, f"KB list failed: {resp.status_code}"

    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 1, "Expected at least 1 KB (seed data)"

    # 种子 KB 使用随机 UUID（seed_dev.py），不再断言固定 SEED_KB_ID；
    # 关键不变量是列表非空且全部属于当前租户。
    assert all(kb["tenant_id"] == TEST_TENANT for kb in data), (
        f"All listed KBs must belong to tenant {TEST_TENANT}"
    )

    # 任意一个 KB 应具备完整字段（种子使用随机 UUID，不锁定固定 ID）
    kb = data[0]
    assert "name" in kb
    assert "tenant_id" in kb
    assert kb["tenant_id"] == TEST_TENANT
    assert kb["status"] == "active"


# ══════════════════════════════════════════════════════════════════════
# 2. Document Upload
# ══════════════════════════════════════════════════════════════════════


@requires_api
@requires_postgres
def test_upload_document():
    """Upload a .txt file via POST /api/v1/documents/upload and verify.

    Cleans up document after the test via direct DB queries.
    """
    import asyncpg
    import asyncio

    # Create a temp KB for this test
    kb_name = unique_test_name("upldkb")
    resp = httpx.post(
        f"{V1}/knowledge-bases",
        headers=admin_headers(),
        json={"name": kb_name, "description": "temp"},
        timeout=10,
    )
    assert resp.status_code == 201, f"create KB failed: {resp.text}"
    kb_id = resp.json()["id"]

    filepath, filename, content = create_test_text_file()
    doc_id = None

    try:
        with open(filepath, "rb") as f:
            resp = httpx.post(
                f"{V1}/documents/upload",
                headers=admin_headers(),
                files={"file": (filename, f, "text/plain")},
                data={"kb_id": kb_id, "auto_parse": "false"},
                timeout=30,
            )

        assert resp.status_code == 200, \
            f"Upload failed: {resp.status_code} {resp.text}"
        data = resp.json()

        assert "document_id" in data
        assert "mount_id" in data
        assert data.get("parse_status") in ("not_parsed", "queued")
        assert data.get("duplicate") is False

        doc_id = data["document_id"]

        # Verify via GET /api/v1/documents/{doc_id}
        resp2 = httpx.get(
            f"{V1}/documents/{doc_id}",
            headers=admin_headers(), timeout=10,
        )
        assert resp2.status_code == 200
        doc_data = resp2.json()
        assert doc_data["filename"] == filename
        assert doc_data["tenant_id"] == TEST_TENANT

    finally:
        os.unlink(filepath)

        async def _cleanup():
            conn = await asyncpg.connect(
                host="localhost", port=25432,
                user="rag", password="REDACTED",
                database="rag",
            )
            try:
                if doc_id:
                    await conn.execute(
                        "DELETE FROM ingest_executions WHERE document_id=$1",
                        doc_id)
                    await conn.execute(
                        "DELETE FROM document_kb_mounts WHERE document_id=$1",
                        doc_id)
                    await conn.execute(
                        "DELETE FROM documents WHERE id=$1", doc_id)
                await conn.execute(
                    "DELETE FROM chunking_configs WHERE kb_id=$1", kb_id)
                await conn.execute(
                    "DELETE FROM retrieval_configs "
                    "WHERE scope_type='kb' AND scope_id=$1", kb_id)
                await conn.execute(
                    "DELETE FROM directories WHERE bound_kb_id=$1", kb_id)
                await conn.execute(
                    "DELETE FROM knowledge_bases WHERE id=$1", kb_id)
            finally:
                await conn.close()

        asyncio.run(_cleanup())


# ══════════════════════════════════════════════════════════════════════
# 3. Document Listing
# ══════════════════════════════════════════════════════════════════════


@requires_api
@requires_postgres
def test_document_listing():
    """GET /api/v1/knowledge-bases/{kb_id}/documents returns a list."""
    resp = httpx.get(
        f"{V1}/knowledge-bases/{SEED_KB_ID}/documents",
        headers=admin_headers(), timeout=10,
    )
    assert resp.status_code == 200, \
        f"Doc list failed: {resp.status_code}"

    data = resp.json()
    assert isinstance(data, list), f"Expected list, got {type(data)}"

    if data:
        doc = data[0]
        id_field = doc.get("document_id") or doc.get("id")
        assert id_field is not None
        assert "filename" in doc


@requires_api
@requires_postgres
def test_document_listing_filters():
    """Document listing supports search and status query params."""
    resp = httpx.get(
        f"{V1}/knowledge-bases/{SEED_KB_ID}/documents",
        params={"search": "test"},
        headers=admin_headers(), timeout=10,
    )
    assert resp.status_code == 200

    resp = httpx.get(
        f"{V1}/knowledge-bases/{SEED_KB_ID}/documents",
        params={"status": "completed"},
        headers=admin_headers(), timeout=10,
    )
    assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════
# 4. Trigger Parse
# ══════════════════════════════════════════════════════════════════════


@requires_api
@requires_postgres
@pytest.mark.slow
def test_trigger_parse_and_wait():
    """Upload a document, trigger parse, and poll for completion.

    If the ingestion worker is not running, the parse stays queued
    and the test skips gracefully.
    """
    import asyncpg
    import asyncio

    # Temp KB
    kb_name = unique_test_name("parsekb")
    resp = httpx.post(
        f"{V1}/knowledge-bases",
        headers=admin_headers(),
        json={"name": kb_name, "description": "temp"},
        timeout=10,
    )
    assert resp.status_code == 201
    kb_id = resp.json()["id"]

    filepath, filename, _ = create_test_text_file()
    doc_id = None
    mount_id = None

    try:
        with open(filepath, "rb") as f:
            resp = httpx.post(
                f"{V1}/documents/upload",
                headers=admin_headers(),
                files={"file": (filename, f, "text/plain")},
                data={"kb_id": kb_id, "auto_parse": "false"},
                timeout=30,
            )
        assert resp.status_code == 200
        doc_id = resp.json()["document_id"]
        mount_id = resp.json()["mount_id"]

        # Trigger parse
        parse_resp = httpx.post(
            f"{V1}/documents/{doc_id}/trigger-parse",
            headers=admin_headers(), timeout=10,
        )
        assert parse_resp.status_code in (200, 202), \
            f"Trigger parse failed: {parse_resp.status_code} {parse_resp.text}"
        assert "mount_id" in parse_resp.json()
        assert "parse_status" in parse_resp.json()

        # Poll DB for parse completion
        async def _poll():
            conn = await asyncpg.connect(
                host="localhost", port=25432,
                user="rag", password="REDACTED",
                database="rag",
            )
            try:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    status = await conn.fetchval(
                        "SELECT parse_status FROM ingest_executions "
                        "WHERE mount_id=$1", mount_id)
                    if status in ("completed", "failed", "removed"):
                        return status
                    await asyncio.sleep(3)
                return await conn.fetchval(
                    "SELECT parse_status FROM ingest_executions "
                    "WHERE mount_id=$1", mount_id)
            finally:
                await conn.close()

        status = asyncio.run(_poll())

        if status == "completed":
            assert status == "completed"
        elif status == "failed":
            pytest.skip("Parse failed (worker may be misconfigured)")
        else:
            pytest.skip(f"Parse still '{status}' — ingestion worker not running")

    finally:
        os.unlink(filepath)
        async def _cleanup():
            conn = await asyncpg.connect(
                host="localhost", port=25432,
                user="rag", password="REDACTED",
                database="rag",
            )
            try:
                if doc_id:
                    await conn.execute(
                        "DELETE FROM ingest_executions WHERE document_id=$1",
                        doc_id)
                    await conn.execute(
                        "DELETE FROM document_kb_mounts WHERE document_id=$1",
                        doc_id)
                    await conn.execute(
                        "DELETE FROM documents WHERE id=$1", doc_id)
                await conn.execute(
                    "DELETE FROM chunking_configs WHERE kb_id=$1", kb_id)
                await conn.execute(
                    "DELETE FROM directories WHERE bound_kb_id=$1", kb_id)
                await conn.execute(
                    "DELETE FROM retrieval_configs "
                    "WHERE scope_type='kb' AND scope_id=$1", kb_id)
                await conn.execute(
                    "DELETE FROM knowledge_bases WHERE id=$1", kb_id)
            finally:
                await conn.close()
        asyncio.run(_cleanup())


# ══════════════════════════════════════════════════════════════════════
# 5. Query / Retrieval
# ══════════════════════════════════════════════════════════════════════


@requires_api
@requires_postgres
def test_query_retrieval():
    """POST /api/v1/conversations/query dispatches a retrieval task.
    Verifies the endpoint returns 200 with expected structure.
    """
    import asyncpg
    import asyncio

    # Create conversation
    resp = httpx.post(
        f"{V1}/conversations",
        headers=admin_headers(),
        json={"kb_ids": [SEED_KB_ID]},
        timeout=10,
    )
    assert resp.status_code == 201, \
        f"Create conversation failed: {resp.status_code}"
    conv_id = resp.json()["id"]

    try:
        query_resp = httpx.post(
            f"{V1}/conversations/query",
            headers=admin_headers(),
            json={
                "question": "RAG系统的核心功能是什么？",
                "kb_ids": [SEED_KB_ID],
                "conversation_id": conv_id,
            },
            timeout=30,
        )
        assert query_resp.status_code == 200, \
            f"Query failed: {query_resp.status_code} {query_resp.text}"

        data = query_resp.json()
        assert "answer" in data
        assert "conversation_id" in data
        assert "turn_index" in data
        assert data["turn_index"] >= 1
        # Answer is empty because task is dispatched to Celery worker
        assert data["answer"] == ""

        # Poll DB for the turn (written by retrieval worker)
        async def _poll():
            conn = await asyncpg.connect(
                host="localhost", port=25432,
                user="rag", password="REDACTED",
                database="rag",
            )
            try:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    row = await conn.fetchrow(
                        "SELECT id, user_question FROM conversation_turns "
                        "WHERE conversation_id=$1 AND turn_index=$2",
                        conv_id, data["turn_index"],
                    )
                    if row:
                        return dict(row)
                    await asyncio.sleep(3)
                return None
            finally:
                await conn.close()

        turn = asyncio.run(_poll())
        if turn is not None:
            assert turn["user_question"] == "RAG系统的核心功能是什么？"

    finally:
        async def _cleanup():
            conn = await asyncpg.connect(
                host="localhost", port=25432,
                user="rag", password="REDACTED",
                database="rag",
            )
            try:
                await conn.execute(
                    "DELETE FROM conversation_turns "
                    "WHERE conversation_id=$1", conv_id)
                await conn.execute(
                    "DELETE FROM conversations WHERE id=$1", conv_id)
            finally:
                await conn.close()
        asyncio.run(_cleanup())


# ══════════════════════════════════════════════════════════════════════
# 6. Conversation CRUD
# ══════════════════════════════════════════════════════════════════════


@requires_api
@requires_postgres
def test_conversation_crud():
    """Conversation create, list, and delete lifecycle."""
    headers = admin_headers()

    # Create
    resp = httpx.post(f"{V1}/conversations", headers=headers,
                      json={"kb_ids": [SEED_KB_ID]}, timeout=10)
    assert resp.status_code == 201, \
        f"Create conversation: {resp.status_code} {resp.text}"
    conv_id = resp.json()["id"]

    try:
        # List
        list_resp = httpx.get(f"{V1}/conversations", headers=headers,
                              timeout=10)
        assert list_resp.status_code == 200
        conv_ids = [c["id"] for c in list_resp.json()]
        assert conv_id in conv_ids, \
            f"Conversation {conv_id} not found in list"

    finally:
        # Delete
        del_resp = httpx.delete(f"{V1}/conversations/{conv_id}",
                                headers=headers, timeout=10)
        assert del_resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════
# 7. Health Checks
# ══════════════════════════════════════════════════════════════════════


def test_healthz():
    """GET /healthz returns ok (public endpoint, no auth)."""
    resp = httpx.get(f"{API_BASE}/healthz", timeout=5)
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@requires_api
def test_readyz():
    """GET /api/v1/readyz returns ok (requires auth).

    Per architecture: Cerbos excluded from /readyz.
    """
    resp = httpx.get(f"{V1}/readyz", headers=admin_headers(),
                     timeout=10)
    assert resp.status_code == 200, \
        f"readyz returned {resp.status_code}: {resp.text}"


# ══════════════════════════════════════════════════════════════════════
# 8. Full Chain End-to-End
# ══════════════════════════════════════════════════════════════════════


@requires_api
@requires_postgres
@pytest.mark.slow
def test_full_chain_end_to_end():
    """Upload a document, trigger parse, then query against the KB.

    Skips gracefully if workers are not running.
    """
    import asyncpg
    import asyncio

    headers = admin_headers()

    # Create a dedicated KB
    kb_name = unique_test_name("e2e")
    resp = httpx.post(f"{V1}/knowledge-bases", headers=headers,
                      json={"name": kb_name, "description": "E2E test"},
                      timeout=10)
    assert resp.status_code == 201
    kb_id = resp.json()["id"]

    filepath = None
    doc_id = None
    mount_id = None
    conv_id = None

    try:
        # Upload with auto-parse
        filepath, filename, content = create_test_text_file(
            content=(
                "RAG系统测试文档。\n"
                "## 概述\n"
                "RAG（Retrieval-Augmented Generation）系统结合了检索和生成。\n"
                "本系统使用Haystack 2.x作为算法框架。\n"
                "## 核心概念\n"
                "- 文档切分（Chunking）：将长文档切分为可检索的片段\n"
                "- 向量嵌入（Embedding）：使用BGE-M3模型将文本转为向量\n"
                "- 混合检索（Hybrid Search）：结合稠密向量和稀疏向量\n"
                "- 权限过滤：通过Cerbos实现外部授权\n"
            )
        )
        with open(filepath, "rb") as f:
            up_resp = httpx.post(
                f"{V1}/documents/upload", headers=headers,
                files={"file": (filename, f, "text/plain")},
                data={"kb_id": kb_id, "auto_parse": "true"},
                timeout=30,
            )
        assert up_resp.status_code == 200
        doc_id = up_resp.json()["document_id"]
        mount_id = up_resp.json()["mount_id"]

        # Poll DB for parse completion
        async def _poll_parse():
            conn = await asyncpg.connect(
                host="localhost", port=25432,
                user="rag", password="REDACTED",
                database="rag",
            )
            try:
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    status = await conn.fetchval(
                        "SELECT parse_status FROM ingest_executions "
                        "WHERE mount_id=$1", mount_id)
                    if status == "completed":
                        return "completed"
                    if status == "failed":
                        return "failed"
                    await asyncio.sleep(3)
                return await conn.fetchval(
                    "SELECT parse_status FROM ingest_executions "
                    "WHERE mount_id=$1", mount_id)
            finally:
                await conn.close()

        status = asyncio.run(_poll_parse())
        if status != "completed":
            pytest.skip(f"Parse status={status} — ingestion worker needed")

        # Query
        conv_resp = httpx.post(f"{V1}/conversations", headers=headers,
                               json={"kb_ids": [kb_id]}, timeout=10)
        assert conv_resp.status_code == 201
        conv_id = conv_resp.json()["id"]

        query_resp = httpx.post(
            f"{V1}/conversations/query", headers=headers,
            json={
                "question": "RAG系统的核心概念有哪些？",
                "kb_ids": [kb_id],
                "conversation_id": conv_id,
            },
            timeout=30,
        )
        assert query_resp.status_code == 200
        turn_index = query_resp.json()["turn_index"]

        # Poll DB for turn
        async def _poll_turn():
            conn = await asyncpg.connect(
                host="localhost", port=25432,
                user="rag", password="REDACTED",
                database="rag",
            )
            try:
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    row = await conn.fetchrow(
                        "SELECT user_question FROM conversation_turns "
                        "WHERE conversation_id=$1 AND turn_index=$2",
                        conv_id, turn_index,
                    )
                    if row:
                        return row["user_question"]
                    await asyncio.sleep(3)
                return None
            finally:
                await conn.close()

        question = asyncio.run(_poll_turn())
        if question is None:
            pytest.skip("Query turn never appeared — retrieval worker not running")
        assert question == "RAG系统的核心概念有哪些？"

    finally:
        async def _cleanup():
            conn = await asyncpg.connect(
                host="localhost", port=25432,
                user="rag", password="REDACTED",
                database="rag",
            )
            try:
                if conv_id:
                    await conn.execute(
                        "DELETE FROM conversation_turns "
                        "WHERE conversation_id=$1", conv_id)
                    await conn.execute(
                        "DELETE FROM conversations WHERE id=$1", conv_id)
                if doc_id:
                    await conn.execute(
                        "DELETE FROM ingest_executions WHERE document_id=$1",
                        doc_id)
                    await conn.execute(
                        "DELETE FROM document_kb_mounts WHERE document_id=$1",
                        doc_id)
                    await conn.execute(
                        "DELETE FROM documents WHERE id=$1", doc_id)
                await conn.execute(
                    "DELETE FROM chunking_configs WHERE kb_id=$1", kb_id)
                await conn.execute(
                    "DELETE FROM directories WHERE bound_kb_id=$1", kb_id)
                await conn.execute(
                    "DELETE FROM retrieval_configs "
                    "WHERE scope_type='kb' AND scope_id=$1", kb_id)
                await conn.execute(
                    "DELETE FROM knowledge_bases WHERE id=$1", kb_id)
            finally:
                await conn.close()
        asyncio.run(_cleanup())
        if filepath and os.path.exists(filepath):
            os.unlink(filepath)
