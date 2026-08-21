# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

企业级多知识库 RAG（检索增强生成）系统，采用 Haystack 2.x 算法框架 + 外部权限服务 Cerbos 实现权限外置。**本系统零权限判定**——所有授权决策由外部权限服务完成。

## Build & development commands

### Infrastructure (Docker)

```bash
make infra              # 启动基础设施 (postgres/redis/milvus/seaweedfs/cerbos)
make infra-down         # 停止基础设施（保留数据）
make infra-reset        # 完全重置（删除所有数据）
docker-compose -f docker-compose.infra.yml ps         # 查看基础设施状态
docker-compose -f docker-compose.infra.yml logs -f cerbos  # 查看 Cerbos 日志
```

### Database

```bash
make db-init            # 初始化表（首次启动后执行一次）
make db-seed            # 写入开发期测试数据
```

### Local development processes (run in separate terminals)

```bash
make dev-api            # FastAPI (端口 8000, --reload)
make dev-ingest         # 摄入 worker (ingestion_queue)
make dev-retrieve       # 检索 worker (retrieval_queue)
make dev-stamp          # 盖戳 worker (stamping_queue)
make dev-relay          # outbox relay
```

Only start the processes you need for the module you're working on. For example, document upload development only needs `dev-api` and `dev-ingest`.

### Testing

```bash
pytest tests/contract/ -v        # 契约测试（CI 强制）
pytest tests/integration/ -v     # 集成测试
```

### Other

```bash
python -m src.scripts.warmup_models   # 预热 BGE-M3 (~2GB, 仅首次)
python -m src.scripts.reset_milvus    # 重置 Milvus 全部向量数据（慎用）
```

## Architecture

### Dependency direction (strict, no exceptions)

```
B-* (业务模块) ──单向──▶ P-* (平台模块) ──单向──▶ 外部权限服务 (Cerbos)
```

- Business modules communicate via internal interfaces or events; **never read/write each other's tables directly**
- Platform modules don't depend on each other's business state
- No circular dependencies
- **No module may call the permission service HTTP endpoints directly** — only through P-AUTHC
- **No Haystack Component's `run()` may contain permission logic** — `MetadataFilter` is injected from outside

### Module responsibilities

**Platform modules** (horizontal, depended on by business):

| Module | Role | Key constraint |
|--------|------|----------------|
| **P-AUTHC** (`src/permission/`) | JWT validation, ctx construction, wraps all 5 permission service endpoints, 3-state mapping, 4 fail-closed modes, ctx_token minting, lifecycle port encapsulation | **No ACL data, no policies, no decisions** — not even `if owner then pass` shortcuts |
| **P-TASK** (`src/platform/task/`) | Celery task wrappers, `run_pipeline_async`, 4 queues (ingestion/retrieval/stamping/outbox-relay), Redis Pub/Sub streaming, health checks | No business logic |
| **P-MODEL** (`src/platform/model/`) | Encapsulates Haystack Generator/Embedder/Ranker behind `invoke_*` facades, prompt version pool, Langfuse model observability | Does not decide which KB uses which model |
| **P-CONFIG** (`src/platform/config/`) | 4-level retrieval config cascade (turn→conversation→kb→tenant), chunking config versioning, feature flags | Does not execute retrieval or chunking |
| **P-AUDIT** (`src/platform/audit/`) | Audit event ingestion, risk-level routing, `decision_id` cross-system correlation | Does not understand business semantics |
| **P-OBS** (`src/platform/obs/`) | OTel tracing/structlog/Metrics facade; Haystack Pipeline nodes auto-generate spans | Does not evaluate retrieval quality or handle model observability |
| **P-STORE** (`src/platform/store/`) | File read/write/signed URL/delete via SeaweedFS S3 gateway | Does not understand file semantics or deduplicate |

**Business modules** (vertical, depend on platform):

