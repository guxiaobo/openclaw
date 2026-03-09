# occd_utils.py / occd_controller.py 命令参考

当前实现分为两层：

- `scripts/occd_utils.py`：受控落地动作
- `scripts/occd_controller.py`：给主代理返回“下一步动作建议”

---

## 1. 全局配置

### config-init

初始化全局配置文件，默认路径：`~/.openclaw/occd-config.json`

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

### config-show

```bash
python scripts/occd_utils.py config-show
```

### config-set

```bash
python scripts/occd_utils.py config-set --poll-interval 120
python scripts/occd_utils.py config-set --max-agents 4 --no-auto-push
python scripts/occd_utils.py config-set --opencode-path /usr/local/bin/opencode --opencode-args run
```

---

## 2. 锁控制

### acquire-lock

获取仓库级主控锁，避免多个主代理同时编排同一 repo。

```bash
python scripts/occd_utils.py acquire-lock --repo ~/projects/github-myapp --holder main
```

### release-lock

```bash
python scripts/occd_utils.py release-lock --repo ~/projects/github-myapp
```

---

## 3. 仓库扫描与 review 检测

### db-init

初始化仓库的标准目录（`req/ task/ report/ review/ logs/`）、`occd.db`，并在 `.gitattributes` 写入：

```text
occd/occd.db binary
```

```bash
python scripts/occd_utils.py db-init --repo ~/projects/github-myapp
```

### git-pull

```bash
python scripts/occd_utils.py git-pull --repo ~/projects/github-myapp --ff-only
```

### scan-repos

扫描 work_dir 下所有 `github-*` 仓库中的 `occd/req/` 文件，与 DB 比对后返回需要处理的需求。

```bash
python scripts/occd_utils.py scan-repos --work-dir ~/projects
```

### check-new-commit

```bash
python scripts/occd_utils.py check-new-commit \
  --repo ~/projects/github-myapp \
  --file occd/req/feature.md \
  --last-commit abc1234
```

---

## 4. requirement 管理

### db-upsert-req

新增或更新需求记录；如果需求内容 hash 变化，会自动重置为 `new`。

```bash
python scripts/occd_utils.py db-upsert-req --repo <repo> --filename feature.md
```

### db-update-req-status

```bash
python scripts/occd_utils.py db-update-req-status \
  --repo <repo> \
  --req-id github-myapp:feature.md \
  --status reviewing
```

状态值：

- `new`
- `reviewing`
- `decomposed`
- `done`
- `failed`

### db-get-req

```bash
python scripts/occd_utils.py db-get-req --repo <repo> --req-id github-myapp:feature.md
```

### db-list-pending-reqs

```bash
python scripts/occd_utils.py db-list-pending-reqs --repo <repo>
```

---

## 5. review / source 写入

### write-review

生成需求澄清文件，并把 requirement 标记为 `reviewing`。
review 文件命名规则为：`<reqId>-XXX.md`，其中 `XXX` 为分析运行序号。

```bash
python scripts/occd_utils.py write-review \
  --repo <repo> \
  --req feature.md \
  --questions '["请明确登录方式", "请给出返回字段"]'
```

### write-tasks

将主代理拆分后的任务定义写入 `occd/task/`，并建立 DB 记录。

```bash
python scripts/occd_utils.py write-tasks \
  --repo <repo> \
  --req feature.md \
  --tasks '[
    {
      "id": "req001-001-001-coding",
      "type": "coding",
      "summary": "实现登录接口",
      "details": "POST /api/login，返回 JWT token",
      "constraints": "遵循现有鉴权结构",
      "acceptance": "- [ ] 返回 200 和 token\n- [ ] 密码错误返回 401",
      "depends_on": [],
      "notes": "参考 src/auth.py"
    }
  ]'
```

`task.id` 必须匹配：

```text
reqZZZ-XXX-YYY-coding
reqZZZ-XXX-YYY-test-write
reqZZZ-XXX-YYY-test-run
```

---

## 6. source / execution 状态管理

### db-update-source-status

主代理与子代理都可以调用，但子代理必须带 `--session-key`。

```bash
python scripts/occd_utils.py db-update-source-status \
  --repo <repo> \
  --task req001-001-001-coding \
  --status spawned \
  --session-key sess_xxx
```

```bash
python scripts/occd_utils.py db-update-source-status \
  --repo <repo> \
  --task req001-001-001-coding \
  --status running \
  --session-key sess_xxx
```

