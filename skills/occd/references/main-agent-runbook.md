# 主代理运行手册

这是给 OpenClaw 主代理的实操手册，不是系统设计说明。

---

## 入口场景：用户说“启动 OCCD / 开始持续开发”

### Step 1. 读取配置

```bash
python scripts/occd_utils.py config-show
```

拿到：

- `work_dir`
- `poll_interval`
- `max_agents`
- `max_fix_retries`
- `base_branch`
- `auto_push`
- `opencode_path`
- `opencode_args`

### Step 2. 获取全局计划

```bash
python scripts/occd_controller.py poll-plan --work-dir <work_dir>
```

结果里每个 repo 会给出一个 action；当同一轮发现多个 `new` requirement 时，应优先进入 `preflight_batch`。

补充约束：controller 与主代理都应以 SQLite 状态为准，不扫描 `occd/report/` 目录来反推任务状态。

---

## action = preflight_batch

1. 收集本轮所有 `status=new` 的 requirement
2. 对每个 requirement 读取：
   - 当前 req 文件全文
   - `processed_commit -> latest_commit` 的提交区间（通过 `req-history`）
3. 由主代理先做跨需求预检：
   - 是否存在整体冲突/矛盾
   - 哪些 requirement 自身信息不足
4. 对存在冲突的 requirement，分别生成 review，并通过 `db-block-req` 标记为 `blocked`
5. 对可安全继续的 requirement，再按 commit 时间顺序进入 `analyze_requirement`

## action = analyze_requirement

1. 读取 `occd/req/<req_file>` 当前全文
2. 调用 `req-history` 读取从 `processed_commit` 到 `latest_commit` 的完整提交区间
3. 由主代理使用大语言模型分析需求
4. 判断是否明确
5. 不明确：由大语言模型决定需要澄清，并生成 review 内容；然后调用 `write-review`
6. 明确：由大语言模型完成任务拆分与串并行规划，再调用 `write-tasks`

关键约束：

- Python 脚本不负责理解需求，也不负责自动拆分任务
- Python 脚本只负责把主代理已经决定好的 task / review 落库、写文件、更新状态
- 若原需求存在关键不确定项，主代理必须优先生成 review，而不是勉强拆任务

### 给大语言模型的分析提示要点

主代理在分析 req 时，应明确提示大语言模型：

- 不能只看最后一个 commit，必须结合整个未处理 commit 区间
- 同一 req 文件的多个未处理 commit，优先视作一个累计区间来分析
- 需求拆分必须按业务域/业务能力划分，不要把“骨架搭建”当作完整需求的替代
- 若第一步只能做骨架或基础设施准备，必须同时给出后续完整任务树，让用户一开始就看到完整交付路径
- 对 worktree 内完成但尚未 merge 的成果，报告里必须明确写成“仅在 task worktree/分支可见”，不得表述成“主仓库已可见/已完成落地”
- 先判断需求是否足够明确
- 若存在关键不确定项，先输出 review 问题，不要拆分 task
- 只有在需求明确时，才输出 task 拆分结果
- task 拆分要同时给出串行批次 `XXX` 与批内并行 `YYY` 的建议
- `coding`、`test-write`、`test-run` 的边界要清楚
- 写入 `occd/task/` 的每个 task 文件会在后续被直接交给 OpenCode，并把文件全文当作提示词运行，因此 task 内容必须完整、自包含、能直接执行
- 每个 task 的目标工作量尽量控制在 10 分钟以内
- 优先把任务拆成可并发执行的小任务，除非存在明确依赖关系

### 需求拆分建议

优先拆成三类任务：

- `coding`
- `test-write`
- `test-run`

原则：

- 功能实现与测试编写分开
- 能并行的放同一 `XXX` 批次下不同 `YYY`
- 真正依赖前置输出的，放到后续 `XXX`

---

## action = check_review_reply

调用：

```bash
python scripts/occd_utils.py check-new-commit \
  --repo <repo_path> \
  --file occd/req/<req_file> \
  --last-commit <last_req_commit>
```

- 有新 commit：
  - requirement 状态重置为 `new`
  - 更新 `pending_from_commit/pending_to_commit/pending_commit_count`
  - 重新参与 preflight / analyze_requirement
- 没有：保持 `reviewing`

---

## action = spawn_batch

### 启动前检查

1. 控制总并发不要超过 `max_agents`
2. 为 repo 获取主控锁：

```bash
python scripts/occd_utils.py acquire-lock --repo <repo_path>
```

3. 先把这些 source 标记为 `spawned`

### spawn 方法

对本批次每个 task：

- 根据 `references/subagent-prompt-template.md` 生成 prompt
- prompt 中明确要求子代理实际调用 OpenCode 执行 coding / test-write / 需要修复的测试任务
- OpenCode 启动命令优先使用配置中的 `opencode_path` 与 `opencode_args`
- 使用 `sessions_spawn(mode="run")`
- 把返回的 session key 回填到 `db-update-task-status --status spawned --session-key <key>`

### 批内并发建议

- 同一 `XXX` 批次内可并发
- 但如果多个任务可能改同一模块，宁可少并发，不要硬并发

---

## action = await_batch_completion

检查该批次下任务：

- 只要存在 `running / spawned`，就继续等待
- 对已结束任务，读取对应 `occd/report/report-*.md` 了解细节；但是否 running/done/failed 一律以 SQLite 为准
- 如果报告 outcome = `failure`，生成 retry plan

---

## action = finalize_requirement

当所有 task 都完成后：

1. 如涉及 coding / test-write 分支，先 merge：

```bash
python scripts/occd_utils.py merge-branches --repo <repo_path> --xxx <xxx> --base-branch <base_branch>
```

2. 跑最终测试
3. 选择性提交：

```bash
python scripts/occd_utils.py commit-push \
  --repo <repo_path> \
  --message "[occd] <req_file>: <摘要>" \
  --path <changed-path> \
  --include-db \
  --push
```

4. 更新 requirement 为 `done`
5. 释放 repo 锁

```bash
python scripts/occd_utils.py release-lock --repo <repo_path>
```

---

## 失败 / 重试策略

### coding / test-write 失败

- 若 `retry_count < max_fix_retries`：重试
- 若超过上限：
  - source 标记 `failed`
  - requirement 标记 `failed`
  - 留待人工介入

### test-run 失败

先判断：

- 是实现 bug？→ 回退到对应 coding 任务重修
- 是测试本身问题？→ 回到 test-write 任务修复
- 是环境 / 框架识别失败？→ 人工介入或补充任务

---

## 不要做的事

- 不要直接改 `occd/req/`
- 不要让子代理直接写 sqlite
- 不要把 `run-tests` 无法识别框架当作通过
- 不要默认 `git add -A`
- 不要在未确认 base branch 的情况下直接 merge
