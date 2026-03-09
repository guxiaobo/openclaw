# Auto-Coder Skill - 详细需求文档

> 状态：待确认  
> 版本：v0.3  
> 最后更新：2026-03-08

---

## 1. 概述

Auto-Coder 是一个自动化编程 Skill，能够：

- 持续监听多个 GitHub 仓库的需求目录
- 将随意格式的需求文档转化为结构化编码任务
- 使用 OpenCode 启动子代理完成编码
- 自动测试、修复，直到全部通过后 commit & push

---

## 2. 初始化参数

Skill 启动时需要以下配置参数：

| 参数              | 类型     | 必填 | 默认值     | 说明                                               |
| ----------------- | -------- | ---- | ---------- | -------------------------------------------------- |
| `work_dir`        | 路径     | ✅   | —          | Skill 主工作目录，所有 `github-*` 仓库放在此目录下 |
| `poll_interval`   | 整数(秒) | ✅   | —          | 轮询新需求的间隔，如 `300` = 5分钟                 |
| `max_agents`      | 整数     | ❌   | CPU 核心数 | 最大并发子代理数                                   |
| `max_fix_retries` | 整数     | ❌   | 5          | 测试失败后最大修复重试次数，超出后暂停该任务并记录 |

初始化示例：

```
/auto-coder init --work-dir ~/projects --poll-interval 300 --max-agents 8 --max-fix-retries 5
```

---

## 3. 目录结构规范

### 3.1 Skill 主目录

```
<work_dir>/
  github-<repo-name>/       ← 每个以 github- 开头的子目录视为一个代码仓库
  github-<repo-name-2>/
  ...
```

### 3.2 每个仓库目录结构

```
github-<repo-name>/
  <源代码...>               ← 正常 git 仓库内容
  occd/                     ← Auto-Coder 所有工作目录（统一放这里）
    req/                    ← 需求来源目录（只读，各代理不得修改）
      <需求文件>.md / .txt
    source/                 ← 编码任务文档（由主代理生成）
      coding-XXX-YYY.md
    test/                   ← 测试任务文档（由主代理生成）
      test-XXX-YYY.md
    task/                   ← 任务状态记录（主代理维护）
      task-status.json
    review/                 ← 需求澄清文件（主代理生成，需求方回复）
      <reqname>-review-XXX.md
    logs/                   ← 运行日志（按日期存放）
      YYYY-MM-DD.log
```

---

## 4. 核心流程

### 4.1 轮询流程（主循环）

```
每隔 poll_interval 秒：
  对每个 github-* 仓库：
    1. git pull（拉取最新代码和需求）
    2. 扫描 occd/req/ 目录
    3. 对比 occd/task/task-status.json，找出新增或未处理的需求
    4. 检查处于 reviewing 状态的需求：
       - 通过 git log occd/req/<reqname> 判断需求文件是否有新 commit
       - 有新 commit → 重新进入「需求分析流程」
    5. 对每个新需求 → 进入「需求分析流程」
```

### 4.2 需求分析流程

```
1. 主代理读取需求文档（md/txt，格式随意）
2. 分析需求是否明确：
   - 明确 → 进入「任务分解流程」
   - 不明确 → 生成 occd/review/<reqname>-review-XXX.md
              在 task-status.json 中标记状态为 reviewing
              等待需求方修改需求文件并 push 新 commit
              （XXX 为递增序号，数字越大越新）
3. 循环直到需求明确
```

> **Review 回复检测机制：** 主代理在每次轮询时，检查处于 `reviewing` 状态的需求，
> 通过 `git log occd/req/<reqname>` 判断需求文件自上次检查后是否有新 commit（对比 `last_req_commit`）。
> 有新 commit 则视为需求方已回复，重新触发需求分析。

### 4.3 任务分解流程

```
1. 主代理将需求转化为编码任务文档
   文件名：coding-XXX-YYY.md
   - XXX：主任务序号（必须串行，数字越大越新）
   - YYY：子任务序号（可并行，数字越大越新）
   保存到：occd/source/

2. 同时生成对应的测试任务文档
   文件名：test-XXX-YYY.md
   保存到：occd/test/

3. 更新 occd/task/task-status.json 记录状态
```

### 4.4 编码执行流程

```
按 XXX（主任务序号）串行执行：
  同一 XXX 下的所有 YYY 子任务并行执行（受 max_agents 限制）：
    对每个 coding-XXX-YYY.md：
      1. 创建 git worktree，分支名 = coding-XXX-YYY
      2. 启动独立 OpenCode session（build agent）
      3. 子代理在独立分支上完成编码
      4. 子代理完成后通知主代理

  所有并行子任务完成后：
    主代理合并各分支（见合并策略）
    进入「测试流程」
```

