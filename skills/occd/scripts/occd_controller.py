#!/usr/bin/env python3
"""
occd_controller.py - OCCD 编排辅助脚本

定位：给 OpenClaw 主代理提供“下一步该做什么”的结构化输出；
真正的 sessions_spawn / 任务分析 / 文本判断仍由主代理完成。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
UTIL = SCRIPT_DIR / "occd_utils.py"


def run_util(*args: str) -> dict[str, Any]:
    cmd = [sys.executable, str(UTIL), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stdout or proc.stderr)
    return json.loads(proc.stdout)


def output(data: Any):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def requirement_detail(repo: Path, req_id: str) -> dict[str, Any]:
    return run_util("db-get-req", "--repo", str(repo), "--req-id", req_id)


def summarize_batch(batch: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {"total": len(batch), "by_status": {}, "task_ids": [t["id"] for t in batch]}
    for task in batch:
        summary["by_status"][task["status"]] = summary["by_status"].get(task["status"], 0) + 1
    return summary


def next_batch_for_repo(repo: Path) -> dict[str, Any]:
    pending = run_util("db-list-pending-reqs", "--repo", str(repo))
    requirements = pending.get("requirements", [])
    if not requirements:
        return {"repo": str(repo), "action": "idle"}

    for req in requirements:
        req_id = req["id"]
        status = req["status"]
        if status == "new":
            return {
                "repo": str(repo),
                "action": "analyze_requirement",
                "req_id": req_id,
                "req_file": req["filename"],
            }
        if status == "reviewing":
            return {
                "repo": str(repo),
                "action": "check_review_reply",
                "req_id": req_id,
                "req_file": req["filename"],
                "last_req_commit": req.get("last_req_commit"),
            }
        if status == "decomposed":
            detail = requirement_detail(repo, req_id)
            sources = detail.get("sources", [])
            if not sources:
                return {
                    "repo": str(repo),
                    "action": "analyze_requirement",
                    "req_id": req_id,
                    "req_file": req["filename"],
                    "reason": "decomposed_without_sources",
                }
            batches = sorted({s["xxx"] for s in sources})
            for xxx in batches:
                batch = [s for s in sources if s["xxx"] == xxx]
                statuses = {s["status"] for s in batch}
                if any(s in {"pending", "failed"} for s in statuses) and not any(s in {"running", "spawned"} for s in statuses):
                    return {
                        "repo": str(repo),
                        "action": "spawn_batch",
                        "req_id": req_id,
                        "req_file": req["filename"],
                        "xxx": xxx,
                        "tasks": [s for s in batch if s["status"] in {"pending", "failed"}],
                        "summary": summarize_batch(batch),
                    }
                if statuses <= {"done"}:
                    continue
                return {
                    "repo": str(repo),
                    "action": "await_batch_completion",
                    "req_id": req_id,
                    "req_file": req["filename"],
                    "xxx": xxx,
                    "tasks": batch,
                    "summary": summarize_batch(batch),
                }
            return {
                "repo": str(repo),
                "action": "finalize_requirement",
                "req_id": req_id,
                "req_file": req["filename"],
                "sources_total": len(sources),
            }

    return {"repo": str(repo), "action": "idle"}


def cmd_repo_status(args):
    output(next_batch_for_repo(Path(args.repo)))


def cmd_global_status(args):
    work_dir = Path(args.work_dir)
    repos = sorted(d for d in work_dir.iterdir() if d.is_dir() and d.name.startswith("github-"))
    output({"repos": [next_batch_for_repo(repo) for repo in repos]})


def cmd_poll_plan(args):
    work_dir = Path(args.work_dir)
    repos = sorted(d for d in work_dir.iterdir() if d.is_dir() and d.name.startswith("github-"))
    for repo in repos:
        run_util("git-pull", "--repo", str(repo), "--ff-only")
    run_util("scan-repos", "--work-dir", str(work_dir))
    plan = [next_batch_for_repo(repo) for repo in repos]
    output({"work_dir": str(work_dir), "plan": plan})


def cmd_batch_ready(args):
    repo = Path(args.repo)
    detail = requirement_detail(repo, args.req_id)
    sources = detail.get("sources", [])
    batch = [s for s in sources if s["xxx"] == args.xxx]
    statuses = {s["status"] for s in batch}
    ready = bool(batch) and any(s in {"pending", "failed"} for s in statuses) and not any(s in {"running", "spawned"} for s in statuses)
    output({
        "repo": str(repo),
        "req_id": args.req_id,
        "xxx": args.xxx,
        "ready": ready,
        "summary": summarize_batch(batch),
        "tasks": [s for s in batch if s["status"] in {"pending", "failed"}],
    })


def cmd_finalize_ready(args):
    repo = Path(args.repo)
    detail = requirement_detail(repo, args.req_id)
    sources = detail.get("sources", [])
    all_done = bool(sources) and all(s["status"] == "done" for s in sources)
    batches = sorted({s["xxx"] for s in sources})
    output({
        "repo": str(repo),
        "req_id": args.req_id,
        "ready": all_done,
        "sources_total": len(sources),
        "batches": batches,
        "not_done": [s["id"] for s in sources if s["status"] != "done"],
    })


def cmd_retry_plan(args):
    repo = Path(args.repo)
    detail = requirement_detail(repo, args.req_id)
    sources = detail.get("sources", [])
    failed = [s for s in sources if s["status"] == "failed"]
    output({
        "repo": str(repo),
        "req_id": args.req_id,
        "failed_tasks": [s["id"] for s in failed],
        "retry_candidates": failed,
        "advice": "先读取 task report，再决定是直接重试、回退到上游 coding/test-write、还是转 review。",
    })


def main():
    p = argparse.ArgumentParser(prog="occd_controller", description="OCCD 编排辅助脚本")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("repo-status", help="返回单仓库下一步动作")
    s.add_argument("--repo", required=True)
    s.set_defaults(func=cmd_repo_status)

    s = sub.add_parser("global-status", help="返回 work_dir 下所有仓库下一步动作")
    s.add_argument("--work-dir", required=True)
    s.set_defaults(func=cmd_global_status)

    s = sub.add_parser("poll-plan", help="先 pull/scan，再返回全局下一步动作")
    s.add_argument("--work-dir", required=True)
    s.set_defaults(func=cmd_poll_plan)

    s = sub.add_parser("batch-ready", help="检查指定 requirement 的某个批次是否可 spawn")
    s.add_argument("--repo", required=True)
    s.add_argument("--req-id", required=True)
    s.add_argument("--xxx", required=True)
    s.set_defaults(func=cmd_batch_ready)

    s = sub.add_parser("finalize-ready", help="检查 requirement 是否可 finalize")
    s.add_argument("--repo", required=True)
    s.add_argument("--req-id", required=True)
    s.set_defaults(func=cmd_finalize_ready)

    s = sub.add_parser("retry-plan", help="列出 requirement 下失败任务，供主代理重试分诊")
    s.add_argument("--repo", required=True)
    s.add_argument("--req-id", required=True)
    s.set_defaults(func=cmd_retry_plan)

    args = p.parse_args()
    if not hasattr(args, "func"):
        p.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
