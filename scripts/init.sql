-- ============================================================================
-- RAG 系统 v14 数据库初始化
-- 所有表按最终形态创建，部分字段阶段一填默认值，后续阶段使用
-- ============================================================================

BEGIN;

-- ── 知识库 ──────────────────────────────────────────────────────────
CREATE TABLE knowledge_bases (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   VARCHAR(64) NOT NULL,
    name        VARCHAR(128) NOT NULL,
    description TEXT DEFAULT '',
    owner_id    VARCHAR(64) NOT NULL,
    status      VARCHAR(16) DEFAULT 'active',  -- active / reindexing
    created_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE (tenant_id, name)
);

-- ── 文档 ────────────────────────────────────────────────────────────
CREATE TABLE documents (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           VARCHAR(64) NOT NULL,
    filename            VARCHAR(256) NOT NULL,
    content_fingerprint VARCHAR(64) NOT NULL,    -- SHA-256，阶段三用于 MD5 幂等去重
    storage_path        VARCHAR(512) NOT NULL,
    file_size           BIGINT DEFAULT 0,
    mime_type           VARCHAR(64) DEFAULT '',
    uploaded_by         VARCHAR(64) NOT NULL,
    created_at          TIMESTAMPTZ DEFAULT now(),
    UNIQUE (tenant_id, content_fingerprint)
);

-- ── 挂载关系 ─────────────────────────────────────────────────────────
CREATE TABLE document_kb_mounts (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES documents(id),
    kb_id       UUID NOT NULL REFERENCES knowledge_bases(id),
    is_enabled  BOOLEAN DEFAULT true,
    mounted_by  VARCHAR(64) NOT NULL,
    mounted_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE (document_id, kb_id)
);

