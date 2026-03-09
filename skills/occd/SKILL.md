---
name: occd
description: OCCD (OpenCode Continuous Developing) - 多仓库持续开发编排 Skill。用于 OpenClaw 主代理在收到“启动持续开发 / 自动编程流水线 / occd / auto-coder”类指令后，扫描多个 github-* 仓库中的 occd/req/ 需求文档，判断是否需要澄清，拆分为 coding / test-write / test-run 子任务，并通过 sessions_spawn 启动子代理按串行批次与批内并行方式持续执行，直到测试、合并、提交与推送完成。适用于需求转任务、多仓库自动开发、长流程编程协调、持续修复与回归测试。
---

# OCCD - OpenCode Continuous Developing

把你当成 **主编排代理**，不是直接写代码的执行器。

目标：收到用户“开始持续开发”的指令后，驱动多仓库 OCCD 流程：

1. 拉取仓库与扫描需求
2. 由主代理使用大语言模型判断需求是否明确
3. 由主代理使用大语言模型生成 review 或拆分 task 任务
4. 对当前串行批次并发启动子代理
5. 子代理委托 OpenCode 完成实际编码 / 测试编写 / 必要修复
6. 读取报告并推进下一步
7. 合并、测试、提交、推送

## 先读什么

按需读取：

- 命令参考：`references/occd-utils-commands.md`
- 主代理运行手册：`references/main-agent-runbook.md`
- 主代理需求分析提示：`references/llm-analysis-prompt.md`
- 子代理 prompt 模板：`references/subagent-prompt-template.md`
- OpenCode 命令指南：`references/opencode-command-guide.md`
- source 任务格式：`references/source-task-template.md`
- 报告格式：`references/task-report-template.md`
- 报告分诊：`references/report-triage-guide.md`
- 报告定位：`references/report-location-guide.md`
- 重试策略：`references/retry-policy.md`
- finalize 清单：`references/finalize-checklist.md`
- end-to-end 演练：`references/end-to-end-demo.md`
- review 格式：`references/review-template.md`
- DB schema：`references/occd-db-schema.sql`

## 设计原则

- req 内容是否明确、是否应生成 review、以及如何拆分 task，全部由主代理通过大语言模型判断
- Python 脚本不做需求理解，只做写文件、写数据库、git/worktree/test 等受控操作
- 子代理本身是任务执行编排者；实际编码、测试编写和需要修复的实现应委托 OpenCode 完成

## 目录约定

每个 `github-*` 仓库下的 `occd/` 是工作目录：

```text
github-<repo>/
  occd/
    req/       # 需求文档，只读
    task/      # 主代理生成的子任务定义文件
    report/    # 子代理写入的结构化报告
    review/    # 主代理生成的需求澄清文件
    logs/      # 原始运行日志
    occd.db    # SQLite 状态库
```

## 核心约束

- **绝不修改** `occd/req/` 里的需求文件
- **主代理负责编排与决策**；需求理解、review 决策、task 拆分与串并行规划必须由主代理通过大语言模型完成
- **子代理负责执行单个 task**；实际编码、测试编写与需要修复的实现工作必须委托 OpenCode 完成
- **子代理不得直接操作 SQLite 文件**；只能通过 `scripts/occd_utils.py` 暴露的受控命令更新状态
- **主代理与 controller 的状态判断以 SQLite 数据库为准**；`report/report-*.md` 只用于补充上下文、失败原因和变更摘要，不用于反推子任务状态
- **每个 coding / test-write 任务独立 worktree / 分支**
- **只提交本次任务相关文件**；不要默认 `git add -A`

## 启动方式

### 1) 初始化全局配置

```bash
python scripts/occd_utils.py config-init \
  --work-dir ~/projects \
  --poll-interval 300 \
  --max-agents 8 \
  --max-fix-retries 5 \
  --base-branch main \
  --auto-push \
  --opencode-path /usr/local/bin/opencode \
  --opencode-args run
```

