# occd_utils.py 命令参考

所有命令输出 JSON，供主代理解析。

---

## DB 初始化

### db-init

初始化仓库的 occd.db，并在 .gitattributes 中写入 `occd/occd.db binary`（幂等，重复调用安全）。

```bash
python occd_utils.py db-init --repo ~/projects/github-myapp
```

输出：`{ "success": true, "db": "/path/to/occd/occd.db" }`

---

## 仓库扫描

### scan-repos

扫描 work_dir 下所有 `github-*` 仓库，比对 occd.db 后返回需要处理的需求列表（自动去重）。

```bash
python occd_utils.py scan-repos --work-dir ~/projects
```

输出：

```json
{
  "repos": [
    {
      "repo": "/path/to/github-myapp",
      "repo_name": "github-myapp",
      "req": "feature.md",
      "req_id": "github-myapp:feature.md",
      "status": "new",
      "hash_changed": false
    }
  ]
}
```

内部逻辑：计算每个 req 文件的 SHA256 content_hash，与 occd.db 比对，跳过已处理且 hash 未变且 status=done 的需求；hash 变化则重置为 new。

### git-pull

```bash
python occd_utils.py git-pull --repo ~/projects/github-myapp
```

### check-new-commit

检查需求文件是否有新 commit（用于 review 回复检测）。

```bash
python occd_utils.py check-new-commit \
  --repo ~/projects/github-myapp \
  --file occd/req/feature.md \
  --last-commit abc1234
```

输出：`{ "has_new_commit": true, "current_commit": "def5678" }`

---

## 需求管理（DB）

### db-upsert-req

新增或更新需求记录（hash 变化时自动重置状态为 new）。

```bash
python occd_utils.py db-upsert-req --repo <repo> --filename feature.md
```

输出：`{ "req_id": "github-myapp:feature.md", "action": "inserted|updated|skipped" }`

### db-update-req-status

```bash
python occd_utils.py db-update-req-status --repo <repo> --req-id <req_id> --status reviewing
python occd_utils.py db-update-req-status --repo <repo> --req-id <req_id> --status decomposed
python occd_utils.py db-update-req-status --repo <repo> --req-id <req_id> --status done
python occd_utils.py db-update-req-status --repo <repo> --req-id <req_id> --status failed --note "原因"
```

status 可选值：`new | reviewing | decomposed | done | failed`

### db-get-req

```bash
python occd_utils.py db-get-req --repo <repo> --req-id <req_id>
```

输出需求记录详情，包含关联的 reviews、sources 列表。

### db-list-pending-reqs

返回仓库中所有未完成的需求（status ∈ new, reviewing, decomposed）。

```bash
python occd_utils.py db-list-pending-reqs --repo <repo>
```

---

## 子任务管理（DB）

### db-upsert-source

新增子任务记录（`write-tasks` 内部已自动调用，一般不需要单独调用）。

```bash
python occd_utils.py db-upsert-source \
  --repo <repo> \
  --req-id github-myapp:feature.md \
  --task-id req001-001-001-coding \
  --task-type coding \
  --filename req001-001-001-coding.md
```

task-type 可选值：`coding | test-write | test-run`

### db-update-source-status

主代理和子代理均可调用。

```bash
# 主代理：spawn 前标记
python occd_utils.py db-update-source-status \
  --repo <repo> --task req001-001-001-coding --status spawned --session-key <key>

# 子代理：开始执行时标记
python occd_utils.py db-update-source-status \
  --repo <repo> --task req001-001-001-coding --status running

# 子代理：完成时标记
python occd_utils.py db-update-source-status \
  --repo <repo> --task req001-001-001-coding --status done

# 子代理：失败时标记
python occd_utils.py db-update-source-status \
  --repo <repo> --task req001-001-001-coding --status failed --note "opencode 超时"
```

status 可选值：`pending | spawned | running | done | failed`

### db-list-sources-by-xxx

返回某个串行批次（XXX）下所有子任务及其状态。

```bash
python occd_utils.py db-list-sources-by-xxx --repo <repo> --xxx 001
```

