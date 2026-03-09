# 需求澄清文件模板

> 文件名规则：`occd/review/<reqname>-review-XXX.md`
> XXX 为递增序号，数字越大越新

---

# 需求澄清请求 #XXX

> **需求文件**：`occd/req/<reqname>`  
> **生成时间**：YYYY-MM-DD HH:MM  
> **状态**：等待回复

---

## 不明确的内容

1. （问题描述）
2. （问题描述）

---

## 如何回复

请直接修改需求文件 `occd/req/<reqname>`，补充说明后执行：

```bash
git add occd/req/<reqname>
git commit -m "clarify: <reqname> - 补充说明XXX"
git push
```

主代理将在下次轮询时自动检测到更新并重新分析。

---

<!-- 以上内容由 auto-coder 自动生成，请勿修改分隔线以上内容 -->
