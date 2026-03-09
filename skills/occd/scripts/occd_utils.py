#!/usr/bin/env python3
"""
occd_utils.py - OCCD Auto-Coder 工具脚本
供 OpenClaw 主代理和子代理调用，封装所有 git / 文件 / SQLite 状态操作

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
import time
from datetime import datetime, timezone
from pathlib import Path


# ─── 工具函数 ────────────────────────────────────────────────────────────────

def now_ms() -> int:
    """当前 Unix 时间戳（毫秒）"""
    return int(time.time() * 1000)

def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()

def output(data):
    """输出 JSON 结果供调用方解析"""
    print(json.dumps(data, ensure_ascii=False, indent=2))

def run_git(args: list, cwd: Path, check=False) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, check=check)

def write_log(repo: Path, level: str, message: str):
    log_dir = repo / "occd" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{ts}] [{level.upper()}] {message}\n"
    with open(log_dir / f"{today}.log", "a", encoding="utf-8") as f:
        f.write(entry)


# ─── SQLite 数据库 ────────────────────────────────────────────────────────────

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

def get_db(repo: Path) -> sqlite3.Connection:
    path = db_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db(repo: Path):
    conn = get_db(repo)
    conn.executescript(DB_SCHEMA)
    conn.commit()
    conn.close()

def add_event(conn: sqlite3.Connection, entity_type: str, entity_id: str,
              from_status, to_status: str, agent: str = "main", note: str = None):
    conn.execute(
        "INSERT INTO task_events (entity_type, entity_id, from_status, to_status, agent, note, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (entity_type, entity_id, from_status, to_status, agent, note, now_ms())
    )

def repo_name(repo: Path) -> str:
    return repo.resolve().name  # github-xxx

def _resolve_repo(path_str: str) -> Path:
    """解析仓库路径，始终返回绝对路径"""
    return Path(path_str).resolve()


# ─── DB 初始化 ────────────────────────────────────────────────────────────────

def cmd_db_init(args):
    """初始化仓库的 occd.db，并写入 .gitattributes"""
    repo = _resolve_repo(args.repo)
    init_db(repo)

    # 写 .gitattributes
    ga = repo / ".gitattributes"
    line = "occd/occd.db binary\n"
    existing = ga.read_text(encoding="utf-8") if ga.exists() else ""
    if "occd/occd.db" not in existing:
        with open(ga, "a", encoding="utf-8") as f:
            f.write(line)
        write_log(repo, "INFO", ".gitattributes 已添加 occd/occd.db binary")

    write_log(repo, "INFO", "occd.db 初始化完成")
    output({"success": True, "db": str(db_path(repo))})


# ─── 仓库扫描 ─────────────────────────────────────────────────────────────────

def cmd_scan_repos(args):
    """扫描 work_dir 下所有 github-* 仓库，比对 occd.db 后返回需要处理的需求列表"""
    work_dir = Path(args.work_dir).resolve()
    repos = sorted([d for d in work_dir.iterdir()
                    if d.is_dir() and d.name.startswith("github-")])
    result = []
    for repo in repos:
        req_dir = repo / "occd" / "req"
        if not req_dir.exists():
            continue

        db_file = db_path(repo)
        if not db_file.exists():
            # 尚未初始化，所有需求视为 new
            for f in req_dir.iterdir():
                if f.suffix in (".md", ".txt"):
                    result.append({
                        "repo": str(repo), "repo_name": repo.name,
                        "req": f.name, "req_id": f"{repo.name}:{f.name}",
                        "status": "new", "hash_changed": False
                    })
            continue

        conn = get_db(repo)
        for f in req_dir.iterdir():
            if f.suffix not in (".md", ".txt"):
                continue
            req_id = f"{repo.name}:{f.name}"
            content_hash = file_sha256(f)
            row = conn.execute(
                "SELECT status, content_hash FROM requirements WHERE id=?", (req_id,)
            ).fetchone()

            if row is None:
                # 新需求：插入
                conn.execute(
                    "INSERT INTO requirements (id, filename, content_hash, status, created_at, updated_at) "
                    "VALUES (?,?,?,'new',?,?)",
                    (req_id, f.name, content_hash, now_ms(), now_ms())
                )
                conn.commit()
                result.append({
                    "repo": str(repo), "repo_name": repo.name,
                    "req": f.name, "req_id": req_id,
                    "status": "new", "hash_changed": False
                })
            else:
                status = row["status"]
                hash_changed = row["content_hash"] != content_hash
                if hash_changed:
                    # 内容变化，重置为 new
                    conn.execute(
                        "UPDATE requirements SET status='new', content_hash=?, updated_at=? WHERE id=?",
                        (content_hash, now_ms(), req_id)
                    )
                    add_event(conn, "req", req_id, status, "new", note="content hash changed")
                    conn.commit()
                    result.append({
                        "repo": str(repo), "repo_name": repo.name,
                        "req": f.name, "req_id": req_id,
                        "status": "new", "hash_changed": True
                    })
                elif status not in ("done",):
                    result.append({
                        "repo": str(repo), "repo_name": repo.name,
                        "req": f.name, "req_id": req_id,
                        "status": status, "hash_changed": False
                    })
        conn.close()

    output({"repos": result})


# ─── Git 操作 ─────────────────────────────────────────────────────────────────

def cmd_git_pull(args):
    repo = _resolve_repo(args.repo)
    r = run_git(["pull"], cwd=repo)
    write_log(repo, "INFO", f"git pull: {r.stdout.strip() or r.stderr.strip()}")
    output({"success": r.returncode == 0, "output": r.stdout + r.stderr})


def cmd_check_new_commit(args):
    """检查某文件自 last_commit 以来是否有新 commit"""
    repo = _resolve_repo(args.repo)
    r = run_git(["log", "-1", "--format=%H", "--", args.file], cwd=repo)
    current = r.stdout.strip()
    has_new = bool(current) and current != (args.last_commit or "")
    output({"has_new_commit": has_new, "current_commit": current})


def cmd_create_worktree(args):
    repo = _resolve_repo(args.repo)
    branch = args.branch
    worktree_path = repo.parent / ".occd-worktrees" / repo.name / branch
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    r = run_git(["worktree", "add", "-b", branch, str(worktree_path)], cwd=repo)
    success = r.returncode == 0
    write_log(repo, "INFO" if success else "ERROR",
              f"worktree {'创建成功' if success else '创建失败'}: {branch} -> {worktree_path}")
    output({"success": success, "worktree_path": str(worktree_path), "error": r.stderr})


def cmd_remove_worktree(args):
    repo = _resolve_repo(args.repo)
    branch = args.branch
    worktree_path = repo.parent / ".occd-worktrees" / repo.name / branch
    run_git(["worktree", "remove", "--force", str(worktree_path)], cwd=repo)
    write_log(repo, "INFO", f"worktree 已清理: {branch}")
    output({"success": True})


def cmd_merge_branches(args):
    """按 commit 时间升序合并同一 XXX 下的所有分支，返回冲突列表"""
    repo = _resolve_repo(args.repo)
    xxx = args.xxx
    conn = get_db(repo)
    rows = conn.execute(
        "SELECT id, branch FROM sources WHERE xxx=? AND task_type IN ('coding','test-write') "
        "AND status='done'",
        (xxx,)
    ).fetchall()
    conn.close()
    branches = [(r["id"], r["branch"]) for r in rows if r["branch"]]

    def get_commit_time(branch):
        r = run_git(["log", "-1", "--format=%aI", branch], cwd=repo)
        return r.stdout.strip() or ""

    branches.sort(key=lambda x: get_commit_time(x[1]))
    conflicts, merged = [], []
    for task_id, branch in branches:
        r = run_git(["merge", "--no-ff", branch, "-m", f"[occd] merge {branch}"], cwd=repo)
        if r.returncode != 0:
            run_git(["merge", "--abort"], cwd=repo)
            conflicts.append(branch)
            write_log(repo, "WARN", f"合并冲突: {branch}")
        else:
            merged.append(branch)
            write_log(repo, "INFO", f"合并成功: {branch}")
    output({"conflicts": conflicts, "merged": merged})


def cmd_run_tests(args):
    repo = _resolve_repo(args.repo)
    cmd = _detect_test_command(repo)
    write_log(repo, "INFO", f"执行测试: {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
    passed = r.returncode == 0
    write_log(repo, "INFO" if passed else "WARN",
              f"测试{'通过' if passed else '失败'}: returncode={r.returncode}")
    output({"passed": passed, "output": r.stdout + r.stderr, "command": cmd})


def cmd_commit_push(args):
    """提交并推送；--include-db 同时提交 occd.db"""
    repo = _resolve_repo(args.repo)
    include_db = getattr(args, "include_db", False)

    if include_db:
        run_git(["add", str(db_path(repo))], cwd=repo)

    run_git(["add", "-A"], cwd=repo)
    # 检查是否有东西可 commit
    r_status = run_git(["status", "--porcelain"], cwd=repo)
    if not r_status.stdout.strip():
        output({"success": True, "skipped": True, "reason": "nothing to commit"})
        return

    r = run_git(["commit", "-m", args.message], cwd=repo)
    if r.returncode != 0:
        output({"success": False, "error": r.stderr})
        return
    r2 = run_git(["push"], cwd=repo)
    success = r2.returncode == 0
    write_log(repo, "INFO" if success else "ERROR",
              f"commit+push {'成功' if success else '失败'}: {args.message}")
    output({"success": success, "error": r2.stderr if not success else ""})


# ─── 需求管理（DB） ────────────────────────────────────────────────────────────

def cmd_db_upsert_req(args):
    """新增或更新需求记录（hash 变化时自动重置状态为 new）"""
    repo = _resolve_repo(args.repo)
    filename = args.filename
    req_id = f"{repo.name}:{filename}"
    f = repo / "occd" / "req" / filename
    if not f.exists():
        output({"success": False, "error": f"req file not found: {f}"})
        return
    content_hash = file_sha256(f)
    conn = get_db(repo)
    row = conn.execute("SELECT status, content_hash FROM requirements WHERE id=?", (req_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO requirements (id, filename, content_hash, status, created_at, updated_at) "
            "VALUES (?,?,?,'new',?,?)",
            (req_id, filename, content_hash, now_ms(), now_ms())
        )
        add_event(conn, "req", req_id, None, "new")
        conn.commit()
        action = "inserted"
    elif row["content_hash"] != content_hash:
        old_status = row["status"]
        conn.execute(
            "UPDATE requirements SET status='new', content_hash=?, updated_at=? WHERE id=?",
            (content_hash, now_ms(), req_id)
        )
        add_event(conn, "req", req_id, old_status, "new", note="content hash changed")
        conn.commit()
        action = "updated"
    else:
        action = "skipped"
    conn.close()
    output({"req_id": req_id, "action": action})


def cmd_db_update_req_status(args):
    req_id = args.req_id
    new_status = args.status
    # 从 req_id 推断 repo 路径需要 work_dir，这里直接用 --repo
    repo = _resolve_repo(args.repo)
    conn = get_db(repo)
    row = conn.execute("SELECT status FROM requirements WHERE id=?", (req_id,)).fetchone()
    if row is None:
        conn.close()
        output({"success": False, "error": f"requirement not found: {req_id}"})
        return
    old_status = row["status"]
    note = getattr(args, "note", None)
    conn.execute(
        "UPDATE requirements SET status=?, updated_at=? WHERE id=?",
        (new_status, now_ms(), req_id)
    )
    add_event(conn, "req", req_id, old_status, new_status, note=note)
    conn.commit()
    conn.close()
    write_log(repo, "INFO", f"需求状态更新: {req_id} {old_status} → {new_status}")
    output({"success": True, "req_id": req_id, "from": old_status, "to": new_status})


def cmd_db_get_req(args):
    req_id = args.req_id
    repo = _resolve_repo(args.repo)
    conn = get_db(repo)
    row = conn.execute("SELECT * FROM requirements WHERE id=?", (req_id,)).fetchone()
    if row is None:
        conn.close()
        output({"success": False, "error": "not found"})
        return
    req = dict(row)
    reviews = [dict(r) for r in conn.execute(
        "SELECT * FROM reviews WHERE req_id=? ORDER BY created_at", (req_id,)).fetchall()]
    sources = [dict(r) for r in conn.execute(
        "SELECT * FROM sources WHERE req_id=? ORDER BY xxx, yyy", (req_id,)).fetchall()]
    conn.close()
    output({"requirement": req, "reviews": reviews, "sources": sources})


def cmd_db_list_pending_reqs(args):
    repo = _resolve_repo(args.repo)
    conn = get_db(repo)
    rows = conn.execute(
        "SELECT * FROM requirements WHERE status IN ('new','reviewing','decomposed') "
        "ORDER BY created_at"
    ).fetchall()
    conn.close()
    output({"requirements": [dict(r) for r in rows]})


# ─── 子任务管理（DB） ─────────────────────────────────────────────────────────

def cmd_db_upsert_source(args):
    repo = _resolve_repo(args.repo)
    task_id = args.task_id
    req_id = args.req_id
    task_type = args.task_type
    filename = args.filename

    # 解析 xxx, yyy from task_id: reqZZZ-XXX-YYY-{type}
    parts = task_id.split("-")
    # parts[0]=reqZZZ, parts[1]=XXX, parts[2]=YYY, parts[3]=type (may have more segments)
    xxx = parts[1] if len(parts) > 1 else "001"
    yyy = parts[2] if len(parts) > 2 else "001"

    conn = get_db(repo)
    row = conn.execute("SELECT id FROM sources WHERE id=?", (task_id,)).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO sources (id, req_id, filename, task_type, xxx, yyy, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,'pending',?,?)",
            (task_id, req_id, filename, task_type, xxx, yyy, now_ms(), now_ms())
        )
        add_event(conn, "source", task_id, None, "pending")
        conn.commit()
        action = "inserted"
    else:
        action = "exists"
    conn.close()
    output({"task_id": task_id, "action": action})


def cmd_db_update_source_status(args):
    """主代理或子代理汇报 source 状态"""
    repo = _resolve_repo(args.repo)
    task_id = args.task
    new_status = args.status
    session_key = getattr(args, "session_key", None)
    note = getattr(args, "note", None)

    conn = get_db(repo)
    row = conn.execute("SELECT status, retry_count FROM sources WHERE id=?", (task_id,)).fetchone()
    if row is None:
        conn.close()
        output({"success": False, "error": f"source not found: {task_id}"})
        return
    old_status = row["status"]
    updates = ["status=?", "updated_at=?"]
    vals = [new_status, now_ms()]
    if session_key:
        updates.append("session_key=?")
        vals.append(session_key)
    if new_status == "failed":
        updates.append("retry_count=retry_count+1")
    vals.append(task_id)
    conn.execute(f"UPDATE sources SET {', '.join(updates)} WHERE id=?", vals)
    agent = f"sub:{session_key}" if session_key else "main"
    add_event(conn, "source", task_id, old_status, new_status, agent=agent, note=note)
    conn.commit()
    conn.close()
    write_log(repo, "INFO", f"source 状态更新: {task_id} {old_status} → {new_status}")
    output({"success": True, "task_id": task_id, "from": old_status, "to": new_status})


def cmd_db_list_sources_by_xxx(args):
    repo = _resolve_repo(args.repo)
    conn = get_db(repo)
    rows = conn.execute(
        "SELECT * FROM sources WHERE xxx=? ORDER BY yyy", (args.xxx,)
    ).fetchall()
    conn.close()
    output({"sources": [dict(r) for r in rows]})


# ─── 执行记录管理（DB） ───────────────────────────────────────────────────────

def cmd_db_add_execution(args):
    repo = _resolve_repo(args.repo)
    task_id = args.task
    report_file = args.report_file
    outcome = args.outcome
    summary = getattr(args, "summary", None)
    session_key = getattr(args, "session_key", None)
    started_at = getattr(args, "started_at", None) or now_ms()
    finished_at = now_ms()

    conn = get_db(repo)
    conn.execute(
        "INSERT INTO executions (source_id, report_file, session_key, outcome, summary, started_at, finished_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (task_id, report_file, session_key, outcome, summary, started_at, finished_at)
    )
    conn.commit()
    exec_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.close()
    write_log(repo, "INFO", f"执行记录已登记: {task_id} → {outcome}")
    output({"success": True, "execution_id": exec_id})


def cmd_db_list_executions(args):
    repo = _resolve_repo(args.repo)
    conn = get_db(repo)
    rows = conn.execute(
        "SELECT * FROM executions WHERE source_id=? ORDER BY started_at", (args.task,)
    ).fetchall()
    conn.close()
    output({"executions": [dict(r) for r in rows]})


# ─── 汇总 ─────────────────────────────────────────────────────────────────────

def cmd_db_summary(args):
    """输出所有仓库的需求 / 任务状态汇总"""
    work_dir = Path(args.work_dir).resolve()
    repos = sorted([d for d in work_dir.iterdir()
                    if d.is_dir() and d.name.startswith("github-")])
    result = {}
    for repo in repos:
        if not db_path(repo).exists():
            continue
        conn = get_db(repo)

        def cnt(sql, *p):
            return conn.execute(sql, p).fetchone()[0]

        req_total = cnt("SELECT COUNT(*) FROM requirements")
        req_done = cnt("SELECT COUNT(*) FROM requirements WHERE status='done'")
        req_failed = cnt("SELECT COUNT(*) FROM requirements WHERE status='failed'")
        req_inprog = req_total - req_done - req_failed

        src_total = cnt("SELECT COUNT(*) FROM sources")
        src_done = cnt("SELECT COUNT(*) FROM sources WHERE status='done'")
        src_failed = cnt("SELECT COUNT(*) FROM sources WHERE status='failed'")
        src_pending = cnt("SELECT COUNT(*) FROM sources WHERE status='pending'")

        by_type = {}
        for row in conn.execute("SELECT task_type, COUNT(*) AS n FROM sources GROUP BY task_type"):
            by_type[row["task_type"]] = row["n"]

        exec_total = cnt("SELECT COUNT(*) FROM executions")
        exec_success = cnt("SELECT COUNT(*) FROM executions WHERE outcome='success'")
        exec_failure = cnt("SELECT COUNT(*) FROM executions WHERE outcome='failure'")

        conn.close()
        result[repo.name] = {
            "requirements": {"total": req_total, "done": req_done,
                             "in_progress": req_inprog, "failed": req_failed},
            "sources": {"total": src_total, "by_type": by_type,
                        "done": src_done, "pending": src_pending, "failed": src_failed},
            "executions": {"total": exec_total, "success": exec_success, "failure": exec_failure},
        }
    output(result)


# ─── 文件写入 ─────────────────────────────────────────────────────────────────

def cmd_write_review(args):
    """生成需求澄清文件，更新 DB 状态为 reviewing"""
    repo = _resolve_repo(args.repo)
    req_name = args.req
    questions = json.loads(args.questions)

    conn = get_db(repo)
    req_id = f"{repo.name}:{req_name}"
    row = conn.execute("SELECT * FROM requirements WHERE id=?", (req_id,)).fetchone()
    if row is None:
        conn.close()
        output({"success": False, "error": f"requirement not found: {req_id}"})
        return

    round_num = row["review_rounds"] + 1
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

    # 更新 last_req_commit
    r = run_git(["log", "-1", "--format=%H", "--", f"occd/req/{req_name}"], cwd=repo)
    last_commit = r.stdout.strip()

    old_status = row["status"]
    review_id = f"review-{base}-{round_num:03d}"
    conn.execute(
        "INSERT INTO reviews (id, req_id, filename, created_at) VALUES (?,?,?,?)",
        (review_id, req_id, filename, now_ms())
    )
    conn.execute(
        "UPDATE requirements SET status='reviewing', review_rounds=?, last_req_commit=?, updated_at=? "
        "WHERE id=?",
        (round_num, last_commit, now_ms(), req_id)
    )
    add_event(conn, "req", req_id, old_status, "reviewing", note=f"review round {round_num}")
    conn.commit()
    conn.close()
    write_log(repo, "INFO", f"已生成 review 文件: {filename}")
    output({"success": True, "file": str(review_dir / filename), "round": round_num})


def cmd_write_tasks(args):
    """将主代理分解的子任务写入 occd/source/，并在 DB 中建立 sources 记录"""
    repo = _resolve_repo(args.repo)
    req_name = args.req
    tasks = json.loads(args.tasks)

    source_dir = repo / "occd" / "source"
    source_dir.mkdir(parents=True, exist_ok=True)

    conn = get_db(repo)
    req_id = f"{repo.name}:{req_name}"
    row = conn.execute("SELECT * FROM requirements WHERE id=?", (req_id,)).fetchone()
    if row is None:
        conn.close()
        output({"success": False, "error": f"requirement not found: {req_id}"})
        return

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    created_task_ids = []

    for task in tasks:
        task_id = task["id"]
        task_type = task.get("type", "coding")
        # 解析 xxx, yyy
        parts = task_id.split("-")
        # reqZZZ-XXX-YYY-type  → parts: [reqZZZ, XXX, YYY, type...]
        xxx = parts[1] if len(parts) > 1 else "001"
        yyy = parts[2] if len(parts) > 2 else "001"
        filename = f"{task_id}.md"

        # 写 source 文件（YAML frontmatter + 正文）
        depends_on = json.dumps(task.get("depends_on", []))
        content = f"""---
