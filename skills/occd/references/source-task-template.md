# 子任务 Prompt 模板

# 文件名：<req-file-stem>-XXX-YYY-{type}.md

# 位置：occd/task/

# 写入者：主代理

# 读取者：子代理（只读）

---

task_id: feature-calc-001-001-coding # 任务唯一 ID
task_type: coding # coding | test-write | test-run
req_file: feature.md # 来源需求文件
xxx: "001" # 串行批次（同批次内任务串行）
yyy: "001" # 并行子序号（同 XXX 内并行）
depends_on: [] # 前置任务 ID 列表（空表示无依赖）

---

## 背景

> 简要说明这个任务要解决的问题，帮助子代理理解上下文。
> 如果本任务只是骨架/前置准备，必须明确它只是前置步骤，不代表完整需求已落地。

## 任务要求

> 详细描述需要做什么。对于不同类型：
>
> - coding：描述要实现的功能、接口、数据结构等
> - test-write：描述要为哪些代码编写测试，覆盖哪些场景
> - test-run：描述要执行哪些测试，预期通过标准

## 约束条件

> 技术选型、代码规范、不能修改的文件等限制。

## 验收条件

> 明确的可验证标准，子代理需在报告中逐条确认。

- [ ] 条件1
- [ ] 条件2

## 参考信息

> 相关文件路径、接口文档、已有代码示例等。

## 后续任务树

> 如果本任务不是完整交付的最后一步，必须列出后续任务树或后续 task 组，说明完整需求还会怎样推进。

## 报告要求

完成后将执行结果报告写入：
`occd/report/report-{task_id}-{YYYYMMDDTHHMMSS}.md`

格式见 `references/task-report-template.md`。
