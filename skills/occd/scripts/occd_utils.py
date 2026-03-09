#!/usr/bin/env python3
"""
occd_utils.py - OCCD 工具脚本 v2
供 OpenClaw 主代理和子代理调用，封装所有 git / 文件 / SQLite DB 操作

用法: python occd_utils.py <command> [--options]
所有命令输出 JSON，供调用方解析。
"""

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


# ─── 基础工具 ──────────────────────────────────────────────────────────────────

def ts_ms() -> int:
    """当前 UTC 时间，毫秒级 Unix timestamp"""
    return int(datetime.now(timezone.utc).timestamp() * 1000)

def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def output(data):
    """统一 JSON 输出"""
    print(json.dumps(data, ensure_ascii=False, indent=2))

def run_git(args: list, cwd: Path, check=False) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, check=check)


# ─── DB 层 ─────────────────────────────────────────────────────────────────────

DB_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS requirements (
    id            TEXT PRIMARY KEY,
    filename      TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'new',
    review_rounds INTEGER NOT NULL DEFAULT 0,
    last_req_commit TEXT,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id            TEXT PRIMARY KEY,
    req_id        TEXT NOT NULL REFERENCES requirements(id),
    filename      TEXT NOT NULL,
    created_at    INTEGER NOT NULL,
    resolved_at   INTEGER
);

CREATE TABLE IF NOT EXISTS sources (
    id            TEXT PRIMARY KEY,
    req_id        TEXT NOT NULL REFERENCES requirements(id),
    filename      TEXT NOT NULL,
    task_type     TEXT NOT NULL,
    xxx           TEXT NOT NULL,
    yyy           TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    session_key   TEXT,
    branch        TEXT,
    retry_count   INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS executions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     TEXT NOT NULL REFERENCES sources(id),
    report_file   TEXT NOT NULL,
    session_key   TEXT,
    outcome       TEXT NOT NULL,
    summary       TEXT,
    started_at    INTEGER NOT NULL,
    finished_at   INTEGER
);

CREATE TABLE IF NOT EXISTS task_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type   TEXT NOT NULL,
    entity_id     TEXT NOT NULL,
    from_status   TEXT,
    to_status     TEXT NOT NULL,
    agent         TEXT NOT NULL DEFAULT 'main',
    note          TEXT,
    created_at    INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_requirements_status   ON requirements(status);