输出：`{ "sources": [{ "id": "req001-001-001-coding", "status": "done", ... }] }`

---

## 执行记录管理（DB）

### db-add-execution

子代理写完报告后，调用此命令在 DB 中登记本次执行记录。

```bash
python occd_utils.py db-add-execution \
  --repo <repo> \
  --task req001-001-001-coding \
  --report-file report-req001-001-001-coding-20260309T094500.md \
  --outcome success \
  --summary "实现了用户登录接口，新增 auth.py 和对应单测" \
  --session-key <key>
```

outcome 可选值：`success | failure | partial`

### db-list-executions

返回某个子任务的所有执行历史。

```bash
python occd_utils.py db-list-executions --repo <repo> --task req001-001-001-coding
```

---

## 文件写入

### write-review

生成需求澄清文件，同时更新 DB 状态为 reviewing，在 reviews 表写入记录。

```bash
python occd_utils.py write-review \
  --repo <repo> \
  --req feature.md \
  --questions '["问题1", "问题2"]'
```

### write-tasks

将主代理拆分的子任务写入 `occd/source/`，同时在 DB 中建立 sources 记录，需求状态更新为 decomposed。

```bash
python occd_utils.py write-tasks \
  --repo <repo> \
  --req feature.md \
  --tasks '[{
    "id": "req001-001-001-coding",
    "type": "coding",
    "summary": "实现登录接口",
    "details": "POST /api/login，返回 JWT token",
    "constraints": "使用 PyJWT>=2.0",
    "acceptance": "- [ ] 返回 200 和 token\n- [ ] 密码错误返回 401",
    "depends_on": [],
    "notes": ""
  }]'
```

task 对象字段说明：

- `id`：必填，格式 `reqZZZ-XXX-YYY-{type}`
- `type`：必填，`coding | test-write | test-run`
- `summary`：背景摘要
- `details`：详细要求
- `constraints`：约束条件
- `acceptance`：验收条件（Markdown checklist）
- `depends_on`：前置任务 ID 列表
- `notes`：参考信息

---

## Git 操作

### create-worktree

```bash
python occd_utils.py create-worktree --repo <repo> --branch req001-001-001-coding
```

输出：`{ "success": true, "worktree_path": ".../.occd-worktrees/github-myapp/req001-001-001-coding" }`

### remove-worktree

```bash
python occd_utils.py remove-worktree --repo <repo> --branch req001-001-001-coding
```

### merge-branches

按 commit 时间升序合并同一 XXX 下所有 `done` 状态的 coding/test-write 子任务分支。

```bash
python occd_utils.py merge-branches --repo <repo> --xxx 001
```

输出：`{ "conflicts": ["req001-001-002-coding"], "merged": ["req001-001-001-coding"] }`

### run-tests

自动识别测试框架并执行，供 test-run 类型子代理调用。

```bash
python occd_utils.py run-tests --repo <repo>
```

输出：`{ "passed": true, "output": "...", "command": ["pytest"] }`

### commit-push

提交代码并 push，`--include-db` 同时提交 occd.db（推荐每次状态批次变更后使用）。

```bash
python occd_utils.py commit-push \
  --repo <repo> \
  --message "[occd] feature.md: 实现登录功能" \
  --include-db
```

---

## 日志与汇总

### log

写入原始技术日志（追加到 `occd/logs/YYYY-MM-DD.log`）。

```bash
python occd_utils.py log --repo <repo> --level INFO --message "任务开始"
```

level 可选值：`INFO | WARN | ERROR`

### db-summary

输出所有仓库的需求/任务状态汇总（跨仓库全局视图）。

```bash
python occd_utils.py db-summary --work-dir ~/projects
```

输出示例：

```json
{
  "github-myapp": {
    "requirements": { "total": 3, "done": 2, "in_progress": 1, "failed": 0 },
    "sources": {
      "total": 8,
      "by_type": { "coding": 4, "test-write": 2, "test-run": 2 },
      "done": 7,
      "pending": 0,
      "failed": 1
    },
    "executions": { "total": 10, "success": 8, "failure": 2 }
  }
}
```