| Module | Role | Key constraint |
|--------|------|----------------|
| **B-DOC** (`src/doc/`) | Document registration & dedup, mount relationships, directory tree, **sync calls to permission service lifecycle ports** (register/link/unlink/retire) | No parsing, no execution state, no chunks, **no ACL data**, no authorization UI |
| **B-INGEST** (`src/ingest/`) | Parse → split (Haystack) → embed (BGE-M3 via P-MODEL) → write Milvus → **stamping pipeline** | Does not own mount relationships, makes no permission decisions, **only transports stamp values, never computes them** |
| **B-RETRIEVE** (`src/retrieve/`) | Haystack query Pipeline: prefilter injection (L1), oversampling+refetch (L2), strict per-item verification (L3), hybrid retrieval + RRF fusion, rerank | No generation orchestration, no conversation state, **no permission decisions, no post-filtering** |
| **B-CHAT** (`src/chat/`) | Conversation/turn storage, retrieval task dispatch, SSE streaming, generation synthesis via Haystack PromptBuilder+LiteLLMGenerator | No low-level retrieval computation, no permission decisions |

### Single-writer principle

Every table has exactly one writer module. Key ownerships:
- `document`, `document_kb_mount`, `directory`, `outbox` → **B-DOC only**
- `ingest_execution`, `chunks` → **B-INGEST only**
- Vector store chunk payload (`allow_stamps`/`deny_stamps`/`vis_version`) → **B-INGEST only** (stamping pipeline is the sole writer)
- `audit_log` → **P-AUDIT only**
- `retrieval_config`, `chunking_config` → **P-CONFIG only**
- `conversation`, `conversation_turn` → **B-CHAT only**
- `model_registry`, `prompt_*` → **P-MODEL only**
- ACL/role_binding/restriction data → **permission service only, not in this system**

### Three-layer retrieval pipeline

1. **Layer 1 (prefilter injection)**: `get_prefilter()` → `compile_filter()` → Haystack `MetadataFilter` (6 conditions) injected into `MilvusEmbeddingRetriever` and `MilvusBM25Retriever`. **Both retrievers must receive the same filter object.**
2. **Layer 2 (oversample + refetch)**: `k' = k × 1.5`, refetch up to 2 rounds if results < `min_results`. **Never relax filter conditions during refetch.**
3. **Layer 3 (strict per-item verification)**: Only for `strict=true` KBs. Calls `/v1/filter` in batches ≤200. **Batches on failure/timeout = entire batch denied. Permanently forbid caching.**

### Stamping pipeline (6 rules, all must hold)

`stamp_channel_task` is the only path for chunks to become visible:
1. On failure → **write nothing, don't ack** (never write empty stamps)
2. If `unmounted == true` → clear all stamps for that channel
3. Version monotonicity: discard if `response.version < current vis_version`
4. Batch upsert (500/batch), yield between batches
5. Checkpoint cursor for resume
6. Audit as `STAMP_APPLIED` (fail-open — stamping volume is high)

### Request context and credential handling

- `credential` (JWT raw) is stored once in P-AUTHC middleware, **never written to logs, trace spans, audit payloads, or task parameters**
- Async worker tasks use `ctx_token` (from `mint_ctx_token`, TTL ≤ 600s), never JWT raw
- `ctx_token` expiry = task failure, no renewal, no degradation
- `tenant_id` in ctx is **never used for permission decisions** — the permission service derives tenant from its resource mirror

### Fail semantics (must not cross-contaminate)

| Component | Unreachable behavior | Reason |
|-----------|---------------------|--------|
| Observability (P-OBS/Langfuse) | **Fail-open**, business continues | Telemetry loss is acceptable |
| Audit high-risk events (P-AUDIT) | **Fail-closed**, transaction rollback | Compliance obligation |
| **Permission service (external)** | **Fail-closed**, all requests denied | **Authorization bypass is irreversible** |

**Permission service is never included in `/readyz`.** Contract tests must assert both semantics coexist without contamination.

## Technical stack

