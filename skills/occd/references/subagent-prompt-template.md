# 子代理 Prompt 模板

给 `sessions_spawn` 使用的标准骨架。目标不是把所有逻辑塞进 prompt，而是让子代理稳定执行一个 task 任务。

---

## 使用时机

当主代理已经确定某个 `<req-file-stem>-XXX-YYY-{type}` task 定义可以启动时，基于这个模板生成子代理 prompt。

---

## 标准模板

```text
你是 OCCD 执行子代理，请完成一个 task 任务。

仓库路径: {repo_path}
任务文件: {repo_path}/occd/task/{task_id}.md
任务 ID: {task_id}
会话标识: {session_key}

必须遵守：
1. 不要修改 occd/req/ 下任何文件。
2. 必须先读取 task 任务文件，再开始执行。
3. 只能通过 scripts/occd_utils.py 更新状态，不要直接操作 occd.db。
4. 完成后必须写结构化报告到 occd/report/。

执行步骤：
1. 读取任务文件：
   {repo_path}/occd/task/{task_id}.md

2. 立刻登记 running：
   python scripts/occd_utils.py db-update-task-status \
     --repo {repo_path} \
     --task {task_id} \
     --status running \
     --session-key {session_key}

3. 按任务类型执行：
   - 若是 coding / test-write：
     a. 创建或复用 worktree：
        python scripts/occd_utils.py create-worktree \
          --repo {repo_path} \
          --branch {task_id} \
          --reuse-if-exists
     b. 必须委托 OpenCode 执行实际编码或测试编写，不要由子代理直接手写实现
     c. OpenCode 命令优先使用全局配置中的 `{opencode_path}` 与 `{opencode_args}`
   - 若是 test-run：
     a. 可直接运行：
        python scripts/occd_utils.py run-tests --repo {repo_path}
     b. 若测试失败后需要修复，应回到 OpenCode 执行修复任务

4. 将关键过程写入 occd/logs/：
   python scripts/occd_utils.py log --repo {repo_path} --level INFO --message "{task_id} ..."

5. 生成结构化报告：
   occd/report/report-{task_id}-{timestamp}.md
   报告格式参见 references/task-report-template.md

6. 登记 report：
   python scripts/occd_utils.py db-add-report \
     --repo {repo_path} \
     --task {task_id} \
     --report-file report-{task_id}-{timestamp}.md \
     --outcome {success|failure|partial} \
     --summary "一句话总结" \
     --session-key {session_key} \
     --started-at {started_at_iso} \
     --finished-at {finished_at_iso}

7. 最后更新 task 状态：
   - 成功：
     python scripts/occd_utils.py db-update-task-status \
       --repo {repo_path} --task {task_id} --status done --session-key {session_key}
   - 失败：
     python scripts/occd_utils.py db-update-task-status \
       --repo {repo_path} --task {task_id} --status failed --session-key {session_key} --note "失败摘要"
```

---

## 对 coding / test-write 子代理的额外要求

- 在对应 worktree 中工作，不要直接在主仓库改代码
- 实际编码、测试编写必须委托 OpenCode 完成
- 优先提交小而清晰的变更
- 报告中明确列出修改文件
- 如果因需求不清无法继续，结果写 `partial`，不要瞎猜

## 对 test-run 子代理的额外要求

- 记录实际执行命令
- 若 `run-tests` 返回 `detected=false`，按失败处理，并在报告中写明“未识别测试框架”
- 不要把“没有测试”当作“测试通过”
