# 报告解析与分诊指南

主代理读取 `occd/task/report-*.md` 后，按下面规则分诊。

---

## outcome = success

动作：

1. 确认 source 状态为 `done`
2. 记录 execution
3. 若当前 `XXX` 已全 done：推进到下一批次
4. 若已是最后批次：进入 finalize

---

## outcome = partial

常见含义：

- 做完了部分实现
- 需求不清无法继续
- 外部依赖缺失
- 测试环境不完整

动作：

1. 读“是否需要主代理介入”段落
2. 若是需求问题：生成 review 或补拆任务
3. 若是环境问题：主代理记录阻塞，暂停 requirement
4. 不要自动把 `partial` 当 `success`

---

## outcome = failure

### 先看失败类型

#### A. 代码实现失败

表现：

- 编译不通过
- 测试失败
- 接口行为不符合验收条件

动作：

- 若未超重试上限：重试同任务
- 若已超上限：标记 requirement `failed`

#### B. 合并冲突引发失败

表现：

- 子代理说明基线过旧
- merge 阶段出现 conflict

动作：

- 先合并已完成分支
- 对冲突分支基于最新 base 重新 spawn

#### C. 测试框架识别失败

表现：

- `run-tests` 返回 `detected=false`

动作：

- 不自动重试 test-run
- 让主代理补充明确测试命令，或新增 source 任务

#### D. 需求问题

表现：

- 子代理无法判断真实业务意图
- 验收条件本身矛盾

动作：

- 生成 review
- requirement 转 `reviewing`

---

## 建议输出给用户/主代理的最小结论

每次分诊至少得出：

- 当前任务是否完成
- 下一步是谁做（主代理 / 子代理 / 人工）
- 是否需要重试
- 是否需要更新 requirement 状态
