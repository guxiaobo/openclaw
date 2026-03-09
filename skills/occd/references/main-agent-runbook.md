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

### Step 2. 获取全局计划

```bash
python scripts/occd_controller.py poll-plan --work-dir <work_dir>
```

结果里每个 repo 会给出一个 action。

补充约束：controller 与主代理都应以 SQLite 状态为准，不扫描 `occd/report/` 目录来反推任务状态。

---

## action = analyze_requirement

1. 读取 `occd/req/<req_file>`
2. 判断是否明确
3. 不明确：调用 `write-review`
4. 明确：拆分任务并调用 `write-tasks`

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
  - `db-update-req-status --status new`
  - 重新分析需求
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
- 使用 `sessions_spawn(mode="run")`
- 把返回的 session key 回填到 `db-update-source-status --status spawned --session-key <key>`

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

当所有 source 都完成后：

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