task_id: {task_id}
task_type: {task_type}
req_file: {req_name}
xxx: "{xxx}"
yyy: "{yyy}"
depends_on: {depends_on}
---

## 背景

{task.get('summary', '')}

## 任务要求

{task.get('details', '')}

## 约束条件

{task.get('constraints', '自动识别')}

## 验收条件

{task.get('acceptance', '')}

## 参考信息

{task.get('notes', '')}

## 报告要求

完成后将执行结果报告写入：
`occd/task/report-{task_id}-{{YYYYMMDDTHHMMSS}}.md`

格式见 `references/task-report-template.md`。
"""
        (source_dir / filename).write_text(content, encoding="utf-8")

        # DB 写入
        existing = conn.execute("SELECT id FROM sources WHERE id=?", (task_id,)).fetchone()
        if existing is None:
            branch = task_id if task_type in ("coding", "test-write") else None
            conn.execute(
                "INSERT INTO sources (id, req_id, filename, task_type, xxx, yyy, status, branch, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,'pending',?,?,?)",
                (task_id, req_id, filename, task_type, xxx, yyy, branch, now_ms(), now_ms())
            )
            add_event(conn, "source", task_id, None, "pending")
        created_task_ids.append(task_id)

    old_status = row["status"]
    conn.execute(
        "UPDATE requirements SET status='decomposed', updated_at=? WHERE id=?",
        (now_ms(), req_id)
    )
    add_event(conn, "req", req_id, old_status, "decomposed",
              note=f"decomposed into {len(tasks)} tasks")
    conn.commit()
    conn.close()
    write_log(repo, "INFO", f"已生成 {len(tasks)} 个任务文档: {req_name}")
    output({"success": True, "tasks": created_task_ids})


# ─── 日志 ─────────────────────────────────────────────────────────────────────

def cmd_log(args):
    write_log(_resolve_repo(args.repo), args.level.upper(), args.message)
    output({"success": True})


# ─── 测试框架检测 ─────────────────────────────────────────────────────────────

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
        pkg = json.loads((repo / "package.json").read_text())
        scripts = pkg.get("scripts", {})
        if "test" in scripts:
            ts = scripts["test"].lower()
            if "vitest" in ts: return ["npx", "vitest", "run"]
            if "jest" in ts:   return ["npx", "jest"]
            if "mocha" in ts:  return ["npx", "mocha"]
        return ["npm", "test"]
    return ["echo", "No test framework detected"]


# ─── CLI 入口 ─────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(prog="occd_utils", description="OCCD 工具脚本")
    sub = p.add_subparsers(dest="cmd")

    def sp(name, help_text):
        return sub.add_parser(name, help=help_text)

    # db-init
    c = sp("db-init", "初始化仓库 occd.db 并写入 .gitattributes")
    c.add_argument("--repo", required=True)

    # scan-repos
    c = sp("scan-repos", "扫描 work_dir 下所有 github-* 仓库，返回需要处理的需求列表")
    c.add_argument("--work-dir", required=True)

    # git-pull
    c = sp("git-pull", "git pull")
    c.add_argument("--repo", required=True)

    # check-new-commit
    c = sp("check-new-commit", "检查文件自 last-commit 以来是否有新 commit")
    c.add_argument("--repo", required=True)
    c.add_argument("--file", required=True)
    c.add_argument("--last-commit", default="")

    # create-worktree
    c = sp("create-worktree", "创建 git worktree")
    c.add_argument("--repo", required=True)
    c.add_argument("--branch", required=True)

    # remove-worktree
    c = sp("remove-worktree", "清理 git worktree")
    c.add_argument("--repo", required=True)
    c.add_argument("--branch", required=True)

    # merge-branches
    c = sp("merge-branches", "合并同一 XXX 下所有 done 分支")
    c.add_argument("--repo", required=True)
    c.add_argument("--xxx", required=True)

    # run-tests
    c = sp("run-tests", "自动识别测试框架并执行")
    c.add_argument("--repo", required=True)

    # commit-push
    c = sp("commit-push", "提交并推送")
    c.add_argument("--repo", required=True)
    c.add_argument("--message", required=True)
    c.add_argument("--include-db", action="store_true", help="同时提交 occd.db")

    # db-upsert-req
    c = sp("db-upsert-req", "新增或更新需求记录")
    c.add_argument("--repo", required=True)
    c.add_argument("--filename", required=True)

    # db-update-req-status
    c = sp("db-update-req-status", "更新需求状态")
    c.add_argument("--repo", required=True)
    c.add_argument("--req-id", required=True)
    c.add_argument("--status", required=True)
    c.add_argument("--note", default=None)

    # db-get-req
    c = sp("db-get-req", "获取需求详情（含关联 reviews/sources）")
    c.add_argument("--repo", required=True)
    c.add_argument("--req-id", required=True)

    # db-list-pending-reqs
    c = sp("db-list-pending-reqs", "列出仓库中所有未完成需求")
    c.add_argument("--repo", required=True)

    # db-upsert-source
    c = sp("db-upsert-source", "新增子任务记录（write-tasks 内部已自动调用）")
    c.add_argument("--repo", required=True)
    c.add_argument("--req-id", required=True)
    c.add_argument("--task-id", required=True)
    c.add_argument("--task-type", required=True,
                   choices=["coding", "test-write", "test-run"])
    c.add_argument("--filename", required=True)

    # db-update-source-status
    c = sp("db-update-source-status", "更新子任务状态（主代理和子代理均可调用）")
    c.add_argument("--repo", required=True)
    c.add_argument("--task", required=True, help="task_id，如 req001-001-001-coding")
    c.add_argument("--status", required=True,
                   choices=["pending", "spawned", "running", "done", "failed"])
    c.add_argument("--session-key", default=None)
    c.add_argument("--note", default=None)

    # db-list-sources-by-xxx
    c = sp("db-list-sources-by-xxx", "列出某串行批次（XXX）下所有子任务及状态")
    c.add_argument("--repo", required=True)
    c.add_argument("--xxx", required=True)

    # db-add-execution
    c = sp("db-add-execution", "登记子代理执行记录")
    c.add_argument("--repo", required=True)
    c.add_argument("--task", required=True)
    c.add_argument("--report-file", required=True)
    c.add_argument("--outcome", required=True, choices=["success", "failure", "partial"])
    c.add_argument("--summary", default=None)
    c.add_argument("--session-key", default=None)
    c.add_argument("--started-at", type=int, default=None, help="Unix ms")

    # db-list-executions
    c = sp("db-list-executions", "列出某子任务的所有执行历史")
    c.add_argument("--repo", required=True)
    c.add_argument("--task", required=True)

    # db-summary
    c = sp("db-summary", "跨仓库全局状态汇总")
    c.add_argument("--work-dir", required=True)

    # write-review
    c = sp("write-review", "生成需求澄清文件，更新 DB 状态为 reviewing")
    c.add_argument("--repo", required=True)
    c.add_argument("--req", required=True)
    c.add_argument("--questions", required=True, help='JSON array of strings，如 \'["问题1","问题2"]\'')

    # write-tasks
    c = sp("write-tasks", "将子任务写入 occd/source/ 并建立 DB 记录")
    c.add_argument("--repo", required=True)
    c.add_argument("--req", required=True)
    c.add_argument("--tasks", required=True, help="JSON array of task objects")

    # log
    c = sp("log", "写原始技术日志")
    c.add_argument("--repo", required=True)
    c.add_argument("--level", default="INFO")
    c.add_argument("--message", required=True)

    # ── dispatch ──
    args = p.parse_args()
    # 将带连字符的 dest 属性名统一（argparse 自动转为下划线）
    cmds = {
        "db-init":                  cmd_db_init,
        "scan-repos":               cmd_scan_repos,
        "git-pull":                 cmd_git_pull,
        "check-new-commit":         cmd_check_new_commit,
        "create-worktree":          cmd_create_worktree,
        "remove-worktree":          cmd_remove_worktree,
        "merge-branches":           cmd_merge_branches,
        "run-tests":                cmd_run_tests,
        "commit-push":              cmd_commit_push,
        "db-upsert-req":            cmd_db_upsert_req,
        "db-update-req-status":     cmd_db_update_req_status,
        "db-get-req":               cmd_db_get_req,
        "db-list-pending-reqs":     cmd_db_list_pending_reqs,
        "db-upsert-source":         cmd_db_upsert_source,
        "db-update-source-status":  cmd_db_update_source_status,
        "db-list-sources-by-xxx":   cmd_db_list_sources_by_xxx,
        "db-add-execution":         cmd_db_add_execution,
        "db-list-executions":       cmd_db_list_executions,
        "db-summary":               cmd_db_summary,
        "write-review":             cmd_write_review,
        "write-tasks":              cmd_write_tasks,
        "log":                      cmd_log,
    }
    if args.cmd in cmds:
        cmds[args.cmd](args)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
