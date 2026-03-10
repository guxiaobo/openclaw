#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
UTIL = SCRIPT_DIR / "occd_utils.py"
CTRL = SCRIPT_DIR / "occd_controller.py"
PY = sys.executable


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(cmd)}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    return proc


def run_json(cmd: list[str], cwd: Path | None = None) -> dict[str, Any]:
    proc = run(cmd, cwd=cwd)
    return json.loads(proc.stdout)


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return run(["git", *args], cwd=repo, check=check)


def occd_util(*args: str) -> dict[str, Any]:
    return run_json([PY, str(UTIL), *args])


def occd_ctrl(*args: str) -> dict[str, Any]:
    return run_json([PY, str(CTRL), *args])


def write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def assert_true(cond: bool, msg: str):
    if not cond:
        raise AssertionError(msg)


class Runner:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.config_path = self.base_dir / "occd-test-config.json"
        self.results: dict[str, Any] = {}

    def create_repo(self, scenario: str, repo_name: str) -> Path:
        wd = self.base_dir / scenario
        remotes = wd / "remotes"
        remotes.mkdir(parents=True, exist_ok=True)
        run(["git", "init", "--bare", f"{repo_name}.git"], cwd=remotes)
        run(["git", "clone", str(remotes / f"{repo_name}.git"), repo_name], cwd=wd)
        repo = wd / repo_name
        git(repo, "checkout", "-b", "main")
        return repo

    def init_repo(self, repo: Path, *, module_name: str = "app.py", content: str = "def noop():\n    return True\n"):
        write(repo / module_name, content)
        write(repo / "Makefile", "test:\n\tpython3 -m unittest discover -s tests -p 'test_*.py' -v\n")
        write(repo / "tests" / ".gitkeep", "")
        git(repo, "add", ".")
        git(repo, "commit", "-m", "init")
        git(repo, "push", "-u", "origin", "main")

    def init_occd(self, work_dir: Path, repo: Path):
        occd_util(
            "config-init",
            "--config",
            str(self.config_path),
            "--work-dir",
            str(work_dir),
            "--poll-interval",
            "300",
            "--max-agents",
            "4",
            "--max-fix-retries",
            "3",
            "--base-branch",
            "main",
            "--auto-push",
            "--opencode-path",
            "/usr/bin/true",
            "--opencode-args",
            "run",
        )
        occd_util("db-init", "--repo", str(repo))
        assert_true((repo / "tests" / ".gitkeep").exists(), "tests/.gitkeep should exist after db-init")

    def add_req_commit(self, repo: Path, req_name: str, content: str, message: str):
        write(repo / "occd" / "req" / req_name, content)
        git(repo, "add", "occd", "tests/.gitkeep", ".gitattributes")
        git(repo, "commit", "-m", message)
        git(repo, "push")

    def add_report(self, repo: Path, task: str, outcome: str, summary: str, session_key: str):
        name = f"report-{task}-{session_key}.md"
        write(repo / "occd" / "report" / name, f"# report\n- task_id: {task}\n- outcome: {outcome}\n")
        occd_util("db-add-report", "--repo", str(repo), "--task", task, "--report-file", name, "--outcome", outcome, "--summary", summary, "--session-key", session_key)

    def mark_task_done(self, repo: Path, task: str, session_key: str, summary: str = "ok"):
        self.add_report(repo, task, "success", summary, session_key)
        occd_util("db-update-task-status", "--repo", str(repo), "--task", task, "--status", "done", "--session-key", session_key)

    def worktree(self, repo: Path, task: str, *, reset: bool = False) -> Path:
        args = ["create-worktree", "--repo", str(repo), "--branch", task]
        args.append("--reset" if reset else "--reuse-if-exists")
        return Path(occd_util(*args)["worktree_path"])

    def scenario_multi_commit_interval(self):
        repo = self.create_repo("scenario-multi-commit", "github-interval")
        wd = repo.parent
        self.init_repo(repo)
        self.init_occd(wd, repo)
        self.add_req_commit(repo, "feature-auth.md", "实现认证。\n", "req commit 1")
        self.add_req_commit(repo, "feature-auth.md", "实现认证。\n补充：支持 refresh token。\n", "req commit 2")
        self.add_req_commit(repo, "feature-auth.md", "实现认证。\n补充：支持 refresh token。\n补充：补自动化测试。\n", "req commit 3")
        scan = occd_util("scan-repos", "--work-dir", str(wd))
        item = scan["repos"][0]
        assert_true(item["pending_commit_count"] == 3, f"expected 3 pending commits, got {item['pending_commit_count']}")
        history = occd_util("req-history", "--repo", str(repo), "--req", "feature-auth.md", "--to-commit", item["latest_commit"])
        assert_true(history["commit_count"] == 3, f"expected 3 commits in history, got {history['commit_count']}")
        tasks = [
            {"id": "feature-auth-001-001-coding", "type": "coding", "summary": "实现认证骨架", "details": "编辑 app.py", "constraints": "最小实现", "acceptance": "- [ ] 有 login", "depends_on": [], "notes": "无"},
            {"id": "feature-auth-002-001-test-run", "type": "test-run", "summary": "跑测试", "details": "执行 make test", "constraints": "失败则失败", "acceptance": "- [ ] 通过", "depends_on": ["feature-auth-001-001-coding"], "notes": "无"},
        ]
        occd_util("write-tasks", "--repo", str(repo), "--req", "feature-auth.md", "--tasks", json.dumps(tasks, ensure_ascii=False))
        req = occd_util("db-get-req", "--repo", str(repo), "--req-id", "github-interval:feature-auth.md")["req"]
        assert_true(req["processed_commit"] == req["latest_commit"], "write-tasks should advance processed_commit to latest_commit")
        assert_true(req["pending_commit_count"] == 0, "write-tasks should clear pending commit count")
        self.results["multi_commit_interval"] = {"scan": item, "history": {"commit_count": history["commit_count"]}, "req": req}

    def scenario_preflight_batch(self):
        repo = self.create_repo("scenario-preflight", "github-batch")
        wd = repo.parent
        self.init_repo(repo)
        self.init_occd(wd, repo)
        self.add_req_commit(repo, "feature-a.md", "需求A\n", "add feature-a")
        self.add_req_commit(repo, "feature-b.md", "需求B\n", "add feature-b")
        plan = occd_ctrl("poll-plan", "--work-dir", str(wd))
        action = plan["plan"][0]
        assert_true(action["action"] == "preflight_batch", f"expected preflight_batch, got {action['action']}")
        assert_true(len(action["requirements"]) == 2, "preflight batch should contain 2 requirements")
        self.results["preflight_batch"] = action

    def scenario_blocked_then_new_commit(self):
        repo = self.create_repo("scenario-blocked", "github-blocked")
        wd = repo.parent
        self.init_repo(repo)
        self.init_occd(wd, repo)
        self.add_req_commit(repo, "feature-profile.md", "实现 profile。\n", "add feature-profile")
        occd_util("scan-repos", "--work-dir", str(wd))
        occd_util("db-block-req", "--repo", str(repo), "--req-id", "github-blocked:feature-profile.md", "--reason", "与 feature-auth.md 冲突", "--conflict-group", "cg-001")
        status1 = occd_ctrl("repo-status", "--repo", str(repo))
        assert_true(status1["action"] == "check_review_reply", "blocked requirement should wait for new commit")
        self.add_req_commit(repo, "feature-profile.md", "实现 profile。\n补充：与 auth 方案兼容。\n", "clarify blocked req")
        scan = occd_util("scan-repos", "--work-dir", str(wd))
        item = scan["repos"][0]
        assert_true(item["status"] == "new", "new commit after blocked should reset requirement to new")
        status2 = occd_ctrl("repo-status", "--repo", str(repo))
        assert_true(status2["action"] == "analyze_requirement", "after new commit blocked req should re-enter analyze")
        self.results["blocked_then_new_commit"] = {"before": status1, "after": status2, "scan": item}

    def scenario_review_marks_processed(self):
        repo = self.create_repo("scenario-review-interval", "github-review-interval")
        wd = repo.parent
        self.init_repo(repo)
        self.init_occd(wd, repo)
        self.add_req_commit(repo, "feature-login.md", "实现登录。\n", "login 1")
        self.add_req_commit(repo, "feature-login.md", "实现登录。\n需要说明接口路径。\n", "login 2")
        occd_util("scan-repos", "--work-dir", str(wd))
        occd_util("write-review", "--repo", str(repo), "--req", "feature-login.md", "--questions", json.dumps(["登录接口路径是什么？"], ensure_ascii=False))
        req = occd_util("db-get-req", "--repo", str(repo), "--req-id", "github-review-interval:feature-login.md")["req"]
        assert_true(req["status"] == "reviewing", "write-review should move req to reviewing")
        assert_true(req["processed_commit"] == req["latest_commit"], "write-review should mark current interval processed")
        assert_true(req["pending_commit_count"] == 0, "write-review should clear pending interval")
        self.results["review_marks_processed"] = req

    def scenario_multi_commit_with_cross_file_conflict(self):
        repo = self.create_repo("scenario-cross-conflict", "github-cross-conflict")
        wd = repo.parent
        self.init_repo(repo)
        self.init_occd(wd, repo)
        self.add_req_commit(repo, "feature-auth.md", "实现认证。\n", "auth 1")
        self.add_req_commit(repo, "feature-auth.md", "实现认证。\n补充：返回 token 字段名为 token。\n", "auth 2")
        self.add_req_commit(repo, "feature-auth.md", "实现认证。\n补充：返回 token 字段名为 token。\n补充：支持 refresh token。\n", "auth 3")
        self.add_req_commit(repo, "feature-session.md", "实现 session 管理。\n要求登录返回 session_id，不返回 token。\n", "session 1")
        plan = occd_ctrl("poll-plan", "--work-dir", str(wd))
        item = plan["plan"][0]
        assert_true(item["action"] == "preflight_batch", f"expected preflight_batch, got {item['action']}")
        reqs = {r['req_file']: r for r in item['requirements']}
        assert_true(reqs['feature-auth.md']['pending_commit_count'] == 3, "feature-auth should aggregate 3 pending commits")
        assert_true(reqs['feature-session.md']['pending_commit_count'] == 1, "feature-session should have 1 pending commit")
        auth_history = occd_util("req-history", "--repo", str(repo), "--req", "feature-auth.md", "--to-commit", reqs['feature-auth.md']['latest_commit'])
        session_history = occd_util("req-history", "--repo", str(repo), "--req", "feature-session.md", "--to-commit", reqs['feature-session.md']['latest_commit'])
        assert_true(auth_history['commit_count'] == 3, "feature-auth history should include 3 commits")
        assert_true(session_history['commit_count'] == 1, "feature-session history should include 1 commit")
        occd_util("db-block-req", "--repo", str(repo), "--req-id", "github-cross-conflict:feature-auth.md", "--reason", "与 feature-session.md 的登录返回结构冲突", "--conflict-group", "cg-auth-session")
        occd_util("db-block-req", "--repo", str(repo), "--req-id", "github-cross-conflict:feature-session.md", "--reason", "与 feature-auth.md 的登录返回结构冲突", "--conflict-group", "cg-auth-session")
        status_blocked = occd_ctrl("repo-status", "--repo", str(repo))
        assert_true(status_blocked['action'] == 'check_review_reply', 'blocked conflicting requirements should wait for new commits')
        self.add_req_commit(repo, "feature-auth.md", "实现认证。\n补充：返回 token 字段名为 token。\n补充：支持 refresh token。\n补充：session 也统一返回 token，不再要求 session_id。\n", "auth clarify after conflict")
        scan = occd_util("scan-repos", "--work-dir", str(wd))
        refreshed = [x for x in scan['repos'] if x['req'] == 'feature-auth.md'][0]
        assert_true(refreshed['status'] == 'new', 'clarified auth requirement should return to new')
        assert_true(refreshed['pending_commit_count'] == 4, 'auth requirement should now aggregate 4 commits from baseline')
        status_after = occd_ctrl("repo-status", "--repo", str(repo))
        assert_true(status_after['action'] in {'preflight_batch', 'analyze_requirement'}, 'after clarification repo should re-enter planning')
        self.results["multi_commit_with_cross_file_conflict"] = {
            "preflight": item,
            "auth_history": {"commit_count": auth_history['commit_count']},
            "session_history": {"commit_count": session_history['commit_count']},
            "blocked_status": status_blocked,
            "after_scan": refreshed,
            "after_status": status_after,
        }

    def run_all(self):
        self.scenario_multi_commit_interval()
        self.scenario_preflight_batch()
        self.scenario_blocked_then_new_commit()
        self.scenario_review_marks_processed()
        self.scenario_multi_commit_with_cross_file_conflict()
        return {"success": True, "base_dir": str(self.base_dir), "scenarios": self.results}


def main():
    parser = argparse.ArgumentParser(description="Run OCCD E2E scenario tests")
    parser.add_argument("--base-dir", default=None)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    if args.base_dir:
        base = Path(args.base_dir).expanduser().resolve()
        if base.exists():
            shutil.rmtree(base)
        base.mkdir(parents=True, exist_ok=True)
        cleanup = False
    else:
        base = Path(tempfile.mkdtemp(prefix="occd-e2e-"))
        cleanup = not args.keep
    try:
        result = Runner(base).run_all()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        if cleanup:
            shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
