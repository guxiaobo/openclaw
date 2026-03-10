# OpenCode 命令指南

OCCD 中，coding / test-write / 需要修复的实现类任务应委托 OpenCode 完成。

---

## 配置来源

来自全局配置：

- `opencode_path`
- `opencode_args`

示例：

```json
{
  "opencode_path": "/usr/local/bin/opencode",
  "opencode_args": "run"
}
```

---

## 推荐做法

主代理或子代理不要手写散乱命令，优先调用。对于 OCCD 任务，推荐使用 `--prompt-file`，直接把 task prompt 文件交给 OpenCode，而不是把整段内容塞进命令行：

```bash
python scripts/occd_utils.py build-opencode-command \
  --config skills/occd/occd.json \
  --prompt-file /path/to/task-prompt.txt
```

仅在一次性短问答场景下，才使用 `--prompt` 直接传文本。

它会返回结构化结果：

- `argv`
- `command`
- `opencode_path`
- `opencode_args`

---

## 典型用法

### coding

```text
使用 OpenCode 在当前 worktree 中实现需求，不要越界修改无关模块。
```

### test-write

```text
使用 OpenCode 为刚才的实现补充测试，覆盖主要成功/失败场景。
```

### fix after failed test-run

```text
使用 OpenCode 根据失败日志修复实现问题，修复后不要自行更新 OCCD 状态，由子代理继续写报告与回填状态。
```
