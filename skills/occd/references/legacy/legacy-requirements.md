# Legacy requirements

这份文档是 OCCD / auto-coder 的早期需求草案，保留仅供历史参考。

**不要把它当成当前实现依据。**

当前实现以以下文件为准：

- `SKILL.md`
- `scripts/occd_utils.py`
- `scripts/occd_controller.py`
- `references/occd-utils-commands.md`
- `references/occd-db-schema.sql`

---

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

```text
/auto-coder init --work-dir ~/projects --poll-interval 300 --max-agents 8 --max-fix-retries 5
```

---

## 3. 目录结构规范

### 3.1 Skill 主目录

```text
<work_dir>/
  github-<repo-name>/
  github-<repo-name-2>/
```

### 3.2 每个仓库目录结构

```text
github-<repo-name>/
  <源代码...>
  occd/
    req/
    source/
    test/
    task/
    review/
    logs/
```

---

## 4. 核心流程

此处略。当前实现已迁移到 SQLite + source/task 报告模型。
