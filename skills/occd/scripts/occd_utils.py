#!/usr/bin/env python3
"""
occd_utils.py - OCCD 工具脚本 v3

供 OpenClaw 主代理和子代理调用，封装所有 git / 文件 / SQLite DB / 配置操作。
所有命令输出 JSON，供调用方解析。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REQ_STATUS = {"new", "reviewing", "decomposed", "done", "failed"}
SOURCE_STATUS = {"pending", "spawned", "running", "done", "failed"}
OUTCOMES = {"success", "failure", "partial"}
TASK_TYPES = {"coding", "test-write", "test-run"}
TASK_ID_RE = re.compile(r"^req(\d+)-(\d+)-(\d+)-(coding|test-write|test-run)$")
DEFAULT_CONFIG_PATH = Path.home() / ".openclaw" / "occd-config.json"
LOCK_DIRNAME = ".locks"

DB_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS requirements (
    id              TEXT PRIMARY KEY,
    filename        TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'new',
    review_rounds   INTEGER NOT NULL DEFAULT 0,
    last_req_commit TEXT,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS reviews (
    id          TEXT PRIMARY KEY,
    req_id      TEXT NOT NULL REFERENCES requirements(id),
    filename    TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    resolved_at INTEGER
);

CREATE TABLE IF NOT EXISTS sources (
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

CREATE TABLE IF NOT EXISTS executions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   TEXT NOT NULL REFERENCES sources(id),
    report_file TEXT NOT NULL,
    session_key TEXT,
    outcome     TEXT NOT NULL,
    summary     TEXT,
    started_at  INTEGER NOT NULL,
    finished_at INTEGER
);

CREATE TABLE IF NOT EXISTS task_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    agent       TEXT NOT NULL DEFAULT 'main',
    note        TEXT,
    created_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_requirements_status   ON requirements(status);
CREATE INDEX IF NOT EXISTS idx_sources_req_id        ON sources(req_id);
CREATE INDEX IF NOT EXISTS idx_sources_status        ON sources(status);
CREATE INDEX IF NOT EXISTS idx_sources_xxx           ON sources(xxx);
CREATE INDEX IF NOT EXISTS idx_executions_source_id  ON executions(source_id);
CREATE INDEX IF NOT EXISTS idx_task_events_entity    ON task_events(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_task_events_time      ON task_events(created_at);
"""


def ts_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def iso_to_ts_ms(value: str | None) -> int | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    return int(datetime.fromisoformat(normalized).timestamp() * 1000)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def output(data: Any):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def fail(message: str, *, code: int = 1, extra: dict[str, Any] | None = None):
    payload = {"success": False, "error": message}
    if extra:
        payload.update(extra)
    output(payload)
    sys.exit(code)


def run_git(args: list[str], cwd: Path, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, check=check)


def ensure_status(value: str, allowed: set[str], field: str):
    if value not in allowed:
        fail(f"invalid {field}: {value}", extra={"allowed": sorted(allowed)})


def parse_task_id(task_id: str) -> dict[str, str]:
    match = TASK_ID_RE.match(task_id)
    if not match:
        fail(
            f"invalid task id: {task_id}",
            extra={"expected": "reqZZZ-XXX-YYY-coding|test-write|test-run"},
        )
    req_seq, xxx, yyy, task_type = match.groups()
    return {"req_seq": req_seq, "xxx": xxx, "yyy": yyy, "task_type": task_type}


def repo_lock_path(repo: Path) -> Path:
    return repo / "occd" / LOCK_DIRNAME / "main.lock"


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