CREATE INDEX IF NOT EXISTS idx_sources_req_id        ON sources(req_id);
CREATE INDEX IF NOT EXISTS idx_sources_status        ON sources(status);
CREATE INDEX IF NOT EXISTS idx_sources_xxx           ON sources(xxx);
CREATE INDEX IF NOT EXISTS idx_executions_source_id  ON executions(source_id);
CREATE INDEX IF NOT EXISTS idx_task_events_entity    ON task_events(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_task_events_time      ON task_events(created_at);
"""

def db_path(repo: Path) -> Path:
    return repo / "occd" / "occd.db"

def get_conn(repo: Path) -> sqlite3.Connection:
    p = db_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn

def init_db(repo: Path):
    conn = get_conn(repo)
    conn.executescript(DB_SCHEMA)
    conn.commit()
    conn.close()

def log_event(conn: sqlite3.Connection, entity_type: str, entity_id: str,
              from_status, to_status: str, agent="main", note=None):
    conn.execute(
        "INSERT INTO task_events(entity_type,entity_id,from_status,to_status,agent,note,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (entity_type, entity_id, from_status, to_status, agent, note, ts_ms())
    )


# ─── Git 工具 ──────────────────────────────────────────────────────────────────

def write_log(repo: Path, level: str, message: str):
    log_dir = repo / "occd" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{ts}] [{level}] {message}\n"
    with open(log_dir / f"{today}.log", "a", encoding="utf-8") as f:
        f.write(entry)

def get_repo_name(repo: Path) -> str:
    return repo.name  # e.g. github-myapp

def make_req_id(repo: Path, filename: str) -> str:
    return f"{get_repo_name(repo)}:{filename}"


# ─── 命令：db-init ─────────────────────────────────────────────────────────────

def cmd_db_init(args):
    repo = Path(args.repo)
    init_db(repo)

    # 写 .gitattributes
    ga = repo / ".gitattributes"
    line = "occd/occd.db binary\n"
    existing = ga.read_text(encoding="utf-8") if ga.exists() else ""
    if "occd/occd.db" not in existing:
        with open(ga, "a", encoding="utf-8") as f:
            f.write(line)

    write_log(repo, "INFO", "occd.db 初始化完成")
    output({"success": True, "db": str(db_path(repo))})


# ─── 命令：scan-repos ──────────────────────────────────────────────────────────

def cmd_scan_repos(args):
    work_dir = Path(args.work_dir)
    repos = sorted([d for d in work_dir.iterdir()
                    if d.is_dir() and d.name.startswith("github-")])
    result = []
    for repo in repos:
        req_dir = repo / "occd" / "req"
        if not req_dir.exists():
            continue
        db = db_path(repo)
        if not db.exists():
            init_db(repo)
        conn = get_conn(repo)
        for f in req_dir.iterdir():
            if f.suffix not in (".md", ".txt"):
                continue
            req_id = make_req_id(repo, f.name)
            content_hash = file_sha256(f)
            row = conn.execute("SELECT status, content_hash FROM requirements WHERE id=?",
                               (req_id,)).fetchone()
            if row:
                if row["status"] not in ("new", "failed") and row["content_hash"] == content_hash:
                    continue  # 跳过已处理且未变化的
                action = "hash_changed" if row["content_hash"] != content_hash else "retry"
                if row["content_hash"] != content_hash:
                    conn.execute(
                        "UPDATE requirements SET content_hash=?,status='new',updated_at=? WHERE id=?",
                        (content_hash, ts_ms(), req_id)
                    )
                    log_event(conn, "req", req_id, row["status"], "new", note="content_hash changed")
                    conn.commit()
            else:
                now = ts_ms()
                conn.execute(
                    "INSERT INTO requirements(id,filename,content_hash,status,review_rounds,created_at,updated_at) "
                    "VALUES(?,?,?,'new',0,?,?)",
                    (req_id, f.name, content_hash, now, now)
                )
                log_event(conn, "req", req_id, None, "new")
                conn.commit()
                action = "new"
            row2 = conn.execute("SELECT status FROM requirements WHERE id=?", (req_id,)).fetchone()
            result.append({
                "repo": str(repo), "repo_name": repo.name,
                "req": f.name, "req_id": req_id,
                "status": row2["status"], "action": action
            })
        conn.close()
    output({"repos": result})


# ─── 命令：git-pull ────────────────────────────────────────────────────────────

def cmd_git_pull(args):
    repo = Path(args.repo)
    r = run_git(["pull"], cwd=repo)
    write_log(repo, "INFO", f"git pull: {(r.stdout + r.stderr).strip()}")
    output({"success": r.returncode == 0, "output": r.stdout + r.stderr})


# ─── 命令：check-new-commit ────────────────────────────────────────────────────

def cmd_check_new_commit(args):
    repo = Path(args.repo)
    r = run_git(["log", "-1", "--format=%H", "--", args.file], cwd=repo)
    current = r.stdout.strip()
    has_new = bool(current) and current != args.last_commit
    output({"has_new_commit": has_new, "current_commit": current})


# ─── 命令：db-upsert-req ──────────────────────────────────────────────────────

def cmd_db_upsert_req(args):
    repo = Path(args.repo)
    req_file = repo / "occd" / "req" / args.filename
    req_id = make_req_id(repo, args.filename)
    content_hash = file_sha256(req_file)
    conn = get_conn(repo)
    row = conn.execute("SELECT status, content_hash FROM requirements WHERE id=?", (req_id,)).fetchone()
    now = ts_ms()
    if row is None:
        conn.execute(
            "INSERT INTO requirements(id,filename,content_hash,status,review_rounds,created_at,updated_at) "
            "VALUES(?,?,?,'new',0,?,?)",
            (req_id, args.filename, content_hash, now, now)
        )
        log_event(conn, "req", req_id, None, "new")
        action = "inserted"
    elif row["content_hash"] != content_hash:
        old_status = row["status"]
        conn.execute(
            "UPDATE requirements SET content_hash=?,status='new',updated_at=? WHERE id=?",
            (content_hash, now, req_id)
        )
        log_event(conn, "req", req_id, old_status, "new", note="content_hash changed")
        action = "updated"
    else:
        action = "skipped"
    conn.commit()
    conn.close()
    output({"req_id": req_id, "action": action})


# ─── 命令：db-update-req-status ───────────────────────────────────────────────

def cmd_db_update_req_status(args):
    repo = Path(args.repo) if hasattr(args, "repo") and args.repo else None
    # req_id can be passed directly or derived
    req_id = args.req_id
    # find repo from req_id if not provided
    conn_repo = repo
    if conn_repo is None:
        # req_id = "github-xxx:filename"
        # We need work_dir
        raise ValueError("--repo is required for db-update-req-status")
    conn = get_conn(conn_repo)
    row = conn.execute("SELECT status FROM requirements WHERE id=?", (req_id,)).fetchone()
    old = row["status"] if row else None
    note = getattr(args, "note", None)
    conn.execute("UPDATE requirements SET status=?,updated_at=? WHERE id=?",
                 (args.status, ts_ms(), req_id))
    log_event(conn, "req", req_id, old, args.status, note=note)
    conn.commit()
    conn.close()
    write_log(conn_repo, "INFO", f"需求状态更新: {req_id} → {args.status}")
    output({"success": True, "req_id": req_id, "status": args.status})


# ─── 命令：db-get-req ─────────────────────────────────────────────────────────

def cmd_db_get_req(args):
    repo = Path(args.repo)
    req_id = args.req_id
    conn = get_conn(repo)
    row = conn.execute("SELECT * FROM requirements WHERE id=?", (req_id,)).fetchone()
    if not row:
        output({"error": "not found"}); conn.close(); return
    reviews = [dict(r) for r in conn.execute(
        "SELECT * FROM reviews WHERE req_id=? ORDER BY created_at", (req_id,))]
    sources = [dict(s) for s in conn.execute(
        "SELECT * FROM sources WHERE req_id=? ORDER BY xxx,yyy", (req_id,))]
    conn.close()
    output({"req": dict(row), "reviews": reviews, "sources": sources})


# ─── 命令：db-list-pending-reqs ───────────────────────────────────────────────

def cmd_db_list_pending_reqs(args):
    repo = Path(args.repo)
    conn = get_conn(repo)
    rows = conn.execute(
        "SELECT * FROM requirements WHERE status IN ('new','reviewing','decomposed') ORDER BY created_at"
    ).fetchall()
    conn.close()
    output({"requirements": [dict(r) for r in rows]})


# ─── 命令：write-review ───────────────────────────────────────────────────────

def cmd_write_review(args):
    repo = Path(args.repo)
    req_name = args.req
    questions = json.loads(args.questions)
    req_id = make_req_id(repo, req_name)

    conn = get_conn(repo)
    row = conn.execute("SELECT status, review_rounds FROM requirements WHERE id=?", (req_id,)).fetchone()
    if not row:
        output({"error": f"requirement not found: {req_id}"}); conn.close(); return

    round_num = (row["review_rounds"] or 0) + 1
    old_status = row["status"]

    # 获取 last_req_commit
    r = run_git(["log", "-1", "--format=%H", "--", f"occd/req/{req_name}"], cwd=repo)
    last_commit = r.stdout.strip()

    # 写文件
    review_dir = repo / "occd" / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    base = Path(req_name).stem
    filename = f"{base}-review-{round_num:03d}.md"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    qs = "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))
    content = f"""# 需求澄清请求 #{round_num:03d}

> **需求文件**：`occd/req/{req_name}`
> **生成时间**：{now_str}
> **状态**：等待回复

---

## 不明确的内容

{qs}

---

## 如何回复

请直接修改需求文件 `occd/req/{req_name}`，补充说明后执行：

```bash
git add occd/req/{req_name}
git commit -m "clarify: {req_name} - 补充说明{round_num:03d}"
git push
```

主代理将在下次轮询时自动检测到更新并重新分析。

---

<!-- 以上内容由 auto-coder 自动生成，请勿修改分隔线以上内容 -->
"""
    (review_dir / filename).write_text(content, encoding="utf-8")

    # 更新 DB
    now = ts_ms()
    conn.execute(
        "UPDATE requirements SET status='reviewing',review_rounds=?,last_req_commit=?,updated_at=? WHERE id=?",
        (round_num, last_commit, now, req_id)
    )
    review_id = f"review-{base}-{round_num:03d}"
    conn.execute(
        "INSERT INTO reviews(id,req_id,filename,created_at) VALUES(?,?,?,?)",
        (review_id, req_id, filename, now)
    )
    log_event(conn, "req", req_id, old_status, "reviewing", note=f"round {round_num}")
    conn.commit()
    conn.close()

    write_log(repo, "INFO", f"已生成 review 文件: {filename}")
    output({"success": True, "file": str(review_dir / filename),
            "round": round_num, "review_id": review_id})


# ─── 命令：write-tasks ────────────────────────────────────────────────────────

def cmd_write_tasks(args):
    repo = Path(args.repo)
    req_name = args.req
    tasks = json.loads(args.tasks)
    req_id = make_req_id(repo, req_name)

    source_dir = repo / "occd" / "source"
    source_dir.mkdir(parents=True, exist_ok=True)

    conn = get_conn(repo)
    row = conn.execute("SELECT status FROM requirements WHERE id=?", (req_id,)).fetchone()
    if not row:
        output({"error": f"requirement not found: {req_id}"}); conn.close(); return
    old_status = row["status"]

    now = ts_ms()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    created_ids = []

    for task in tasks:
        task_id = task["id"]
        # 解析 reqZZZ-XXX-YYY-type
        parts = task_id.split("-")
        # parts: ['reqZZZ', 'XXX', 'YYY', 'type'] or ['reqZZZ', 'XXX', 'YYY', 'test', 'write']
        xxx = parts[1]
        yyy = parts[2]
        task_type = task.get("type", "-".join(parts[3:]))
        filename = f"{task_id}.md"
        branch = task_id if task_type in ("coding", "test-write") else None

        depends_on = task.get("depends_on", [])
        depends_str = json.dumps(depends_on, ensure_ascii=False)

        content = f"""---
task_id: {task_id}
task_type: {task_type}
req_file: {req_name}
xxx: "{xxx}"
yyy: "{yyy}"
depends_on: {depends_str}
---

## 背景

{task.get('background', task.get('summary', ''))}

## 任务要求

{task.get('details', '')}

## 约束条件

{task.get('constraints', '自动识别，遵循仓库现有代码规范。')}

## 验收条件

{task.get('acceptance', '')}

## 参考信息

{task.get('notes', '无')}

## 报告要求

完成后将执行结果报告写入：
`occd/task/report-{task_id}-{{YYYYMMDDTHHMMSS}}.md`

格式见 `references/task-report-template.md`。
"""
        (source_dir / filename).write_text(content, encoding="utf-8")

        # 写入 DB
        conn.execute(
            "INSERT OR REPLACE INTO sources"
            "(id,req_id,filename,task_type,xxx,yyy,status,branch,retry_count,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,'pending',?,0,?,?)",
            (task_id, req_id, filename, task_type, xxx, yyy, branch, now, now)
        )
        log_event(conn, "source", task_id, None, "pending")
        created_ids.append(task_id)

    conn.execute("UPDATE requirements SET status='decomposed',updated_at=? WHERE id=?", (now, req_id))
    log_event(conn, "req", req_id, old_status, "decomposed", note=f"{len(tasks)} tasks")
    conn.commit()
    conn.close()

    write_log(repo, "INFO", f"已生成 {len(tasks)} 个子任务: {req_name}")
    output({"success": True, "tasks": created_ids})


# ─── 命令：db-upsert-source ───────────────────────────────────────────────────

def cmd_db_upsert_source(args):
    repo = Path(args.repo)
    now = ts_ms()
    task_id = args.task_id
    task_type = args.task_type
    parts = task_id.split("-")
    xxx, yyy = parts[1], parts[2]
    branch = task_id if task_type in ("coding", "test-write") else None
    conn = get_conn(repo)
    conn.execute(
        "INSERT OR REPLACE INTO sources"
        "(id,req_id,filename,task_type,xxx,yyy,status,branch,retry_count,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,'pending',?,0,?,?)",
        (task_id, args.req_id, args.filename, task_type, xxx, yyy, branch, now, now)
    )
    log_event(conn, "source", task_id, None, "pending")
    conn.commit(); conn.close()
    output({"success": True, "task_id": task_id})


# ─── 命令：db-update-source-status ────────────────────────────────────────────

def cmd_db_update_source_status(args):
    repo = Path(args.repo)
    task_id = args.task
    new_status = args.status
    session_key = getattr(args, "session_key", None)
    note = getattr(args, "note", None)

    conn = get_conn(repo)
    row = conn.execute("SELECT status, retry_count FROM sources WHERE id=?", (task_id,)).fetchone()
    if not row:
        output({"error": f"source not found: {task_id}"}); conn.close(); return
    old_status = row["status"]

    updates = ["status=?", "updated_at=?"]
    vals = [new_status, ts_ms()]
    if session_key:
        updates.append("session_key=?"); vals.append(session_key)
    if new_status == "spawned" and old_status == "failed":
        updates.append("retry_count=retry_count+1")
    vals.append(task_id)

    conn.execute(f"UPDATE sources SET {', '.join(updates)} WHERE id=?", vals)
    agent = f"sub:{session_key}" if session_key and new_status in ("running", "done", "failed") else "main"
    log_event(conn, "source", task_id, old_status, new_status, agent=agent, note=note)
    conn.commit(); conn.close()

    write_log(repo, "INFO", f"子任务状态更新: {task_id} → {new_status}")
    output({"success": True, "task_id": task_id, "status": new_status})


# ─── 命令：db-list-sources-by-xxx ─────────────────────────────────────────────

def cmd_db_list_sources_by_xxx(args):
    repo = Path(args.repo)
    conn = get_conn(repo)
    rows = conn.execute(
        "SELECT * FROM sources WHERE xxx=? ORDER BY yyy", (args.xxx,)
    ).fetchall()
    conn.close()
    output({"sources": [dict(r) for r in rows]})


# ─── 命令：db-add-execution ───────────────────────────────────────────────────

def cmd_db_add_execution(args):
    repo = Path(args.repo)
    conn = get_conn(repo)
    now = ts_ms()
    conn.execute(
        "INSERT INTO executions(source_id,report_file,session_key,outcome,summary,started_at,finished_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (args.task, args.report_file,
         getattr(args, "session_key", None),
         args.outcome,
         getattr(args, "summary", None),
         now, now)
    )
    conn.commit(); conn.close()
    output({"success": True})


# ─── 命令：db-list-executions ─────────────────────────────────────────────────

def cmd_db_list_executions(args):
    repo = Path(args.repo)
    conn = get_conn(repo)
    rows = conn.execute(
        "SELECT * FROM executions WHERE source_id=? ORDER BY started_at", (args.task,)
    ).fetchall()
    conn.close()
    output({"executions": [dict(r) for r in rows]})


# ─── 命令：db-summary ─────────────────────────────────────────────────────────

def cmd_db_summary(args):
    work_dir = Path(args.work_dir)
    repos = sorted([d for d in work_dir.iterdir()
                    if d.is_dir() and d.name.startswith("github-")])
    result = {}
    for repo in repos:
        db = db_path(repo)
        if not db.exists():
            continue
        conn = get_conn(repo)
        req_rows = conn.execute("SELECT status, COUNT(*) as cnt FROM requirements GROUP BY status").fetchall()
        src_rows = conn.execute("SELECT status, task_type, COUNT(*) as cnt FROM sources GROUP BY status,task_type").fetchall()
        exc_rows = conn.execute("SELECT outcome, COUNT(*) as cnt FROM executions GROUP BY outcome").fetchall()
        conn.close()

        req_stats = {"total": 0}
        for r in req_rows:
            req_stats[r["status"]] = r["cnt"]
            req_stats["total"] += r["cnt"]

        src_stats = {"total": 0, "by_type": {}, "by_status": {}}
        for r in src_rows:
            src_stats["total"] += r["cnt"]
            src_stats["by_type"][r["task_type"]] = src_stats["by_type"].get(r["task_type"], 0) + r["cnt"]
            src_stats["by_status"][r["status"]] = src_stats["by_status"].get(r["status"], 0) + r["cnt"]

        exc_stats = {"total": 0}
        for r in exc_rows:
            exc_stats[r["outcome"]] = r["cnt"]
            exc_stats["total"] += r["cnt"]

        result[repo.name] = {
            "requirements": req_stats,
            "sources": src_stats,
            "executions": exc_stats
        }
    output(result)


# ─── 命令：create-worktree ────────────────────────────────────────────────────

def cmd_create_worktree(args):
    repo = Path(args.repo)
    branch = args.branch
    worktree_path = repo.parent / ".occd-worktrees" / repo.name / branch
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    r = run_git(["worktree", "add", "-b", branch, str(worktree_path)], cwd=repo)
    success = r.returncode == 0
    write_log(repo, "INFO" if success else "ERROR",
              f"worktree {'创建成功' if success else '创建失败'}: {branch} → {worktree_path}")
    output({"success": success, "worktree_path": str(worktree_path), "error": r.stderr})


# ─── 命令：remove-worktree ────────────────────────────────────────────────────

def cmd_remove_worktree(args):
    repo = Path(args.repo)
    branch = args.branch
    worktree_path = repo.parent / ".occd-worktrees" / repo.name / branch
    run_git(["worktree", "remove", "--force", str(worktree_path)], cwd=repo)
    write_log(repo, "INFO", f"worktree 已清理: {branch}")
    output({"success": True})


# ─── 命令：merge-branches ─────────────────────────────────────────────────────

def cmd_merge_branches(args):
    repo = Path(args.repo)
    xxx = args.xxx
    conn = get_conn(repo)
    rows = conn.execute(
        "SELECT id, branch FROM sources WHERE xxx=? AND task_type IN ('coding','test-write') AND status='done'",
        (xxx,)
    ).fetchall()
    conn.close()
    branches = [r["branch"] for r in rows if r["branch"]]

    def get_commit_time(branch):
        r = run_git(["log", "-1", "--format=%aI", branch], cwd=repo)
        return r.stdout.strip() or "9999"

    branches.sort(key=get_commit_time)
    conflicts, merged = [], []
    for branch in branches:
        r = run_git(["merge", "--no-ff", branch, "-m", f"[occd] merge {branch}"], cwd=repo)
        if r.returncode != 0:
            run_git(["merge", "--abort"], cwd=repo)
            conflicts.append(branch)
            write_log(repo, "WARN", f"合并冲突: {branch}")
        else:
            merged.append(branch)
            write_log(repo, "INFO", f"合并成功: {branch}")
    output({"conflicts": conflicts, "merged": merged})


# ─── 命令：run-tests ──────────────────────────────────────────────────────────

def _detect_test_command(repo: Path) -> list:
    detectors = [
        ("pytest.ini",     ["pytest"]),
        ("pyproject.toml", ["pytest"]),
        ("setup.py",       ["pytest"]),
        ("go.mod",         ["go", "test", "./..."]),
        ("Cargo.toml",     ["cargo", "test"]),
        ("Makefile",       ["make", "test"]),
    ]
    for fname, cmd in detectors:
        if (repo / fname).exists():
            return cmd
    if (repo / "package.json").exists():
        pkg = json.loads((repo / "package.json").read_text(encoding="utf-8"))
        scripts = pkg.get("scripts", {})
        if "test" in scripts:
            ts_str = scripts["test"].lower()
            if "vitest" in ts_str: return ["npx", "vitest", "run"]
            if "jest" in ts_str:   return ["npx", "jest"]
            if "mocha" in ts_str:  return ["npx", "mocha"]
        return ["npm", "test"]
    return ["echo", "No test framework detected"]

def cmd_run_tests(args):
    repo = Path(args.repo)
    cmd = _detect_test_command(repo)
    write_log(repo, "INFO", f"执行测试: {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
    passed = r.returncode == 0
    write_log(repo, "INFO" if passed else "WARN",
              f"测试{'通过' if passed else '失败'}: returncode={r.returncode}")
    output({"passed": passed, "output": r.stdout + r.stderr, "command": cmd})


# ─── 命令：commit-push ────────────────────────────────────────────────────────

def cmd_commit_push(args):
    repo = Path(args.repo)
    include_db = getattr(args, "include_db", False)
    if include_db:
        run_git(["add", str(db_path(repo))], cwd=repo)
    run_git(["add", "-A"], cwd=repo, check=True)
    r = run_git(["commit", "-m", args.message], cwd=repo)
    if r.returncode != 0:
        output({"success": False, "error": r.stderr}); return
    r2 = run_git(["push"], cwd=repo)
    success = r2.returncode == 0
    write_log(repo, "INFO" if success else "ERROR",
              f"commit+push {'成功' if success else '失败'}: {args.message}")
    output({"success": success, "error": r2.stderr if not success else ""})


# ─── 命令：log ────────────────────────────────────────────────────────────────

def cmd_log(args):
    write_log(Path(args.repo), args.level.upper(), args.message)
    output({"success": True})


# ─── CLI 入口 ─────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(prog="occd_utils", description="OCCD 工具脚本 v2")
    sub = p.add_subparsers(dest="cmd")

    def sp(name, help_text):
        return sub.add_parser(name, help=help_text)

    # db-init
    s = sp("db-init", "初始化仓库 occd.db 和 .gitattributes")
    s.add_argument("--repo", required=True)

    # scan-repos
    s = sp("scan-repos", "扫描 work_dir 下所有 github-* 仓库，返回需要处理的需求")
    s.add_argument("--work-dir", required=True)

    # git-pull
    s = sp("git-pull", "git pull"); s.add_argument("--repo", required=True)

    # check-new-commit
    s = sp("check-new-commit", "检查需求文件是否有新 commit")
    s.add_argument("--repo", required=True)
    s.add_argument("--file", required=True)
    s.add_argument("--last-commit", default="")

    # db-upsert-req
    s = sp("db-upsert-req", "新增或更新需求记录")
    s.add_argument("--repo", required=True)
    s.add_argument("--filename", required=True)

    # db-update-req-status
    s = sp("db-update-req-status", "更新需求状态")
    s.add_argument("--repo", required=True)
    s.add_argument("--req-id", required=True)
    s.add_argument("--status", required=True)
    s.add_argument("--note", default=None)

    # db-get-req
    s = sp("db-get-req", "获取需求详情")
    s.add_argument("--repo", required=True)
    s.add_argument("--req-id", required=True)

    # db-list-pending-reqs
    s = sp("db-list-pending-reqs", "列出未完成需求")
    s.add_argument("--repo", required=True)

    # write-review
    s = sp("write-review", "生成需求澄清文件")
    s.add_argument("--repo", required=True)
    s.add_argument("--req", required=True)
    s.add_argument("--questions", required=True, help="JSON 数组字符串")

    # write-tasks
    s = sp("write-tasks", "写入子任务 prompt 文件并更新 DB")
    s.add_argument("--repo", required=True)
    s.add_argument("--req", required=True)
    s.add_argument("--tasks", required=True, help="JSON 数组字符串")

    # db-upsert-source
    s = sp("db-upsert-source", "新增子任务记录")
    s.add_argument("--repo", required=True)
    s.add_argument("--req-id", required=True)
    s.add_argument("--task-id", required=True)
    s.add_argument("--task-type", required=True)
    s.add_argument("--filename", required=True)

    # db-update-source-status
    s = sp("db-update-source-status", "更新子任务状态（主/子代理均可调用）")
    s.add_argument("--repo", required=True)
    s.add_argument("--task", required=True)
    s.add_argument("--status", required=True)
    s.add_argument("--session-key", default=None)
    s.add_argument("--note", default=None)

    # db-list-sources-by-xxx
    s = sp("db-list-sources-by-xxx", "列出某串行批次下所有子任务")
    s.add_argument("--repo", required=True)
    s.add_argument("--xxx", required=True)

    # db-add-execution
    s = sp("db-add-execution", "登记一次执行记录")
    s.add_argument("--repo", required=True)
    s.add_argument("--task", required=True)
    s.add_argument("--report-file", required=True)
    s.add_argument("--outcome", required=True, choices=["success", "failure", "partial"])
    s.add_argument("--summary", default=None)
    s.add_argument("--session-key", default=None)

    # db-list-executions
    s = sp("db-list-executions", "列出某子任务所有执行历史")
    s.add_argument("--repo", required=True)
    s.add_argument("--task", required=True)

    # db-summary
    s = sp("db-summary", "全局状态汇总")
    s.add_argument("--work-dir", required=True)

    # create-worktree
    s = sp("create-worktree", "创建 git worktree")
    s.add_argument("--repo", required=True)
    s.add_argument("--branch", required=True)

    # remove-worktree
    s = sp("remove-worktree", "清理 git worktree")
    s.add_argument("--repo", required=True)
    s.add_argument("--branch", required=True)

    # merge-branches
    s = sp("merge-branches", "合并同批次完成分支")
    s.add_argument("--repo", required=True)
    s.add_argument("--xxx", required=True)

    # run-tests
    s = sp("run-tests", "自动识别并执行测试")
    s.add_argument("--repo", required=True)

    # commit-push
    s = sp("commit-push", "提交并推送")
    s.add_argument("--repo", required=True)
    s.add_argument("--message", required=True)
    s.add_argument("--include-db", action="store_true", help="同时提交 occd.db")

    # log
    s = sp("log", "写运行日志")
    s.add_argument("--repo", required=True)
    s.add_argument("--level", default="INFO")
    s.add_argument("--message", required=True)

    args = p.parse_args()

    # argparse 会把 --req-id 变成 args.req_id（连字符→下划线）
    cmds = {
        "db-init":                cmd_db_init,
        "scan-repos":             cmd_scan_repos,
        "git-pull":               cmd_git_pull,
        "check-new-commit":       cmd_check_new_commit,
        "db-upsert-req":          cmd_db_upsert_req,
        "db-update-req-status":   cmd_db_update_req_status,
        "db-get-req":             cmd_db_get_req,
        "db-list-pending-reqs":   cmd_db_list_pending_reqs,
        "write-review":           cmd_write_review,
        "write-tasks":            cmd_write_tasks,
        "db-upsert-source":       cmd_db_upsert_source,
        "db-update-source-status":cmd_db_update_source_status,
        "db-list-sources-by-xxx": cmd_db_list_sources_by_xxx,
        "db-add-execution":       cmd_db_add_execution,
        "db-list-executions":     cmd_db_list_executions,
        "db-summary":             cmd_db_summary,
        "create-worktree":        cmd_create_worktree,
        "remove-worktree":        cmd_remove_worktree,
        "merge-branches":         cmd_merge_branches,
        "run-tests":              cmd_run_tests,
        "commit-push":            cmd_commit_push,
        "log":                    cmd_log,
    }

    if args.cmd in cmds:
        cmds[args.cmd](args)
    else:
        p.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