-- ── 摄入执行状态 ─────────────────────────────────────────────────────
CREATE TABLE ingest_executions (
    mount_id                UUID PRIMARY KEY REFERENCES document_kb_mounts(id),
    document_id             UUID NOT NULL,
    kb_id                   UUID NOT NULL,
    parse_status            VARCHAR(16) DEFAULT 'not_parsed',
    -- not_parsed / queued / processing / completed / failed / cancelling / removed
    chunking_config_version VARCHAR(32) DEFAULT 'v1',
    pipeline_yaml_version   VARCHAR(32) DEFAULT 'v1',    -- 阶段二开始真正用
    execution_epoch         INTEGER DEFAULT 1,           -- 阶段三开始真正用（栅栏令牌）
    failure_reason          TEXT DEFAULT '',
    retry_count             INTEGER DEFAULT 0,
    updated_at              TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_ingest_executions_kb_status ON ingest_executions(kb_id, parse_status);

-- ── 对话 ─────────────────────────────────────────────────────────────
CREATE TABLE conversations (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     VARCHAR(64) NOT NULL,
    user_id       VARCHAR(64) NOT NULL,
    bound_kb_ids  UUID[] NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ DEFAULT now()
);

-- ── 对话轮次 ─────────────────────────────────────────────────────────
CREATE TABLE conversation_turns (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id          UUID NOT NULL REFERENCES conversations(id),
    turn_index               INTEGER NOT NULL,
    user_question            TEXT NOT NULL,
    resolved_query           TEXT NOT NULL,      -- 阶段一 = user_question，阶段二改写
    retrieval_params_snapshot JSONB,
    retrieved_chunk_ids      TEXT[] DEFAULT '{}',
    retrieved_chunks         JSONB DEFAULT '[]',   -- 来源元数据 [{chunk_id, doc_name, content}]，供前端来源标注
    trace_id                 VARCHAR(64) DEFAULT '',
    authz_decision_ref       VARCHAR(64) DEFAULT '',  -- 阶段二审计补全
    pipeline_yaml_version    VARCHAR(32) DEFAULT 'v1',
    created_at               TIMESTAMPTZ DEFAULT now(),
    UNIQUE (conversation_id, turn_index)
);

-- ── 目录 ─────────────────────────────────────────────────────────────
CREATE TABLE directories (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        VARCHAR(64) NOT NULL,
    name             VARCHAR(128) NOT NULL,
    parent_id        UUID REFERENCES directories(id),
    directory_type   VARCHAR(16) DEFAULT 'manual',  -- kb_bound / manual
    bound_kb_id      UUID UNIQUE REFERENCES knowledge_bases(id),
    created_by       VARCHAR(64) NOT NULL,
    created_at       TIMESTAMPTZ DEFAULT now()
);

-- ── Outbox（B-DOC 分区） ─────────────────────────────────────────────
CREATE TABLE outbox (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type           VARCHAR(64) NOT NULL,
    payload              JSONB NOT NULL,
    tenant_id            VARCHAR(64) NOT NULL,
    trace_id             VARCHAR(64) DEFAULT '',
    status               VARCHAR(16) DEFAULT 'pending',  -- pending / sent
    created_at           TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_outbox_pending ON outbox(status, created_at) WHERE status = 'pending';

-- ── 审计日志（阶段一建表，阶段二开始落数据） ──────────────────────────
CREATE TABLE audit_logs (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type         VARCHAR(64) NOT NULL,
    request_id         VARCHAR(64) NOT NULL,
    user_id            VARCHAR(64),
    tenant_id          VARCHAR(64),
    client_ip          VARCHAR(64),
    is_service_account BOOLEAN DEFAULT false,
    action             VARCHAR(64),
    resource_type      VARCHAR(32),
    resource_id        VARCHAR(128),
    allowed            BOOLEAN,
    authz_decision_ref VARCHAR(64),     -- 跨系统取证主键
    risk_level         VARCHAR(16) DEFAULT 'normal',
    payload            JSONB,
    created_at         TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_audit_tenant_time ON audit_logs(tenant_id, created_at DESC);
CREATE INDEX idx_audit_request    ON audit_logs(request_id);
CREATE INDEX idx_audit_decision   ON audit_logs(authz_decision_ref)
    WHERE authz_decision_ref IS NOT NULL;

-- ── P-CONFIG：检索参数（阶段一用默认值，阶段二开始级联生效） ────────────
CREATE TABLE retrieval_configs (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_type        VARCHAR(16) NOT NULL,   -- tenant / kb / conversation / turn
    scope_id          VARCHAR(128) NOT NULL,
    top_k             INTEGER DEFAULT 10,
    retrieval_mode    VARCHAR(16) DEFAULT 'hybrid',
    fusion_method     VARCHAR(16) DEFAULT 'rrf',
    synthesis_mode    VARCHAR(32) DEFAULT 'compact',
    rerank_model_id   VARCHAR(128) DEFAULT '',
    strict            BOOLEAN DEFAULT false,
    oversample_factor FLOAT DEFAULT 1.5,
    min_results       INTEGER DEFAULT 3,
    refetch_max_rounds INTEGER DEFAULT 2,
    haystack_pipeline_name VARCHAR(64) DEFAULT 'query_v1',
    -- ── 检索/合成参数（与 src/platform/config/service.py RetrievalConfig 对齐）──
    dense_weight      FLOAT DEFAULT 0.5,
    sparse_weight     FLOAT DEFAULT 0.5,
    min_score         FLOAT DEFAULT 0.0,
    refine_batch_size INTEGER DEFAULT 2,
    tree_summarize_batch_size INTEGER DEFAULT 5,
    max_answer_length INTEGER DEFAULT 3000,
    compress_target_length INTEGER DEFAULT 1000,
    doc_preview_max_chars INTEGER DEFAULT 1000,
    UNIQUE (scope_type, scope_id)
);

-- ── P-CONFIG：切分配置 ───────────────────────────────────────────────
CREATE TABLE chunking_configs (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kb_id                UUID NOT NULL REFERENCES knowledge_bases(id),
    version              VARCHAR(32) NOT NULL,
    haystack_strategy    VARCHAR(32) DEFAULT 'sentence',
    split_length         INTEGER DEFAULT 256,
    split_overlap        INTEGER DEFAULT 32,
    language             VARCHAR(16) DEFAULT 'zh',
    pipeline_yaml_version VARCHAR(32) DEFAULT 'v1',
    advanced_params      JSONB DEFAULT '{}'::jsonb,
    created_at           TIMESTAMPTZ DEFAULT now(),
    UNIQUE (kb_id, version)
);

-- ── P-AUTHC：refresh_token 安全存储（SHA-256 hash，单向不可逆） ─────
CREATE TABLE IF NOT EXISTS refresh_token_hashes (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash     VARCHAR(64) NOT NULL UNIQUE,      -- SHA-256(refresh_token)
    sub            VARCHAR(128) NOT NULL,            -- OIDC subject
    tenant_id      VARCHAR(64) DEFAULT '',
    roles          JSONB DEFAULT '[]',
    name           VARCHAR(256) DEFAULT '',
    revoked        BOOLEAN DEFAULT false,            -- 轮换后标记 revoked
    created_at     TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_refresh_hash ON refresh_token_hashes(token_hash);
CREATE INDEX IF NOT EXISTS idx_refresh_sub ON refresh_token_hashes(sub);

COMMIT;
