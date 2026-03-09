#!/usr/bin/env python3
"""
occd_utils.py - Auto-Coder 工具脚本
供 OpenClaw 主代理和子代理调用，封装所有 git/文件/状态操作

用法: python occd_utils.py <command> [--options]
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


# ─── 工具函数 ────────────────────────────────────────────

def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()

def load_status(repo: Path) -> dict:
    f = repo / "occd" / "task" / "task-status.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    return {"requirements": {}, "tasks": {}}

def save_status(repo: Path, status: dict):
    f = repo / "occd" / "task" / "task-status.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")

def write_log(repo: Path, level: str, message: str):
    log_dir = repo / "occd" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{ts}] [{level}] {message}\n"
    with open(log_dir / f"{today}.log", "a", encoding="utf-8") as f:
        f.write(entry)

def run_git(args: list, cwd: Path, check=False) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True, check=check)

def output(data):
    """输出 JSON 结果供调用方解析"""
    print(json.dumps(data, ensure_ascii=False, indent=2))


# ─── 命令实现 ────────────────────────────────────────────

def cmd_scan_repos(args):
    """扫描 work_dir 下所有 github-* 仓库，返回新增/未完成需求"""
    work_dir = Path(args.work_dir)
    repos = sorted([d for d in work_dir.iterdir()
                    if d.is_dir() and d.name.startswith("github-")])
    result = []
    for repo in repos:
        status = load_status(repo)
        req_dir = repo / "occd" / "req"
        if not req_dir.exists():
            continue
        for f in req_dir.iterdir():
            if f.suffix in (".md", ".txt"):
                req_name = f.name
                s = status["requirements"].get(req_name, {}).get("status")
                if s not in ("done",):
                    result.append({"repo": str(repo), "repo_name": repo.name, "req": req_name, "status": s or "new"})
    output({"repos": result})


def cmd_git_pull(args):
    repo = Path(args.repo)
    r = run_git(["pull"], cwd=repo)
    write_log(repo, "INFO", f"git pull: {r.stdout.strip() or r.stderr.strip()}")
    output({"success": r.returncode == 0, "output": r.stdout + r.stderr})


def cmd_check_new_commit(args):
    """检查某文件自 last_commit 以来是否有新 commit"""
    repo = Path(args.repo)
    r = run_git(["log", "-1", "--format=%H", "--", args.file], cwd=repo)
    current = r.stdout.strip()
    has_new = bool(current) and current != args.last_commit
    output({"has_new_commit": has_new, "current_commit": current})


def cmd_write_review(args):
    """生成需求澄清文件"""
    repo = Path(args.repo)
    req_name = args.req
    questions = json.loads(args.questions)
    status = load_status(repo)
    req_entry = status["requirements"].setdefault(req_name, {
        "status": "pending", "review_rounds": 0, "tasks": [],
        "created_at": utcnow(), "updated_at": utcnow()
    })
    round_num = req_entry.get("review_rounds", 0) + 1
    req_entry["review_rounds"] = round_num
    req_entry["status"] = "reviewing"
    req_entry["updated_at"] = utcnow()

    # 更新 last_req_commit
    r = run_git(["log", "-1", "--format=%H", "--", f"occd/req/{req_name}"], cwd=repo)
    req_entry["last_req_commit"] = r.stdout.strip()

    review_dir = repo / "occd" / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    base = Path(req_name).stem
    filename = f"{base}-review-{round_num:03d}.md"
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    qs = "\n".join(f"{i+1}. {q}" for i, q in enumerate(questions))
    content = f"""# 需求澄清请求 #{round_num:03d}

> **需求文件**：`occd/req/{req_name}`
> **生成时间**：{now}

## 不明确的内容

{qs}

## 如何回复

请直接修改需求文件 `occd/req/{req_name}`，补充说明后执行：

```bash
git add occd/req/{req_name}
git commit -m "clarify: {req_name}"
git push
```