def write_json(path: Path, data: dict[str, Any]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        fail(f"config not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def shell_quote(value: str) -> str:
    if value == "":
        return "''"
    if all(ch.isalnum() or ch in "._/-:" for ch in value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def build_opencode_command_from_config(config: dict[str, Any], prompt: str, extra_args: list[str] | None = None) -> dict[str, Any]:
    opencode_path = config.get("opencode_path") or "opencode"
    raw_args = config.get("opencode_args") or "run"
    argv = [opencode_path] + [part for part in str(raw_args).split() if part]
    if extra_args:
        argv.extend(extra_args)
    argv.append(prompt)
    return {
        "opencode_path": opencode_path,
        "opencode_args": raw_args,
        "argv": argv,
        "command": " ".join(shell_quote(x) for x in argv),
    }


def get_repo_name(repo: Path) -> str:
    return repo.name


def make_req_id(repo: Path, filename: str) -> str:
    return f"{get_repo_name(repo)}:{filename}"


def write_log(repo: Path, level: str, message: str):
    log_dir = repo / "occd" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(log_dir / f"{today}.log", "a", encoding="utf-8") as f:
        f.write(f"[{ts}] [{level.upper()}] {message}\n")


def log_event(conn: sqlite3.Connection, entity_type: str, entity_id: str,
              from_status: str | None, to_status: str, *, agent: str = "main", note: str | None = None):
    conn.execute(
        "INSERT INTO task_events(entity_type,entity_id,from_status,to_status,agent,note,created_at) VALUES(?,?,?,?,?,?,?)",
        (entity_type, entity_id, from_status, to_status, agent, note, ts_ms()),
    )


def ensure_repo(repo: Path):
    if not repo.exists() or not repo.is_dir():
        fail(f"repo not found: {repo}")
    if not (repo / ".git").exists():
        fail(f"not a git repository: {repo}")


def ensure_clean_worktree_base(repo: Path, base_branch: str):
    current = run_git(["branch", "--show-current"], cwd=repo)
    current_branch = current.stdout.strip()
    if current_branch != base_branch:
        checkout = run_git(["checkout", base_branch], cwd=repo)
        if checkout.returncode != 0:
            fail("failed to checkout base branch", extra={"stderr": checkout.stderr, "base_branch": base_branch})
    pull = run_git(["pull", "--ff-only"], cwd=repo)
    if pull.returncode != 0:
        fail("failed to update base branch", extra={"stderr": pull.stderr, "base_branch": base_branch})


def get_default_branch(repo: Path) -> str:
    ref = run_git(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd=repo)
    if ref.returncode == 0:
        value = ref.stdout.strip()
        if value.startswith("refs/remotes/origin/"):
            return value.rsplit("/", 1)[-1]
    for candidate in ("main", "master"):
        probe = run_git(["rev-parse", "--verify", candidate], cwd=repo)
        if probe.returncode == 0:
            return candidate
    return "main"


def resolve_paths_for_commit(repo: Path, paths: list[str] | None, include_db: bool) -> list[str]:
    resolved = []
    if paths:
        resolved.extend(paths)
    if include_db:
        resolved.append(str(db_path(repo).relative_to(repo)))
    return sorted(dict.fromkeys(resolved))


def cmd_config_init(args):
    path = Path(args.config).expanduser()
    config = {
        "work_dir": str(Path(args.work_dir).expanduser()),
        "poll_interval": args.poll_interval,
        "max_agents": args.max_agents,
        "max_fix_retries": args.max_fix_retries,
        "base_branch": args.base_branch,
        "auto_push": args.auto_push,
        "opencode_path": args.opencode_path,
        "opencode_args": args.opencode_args,
        "updated_at": utcnow_iso(),
    }
    write_json(path, config)
    output({"success": True, "config": str(path), "data": config})


def cmd_config_show(args):
    path = Path(args.config).expanduser()
    data = read_config(path)
    output({"success": True, "config": str(path), "data": data})


def cmd_config_set(args):
    path = Path(args.config).expanduser()
    data = read_config(path)
    if args.work_dir:
        data["work_dir"] = str(Path(args.work_dir).expanduser())
    if args.poll_interval is not None:
        data["poll_interval"] = args.poll_interval
    if args.max_agents is not None:
        data["max_agents"] = args.max_agents
    if args.max_fix_retries is not None:
        data["max_fix_retries"] = args.max_fix_retries
    if args.base_branch:
        data["base_branch"] = args.base_branch
    if args.auto_push is not None:
        data["auto_push"] = args.auto_push
    if args.opencode_path:
        data["opencode_path"] = args.opencode_path
    if args.opencode_args is not None:
        data["opencode_args"] = args.opencode_args
    data["updated_at"] = utcnow_iso()
    write_json(path, data)
    output({"success": True, "config": str(path), "data": data})


def cmd_db_init(args):
    repo = Path(args.repo)
    ensure_repo(repo)
    for name in ("req", "task", "report", "review", "logs"):
        (repo / "occd" / name).mkdir(parents=True, exist_ok=True)
    init_db(repo)
    ga = repo / ".gitattributes"
    line = "occd/occd.db binary\n"
    existing = ga.read_text(encoding="utf-8") if ga.exists() else ""
    if "occd/occd.db" not in existing:
        with open(ga, "a", encoding="utf-8") as f:
            f.write(line)
    write_log(repo, "INFO", "occd.db 初始化完成")
    output({"success": True, "db": str(db_path(repo))})


def cmd_acquire_lock(args):
    repo = Path(args.repo)
    lock = repo_lock_path(repo)
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.exists() and not args.force:
        data = json.loads(lock.read_text(encoding="utf-8"))
        fail("repo lock already exists", extra={"lock": str(lock), "holder": data})
    payload = {
        "holder": args.holder,
        "created_at": utcnow_iso(),
        "pid": os.getpid(),
    }
    write_json(lock, payload)
    output({"success": True, "lock": str(lock), "holder": payload})


def cmd_release_lock(args):
    repo = Path(args.repo)
    lock = repo_lock_path(repo)
    existed = lock.exists()
    if existed:
        lock.unlink()
    output({"success": True, "lock": str(lock), "removed": existed})


def cmd_scan_repos(args):
    work_dir = Path(args.work_dir)
    if not work_dir.exists():
        fail(f"work dir not found: {work_dir}")
    repos = sorted(d for d in work_dir.iterdir() if d.is_dir() and d.name.startswith("github-"))
    result = []
    for repo in repos:
        req_dir = repo / "occd" / "req"
        if not req_dir.exists():
            continue
        if not db_path(repo).exists():
            init_db(repo)
        conn = get_conn(repo)
        for f in sorted(req_dir.iterdir()):
            if f.suffix not in (".md", ".txt"):
                continue
            req_id = make_req_id(repo, f.name)
            content_hash = file_sha256(f)
            row = conn.execute("SELECT status, content_hash FROM requirements WHERE id=?", (req_id,)).fetchone()
            action = None
            if row:
                if row["status"] not in ("new", "failed") and row["content_hash"] == content_hash:
                    continue
                action = "hash_changed" if row["content_hash"] != content_hash else "retry"
                if row["content_hash"] != content_hash:
                    conn.execute(
                        "UPDATE requirements SET content_hash=?,status='new',updated_at=? WHERE id=?",
                        (content_hash, ts_ms(), req_id),
                    )
                    log_event(conn, "req", req_id, row["status"], "new", note="content_hash changed")
                    conn.commit()
            else:
                now = ts_ms()
                conn.execute(
                    "INSERT INTO requirements(id,filename,content_hash,status,review_rounds,created_at,updated_at) VALUES(?,?,?,'new',0,?,?)",
                    (req_id, f.name, content_hash, now, now),
                )
                log_event(conn, "req", req_id, None, "new")
                conn.commit()
                action = "new"
            row2 = conn.execute("SELECT status FROM requirements WHERE id=?", (req_id,)).fetchone()
            result.append({
                "repo": str(repo),
                "repo_name": repo.name,
                "req": f.name,
                "req_id": req_id,
                "status": row2["status"],
                "action": action,
            })
        conn.close()
    output({"success": True, "repos": result})


def cmd_git_pull(args):
    repo = Path(args.repo)
    ensure_repo(repo)
    git_args = ["pull"]
    if args.ff_only:
        git_args.append("--ff-only")
    r = run_git(git_args, cwd=repo)
    write_log(repo, "INFO", f"git pull: {(r.stdout + r.stderr).strip()}")
    output({"success": r.returncode == 0, "output": r.stdout + r.stderr})


def cmd_check_new_commit(args):
    repo = Path(args.repo)
    ensure_repo(repo)
    r = run_git(["log", "-1", "--format=%H", "--", args.file], cwd=repo)
    current = r.stdout.strip()
    output({"success": True, "has_new_commit": bool(current) and current != args.last_commit, "current_commit": current})


def cmd_db_upsert_req(args):
    repo = Path(args.repo)
    req_file = repo / "occd" / "req" / args.filename
    if not req_file.exists():
        fail(f"requirement file not found: {req_file}")
    req_id = make_req_id(repo, args.filename)
    content_hash = file_sha256(req_file)
    conn = get_conn(repo)
    row = conn.execute("SELECT status, content_hash FROM requirements WHERE id=?", (req_id,)).fetchone()
    now = ts_ms()
    if row is None:
        conn.execute(
            "INSERT INTO requirements(id,filename,content_hash,status,review_rounds,created_at,updated_at) VALUES(?,?,?,'new',0,?,?)",
            (req_id, args.filename, content_hash, now, now),
        )
        log_event(conn, "req", req_id, None, "new")
        action = "inserted"
    elif row["content_hash"] != content_hash:
        conn.execute(
            "UPDATE requirements SET content_hash=?,status='new',updated_at=? WHERE id=?",
            (content_hash, now, req_id),
        )
        log_event(conn, "req", req_id, row["status"], "new", note="content_hash changed")
        action = "updated"
    else:
        action = "skipped"
    conn.commit()
    conn.close()
    output({"success": True, "req_id": req_id, "action": action})


def cmd_db_update_req_status(args):
    repo = Path(args.repo)
    ensure_status(args.status, REQ_STATUS, "requirement status")
    conn = get_conn(repo)
    row = conn.execute("SELECT status FROM requirements WHERE id=?", (args.req_id,)).fetchone()
    if not row:
        fail(f"requirement not found: {args.req_id}")
    conn.execute("UPDATE requirements SET status=?,updated_at=? WHERE id=?", (args.status, ts_ms(), args.req_id))
    log_event(conn, "req", args.req_id, row["status"], args.status, note=args.note)
    conn.commit()
    conn.close()
    write_log(repo, "INFO", f"需求状态更新: {args.req_id} → {args.status}")
    output({"success": True, "req_id": args.req_id, "status": args.status})


def cmd_db_get_req(args):
    repo = Path(args.repo)
    conn = get_conn(repo)
    row = conn.execute("SELECT * FROM requirements WHERE id=?", (args.req_id,)).fetchone()
    if not row:
        fail(f"requirement not found: {args.req_id}")
    reviews = [dict(r) for r in conn.execute("SELECT * FROM reviews WHERE req_id=? ORDER BY created_at", (args.req_id,))]
    sources = [dict(s) for s in conn.execute("SELECT * FROM sources WHERE req_id=? ORDER BY xxx, yyy", (args.req_id,))]
    conn.close()
    output({"success": True, "req": dict(row), "reviews": reviews, "sources": sources})


def cmd_db_list_pending_reqs(args):
    repo = Path(args.repo)
    conn = get_conn(repo)
    rows = conn.execute("SELECT * FROM requirements WHERE status IN ('new','reviewing','decomposed') ORDER BY created_at").fetchall()
    conn.close()
    output({"success": True, "requirements": [dict(r) for r in rows]})


def cmd_write_review(args):
    repo = Path(args.repo)
    req_name = args.req
    questions = json.loads(args.questions)
    if not isinstance(questions, list) or not questions:
        fail("questions must be a non-empty JSON array")
    req_id = make_req_id(repo, req_name)
    conn = get_conn(repo)
    row = conn.execute("SELECT status, review_rounds FROM requirements WHERE id=?", (req_id,)).fetchone()
    if not row:
        fail(f"requirement not found: {req_id}")
    round_num = (row["review_rounds"] or 0) + 1
    r = run_git(["log", "-1", "--format=%H", "--", f"occd/req/{req_name}"], cwd=repo)
    last_commit = r.stdout.strip()
    review_dir = repo / "occd" / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    safe_req_id = req_id.replace(":", "-")
    filename = f"{safe_req_id}-{round_num:03d}.md"
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    qs = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(questions))
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
git commit -m \"clarify: {req_name} - 补充说明{round_num:03d}\"
git push
```

主代理将在下次轮询时自动检测到更新并重新分析。
"""
    (review_dir / filename).write_text(content + "\n", encoding="utf-8")
    now = ts_ms()
    conn.execute(
        "UPDATE requirements SET status='reviewing', review_rounds=?, last_req_commit=?, updated_at=? WHERE id=?",
        (round_num, last_commit, now, req_id),
    )
    review_id = f"review-{safe_req_id}-{round_num:03d}"
    conn.execute("INSERT INTO reviews(id,req_id,filename,created_at) VALUES(?,?,?,?)", (review_id, req_id, filename, now))
    log_event(conn, "req", req_id, row["status"], "reviewing", note=f"round {round_num}")
    conn.commit()
    conn.close()
    write_log(repo, "INFO", f"已生成 review 文件: {filename}")
    output({"success": True, "file": str(review_dir / filename), "round": round_num, "review_id": review_id})


def _validate_task_payload(task: dict[str, Any]):
    task_id = task.get("id")
    if not task_id:
        fail("task.id is required")
    parsed = parse_task_id(task_id)
    task_type = task.get("type", parsed["task_type"])
    ensure_status(task_type, TASK_TYPES, "task_type")
    depends_on = task.get("depends_on", [])
    if not isinstance(depends_on, list):
        fail("task.depends_on must be a JSON array", extra={"task_id": task_id})
    for dep in depends_on:
        parse_task_id(dep)
    return parsed, task_type, depends_on


def cmd_write_tasks(args):
    repo = Path(args.repo)
    req_name = args.req
    tasks = json.loads(args.tasks)
    if not isinstance(tasks, list) or not tasks:
        fail("tasks must be a non-empty JSON array")
    req_id = make_req_id(repo, req_name)
    task_dir = repo / "occd" / "task"
    task_dir.mkdir(parents=True, exist_ok=True)
    conn = get_conn(repo)
    row = conn.execute("SELECT status FROM requirements WHERE id=?", (req_id,)).fetchone()
    if not row:
        fail(f"requirement not found: {req_id}")
    old_status = row["status"]
    now = ts_ms()
    created_ids = []
    for task in tasks:
        parsed, task_type, depends_on = _validate_task_payload(task)
        task_id = task["id"]
        xxx, yyy = parsed["xxx"], parsed["yyy"]
        filename = f"{task_id}.md"
        branch = task_id if task_type in {"coding", "test-write"} else None
        content = f"""---
task_id: {task_id}
task_type: {task_type}
req_file: {req_name}
xxx: \"{xxx}\"
yyy: \"{yyy}\"
depends_on: {json.dumps(depends_on, ensure_ascii=False)}
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
`occd/report/report-{task_id}-{{YYYYMMDDTHHMMSS}}.md`

格式见 `references/task-report-template.md`。
"""
        (task_dir / filename).write_text(content + "\n", encoding="utf-8")
        conn.execute(
            "INSERT OR REPLACE INTO sources(id,req_id,filename,task_type,xxx,yyy,status,branch,retry_count,created_at,updated_at) VALUES(?,?,?,?,?,?,'pending',?,0,?,?)",
            (task_id, req_id, filename, task_type, xxx, yyy, branch, now, now),
        )
        log_event(conn, "source", task_id, None, "pending")
        created_ids.append(task_id)
    conn.execute("UPDATE requirements SET status='decomposed',updated_at=? WHERE id=?", (now, req_id))
    log_event(conn, "req", req_id, old_status, "decomposed", note=f"{len(tasks)} tasks")
    conn.commit()
    conn.close()
    write_log(repo, "INFO", f"已生成 {len(tasks)} 个子任务: {req_name}")
    output({"success": True, "tasks": created_ids})


def cmd_db_update_source_status(args):
    repo = Path(args.repo)
    parse_task_id(args.task)
    ensure_status(args.status, SOURCE_STATUS, "source status")
    conn = get_conn(repo)
    row = conn.execute("SELECT status, retry_count FROM sources WHERE id=?", (args.task,)).fetchone()
    if not row:
        fail(f"source not found: {args.task}")
    old_status = row["status"]
    updates = ["status=?", "updated_at=?"]
    values: list[Any] = [args.status, ts_ms()]
    if args.session_key:
        updates.append("session_key=?")
        values.append(args.session_key)
    if args.status == "spawned" and old_status == "failed":
        updates.append("retry_count=retry_count+1")
    values.append(args.task)
    conn.execute(f"UPDATE sources SET {', '.join(updates)} WHERE id=?", values)
    agent = "main"
    if args.session_key and args.status in {"running", "done", "failed"}:
        agent = f"sub:{args.session_key}"
    log_event(conn, "source", args.task, old_status, args.status, agent=agent, note=args.note)
    conn.commit()
    conn.close()
    write_log(repo, "INFO", f"子任务状态更新: {args.task} → {args.status}")
    output({"success": True, "task_id": args.task, "status": args.status})


def cmd_db_list_sourcese_by_xxx_common(repo: Path, xxx: str) -> list[dict[str, Any]]:
    conn = get_conn(repo)
    rows = conn.execute("SELECT * FROM sources WHERE xxx=? ORDER BY yyy", (xxx,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def cmd_db_list_sources_by_xxx(args):
    output({"success": True, "sources": cmd_db_list_sourcese_by_xxx_common(Path(args.repo), args.xxx)})


def cmd_db_add_execution(args):
    repo = Path(args.repo)
    parse_task_id(args.task)
    ensure_status(args.outcome, OUTCOMES, "outcome")
    started_at = iso_to_ts_ms(args.started_at) or ts_ms()
    finished_at = iso_to_ts_ms(args.finished_at) or ts_ms()
    conn = get_conn(repo)
    source_exists = conn.execute("SELECT 1 FROM sources WHERE id=?", (args.task,)).fetchone()
    if not source_exists:
        fail(f"source not found: {args.task}")
    conn.execute(
        "INSERT INTO executions(source_id,report_file,session_key,outcome,summary,started_at,finished_at) VALUES(?,?,?,?,?,?,?)",
        (args.task, args.report_file, args.session_key, args.outcome, args.summary, started_at, finished_at),
    )
    conn.commit()
    conn.close()
    output({"success": True})


def cmd_db_list_executions(args):
    repo = Path(args.repo)
    conn = get_conn(repo)
    rows = conn.execute("SELECT * FROM executions WHERE source_id=? ORDER BY started_at", (args.task,)).fetchall()
    conn.close()
    output({"success": True, "executions": [dict(r) for r in rows]})


def cmd_db_summary(args):
    work_dir = Path(args.work_dir)
    repos = sorted(d for d in work_dir.iterdir() if d.is_dir() and d.name.startswith("github-"))
    result = {}
    for repo in repos:
        if not db_path(repo).exists():
            continue
        conn = get_conn(repo)
        req_rows = conn.execute("SELECT status, COUNT(*) as cnt FROM requirements GROUP BY status").fetchall()
        src_rows = conn.execute("SELECT status, task_type, COUNT(*) as cnt FROM sources GROUP BY status, task_type").fetchall()
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
        result[repo.name] = {"requirements": req_stats, "sources": src_stats, "executions": exc_stats}
    output({"success": True, "repos": result})


def cmd_create_worktree(args):
    repo = Path(args.repo)
    ensure_repo(repo)
    branch = args.branch
    parse_task_id(branch)
    worktree_path = repo.parent / ".occd-worktrees" / repo.name / branch
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    branch_exists = run_git(["rev-parse", "--verify", branch], cwd=repo).returncode == 0
    path_exists = worktree_path.exists()
    if path_exists and args.reuse_if_exists:
        output({"success": True, "worktree_path": str(worktree_path), "reused": True})
        return
    if path_exists and args.reset:
        run_git(["worktree", "remove", "--force", str(worktree_path)], cwd=repo)
    if branch_exists and args.reset:
        run_git(["branch", "-D", branch], cwd=repo)
        branch_exists = False
    git_args = ["worktree", "add"]
    if not branch_exists:
        git_args += ["-b", branch]
    git_args.append(str(worktree_path))
    if branch_exists:
        git_args.append(branch)
    r = run_git(git_args, cwd=repo)
    success = r.returncode == 0
    write_log(repo, "INFO" if success else "ERROR", f"worktree {'创建成功' if success else '创建失败'}: {branch} → {worktree_path}")
    output({"success": success, "worktree_path": str(worktree_path), "reused": False, "error": r.stderr})


def cmd_remove_worktree(args):
    repo = Path(args.repo)
    branch = args.branch
    parse_task_id(branch)
    worktree_path = repo.parent / ".occd-worktrees" / repo.name / branch
    removed_worktree = run_git(["worktree", "remove", "--force", str(worktree_path)], cwd=repo) if worktree_path.exists() else None
    branch_deleted = False
    if args.delete_branch:
        merged_check = run_git(["branch", "--merged", args.base_branch], cwd=repo)
        merged_branches = merged_check.stdout.splitlines()
        if any(line.replace("*", "").strip() == branch for line in merged_branches) or args.force_branch_delete:
            run_git(["branch", "-D", branch], cwd=repo)
            branch_deleted = True
    run_git(["worktree", "prune"], cwd=repo)
    write_log(repo, "INFO", f"worktree 已清理: {branch}")
    output({"success": True, "worktree_removed": removed_worktree is not None, "branch_deleted": branch_deleted})


def cmd_merge_branches(args):
    repo = Path(args.repo)
    ensure_repo(repo)
    base_branch = args.base_branch or get_default_branch(repo)
    ensure_clean_worktree_base(repo, base_branch)
    conn = get_conn(repo)
    rows = conn.execute(
        "SELECT id, branch FROM sources WHERE xxx=? AND task_type IN ('coding','test-write') AND status='done' ORDER BY created_at",
        (args.xxx,),
    ).fetchall()
    conn.close()
    branches = [r["branch"] for r in rows if r["branch"]]
    def commit_time(branch: str) -> str:
        r = run_git(["log", "-1", "--format=%aI", branch], cwd=repo)
        return r.stdout.strip() or "9999"
    branches.sort(key=commit_time)
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
    output({"success": True, "base_branch": base_branch, "conflicts": conflicts, "merged": merged})


def _detect_test_command(repo: Path) -> tuple[list[str] | None, str | None]:
    detectors = [
        ("pytest.ini", ["pytest"], "pytest.ini"),
        ("pyproject.toml", ["pytest"], "pyproject.toml"),
        ("setup.py", ["pytest"], "setup.py"),
        ("go.mod", ["go", "test", "./..."], "go.mod"),
        ("Cargo.toml", ["cargo", "test"], "Cargo.toml"),
        ("Makefile", ["make", "test"], "Makefile"),
    ]
    for fname, cmd, detected_by in detectors:
        if (repo / fname).exists():
            return cmd, detected_by
    package_json = repo / "package.json"
    if package_json.exists():
        pkg = json.loads(package_json.read_text(encoding="utf-8"))
        scripts = pkg.get("scripts", {})
        if "test" in scripts:
            text = scripts["test"].lower()
            if "vitest" in text:
                return ["npx", "vitest", "run"], "package.json:scripts.test"
            if "jest" in text:
                return ["npx", "jest"], "package.json:scripts.test"
            if "mocha" in text:
                return ["npx", "mocha"], "package.json:scripts.test"
            return ["npm", "test"], "package.json:scripts.test"
    return None, None


def cmd_run_tests(args):
    repo = Path(args.repo)
    ensure_repo(repo)
    cmd, detected_by = _detect_test_command(repo)
    if not cmd:
        write_log(repo, "WARN", "未识别到测试框架")
        output({
            "success": True,
            "passed": False,
            "detected": False,
            "reason": "no_test_framework_detected",
            "command": None,
            "output": "No test framework detected",
        })
        return
    write_log(repo, "INFO", f"执行测试: {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
    passed = r.returncode == 0
    write_log(repo, "INFO" if passed else "WARN", f"测试{'通过' if passed else '失败'}: returncode={r.returncode}")
    output({
        "success": True,
        "passed": passed,
        "detected": True,
        "detected_by": detected_by,
        "command": cmd,
        "returncode": r.returncode,
        "output": r.stdout + r.stderr,
    })


def cmd_commit_push(args):
    repo = Path(args.repo)
    ensure_repo(repo)
    base_branch = args.base_branch or get_default_branch(repo)
    ensure_clean_worktree_base(repo, base_branch)
    paths = resolve_paths_for_commit(repo, args.path, args.include_db)
    if not paths and not args.all:
        fail("no paths selected for commit; pass --path, --include-db, or --all")
    if args.all:
        add = run_git(["add", "-A"], cwd=repo)
    else:
        add = run_git(["add", "--"] + paths, cwd=repo)
    if add.returncode != 0:
        fail("git add failed", extra={"stderr": add.stderr})
    diff = run_git(["diff", "--cached", "--name-only"], cwd=repo)
    changed = [line for line in diff.stdout.splitlines() if line.strip()]
    if not changed:
        output({"success": True, "committed": False, "pushed": False, "paths": paths, "message": "nothing staged"})
        return
    commit = run_git(["commit", "-m", args.message], cwd=repo)
    if commit.returncode != 0:
        fail("git commit failed", extra={"stderr": commit.stderr, "stdout": commit.stdout})
    pushed = False
    push_output = ""
    if args.push:
        push = run_git(["push"], cwd=repo)
        push_output = push.stdout + push.stderr
        if push.returncode != 0:
            fail("git push failed", extra={"stderr": push.stderr, "stdout": push.stdout})
        pushed = True
    write_log(repo, "INFO", f"commit {'+ push ' if pushed else ''}成功: {args.message}")
    output({"success": True, "committed": True, "pushed": pushed, "paths": changed, "push_output": push_output})


def cmd_build_opencode_command(args):
    config = read_config(Path(args.config).expanduser())
    extra_args = args.extra_arg or []
    result = build_opencode_command_from_config(config, args.prompt, extra_args)
    output({"success": True, **result})


def cmd_latest_report(args):
    repo = Path(args.repo)
    conn = get_conn(repo)
    row = conn.execute(
        "SELECT report_file, session_key, outcome, summary, started_at, finished_at FROM executions WHERE source_id=? ORDER BY finished_at DESC, id DESC LIMIT 1",
        (args.task,),
    ).fetchone()
    conn.close()
    if not row:
        output({"success": True, "found": False, "task": args.task})
        return
    output({"success": True, "found": True, "task": args.task, **dict(row)})


def cmd_log(args):
    write_log(Path(args.repo), args.level.upper(), args.message)
    output({"success": True})


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="occd_utils", description="OCCD 工具脚本 v3")
    sub = p.add_subparsers(dest="cmd")

    def sp(name: str, help_text: str):
        return sub.add_parser(name, help=help_text)

    s = sp("config-init", "初始化全局配置")
    s.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    s.add_argument("--work-dir", required=True)
    s.add_argument("--poll-interval", type=int, required=True)
    s.add_argument("--max-agents", type=int, default=os.cpu_count() or 4)
    s.add_argument("--max-fix-retries", type=int, default=5)
    s.add_argument("--base-branch", default="main")
    s.add_argument("--auto-push", action="store_true")
    s.add_argument("--opencode-path", default="opencode")
    s.add_argument("--opencode-args", default="run")
    s.set_defaults(func=cmd_config_init)

    s = sp("config-show", "显示全局配置")
    s.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    s.set_defaults(func=cmd_config_show)

    s = sp("config-set", "更新全局配置")
    s.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    s.add_argument("--work-dir")
    s.add_argument("--poll-interval", type=int)
    s.add_argument("--max-agents", type=int)
    s.add_argument("--max-fix-retries", type=int)
    s.add_argument("--base-branch")
    s.add_argument("--opencode-path")
    s.add_argument("--opencode-args")
    s.add_argument("--auto-push", dest="auto_push", action="store_true")
    s.add_argument("--no-auto-push", dest="auto_push", action="store_false")
    s.set_defaults(auto_push=None, func=cmd_config_set)

    s = sp("db-init", "初始化仓库 occd.db 和 .gitattributes")
    s.add_argument("--repo", required=True)
    s.set_defaults(func=cmd_db_init)

    s = sp("acquire-lock", "获取仓库级主控锁")
    s.add_argument("--repo", required=True)
    s.add_argument("--holder", default="main")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_acquire_lock)

    s = sp("release-lock", "释放仓库级主控锁")
    s.add_argument("--repo", required=True)
    s.set_defaults(func=cmd_release_lock)

    s = sp("scan-repos", "扫描 work_dir 下所有 github-* 仓库，返回需要处理的需求")
    s.add_argument("--work-dir", required=True)
    s.set_defaults(func=cmd_scan_repos)

    s = sp("git-pull", "git pull")
    s.add_argument("--repo", required=True)
    s.add_argument("--ff-only", action="store_true")
    s.set_defaults(func=cmd_git_pull)

    s = sp("check-new-commit", "检查需求文件是否有新 commit")
    s.add_argument("--repo", required=True)
    s.add_argument("--file", required=True)
    s.add_argument("--last-commit", default="")
    s.set_defaults(func=cmd_check_new_commit)

    s = sp("db-upsert-req", "新增或更新需求记录")
    s.add_argument("--repo", required=True)
    s.add_argument("--filename", required=True)
    s.set_defaults(func=cmd_db_upsert_req)

    s = sp("db-update-req-status", "更新需求状态")
    s.add_argument("--repo", required=True)
    s.add_argument("--req-id", required=True)
    s.add_argument("--status", required=True)
    s.add_argument("--note", default=None)
    s.set_defaults(func=cmd_db_update_req_status)

    s = sp("db-get-req", "获取需求详情")
    s.add_argument("--repo", required=True)
    s.add_argument("--req-id", required=True)
    s.set_defaults(func=cmd_db_get_req)

    s = sp("db-list-pending-reqs", "列出未完成需求")
    s.add_argument("--repo", required=True)
    s.set_defaults(func=cmd_db_list_pending_reqs)

    s = sp("write-review", "生成需求澄清文件")
    s.add_argument("--repo", required=True)
    s.add_argument("--req", required=True)
    s.add_argument("--questions", required=True)
    s.set_defaults(func=cmd_write_review)

    s = sp("write-tasks", "写入子任务 prompt 文件并更新 DB")
    s.add_argument("--repo", required=True)
    s.add_argument("--req", required=True)
    s.add_argument("--tasks", required=True)
    s.set_defaults(func=cmd_write_tasks)

    s = sp("db-update-source-status", "更新子任务状态（主/子代理均可调用）")
    s.add_argument("--repo", required=True)
    s.add_argument("--task", required=True)
    s.add_argument("--status", required=True)
    s.add_argument("--session-key", default=None)
    s.add_argument("--note", default=None)
    s.set_defaults(func=cmd_db_update_source_status)

    s = sp("db-list-sources-by-xxx", "列出某串行批次下所有子任务")
    s.add_argument("--repo", required=True)
    s.add_argument("--xxx", required=True)
    s.set_defaults(func=cmd_db_list_sources_by_xxx)

    s = sp("db-add-execution", "登记一次执行记录")
    s.add_argument("--repo", required=True)
    s.add_argument("--task", required=True)
    s.add_argument("--report-file", required=True)
    s.add_argument("--outcome", required=True)
    s.add_argument("--summary", default=None)
    s.add_argument("--session-key", default=None)
    s.add_argument("--started-at", default=None)
    s.add_argument("--finished-at", default=None)
    s.set_defaults(func=cmd_db_add_execution)

    s = sp("db-list-executions", "列出某子任务所有执行历史")
    s.add_argument("--repo", required=True)
    s.add_argument("--task", required=True)
    s.set_defaults(func=cmd_db_list_executions)

    s = sp("db-summary", "全局状态汇总")
    s.add_argument("--work-dir", required=True)
    s.set_defaults(func=cmd_db_summary)

    s = sp("create-worktree", "创建 git worktree")
    s.add_argument("--repo", required=True)
    s.add_argument("--branch", required=True)
    s.add_argument("--reuse-if-exists", action="store_true")
    s.add_argument("--reset", action="store_true")
    s.set_defaults(func=cmd_create_worktree)

    s = sp("remove-worktree", "清理 git worktree")
    s.add_argument("--repo", required=True)
    s.add_argument("--branch", required=True)
    s.add_argument("--delete-branch", action="store_true")
    s.add_argument("--force-branch-delete", action="store_true")
    s.add_argument("--base-branch", default="main")
    s.set_defaults(func=cmd_remove_worktree)

    s = sp("merge-branches", "合并同批次完成分支")
    s.add_argument("--repo", required=True)
    s.add_argument("--xxx", required=True)
    s.add_argument("--base-branch", default=None)
    s.set_defaults(func=cmd_merge_branches)

    s = sp("run-tests", "自动识别并执行测试")
    s.add_argument("--repo", required=True)
    s.set_defaults(func=cmd_run_tests)

    s = sp("commit-push", "提交代码并可选推送")
    s.add_argument("--repo", required=True)
    s.add_argument("--message", required=True)
    s.add_argument("--path", action="append")
    s.add_argument("--include-db", action="store_true")
    s.add_argument("--all", action="store_true")
    s.add_argument("--push", action="store_true")
    s.add_argument("--base-branch", default=None)
    s.set_defaults(func=cmd_commit_push)

    s = sp("build-opencode-command", "根据全局配置生成 OpenCode 命令")
    s.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    s.add_argument("--prompt", required=True)
    s.add_argument("--extra-arg", action="append")
    s.set_defaults(func=cmd_build_opencode_command)

    s = sp("latest-report", "根据 executions 表获取某任务最新报告")
    s.add_argument("--repo", required=True)
    s.add_argument("--task", required=True)
    s.set_defaults(func=cmd_latest_report)

    s = sp("log", "写运行日志")
    s.add_argument("--repo", required=True)
    s.add_argument("--level", default="INFO")
    s.add_argument("--message", required=True)
    s.set_defaults(func=cmd_log)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