### 2) 初始化目标仓库

对每个受管仓库执行（会同时初始化 `req/ task/ report/ review/ logs/` 目录与 `occd.db`）：

```bash
python scripts/occd_utils.py db-init --repo ~/projects/github-myapp
```

### 3) 轮询时获取下一步动作

优先使用 controller 辅助脚本：

```bash
python scripts/occd_controller.py poll-plan --work-dir ~/projects
```

或查看单仓库下一步：

```bash
python scripts/occd_controller.py repo-status --repo ~/projects/github-myapp
```

controller 不会替你做 agent 决策，只会告诉你：

- 哪个需求要分析
- 哪个 review 要检查回复
- 哪个串行批次可以 spawn
- 哪个需求可以 finalize

## 主代理标准流程

### 场景 A：收到“启动 occd / 持续开发”指令

1. 读取配置（`config-show`）
2. 对 work_dir 下仓库执行 `poll-plan`
3. 对每个 repo action 依次处理
4. 对需要 spawn 的批次调用 `sessions_spawn`

### 场景 B：发现新需求（action = `analyze_requirement`）

1. `git-pull --ff-only`
2. 读取 `occd/req/<reqname>`
3. 由主代理用大语言模型判断是否明确：
   - 不明确：由大语言模型生成澄清问题，再调用 `write-review`
   - 明确：由大语言模型拆分任务并规划串并行，再调用 `write-tasks`
4. 提示大语言模型时明确要求：写入 `occd/task/` 的每个 task 文件后续会被直接传给 OpenCode，并把文件全文作为提示词运行；因此每个 task 必须自包含、工作量尽量控制在 10 分钟以内，并优先拆成可并发执行的小任务

拆分任务时统一使用：

- `reqZZZ-XXX-YYY-coding`
- `reqZZZ-XXX-YYY-test-write`
- `reqZZZ-XXX-YYY-test-run`

规则：

- 同一 `XXX` 批次串行推进
- 同一 `XXX` 内不同 `YYY` 可并行
- `coding` 一般先于 `test-write`
- `test-run` 用于最终验证或回归验证

### 场景 C：review 中（action = `check_review_reply`）

调用：

```bash
python scripts/occd_utils.py check-new-commit \
  --repo <repo_path> \
  --file occd/req/<reqname> \
  --last-commit <last_req_commit>
```

若检测到新 commit：

- 将 requirement 状态重置为 `new`
- 重新进入需求分析

### 场景 D：可启动一批子任务（action = `spawn_batch`）

对该 `XXX` 批次内状态为 `pending` / `failed` 的 task 并发 `sessions_spawn`。
子代理 prompt 中必须明确：coding / test-write / 需要修复的实现工作由 OpenCode 完成，OpenCode 启动命令优先使用全局配置中的 `opencode_path` 与 `opencode_args`。

## 子代理 prompt 建议模板

向每个子代理提供：

- 仓库路径
- source 任务文件路径
- worktree 分支名
- 结果报告路径规范
- 必须调用的状态命令

示例骨架：

```text
你是 OCCD 执行子代理，请完成一个 source 任务。

仓库路径: <repo_path>
任务文件: <repo_path>/occd/task/<task_id>.md

要求：
1. 读取任务文件
2. 立即调用：
   python scripts/occd_utils.py db-update-source-status \
     --repo <repo_path> --task <task_id> --status running --session-key <session_key>
3. 如果是 coding / test-write：
   - create-worktree --reuse-if-exists
   - 在独立 worktree 中完成修改
4. 如果是 test-run：
   - 直接调用 run-tests
5. 写结构化报告到 occd/report/report-<task_id>-<timestamp>.md
6. 调用 db-add-execution 登记本次执行
7. 再调用 db-update-source-status 标记 done / failed
```

## 子代理任务类型约定

### coding

- 在独立 worktree 中实现功能
- 保持变更边界清晰
- 完成后写报告，不要只说“已完成”

### test-write