---
<!-- 由 auto-coder 自动生成 -->
"""
    (review_dir / filename).write_text(content, encoding="utf-8")
    save_status(repo, status)
    write_log(repo, "INFO", f"已生成 review 文件: {filename}")
    output({"success": True, "file": str(review_dir / filename), "round": round_num})


def cmd_write_tasks(args):
    """将主代理分解的任务列表写入 occd/source/ 和 occd/test/"""
    repo = Path(args.repo)
    req_name = args.req
    tasks = json.loads(args.tasks)

    source_dir = repo / "occd" / "source"
    test_dir = repo / "occd" / "test"
    source_dir.mkdir(parents=True, exist_ok=True)
    test_dir.mkdir(parents=True, exist_ok=True)

    status = load_status(repo)
    req_entry = status["requirements"].setdefault(req_name, {
        "status": "pending", "review_rounds": 0, "tasks": [],
        "created_at": utcnow(), "updated_at": utcnow()
    })
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    for task in tasks:
        task_id = task["id"]
        parts = task_id.replace("coding-", "").split("-")
        xxx, yyy = parts[0], parts[1]

        # 编码任务文档
        coding_content = f"""# 编码任务 {task_id}

## 元信息
- **来源需求**：`occd/req/{req_name}`
- **分支名**：`{task_id}`
- **创建时间**：{now}

## 需求摘要
{task.get('summary', '')}

## 详细要求
{task.get('details', '')}

## 技术约束
{task.get('constraints', '自动识别')}

## 验收标准
{task.get('acceptance', '')}

## 注意事项
{task.get('notes', '')}
"""
        (source_dir / f"{task_id}.md").write_text(coding_content, encoding="utf-8")

        # 测试任务文档
        test_content = f"""# 测试任务 test-{xxx}-{yyy}

## 元信息
- **对应编码任务**：`{task_id}`
- **创建时间**：{now}

## 测试范围
{task.get('summary', '')}

## 验收标准
{task.get('acceptance', '')}

