---
name: occd
description: OCCD (OpenCode Continuous Developing) - 自动化多仓库持续开发 Skill。OpenClaw 作为主代理，持续监听多个 GitHub 仓库（github-* 目录）的需求目录（occd/req/），将需求转化为子任务，通过 sessions_spawn 启动子代理并行/串行执行，自动测试修复直到通过后 commit & push。触发关键词：occd、auto-coder、自动编程、需求转任务、多仓库自动开发、启动编程流水线、持续开发。
---

# OCCD - OpenCode Continuous Developing

OpenClaw 作为主代理驱动的自动化编程流水线。

## 架构

```
OpenClaw 主代理（你）
  ├── cron 定期触发轮询
  ├── 读取 occd/req/ 新需求，通过 occd.db 去重防漏
  ├── 分析需求，拆分为子任务写入 occd/source/（类型：coding/test-write/test-run）
  ├── 按依赖关系串行/并行 sessions_spawn 子代理
  │     └── 子代理执行任务，将结果报告写入 occd/task/，原始输出写入 occd/logs/
  │     └── 完成后通过 occd_utils.py 汇报状态（不直接写 DB）
  └── 主代理读取报告，决定下一步（通过/修复/失败）
```

Python 脚本仅作为工具辅助（git 操作、DB 读写、日志）。

## 目录约定

每个 `github-*` 仓库下的 `occd/` 是所有工作目录：

```
github-<repo>/
  occd/
    req/       ← 需求文档（md/txt，只读，由人工或外部系统写入）
    source/    ← 子任务 prompt 文件（主代理生成，子代理只读）
    task/      ← 子任务执行结果报告（子代理写入，每次执行一个文件）
    review/    ← 需求澄清文件（主代理生成）
    logs/      ← 子代理原始技术输出（YYYY-MM-DD.log）
    occd.db    ← SQLite 状态数据库（主代理独占读写，随仓库 git 同步）
```

### 目录职责说明

| 目录      | 写入者    | 内容          | 说明                                |
| --------- | --------- | ------------- | ----------------------------------- |
| `req/`    | 人工/外部 | 需求文档      | **只读**，任何代理禁止修改          |
| `source/` | 主代理    | 子任务 prompt | 子代理的工作说明书，子代理只读      |
| `task/`   | 子代理    | 执行结果报告  | 每次执行对应一个文件，记录过程+结果 |
| `review/` | 主代理    | 需求澄清      | 需求不清时生成，等待人工更新 req    |
| `logs/`   | 子代理    | 原始技术输出  | 日志流水账，不含结构化结论          |
| `occd.db` | 主代理    | 状态数据库    | 通过 occd_utils.py 操作             |

### source/ 子任务类型

主代理拆分需求时，子任务类型可以是：

统一命名格式：`reqZZZ-XXX-YYY-{type}.md`

| 段       | 含义                            | 示例                                 |
| -------- | ------------------------------- | ------------------------------------ |
| `ZZZ`    | 需求序号（在本仓库内唯一递增）  | `001`                                |
| `XXX`    | 串行批次号，同 ZZZ 下按顺序执行 | `001`                                |
| `YYY`    | 并行子序号，同 XXX 内可并行执行 | `002`                                |
| `{type}` | 任务类型                        | `coding` / `test-write` / `test-run` |

示例：

- `req001-001-001-coding.md`：需求001的第一批次第一个编码任务
- `req001-001-002-coding.md`：需求001的第一批次第二个编码任务（与上一个并行）
- `req001-002-001-test-write.md`：需求001的第二批次写测试任务（等第一批次全部完成后启动）
- `req001-003-001-test-run.md`：需求001的第三批次跑测试任务

### task/ 报告文件

每次子代理执行一个 source 任务后，写入一个报告文件：

```
task/report-{task_id}-{timestamp}.md
```

例：`task/report-req001-001-001-coding-20260309T094500.md`