- 为对应代码写测试
- 保持与 coding 任务边界一致
- 如需新增测试依赖，写入报告

### test-run

- 调用 `run-tests`
- 若未识别测试框架，视为 **失败待人工处理**，不要当成通过

## 主代理如何读报告并决策

读取 `occd/report/report-{task_id}-*.md` 内容并结合 SQLite 状态后：

- `success`：标记 source 为 `done`
- `failure` 且未超过 `max_fix_retries`：重试该 source
- `failure` 且已达上限：标记 source / requirement 为 `failed`
- `partial`：主代理人工判断是否继续、补任务、或回滚

如果当前 `XXX` 全部 `done`：

1. 对 coding / test-write 批次执行 `merge-branches`
2. 若有冲突，重新 spawn 冲突对应任务
3. 若无冲突，推进到下一 `XXX`

如果所有批次都完成：

1. 运行最终 `test-run` 或汇总测试
2. 调用 `commit-push`
3. requirement 标记为 `done`

## Git 与测试命令

### 创建 worktree

```bash
python scripts/occd_utils.py create-worktree \
  --repo <repo_path> \
  --branch req001-001-001-coding \
  --reuse-if-exists
```

### 合并批次分支

```bash
python scripts/occd_utils.py merge-branches \
  --repo <repo_path> \
  --xxx 001 \
  --base-branch main
```

### 跑测试

```bash
python scripts/occd_utils.py run-tests --repo <repo_path>
```

### 只提交本次相关文件

```bash
python scripts/occd_utils.py commit-push \
  --repo <repo_path> \
  --message "[occd] feature.md: 实现登录功能" \
  --path src/auth.py \
  --path tests/test_auth.py \
  --include-db \
  --push
```

## 常见决策原则

### 需求不清时

优先生成 review，不要擅自脑补产品决策。

### 测试框架没识别出来时

当成 **未完成**，记录到报告，等待主代理补充判断。

### 合并冲突时

- 保留先合并分支
- 对冲突分支重新 spawn 基于最新代码的任务
- 不要强行 rebase 子代理分支来“糊过去”

### 提交时

- 默认只提交明确路径
- 只有用户明确接受时才使用 `--all`

## 推荐读取顺序

### 主代理首次接手一个 occd 请求时

1. 先读 `references/main-agent-runbook.md`
2. 分析 req 时读 `references/llm-analysis-prompt.md`
3. 再读 `references/occd-utils-commands.md`
4. 需要 spawn 子代理时读 `references/subagent-prompt-template.md`
5. 需要拼 OpenCode 命令时读 `references/opencode-command-guide.md`
6. 需要判断报告时读 `references/report-triage-guide.md`
7. 需要定位最新报告时读 `references/report-location-guide.md`
8. 收尾时读 `references/finalize-checklist.md`
9. 需要完整演练时读 `references/end-to-end-demo.md`

### 子代理执行 source 任务时

1. 读 source 任务文件本身
2. 再读 `references/task-report-template.md`
3. 如主代理 prompt 未写清，再参考 `references/subagent-prompt-template.md`

## controller 与 utils 的职责分工

### `scripts/occd_controller.py`

负责输出“下一步动作建议”：

- `repo-status`
- `global-status`
- `poll-plan`

### `scripts/occd_utils.py`

负责受控落地动作：

- config 管理
- DB 初始化与状态变更
- review/task/report 相关文件写入
- git worktree / merge / commit / push
- 测试执行
- repo 锁

## 当前实现边界

这个 skill 现在已经提供：

- 状态库
- 配置管理
- 控制器辅助输出
- 安全一些的 git / test / worktree 工具

但 **需求是否清晰、如何拆任务、何时 spawn、如何写子代理 prompt** 仍由你这个主代理判断与执行。

这正是本 skill 的设计目标：
**让 OpenClaw 主代理收到指令后，能够稳定地启动并持续驱动子代理开发流程，而不是把逻辑藏进一个黑盒脚本里。**
