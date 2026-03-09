-- occd.db Schema
-- 每个 github-* 仓库的 occd/occd.db 使用此 schema
-- 由 occd_utils.py db-init 自动创建

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ─────────────────────────────────────────
-- 需求表（对应 occd/req/ 下的文件）
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS requirements (
    id            TEXT PRIMARY KEY,          -- {repo_name}:{filename}，如 github-myapp:feature.md
    filename      TEXT NOT NULL,             -- req/ 下的文件名
    content_hash  TEXT NOT NULL,             -- 文件内容 SHA256，用于检测需求变更
    status        TEXT NOT NULL DEFAULT 'new',
                                             -- new | reviewing | decomposed | done | failed
    review_rounds INTEGER NOT NULL DEFAULT 0,
    last_req_commit TEXT,                    -- 最后处理时的 git commit hash
    created_at    INTEGER NOT NULL,          -- Unix timestamp ms
    updated_at    INTEGER NOT NULL
);

-- ─────────────────────────────────────────
-- 需求澄清表（对应 occd/review/ 下的文件）
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS reviews (
    id            TEXT PRIMARY KEY,          -- review-{req_filename}-{round}
    req_id        TEXT NOT NULL REFERENCES requirements(id),
    filename      TEXT NOT NULL,             -- review/ 下的文件名
    created_at    INTEGER NOT NULL,
    resolved_at   INTEGER                    -- NULL = 未解决；有值 = 已通过新 commit 解决
);

-- ─────────────────────────────────────────
-- 子任务表（对应 occd/task/ 下的任务定义文件）
-- 类型：coding | test-write | test-run
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sources (
    id            TEXT PRIMARY KEY,          -- reqZZZ-XXX-YYY-{type}，如 req001-001-001-coding
    req_id        TEXT NOT NULL REFERENCES requirements(id),
    filename      TEXT NOT NULL,             -- task/ 下的文件名
    task_type     TEXT NOT NULL,             -- coding | test-write | test-run
    xxx           TEXT NOT NULL,             -- 串行批次号，如 001
    yyy           TEXT NOT NULL,             -- 并行子序号，如 001
    status        TEXT NOT NULL DEFAULT 'pending',
                                             -- pending | spawned | running | done | failed
    session_key   TEXT,                      -- 当前/最后一次执行的子代理 session key
    branch        TEXT,                      -- git worktree 分支名（coding/test-write 任务使用）
    retry_count   INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);

-- ─────────────────────────────────────────
-- 执行记录表（对应 occd/report/ 下的报告文件）
-- 每次子代理执行一个 source 任务对应一条记录
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS executions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     TEXT NOT NULL REFERENCES sources(id),
    report_file   TEXT NOT NULL,             -- report/ 下的报告文件名，如 report-req001-001-001-coding-20260309T094500.md
    session_key   TEXT,                      -- 执行本次任务的子代理 session key
    outcome       TEXT NOT NULL,             -- success | failure | partial
    summary       TEXT,                      -- 关键变更/结论摘要（从报告中提取）
    started_at    INTEGER NOT NULL,
    finished_at   INTEGER
);

-- ─────────────────────────────────────────
-- 状态变更事件表（append-only，完整历史）
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS task_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type   TEXT NOT NULL,             -- req | source | review | execution
    entity_id     TEXT NOT NULL,             -- 对应表的主键
    from_status   TEXT,
    to_status     TEXT NOT NULL,
    agent         TEXT NOT NULL DEFAULT 'main',
                                             -- main | sub:{session_key}
    note          TEXT,                      -- 附加信息（错误摘要、retry 轮次等）
    created_at    INTEGER NOT NULL           -- Unix timestamp ms
);

-- ─────────────────────────────────────────
-- 索引
-- ─────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_requirements_status   ON requirements(status);
CREATE INDEX IF NOT EXISTS idx_sources_req_id        ON sources(req_id);
CREATE INDEX IF NOT EXISTS idx_sources_status        ON sources(status);
CREATE INDEX IF NOT EXISTS idx_sources_xxx           ON sources(xxx);
CREATE INDEX IF NOT EXISTS idx_executions_source_id  ON executions(source_id);
CREATE INDEX IF NOT EXISTS idx_task_events_entity    ON task_events(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_task_events_time      ON task_events(created_at);
