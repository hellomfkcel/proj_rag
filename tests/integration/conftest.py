"""Integration test fixtures for RAG v14.

Supports both async (pytest-asyncio) and sync test patterns.
Usage:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/integration/ -v
"""

import asyncio
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional, Tuple

import asyncpg
import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

# ══════════════════════════════════════════════════════════════════
# Configuration
# ══════════════════════════════════════════════════════════════════

API_BASE = os.environ.get("TEST_API_BASE", "http://localhost:8000")
DB_HOST = os.environ.get("TEST_DB_HOST", "localhost")
DB_PORT = int(os.environ.get("TEST_DB_PORT", "25432"))
DB_USER = os.environ.get("TEST_DB_USER", "rag")
DB_PASSWORD = os.environ.get("TEST_DB_PASSWORD", "REDACTED")
DB_NAME = os.environ.get("TEST_DB_NAME", "rag")
DB_DSN = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

TEST_TENANT = "tenant-dev"
SEED_KB_ID = "a0000000-0000-0000-0000-000000000001"
SEED_KB_ID_RESTRICTED = "b0000000-0000-0000-0000-000000000002"

# ══════════════════════════════════════════════════════════════════
# pytest markers
# ══════════════════════════════════════════════════════════════════

def pytest_configure(config):
    config.addinivalue_line("markers", "integration: integration test requiring live infrastructure")
    config.addinivalue_line("markers", "slow: slow test (may take >30s)")


# ══════════════════════════════════════════════════════════════════
# Infrastructure checks
# ══════════════════════════════════════════════════════════════════

def _api_reachable() -> bool:
    try:
        resp = httpx.get(f"{API_BASE}/healthz", timeout=3)
        return resp.status_code == 200
    except Exception:
        return False


def _postgres_reachable() -> bool:
    try:
        async def _check():
            conn = await asyncpg.connect(DB_DSN, timeout=3)
            await conn.close()
        asyncio.run(_check())
        return True
    except Exception:
        return False


requires_api = pytest.mark.skipif(not _api_reachable(), reason="API not reachable")
requires_postgres = pytest.mark.skipif(not _postgres_reachable(), reason="PostgreSQL not reachable")


# ══════════════════════════════════════════════════════════════════
# JWT helpers (sync wrappers for external test scripts)
# ══════════════════════════════════════════════════════════════════

DEV_PASSWORD = os.environ.get("TEST_DEV_PASSWORD", "admin123")


def admin_token_sync() -> str:
    resp = httpx.post(f"{API_BASE}/api/v1/auth/dev-login", json={
        "username": "admin", "password": DEV_PASSWORD, "tenant": TEST_TENANT,
    }, timeout=10)
    assert resp.status_code == 200, f"dev-login failed: {resp.status_code} {resp.text[:150]}"
    return resp.json()["access_token"]


def reader_token_sync() -> str:
    resp = httpx.post(f"{API_BASE}/api/v1/auth/dev-login", json={
        "username": "reader", "password": DEV_PASSWORD, "tenant": TEST_TENANT,
    }, timeout=10)
    assert resp.status_code == 200, f"dev-login failed: {resp.status_code} {resp.text[:150]}"
    return resp.json()["access_token"]


def writer_token_sync() -> str:
    resp = httpx.post(f"{API_BASE}/api/v1/auth/dev-login", json={
        "username": "writer", "password": DEV_PASSWORD, "tenant": TEST_TENANT,
    }, timeout=10)
    assert resp.status_code == 200, f"dev-login failed: {resp.status_code} {resp.text[:150]}"
    return resp.json()["access_token"]


def admin_headers_sync() -> dict:
    return {"Authorization": f"Bearer {admin_token_sync()}"}


# ══════════════════════════════════════════════════════════════════
# Async helpers
# ══════════════════════════════════════════════════════════════════

async def get_token(
    username: str = "admin",
    tenant: str = TEST_TENANT,
    role: str = "system_admin",
    client: Optional[httpx.AsyncClient] = None,
) -> str:
    # 角色由 Keycloak 决定，dev-login 不再接受 role 字段；统一用共享测试口令
    payload = {"username": username, "tenant": tenant, "password": DEV_PASSWORD}
    async def _fetch(c: httpx.AsyncClient) -> str:
        resp = await c.post("/api/v1/auth/dev-login", json=payload)
        if resp.status_code != 200:
            raise RuntimeError(f"dev-login failed: {resp.status_code} {resp.text[:200]}")
        return resp.json()["access_token"]
    if client is not None:
        return await _fetch(client)
    async with httpx.AsyncClient(base_url=API_BASE) as c:
        return await _fetch(c)


async def get_parse_status(conn: asyncpg.Connection, mount_id: str) -> str:
    status = await conn.fetchval(
        "SELECT parse_status FROM ingest_executions WHERE mount_id=$1", mount_id)
    return status or "not_parsed"


