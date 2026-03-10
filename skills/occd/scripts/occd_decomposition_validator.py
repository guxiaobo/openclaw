#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

SCAFFOLD_KEYWORDS = {
    "skeleton",
    "scaffold",
    "bootstrap",
    "init",
    "initialize",
    "initialise",
    "setup",
    "placeholder",
    "boilerplate",
    "骨架",
    "脚手架",
    "初始化",
    "占位",
}

DELIVERY_KEYWORDS = {
    "implement",
    "feature",
    "business",
    "logic",
    "test",
    "validate",
    "run",
    "登录",
    "功能",
    "测试",
    "实现",
    "验证",
}


def fail(msg: str, extra: dict[str, Any] | None = None):
    payload: dict[str, Any] = {"success": False, "error": msg}
    if extra:
        payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    sys.exit(1)


def load_payload(args) -> dict[str, Any]:
    if args.input_file:
        return json.loads(open(args.input_file, encoding="utf-8").read())
    if args.input_json:
        return json.loads(args.input_json)
    fail("one of --input-file or --input-json is required")


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join(str(x) for x in value)
    return str(value)


def contains_keywords(text: str, words: set[str]) -> bool:
    lower = text.lower()
    return any(w in lower for w in words)


def validate(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("decision") != "tasks":
        fail("payload decision must be 'tasks'", {"decision": payload.get("decision")})

    coverage_points = payload.get("coverage_points") or []
    deliverables = payload.get("deliverables") or []
    tasks = payload.get("tasks") or []
    decomposition_check = payload.get("decomposition_check") or {}

    if not isinstance(coverage_points, list) or not coverage_points:
        fail("coverage_points must be a non-empty array")
    if not isinstance(deliverables, list) or not deliverables:
        fail("deliverables must be a non-empty array")
    if not isinstance(tasks, list) or not tasks:
        fail("tasks must be a non-empty array")

    missing_fields = []
    all_covers: set[str] = set()
    scaffold_only = True
    for idx, task in enumerate(tasks):
        for field in ("id", "type", "summary", "details", "constraints", "acceptance", "depends_on", "notes", "covers"):
            if field not in task:
                missing_fields.append({"task_index": idx, "task_id": task.get("id"), "missing": field})
        covers = task.get("covers") or []
        if not isinstance(covers, list) or not covers:
            missing_fields.append({"task_index": idx, "task_id": task.get("id"), "missing": "covers(non-empty list)"})
        else:
            all_covers.update(str(x) for x in covers)
        text = " ".join(
            normalize_text(task.get(k)) for k in ("summary", "details", "constraints", "acceptance", "notes")
        )
        if not contains_keywords(text, SCAFFOLD_KEYWORDS) or contains_keywords(text, DELIVERY_KEYWORDS):
            scaffold_only = False

    if missing_fields:
        fail("task fields incomplete", {"issues": missing_fields})

    missing_coverage = [point for point in coverage_points if point not in all_covers]
    if missing_coverage:
        fail("coverage points not fully covered by tasks", {"missing_coverage_points": missing_coverage})

    task_types = {task.get("type") for task in tasks}
    if "test-run" not in task_types:
        fail("task tree must include at least one test-run task")

    has_test_write = "test-write" in task_types
    if not has_test_write:
        fail("task tree must include at least one test-write task")

    task_tree = decomposition_check.get("task_tree") or []
    if scaffold_only and not task_tree:
        fail("scaffold-only plan rejected without full follow-up task tree")

    check_missing = decomposition_check.get("missing_points") or []
    if check_missing:
        fail("decomposition_check still reports missing points", {"missing_points": check_missing})

    if decomposition_check.get("is_complete") is not True:
        fail("decomposition_check.is_complete must be true for accepted tasks")

    return {
        "success": True,
        "coverage_points": coverage_points,
        "deliverables": deliverables,
        "task_count": len(tasks),
        "task_types": sorted(task_types),
        "scaffold_only": scaffold_only,
        "task_tree_count": len(task_tree),
        "covered_points": sorted(all_covers),
    }


def main():
    parser = argparse.ArgumentParser(description="Validate OCCD decomposition output before write-tasks")
    parser.add_argument("--input-file")
    parser.add_argument("--input-json")
    args = parser.parse_args()
    payload = load_payload(args)
    result = validate(payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