## 执行命令
```bash
# 自动识别填入
```
"""
        (test_dir / f"test-{xxx}-{yyy}.md").write_text(test_content, encoding="utf-8")

        # 更新状态
        status["tasks"][task_id] = {
            "status": "pending", "branch": task_id,
            "agent_session": "", "fix_rounds": 0,
            "created_at": utcnow(), "updated_at": utcnow()
        }

    req_entry["status"] = "in_progress"
    req_entry["tasks"] = [t["id"] for t in tasks]
    req_entry["updated_at"] = utcnow()
    save_status(repo, status)
    write_log(repo, "INFO", f"已生成 {len(tasks)} 个任务文档: {req_name}")
    output({"success": True, "tasks": [t["id"] for t in tasks]})


def cmd_create_worktree(args):
    """创建 git worktree"""
    repo = Path(args.repo)
    branch = args.branch
    worktree_path = repo.parent / ".occd-worktrees" / repo.name / branch
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    r = run_git(["worktree", "add", "-b", branch, str(worktree_path)], cwd=repo)
    success = r.returncode == 0
    write_log(repo, "INFO" if success else "ERROR",
              f"worktree {'创建成功' if success else '创建失败'}: {branch}")
    output({"success": success, "worktree_path": str(worktree_path), "error": r.stderr})


def cmd_remove_worktree(args):
    """清理 git worktree"""
    repo = Path(args.repo)
    branch = args.branch
    worktree_path = repo.parent / ".occd-worktrees" / repo.name / branch
    run_git(["worktree", "remove", "--force", str(worktree_path)], cwd=repo)
    write_log(repo, "INFO", f"worktree 已清理: {branch}")
    output({"success": True})


def cmd_merge_branches(args):
    """按 commit 时间升序合并同一 XXX 下的所有分支，返回冲突列表"""
    repo = Path(args.repo)
    xxx = args.xxx
    status = load_status(repo)
    branches = [tid for tid in status["tasks"]
                if tid.startswith(f"coding-{xxx}-")]

    def get_commit_time(branch):
        r = run_git(["log", "-1", "--format=%aI", branch], cwd=repo)
        return r.stdout.strip()

    branches.sort(key=get_commit_time)
    conflicts = []
    for branch in branches:
        r = run_git(["merge", "--no-ff", branch, "-m", f"[auto-coder] merge {branch}"], cwd=repo)
        if r.returncode != 0:
            run_git(["merge", "--abort"], cwd=repo)
            conflicts.append(branch)
            write_log(repo, "WARN", f"合并冲突: {branch}")
        else:
            write_log(repo, "INFO", f"合并成功: {branch}")
    output({"conflicts": conflicts, "merged": [b for b in branches if b not in conflicts]})


def cmd_run_tests(args):
    """自动识别测试框架并执行，返回结果"""
    repo = Path(args.repo)
    cmd = _detect_test_command(repo)
    write_log(repo, "INFO", f"执行测试: {' '.join(cmd)}")
    r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
    passed = r.returncode == 0
    write_log(repo, "INFO" if passed else "WARN",
              f"测试{'通过' if passed else '失败'}: returncode={r.returncode}")
    output({"passed": passed, "output": r.stdout + r.stderr, "command": cmd})


def cmd_commit_push(args):
    """提交并推送"""
    repo = Path(args.repo)
    run_git(["add", "-A"], cwd=repo, check=True)
    r = run_git(["commit", "-m", args.message], cwd=repo)
    if r.returncode != 0:
        output({"success": False, "error": r.stderr})
        return
    r2 = run_git(["push"], cwd=repo)
    success = r2.returncode == 0
    write_log(repo, "INFO" if success else "ERROR",
              f"commit+push {'成功' if success else '失败'}: {args.message}")
    output({"success": success, "error": r2.stderr if not success else ""})


def cmd_update_task_status(args):
    """更新单个任务状态"""
    repo = Path(args.repo)
    status = load_status(repo)
    entry = status["tasks"].setdefault(args.task, {})
    entry["status"] = args.status
    entry["updated_at"] = utcnow()
    if hasattr(args, "session") and args.session:
        entry["agent_session"] = args.session
    if hasattr(args, "fix_rounds") and args.fix_rounds is not None:
        entry["fix_rounds"] = args.fix_rounds
    save_status(repo, status)
    write_log(repo, "INFO", f"任务状态更新: {args.task} → {args.status}")
    output({"success": True})


def cmd_log(args):
    """写日志"""
    write_log(Path(args.repo), args.level.upper(), args.message)
    output({"success": True})


def cmd_status(args):
    """输出仓库任务状态摘要"""
    work_dir = Path(args.work_dir)
    repos = sorted([d for d in work_dir.iterdir()
                    if d.is_dir() and d.name.startswith("github-")])
    result = {}
    for repo in repos:
        s = load_status(repo)
        result[repo.name] = s
    output(result)


# ─── 测试框架检测 ────────────────────────────────────────

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


# ─── CLI 入口 ────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(prog="occd_utils")
    sub = p.add_subparsers(dest="cmd")

    def add(name, help_text, **kwargs):
        sp = sub.add_parser(name, help=help_text)
        for k, v in kwargs.items():
            sp.add_argument(f"--{k.replace('_','-')}", **v)
        return sp

    add("scan-repos",  "扫描新需求", work_dir={"required": True})
    add("git-pull",    "git pull",   repo={"required": True})
    add("check-new-commit", "检查文件新commit",
        repo={"required": True}, file={"required": True}, last_commit={"default": ""})
    add("write-review", "生成review文件",
        repo={"required": True}, req={"required": True}, questions={"required": True})
    add("write-tasks",  "写入任务文档",
        repo={"required": True}, req={"required": True}, tasks={"required": True})
    add("create-worktree", "创建worktree",
        repo={"required": True}, branch={"required": True})
    add("remove-worktree", "清理worktree",
        repo={"required": True}, branch={"required": True})
    add("merge-branches", "合并分支",
        repo={"required": True}, xxx={"required": True})
    add("run-tests",   "执行测试",   repo={"required": True})
    add("commit-push", "提交推送",   repo={"required": True}, message={"required": True})
    add("update-task-status", "更新任务状态",
        repo={"required": True}, task={"required": True},
        status={"required": True}, session={"default": ""}, fix_rounds={"type": int, "default": None})
    add("log", "写日志",
        repo={"required": True}, level={"default": "INFO"}, message={"required": True})
    add("status", "查看状态", work_dir={"required": True})

    args = p.parse_args()
    cmds = {
        "scan-repos": cmd_scan_repos, "git-pull": cmd_git_pull,
        "check-new-commit": cmd_check_new_commit, "write-review": cmd_write_review,
        "write-tasks": cmd_write_tasks, "create-worktree": cmd_create_worktree,
        "remove-worktree": cmd_remove_worktree, "merge-branches": cmd_merge_branches,
        "run-tests": cmd_run_tests, "commit-push": cmd_commit_push,
        "update-task-status": cmd_update_task_status, "log": cmd_log,
        "status": cmd_status,
    }
    if args.cmd in cmds:
        cmds[args.cmd](args)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