async def wait_for_parse(
    conn: asyncpg.Connection, mount_id: str,
    timeout: float = 90.0, interval: float = 3.0,
) -> str:
    terminal = {"completed", "failed", "removed"}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = await get_parse_status(conn, mount_id)
        if status in terminal:
            return status
        await asyncio.sleep(interval)
    return await get_parse_status(conn, mount_id)


# ══════════════════════════════════════════════════════════════════
# Test data helpers
# ══════════════════════════════════════════════════════════════════

def unique_name(prefix: str = "inttest") -> str:
    ts = datetime.now(timezone.utc).strftime("%H%M%S%f")
    return f"{prefix}-{ts}-{uuid.uuid4().hex[:6]}"


def create_test_text_file(content: str = None, filename: str = None) -> tuple:
    if filename is None:
        filename = f"{unique_name('doc')}.txt"
    if content is None:
        content = f"RAG系统集成测试文档 - {unique_name()}\n\n这是测试内容。\n"
    filepath = os.path.join("/tmp", filename)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    return filepath, filename, content


# ══════════════════════════════════════════════════════════════════
# DB cleanup helpers
# ══════════════════════════════════════════════════════════════════

async def cleanup_document(conn: asyncpg.Connection, doc_id: str) -> None:
    await conn.execute("DELETE FROM ingest_executions WHERE document_id=$1", doc_id)
    await conn.execute("DELETE FROM document_kb_mounts WHERE document_id=$1", doc_id)
    await conn.execute("DELETE FROM documents WHERE id=$1", doc_id)


async def cleanup_kb(conn: asyncpg.Connection, kb_id: str) -> None:
    await conn.execute("DELETE FROM ingest_executions WHERE kb_id=$1", kb_id)
    await conn.execute("DELETE FROM document_kb_mounts WHERE kb_id=$1", kb_id)
    await conn.execute("DELETE FROM directories WHERE bound_kb_id=$1", kb_id)
    await conn.execute("DELETE FROM chunking_configs WHERE kb_id=$1", kb_id)
    await conn.execute(
        "DELETE FROM retrieval_configs WHERE scope_type='kb' AND scope_id=$1", kb_id)
    await conn.execute("DELETE FROM knowledge_bases WHERE id=$1", kb_id)


# ══════════════════════════════════════════════════════════════════
# Async fixtures (for pytest-asyncio)
# ══════════════════════════════════════════════════════════════════

@pytest.fixture
async def db() -> AsyncGenerator[asyncpg.Connection, None]:
    """Async PostgreSQL connection per test."""
    if not _postgres_reachable():
        pytest.skip("PostgreSQL not reachable")
    conn = await asyncpg.connect(DB_DSN)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def api_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    """Async HTTPX client pointed at the running API server."""
    if not _api_reachable():
        pytest.skip("API not reachable")
    async with httpx.AsyncClient(base_url=API_BASE, timeout=60) as client:
        yield client


@pytest.fixture
async def admin_token(api_client: httpx.AsyncClient) -> str:
    return await get_token("admin", TEST_TENANT, "system_admin", api_client)


@pytest.fixture
async def reader_token(api_client: httpx.AsyncClient) -> str:
    return await get_token("reader", TEST_TENANT, "user", api_client)


@pytest.fixture
async def writer_token(api_client: httpx.AsyncClient) -> str:
    return await get_token("writer", TEST_TENANT, "user", api_client)


@pytest.fixture
async def auth_headers(admin_token: str) -> dict:
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture
async def reader_headers(reader_token: str) -> dict:
    return {"Authorization": f"Bearer {reader_token}"}


@pytest.fixture
async def test_kb(api_client: httpx.AsyncClient, admin_token: str) -> AsyncGenerator[str, None]:
    """Create a temporary KB, yield its ID, cleanup on teardown."""
    name = unique_name("tmpkb")
    resp = await api_client.post(
        "/api/v1/knowledge-bases",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"name": name, "description": "temp KB for integration tests"},
    )
    assert resp.status_code in (200, 201), f"create KB failed: {resp.status_code} {resp.text}"
    kb_id = resp.json()["id"]
    yield kb_id
    # Cleanup
    del_resp = await api_client.delete(
        f"/api/v1/knowledge-bases/{kb_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    if del_resp.status_code not in (200, 404):
        conn = await asyncpg.connect(DB_DSN)
        try:
            await cleanup_kb(conn, kb_id)
        finally:
            await conn.close()


# ══════════════════════════════════════════════════════════════════
# Sync function aliases (for non-async test files)
# ══════════════════════════════════════════════════════════════════

# Expose sync token functions under the names expected by test files
admin_token = admin_token_sync
reader_token = reader_token_sync
writer_token = writer_token_sync
admin_headers = admin_headers_sync
unique_test_name = unique_name
create_test_text_file = create_test_text_file  # re-export for clarity

def reader_headers() -> dict:
    return {"Authorization": f"Bearer {reader_token_sync()}"}

def writer_headers() -> dict:
    return {"Authorization": f"Bearer {writer_token_sync()}"}
