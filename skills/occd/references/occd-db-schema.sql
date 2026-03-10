-- occd.db Schema
-- 每个 github-* 仓库的 occd/occd.db 使用此 schema
-- 由 occd_utils.py db-init 自动创建

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS requirements (
    id                   TEXT PRIMARY KEY,          -- {repo_name}:{filename}
    filename             TEXT NOT NULL,             -- req/ 下的文件名
    content_hash         TEXT NOT NULL,             -- 当前文件内容 SHA256
    status               TEXT NOT NULL DEFAULT 'new',
                                                 -- new | preflight | reviewing | blocked | decomposed | done | failed
    review_rounds        INTEGER NOT NULL DEFAULT 0,
    last_req_commit      TEXT,                      -- 最近一次落地 review/tasks 时对应的 req commit
    processed_commit     TEXT,                      -- 已处理基线 commit
    processed_commit_at  INTEGER,
    latest_commit        TEXT,                      -- 当前扫描到的最新 commit
    latest_commit_at     INTEGER,
    pending_from_commit  TEXT,                      -- 当前待处理区间起点（通常为 processed_commit）
    pending_to_commit    TEXT,                      -- 当前待处理区间终点（当前最新 commit）
    pending_commit_count INTEGER NOT NULL DEFAULT 0,
    blocked_reason       TEXT,
    conflict_group_id    TEXT,
    created_at           INTEGER NOT NULL,
    updated_at           INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id          TEXT PRIMARY KEY,
    req_id      TEXT NOT NULL REFERENCES requirements(id),
    filename    TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    resolved_at INTEGER
);

CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    req_id      TEXT NOT NULL REFERENCES requirements(id),
    filename    TEXT NOT NULL,
    task_type   TEXT NOT NULL,
    xxx         TEXT NOT NULL,
    yyy         TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    session_key TEXT,
    branch      TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES tasks(id),
    report_file TEXT NOT NULL,
    session_key TEXT,
    outcome     TEXT NOT NULL,
    summary     TEXT,
    started_at  INTEGER NOT NULL,
    finished_at INTEGER
);

CREATE TABLE IF NOT EXISTS task_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,                     -- req | task | review | report
    entity_id   TEXT NOT NULL,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    agent       TEXT NOT NULL DEFAULT 'main',
    note        TEXT,
    created_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_requirements_status       ON requirements(status);
CREATE INDEX IF NOT EXISTS idx_requirements_latest_time  ON requirements(latest_commit_at);
CREATE INDEX IF NOT EXISTS idx_requirements_pending_time ON requirements(pending_commit_count, latest_commit_at);
CREATE INDEX IF NOT EXISTS idx_tasks_req_id              ON tasks(req_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status              ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_xxx                 ON tasks(xxx);
CREATE INDEX IF NOT EXISTS idx_reports_task_id           ON reports(task_id);
CREATE INDEX IF NOT EXISTS idx_task_events_entity        ON task_events(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_task_events_time          ON task_events(created_at);