| Concern | Component | Version |
|---------|-----------|---------|
| Algorithm framework | Haystack 2.x | haystack-ai==2.3.1 |
| Vector store | Milvus (standalone) | milvusdb/milvus:v2.4.13, pymilvus==2.4.9 |
| Relational DB | PostgreSQL | postgres:16-alpine, asyncpg==0.29.0, SQLAlchemy[asyncio]==2.0.35 |
| Object storage | SeaweedFS (S3 gateway) | chrislusf/seaweedfs:3.68, boto3==1.35.0 |
| Task queue | Celery + Redis | celery==5.4.0, redis:7-alpine |
| Embedding model | BGE-M3 (dense + sparse) | sentence-transformers==3.1.1, fastembed-haystack==1.1.0 |
| Reranker | BGE Reranker v2 | sentence-transformers==3.1.1 |
| LLM gateway | LiteLLM (dev: Ollama/Qwen2.5) | litellm==1.42.0 |
| Web framework | FastAPI | fastapi==0.115.0, uvicorn[standard]==0.31.0 |
| Permission service | 外部权限平台（含 Cerbos PDP，RAG 不部署） | 经 P-AUTHC `AUTHZ_SERVICE_URL` 调用 |
| Auth (JWT) | IdP (Keycloak or enterprise) + local validation | python-jose[cryptography]==3.3.0 |
| Observability | OTel Collector + Tempo/Loki/Prometheus + Grafana | opentelemetry-sdk==1.27.0 |
| Model observability | Langfuse | langfuse==2.43.3 |
| Logging | structlog | structlog==24.4.0 |
| RAG evaluation | RAGAS | ragas==0.1.21 |
| Python runtime | 3.11 | python:3.11-slim |

## Service ports

| Service | Port | Notes |
|---------|------|-------|
| API (FastAPI) | 8000 | External entry point |
| PostgreSQL | 5432 | Business DB |
| Redis | 6379 | Celery broker + result backend + Pub/Sub |
| Milvus | 19530 (gRPC), 9091 (health) | Vector store |
| SeaweedFS | 8333 | S3 gateway |
| Cerbos | 3592 (HTTP), 3593 (gRPC) | Permission service PDP |
| etcd | 2379 | Milvus metadata (internal only) |
| MinIO | 9000, 9001 (console) | Milvus data persistence (internal only) |
| Grafana | 3000 | Observability query |
| OTel Collector | 4317 (gRPC), 4318 (HTTP) | Observability ingest |

## Naming conventions

- **Action verbs**: Authoritative source is the upstream contract (16 verbs). Never create, rename, or use deprecated verbs. Deprecated: `doc:write` → `kb:write`; `doc:delete` → `doc:purge`/`doc:unmount`; `acl:update` → `doc:share`/`kb:grant`
- **Error codes**: `module_prefix:error_category` (e.g., `auth:unauthenticated`, `doc:not_found`)
- **Events**: PascalCase (e.g., `DocumentMounted`, `VisibilityChanged`)
- **Idempotency keys**: `{facade}-{tenant}-{resource_id}[-{kb_id}]-{schema_version}` — deterministic and recomputable, **never use timestamps/random/UUID**
- **`request_id`** = OTel `trace_id` (32 hex chars), also used as `environment.request_id` in permission service envelope
- **Pipeline YAML versions**: Incremented strings like `"v1"`, `"v2"`

## Prohibitions (from design docs)

1. **No direct calls to permission service HTTP endpoints** — always through P-AUTHC
2. **No permission logic inside Haystack Component `run()`** — inject `MetadataFilter` from outside
3. **No business module imports of observability SDKs** — use P-OBS facade
4. **No business module construction of permission service Envelope/addresses/reasons**
5. **No bypassing P-MODEL to call models** — Generator/Embedder/Ranker instances are encapsulated in P-MODEL
6. **No cross-module direct table reads/writes** between business modules
7. **No `pipeline.run()` in API process** — computation only in workers
8. **No local permission decisions** (including `if uploaded_by == user_id`)
9. **No caching decision results** (`/v1/filter` permanently forbidden; `/v1/check` off by default)
10. **No JWT raw in logs, traces, audit payloads, or task parameters**
11. **No post-filtering** (query-then-filter in application layer) — recall collapse, count leakage, context leakage
12. **No relaxing filter conditions during refetch** — MetadataFilter must be identical across rounds
13. **No falling back to unfiltered query when prefilter fails** — return empty/deny
14. **No writing empty stamps on stamping failure** — don't ack, retry
15. **No expanding `allow_stamps`/`deny_stamps` members** — keep original principal forms only
16. **No computing/deriving stamp values** in B-INGEST — only transport from `/v1/visibility`
17. **No revealing filtered items** in user-visible output — deny uses same wording as "insufficient information"
18. **No framework default prompts** — templates must come from `resolve_prompt`
19. **No Haystack types in module interface signatures**
20. **No signed URL for `view`** — must use server-side rendering
21. **Permission service never in `/readyz`**