```bash
python scripts/occd_utils.py db-update-source-status \
  --repo <repo> \
  --task req001-001-001-coding \
  --status done \
  --session-key sess_xxx
```

状态值：

- `pending`
- `spawned`
- `running`
- `done`
- `failed`

### db-list-sources-by-xxx

```bash
python scripts/occd_utils.py db-list-sources-by-xxx --repo <repo> --xxx 001
```

### db-add-execution

登记一次执行记录。支持传真实开始/结束时间。

```bash
python scripts/occd_utils.py db-add-execution \
  --repo <repo> \
  --task req001-001-001-coding \
  --report-file report-req001-001-001-coding-20260309T094500.md \
  --outcome success \
  --summary "实现登录接口与单测" \
  --session-key sess_xxx \
  --started-at 2026-03-09T09:45:00+08:00 \
  --finished-at 2026-03-09T10:12:33+08:00
```

### db-list-executions

```bash
python scripts/occd_utils.py db-list-executions --repo <repo> --task req001-001-001-coding
```

### db-summary

```bash
python scripts/occd_utils.py db-summary --work-dir ~/projects
```

---

## 7. Git 操作

### create-worktree

支持：

- `--reuse-if-exists`：若 worktree 已存在则直接复用
- `--reset`：若 worktree / branch 冲突则先清理再创建

```bash
python scripts/occd_utils.py create-worktree \
  --repo <repo> \
  --branch req001-001-001-coding \
  --reuse-if-exists
```

### remove-worktree

支持清理 worktree，并可选删除任务分支。

```bash
python scripts/occd_utils.py remove-worktree \
  --repo <repo> \
  --branch req001-001-001-coding \
  --delete-branch \
  --base-branch main
```

### merge-branches

在指定 base branch 上，按 commit 时间顺序合并某个 `XXX` 批次所有已完成的 coding / test-write 任务分支。

```bash
python scripts/occd_utils.py merge-branches \
  --repo <repo> \
  --xxx 001 \
  --base-branch main
```

输出包含：

- `merged`
- `conflicts`
- `base_branch`

### commit-push

默认 **不会** 无脑 `git add -A`。建议显式指定路径：

```bash
python scripts/occd_utils.py commit-push \
  --repo <repo> \
  --message "[occd] feature.md: 实现登录功能" \
  --path src/auth.py \
  --path tests/test_auth.py \
  --include-db \
  --push
```

需要全量提交时才显式使用：

```bash
python scripts/occd_utils.py commit-push \
  --repo <repo> \
  --message "[occd] sync all changes" \
  --all \
  --push
```

---

## 8. 测试

### run-tests

自动识别测试框架并执行。若**无法识别测试框架**，将返回：

- `detected: false`
- `passed: false`
- `reason: no_test_framework_detected`

而不是误报成功。

```bash
python scripts/occd_utils.py run-tests --repo <repo>
```

---

## 9. controller 辅助命令

controller 只负责告诉主代理“下一步建议做什么”，不直接替代主代理做文本判断、sessions_spawn 或报告分诊。

controller 的任务状态判断只依赖 SQLite，不扫描 `occd/report/` 目录来推断状态。

同样地，需求理解、review 决策、task 拆分不由 Python 脚本完成，而是由主代理通过大语言模型完成；工具脚本只负责结构化落地。

### repo-status

返回单仓库的下一步动作：

```bash
python scripts/occd_controller.py repo-status --repo ~/projects/github-myapp
```

可能动作包括：

- `idle`
- `analyze_requirement`
- `check_review_reply`
- `spawn_batch`
- `await_batch_completion`
- `finalize_requirement`

### global-status

```bash
python scripts/occd_controller.py global-status --work-dir ~/projects
```

### poll-plan

先对每个仓库执行 `git-pull --ff-only` 和 `scan-repos`，再给出全局动作计划。

```bash
python scripts/occd_controller.py poll-plan --work-dir ~/projects
```

### batch-ready

检查某个 requirement 的某个 `XXX` 批次是否已经可以 spawn。

```bash
python scripts/occd_controller.py batch-ready \
  --repo <repo> \
  --req-id github-myapp:feature.md \
  --xxx 001
```

### finalize-ready

检查某个 requirement 是否已满足 finalize 条件。

```bash
python scripts/occd_controller.py finalize-ready \
  --repo <repo> \
  --req-id github-myapp:feature.md
```

### retry-plan

列出 requirement 下当前失败任务，供主代理结合 task report 做重试分诊。

```bash
python scripts/occd_controller.py retry-plan \
  --repo <repo> \
  --req-id github-myapp:feature.md
```