### 4.5 合并策略

```
- 按 commit 时间先后排序，时间早的优先合并
- 出现冲突时：
  1. 保留先提交的分支内容
  2. 其余冲突分支基于合并后最新代码重新触发编码（重启子代理）
  3. 循环直到无冲突
```

### 4.6 测试流程

```
1. 主代理自动识别项目测试框架
   优先级：需求文档中指定 > 自动识别
   自动识别规则（按优先级）：
   - package.json → jest / vitest / mocha
   - pytest.ini / pyproject.toml → pytest
   - go.mod → go test
   - Cargo.toml → cargo test
   - 其他 → 主代理综合判断

2. 执行测试
3. 测试通过 → 进入「最终提交流程」
4. 测试失败：
   a. 将失败详情写入 GitHub Issue
   b. 检查当前修复次数是否已达 max_fix_retries：
      - 未达上限 → 启动修复子代理（独立 OpenCode session）→ 修复完成后重新测试
      - 已达上限 → 将该任务标记为 failed，记录到 occd/task/task-status.json，暂停该任务
                   等待人工介入（可通过 status 命令查看）
5. 循环直到全部通过或达到重试上限
```

### 4.7 最终提交流程

```
1. 所有测试通过
2. 主代理执行 git commit
   commit message 格式：[auto-coder] coding-XXX: <需求摘要>
3. git push 到 remote（跟随仓库现有默认分支，如 main / master）
4. 更新 occd/task/task-status.json 标记任务 done
5. 清理 git worktree
```

---

## 5. 任务状态记录

`occd/task/task-status.json` 格式：

```json
{
  "requirements": {
    "<reqname>": {
      "status": "pending | reviewing | in_progress | testing | done | failed",
      "review_rounds": 0,
      "last_req_commit": "abc1234",
      "tasks": ["coding-001-001", "coding-001-002"],
      "created_at": "2026-03-08T12:00:00Z",
      "updated_at": "2026-03-08T14:00:00Z"
    }
  },
  "tasks": {
    "coding-001-001": {
      "status": "pending | coding | merging | testing | done | failed",
      "branch": "coding-001-001",
      "agent_session": "...",
      "fix_rounds": 0,
      "created_at": "...",
      "updated_at": "..."
    }
  }
}
```

> `last_req_commit`：记录需求文件最后一次处理时的 commit hash，用于检测需求方是否有新回复。

---

## 6. Review 文件规范

`occd/review/<reqname>-review-XXX.md` 格式：

```markdown
# 需求澄清请求 #XXX

> 需求文件：<reqname>
> 生成时间：2026-03-08 14:00

## 不明确的内容

1. [问题描述]
2. [问题描述]

## 如何回复

请直接修改需求文件 `occd/req/<reqname>` 补充说明后 git push，
主代理将在下次轮询时自动检测到更新并重新分析。
```

---

## 7. 命令接口

| 命令                                | 说明                             |
| ----------------------------------- | -------------------------------- |
| `auto-coder init [options]`         | 初始化 skill，设置工作目录和参数 |
| `auto-coder start`                  | 启动主轮询循环                   |
| `auto-coder stop`                   | 停止主循环                       |
| `auto-coder pause [--repo <name>]`  | 暂停所有或指定仓库的处理         |
| `auto-coder resume [--repo <name>]` | 恢复所有或指定仓库的处理         |
| `auto-coder status [--repo <name>]` | 查看所有或指定仓库的任务进度     |

`status` 输出示例：

```
github-myapp:
  需求: feature-login.md → in_progress (coding-001)
  任务: coding-001-001 [coding] | coding-001-002 [coding]
  需求: feature-export.md → reviewing (等待需求方回复 review-002.md)

github-backend:
  需求: bugfix-api.md → testing (fix_rounds: 2/5)
```

---

## 8. 并发控制

- 同时运行的子代理数量 ≤ `max_agents`
- 超出限制的任务进入队列等待
- 每个子代理绑定独立的 git worktree 和 OpenCode session

---

## 9. OpenCode 集成

- 使用本地默认安装的 OpenCode
- 主代理使用 **plan agent**（规划和协调）
- 子代理使用 **build agent**（编码实现）
- 每个子任务为独立 session，互不干扰

---

## 10. 日志规范

- 日志路径：`<repo_dir>/occd/logs/YYYY-MM-DD.log`
- 每条日志包含：时间戳、仓库名、任务ID、事件类型、详情
- 日志级别：INFO / WARN / ERROR

---

_所有遗留问题已确认，文档版本：v0.3，待最终确认后开始创建 Skill。_
