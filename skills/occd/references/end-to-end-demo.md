# OCCD End-to-End Demo

这是一份从 req 到 review / task / spawn / report / finalize 的主代理演练示例。

## 自动化回归

如需一次性跑完标准回归场景，可直接执行：

```bash
python scripts/occd_e2e_scenarios.py --base-dir ~/tmp/occd-e2e
```

该脚本会自动使用独立测试配置文件，不会覆盖默认的 skill 配置 `skills/occd/occd.json`，也不会污染旧全局路径 `~/.openclaw/occd-config.json`。

当前脚本覆盖：

- 场景 1：同一 req 文件存在多个未处理 commit，按 commit 区间累计分析
- 场景 2：同一轮出现多个 new requirement，controller 返回 `preflight_batch`
- 场景 3：blocked requirement 在同文件出现新 commit 后重新进入 analyze
- 场景 4：write-review / write-tasks 会把当前 commit 区间标记为已处理

---

## 场景

仓库：`~/projects/github-myapp`
需求文件：`occd/req/feature-login.md`

需求内容示例：

```md
实现一个登录接口，支持用户名密码登录，并补充测试。
```

---

## Step 1. 主代理读取配置

```bash
python scripts/occd_utils.py config-show
```

拿到：

- `work_dir`
- `max_agents`
- `max_fix_retries`
- `opencode_path`
- `opencode_args`

---

## Step 2. 主代理轮询计划

```bash
python scripts/occd_controller.py poll-plan --work-dir ~/projects
```

如果该 repo 返回：

```json
{
  "action": "analyze_requirement",
  "req_id": "github-myapp:feature-login.md",
  "req_file": "feature-login.md"
}
```

则进入需求分析。

---

## Step 3A. 先让大语言模型判断：是否需要 review

主代理给大语言模型的分析提示建议：

```text
请阅读需求文件内容，并严格先回答：
1. 需求是否足够明确到可以拆分 task？
2. 如果不明确，请输出 review 问题列表，不要拆 task。
3. 只有在明确时，才输出 task 拆分方案。
4. task 拆分结果必须包含：task_id、type、summary、details、constraints、acceptance、depends_on、notes。
5. 拆分时要区分串行批次 XXX 与批内并行 YYY。
6. 生成到 `occd/task/` 的每个 task 文件后续会被直接传给 OpenCode，并把文件全文作为提示词运行，所以 task 内容必须自包含、清晰、可直接执行。
7. 每个 task 尽量控制在 10 分钟以内，优先生成可并发执行的小任务。
```

### 如果大语言模型判断“不明确”

例如输出问题：

- 登录接口是 `/api/login` 还是别的路径？
- 返回 token 的格式是否有要求？
- 是否已有用户表和密码校验逻辑可复用？

则主代理调用：

```bash
python scripts/occd_utils.py write-review \
  --repo ~/projects/github-myapp \
  --req feature-login.md \
  --questions '[
    "登录接口路径是否固定为 /api/login？",
    "返回 token 的字段名和格式是否有要求？",
    "是否已有用户表和密码校验逻辑可复用？"
  ]'
```

此时 requirement 转为 `reviewing`。

---

## Step 3B. 如果大语言模型判断“明确”，再拆 task

示例输出：

```json
[
  {
    "id": "feature-calc-001-001-coding",
    "type": "coding",
    "summary": "实现登录接口",
    "details": "新增 POST /api/login，校验用户名密码并返回 token",
    "constraints": "遵循现有 auth 模块结构",
    "acceptance": "- [ ] POST /api/login 返回 200 和 token\n- [ ] 错误密码返回 401",
    "depends_on": [],
    "notes": "优先复用现有用户查询逻辑"
  },
  {
    "id": "feature-calc-002-001-test-write",
    "type": "test-write",
    "summary": "为登录接口补充测试",
    "details": "覆盖正常登录与错误密码场景",
    "constraints": "沿用现有测试框架",
    "acceptance": "- [ ] 正常登录用例通过\n- [ ] 错误密码用例通过",
    "depends_on": ["feature-calc-001-001-coding"],
    "notes": "测试文件命名遵循仓库规范"
  },
  {
    "id": "feature-calc-003-001-test-run",
    "type": "test-run",
    "summary": "运行登录相关测试",
    "details": "执行项目测试并确认回归通过",
    "constraints": "使用仓库已有测试命令",
    "acceptance": "- [ ] 测试通过",
    "depends_on": ["feature-calc-002-001-test-write"],
    "notes": "若未识别测试框架，不得视为成功"
  }
]
```

主代理调用：

```bash
python scripts/occd_utils.py write-tasks \
  --repo ~/projects/github-myapp \
  --req feature-login.md \
  --tasks '<上面的 JSON 数组>'
```

---

## Step 4. 检查某批次是否可以 spawn

```bash
python scripts/occd_controller.py batch-ready \
  --repo ~/projects/github-myapp \
  --req-id github-myapp:feature-login.md \
  --xxx 001
```

若 `ready=true`，则主代理开始 spawn。

---

## Step 5. 主代理为子代理生成 prompt

主代理先生成 OpenCode 命令：

```bash
python scripts/occd_utils.py build-opencode-command \
  --config skills/occd/occd.json \
  --prompt-file /path/to/task-prompt.txt
```

再按 `references/subagent-prompt-template.md` 组织完整 prompt，关键要求包括：

- 读取 `occd/task/<task_id>.md`
- 先标记 `running`
- coding / test-write 必须委托 OpenCode
- 完成后写 `occd/report/report-*.md`
- 再登记 report 与最终状态

---

## Step 6. 主代理读取 report，但状态以 SQLite 为准

主代理可以通过 report 记录定位最新报告：

```bash
python scripts/occd_utils.py latest-report \
  --repo ~/projects/github-myapp \
  --task feature-calc-001-001-coding
```

然后：

- 读报告内容了解细节
- 但 `done / failed / running` 仍以 SQLite 为准

---

## Step 7. 重试或推进下一批

如果某个 task 失败：

```bash
python scripts/occd_controller.py retry-plan \
  --repo ~/projects/github-myapp \
  --req-id github-myapp:feature-login.md
```

如果某个批次已完成，则推进到下一 `XXX`。

---

## Step 8. finalize

当所有 task 都完成后：

```bash
python scripts/occd_controller.py finalize-ready \
  --repo ~/projects/github-myapp \
  --req-id github-myapp:feature-login.md
```

若 `ready=true`，主代理：

1. merge 批次分支
2. 跑最终测试
3. commit / push
4. requirement 标记 `done`
5. 释放 repo 锁

---

## 这个 demo 的核心原则

- req 分析与 task 拆分：主代理 + 大语言模型
- review 决策：主代理 + 大语言模型
- task 定义与 DB 落地：`occd_utils.py`
- 实际编码 / 测试编写：OpenCode
- 状态判断：SQLite
- report：补充上下文，不反推状态
