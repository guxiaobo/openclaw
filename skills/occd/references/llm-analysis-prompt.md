# 主代理需求分析提示模板

给主代理在分析 req 文件时参考。

---

## 目标

让大语言模型先判断“是否应生成 review”，只有在需求足够明确时才拆分 task。

---

## 推荐提示词

```text
你正在为 OCCD 主代理分析一个软件需求文件。

请严格按下面顺序思考并输出：

第一步：判断该需求是否足够明确到可以直接拆分 task。
- 如果存在关键不确定项、缺少边界条件、接口不明确、验收标准缺失，视为“不明确”。
- 不明确时，不要拆分 task。

第二步：
- 如果不明确，输出 JSON：
  {
    "decision": "review",
    "questions": ["问题1", "问题2"]
  }

- 如果明确，输出 JSON：
  {
    "decision": "tasks",
    "coverage_points": ["必须覆盖点1", "必须覆盖点2"],
    "deliverables": ["交付物1", "交付物2"],
    "decomposition_check": {
      "contains_only_scaffold": false,
      "is_complete": true,
      "missing_points": [],
      "task_tree": ["第1批：骨架", "第2批：业务实现", "第3批：测试与回归"]
    },
    "tasks": [
      {
        "id": "<req-file-stem>-XXX-YYY-coding|test-write|test-run",
        "type": "coding|test-write|test-run",
        "summary": "一句话说明",
        "details": "详细要求",
        "constraints": "约束条件",
        "acceptance": "Markdown checklist",
        "depends_on": [],
        "notes": "补充信息",
        "covers": ["本任务覆盖的 coverage point"]
      }
    ]
  }

第三步：拆分 task 时遵守：
- 串行批次用 XXX
- 批内并行子任务用 YYY
- coding、test-write、test-run 边界清晰
- 必须先提炼 coverage_points 与 deliverables，再生成 tasks
- 必须按业务域/业务能力拆分任务；“骨架/占位/基础设施准备”只能作为前置任务，不能替代完整需求拆分
- 如果第一批只包含骨架任务，必须同时输出后续完整任务树，覆盖真正功能实现、测试编写、测试执行等后续步骤
- 每个 task 必须通过 `covers` 字段显式说明它覆盖了哪些 coverage_points
- 对尚未 merge 回主仓库的 worktree 成果，不得在 task 语义上描述成“主仓库已可见”或“需求已完成”
- 不要把需求不清的部分硬拆成 task
- 生成到 `occd/task/` 下的每个 task 文件，后续会被直接传给 OpenCode，并把该文件全文作为提示词运行；因此 task 内容必须自包含、可直接执行、不能依赖主代理后续口头补充
- 每个 task 的工作量尽量控制在 10 分钟以内；如果工作量明显超过 10 分钟，应继续拆细
- 优先生成可并发执行的 task；只有确实存在前置依赖时才放到后续串行批次
```

---

## 输出要求

- 只输出一个 JSON 对象
- `decision` 只能是 `review` 或 `tasks`
- 生成 review 时不要附带 task
- 生成 tasks 时不要附带 questions
- 生成 tasks 时必须同时带上 `coverage_points`、`deliverables`、`decomposition_check`、`tasks[*].covers`
