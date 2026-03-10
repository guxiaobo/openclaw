# Finalize Checklist

当 requirement 的所有 task 任务都完成后，主代理按这份清单收尾。

- [ ] 当前 requirement 下所有 task 状态均为 `done`
- [ ] 已读取最后一轮 task report，没有遗漏 `partial` / `failure`
- [ ] coding / test-write 分支已按批次合并
- [ ] 最终测试已执行
- [ ] `run-tests` 结果不是 `detected=false`
- [ ] 提交范围已确认，不是误用 `git add -A`
- [ ] `occd.db` 如需同步，已使用 `--include-db`
- [ ] requirement 已更新为 `done`
- [ ] repo 锁已释放
