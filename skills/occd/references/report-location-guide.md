# Report 定位指南

report 文件只是补充上下文，不负责定义任务状态。

---

## 原则

- 任务状态：看 SQLite
- 报告内容：看 `report/report-*.md`
- 报告定位：优先通过 executions 表，不要靠目录扫描瞎猜

---

## 获取最新报告

```bash
python scripts/occd_utils.py latest-report \
  --repo <repo> \
  --task req001-001-001-coding
```

返回：

- `report_file`
- `outcome`
- `summary`
- `session_key`
- `started_at`
- `finished_at`

---

## 什么时候看报告

- 子任务刚完成，需要了解具体改动
- 失败后，需要决定是否重试
- `partial` 时，需要判断是否应转 review 或补 task

---

## 不要做的事

- 不要遍历 `occd/report/` 文件名来推断状态
- 不要因为最新 report 是 success 就忽略 SQLite 里仍可能是 failed / running 的情况