报告格式见 [references/task-report-template.md](references/task-report-template.md)，包含：

- 执行时间、使用的 session key
- 执行结论（success / failure / partial）
- 关键变更摘要
- 遇到的问题
- 是否需要主代理介入

> **区别于 logs/**：task/ 是结构化的执行报告，主代理依据此做决策；logs/ 是原始输出流水账。

### occd.db 约定

- **唯一写者**：只有主代理通过 `occd_utils.py` 写入，子代理不直接访问
- **随仓库同步**：每次状态批次变更后，通过 `commit-push --include-db` 将 `occd.db` 一起提交
- **二进制文件**：仓库 `.gitattributes` 须包含 `occd/occd.db binary`，防止 merge 冲突
- **多实例接力**：另一台机器 `git pull` 后即可获取最新状态，无缝接力
- **Schema**：见 [references/occd-db-schema.sql](references/occd-db-schema.sql)

## 初始化

```
/occd init --work-dir ~/projects --poll-interval 300
```

这会在 `~/.openclaw/occd-config.json` 保存配置，并注册 cron 任务。
同时在每个仓库的 `occd/` 目录下初始化 `occd.db` 和写入 `.gitattributes`。

配置参数见 [REQUIREMENTS.md](REQUIREMENTS.md)。

## 主代理职责（你需要做的）

### 1. 轮询触发（cron 回调时执行）

```
调用 scripts/occd_utils.py scan-repos --work-dir <work_dir>
→ 返回各仓库需要处理的需求列表（已自动比对 occd.db 去重）

对每个需求：
  调用 scripts/occd_utils.py git-pull --repo <repo_path>
  读取 occd/req/<reqname> 内容
  判断需求是否明确（见下）
```

`scan-repos` 内部逻辑：

1. 遍历 `req/` 下所有文件，计算 `content_hash`
2. 查询 `occd.db`：若记录存在且 hash 未变且 status ∉ `{new, failed}` → 跳过
3. hash 变化 → 重置状态为 `new`，重新分析
4. 不存在 → 插入新记录，status = `new`

### 2. 需求分析

自行阅读需求文档，判断是否足够明确：

- **不明确** → 调用 `scripts/occd_utils.py write-review` 生成 review 文件，DB 状态更新为 `reviewing`
- **明确** → 拆分为子任务，调用 `scripts/occd_utils.py write-tasks` 写入 source/ 和 DB

拆分原则：

- 编码任务（`req001-001-001-coding`）：实现具体功能
- 写测试任务（`req001-002-001-test-write`）：为编码任务编写测试用例（通常在对应 coding 任务之后）
- 跑测试任务（`req001-003-001-test-run`）：执行测试并报告（通常在 test-write 完成之后）
- 同 XXX 的任务串行，同 XXX 内不同 YYY 的任务并行

### 3. 启动子代理（sessions_spawn）

对当前串行批次（XXX）内所有 YYY 并行 spawn：

```python
task = f"""
你是一个执行子代理。请完成以下任务：

仓库路径: {repo_path}
任务文件: {repo_path}/occd/source/{task_id}.md

步骤：
1. 读取任务文件，了解任务类型和要求
2. 若任务类型为 coding 或 test-write：
   调用 scripts/occd_utils.py create-worktree --repo {repo_path} --branch {task_id}
   在 worktree 目录中使用 opencode 完成工作（见 opencode-controller skill）
3. 若任务类型为 test-run：
   调用 scripts/occd_utils.py run-tests --repo {repo_path}
4. 完成后，将执行结果报告写入：
   occd/task/report-{task_id}-{timestamp}.md（格式见 task-report-template.md）
5. 调用 scripts/occd_utils.py update-source-status \
     --repo {repo_path} --task {task_id} --status done --session-key {session_key}
"""
sessions_spawn(task=task, mode="run")
```

spawn 前主代理在 DB 中将 source 状态更新为 `spawned`，记录 session_key。

### 4. 读取报告，决策下一步

子代理完成后，主代理读取 `occd/task/report-{task_id}-*.md`：

```
结论为 success：
  → DB 更新 source 状态为 done
  → 若当前 XXX 全部 done → 启动下一个 XXX 批次
  → 若所有批次完成 → commit-push --include-db，需求标记 done

结论为 failure 且 retry_count < max_fix_retries：
  → DB 记录 retry_count + 1
  → 重新 spawn 子代理（任务文件不变，子代理读报告了解上次失败原因）

结论为 failure 且超过重试上限：
  → DB 标记 source 为 failed，需求标记 failed
  → 记录日志，等待人工介入
```

对于 `test-run` 类型，若测试失败且存在对应 `coding` 任务：
→ 重新 spawn 对应的 coding 子代理修复，再重新跑测试。

### 5. 合并分支（coding/test-write 任务完成后）

```
调用 scripts/occd_utils.py merge-branches --repo <repo_path> --xxx <xxx>
→ 按 commit 时间升序合并，返回冲突列表

冲突分支 → 重新 spawn 对应编码子代理（基于最新代码）
```

### 6. Review 回复检测

轮询时对 `reviewing` 状态的需求：

```
scripts/occd_utils.py check-new-commit --repo <repo_path> --file occd/req/<reqname>
有新 commit → DB 中重置需求状态为 new → 重新触发需求分析
```

## 子代理职责

子代理收到任务后：

1. 读取 `occd/source/{task_id}.md`，明确任务类型和要求
2. 执行任务（coding/test-write 用 opencode；test-run 直接跑测试）
3. 将结构化执行报告写入 `occd/task/report-{task_id}-{timestamp}.md`
4. 将原始技术输出追加到 `occd/logs/YYYY-MM-DD.log`
5. 通过 `occd_utils.py update-source-status` 汇报状态（**不直接读写 occd.db**）

参考 `opencode-controller` skill 了解如何操作 opencode。

## 工具脚本

所有 git 操作、文件操作、DB 操作通过 `scripts/occd_utils.py` 完成。

用法：

```bash
python scripts/occd_utils.py <command> [--options]
```

命令列表见 [references/occd-utils-commands.md](references/occd-utils-commands.md)。

## 状态数据库（occd.db）

Schema 定义见 [references/occd-db-schema.sql](references/occd-db-schema.sql)。

核心表关系：

```
requirements (req/ 文件)
  ├── reviews (review/ 文件，1:N)
  ├── sources (source/ 子任务 prompt，1:N)
  │     └── executions (task/ 报告，1:N，每次执行一条记录)
  └── task_events (所有状态变更历史，append-only)
```

需求状态：`new → reviewing → new → decomposed → done / failed`

source 状态：`pending → spawned → running → done / failed`

## 模板文件

- [references/source-task-template.md](references/source-task-template.md)：子任务 prompt 格式
- [references/task-report-template.md](references/task-report-template.md)：执行结果报告格式
- [references/review-template.md](references/review-template.md)：需求澄清文件格式
- [references/occd-db-schema.sql](references/occd-db-schema.sql)：SQLite DB schema

## 关键约定

- **需求文件只读**：所有代理禁止修改 `occd/req/`
- **DB 单写者**：只有主代理通过 `occd_utils.py` 写 `occd.db`，子代理通过命令接口汇报
- **DB 随仓库同步**：`commit-push --include-db` 将 `occd.db` 和代码一并提交
- **报告驱动决策**：主代理依据 `task/` 下的报告做下一步决策，不依赖子代理的口头描述
- **分支名 = 任务ID**：如 `req001-001-002-coding`
- **commit 格式**：`[occd] {req_filename}: <需求摘要>`
- **冲突处理**：早提交优先，冲突方重新运行 opencode（不是 rebase）
- **日志**：调用 `occd_utils.py log` 写入，不要直接写文件
