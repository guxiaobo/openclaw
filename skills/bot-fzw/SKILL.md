# bot-fzw — 全国法院失信被执行人查询 Skill

查询 [全国法院综合查询系统](https://zxgk.court.gov.cn/zhzxgk/)，支持按姓名、身份证号查询失信被执行人，结果自动存入本地 SQLite 数据库 `fzw.db`。

---

## 安装依赖

```bash
cd skills/bot-fzw
python3.10 -m venv venv
venv/bin/pip install -r requirements.txt
```

> 强烈建议使用 Python 3.10。当前依赖组合里，`ddddocr` 在 3.13/3.14 上兼容性不稳定，容易出现导入失败或 Pillow 兼容问题。

---

## 用法

```bash
# 按身份证查询（个人）
venv/bin/python3 fzw.py --card 110101199001011234

# 按姓名+身份证查询
venv/bin/python3 fzw.py --name 张三 --card 110101199001011234

# 按企业名称查询
venv/bin/python3 fzw.py --name 某某科技有限公司

# 只获取第一页（不翻页）
venv/bin/python3 fzw.py --card 110101199001011234 --no-paging

# 查看数据库记录（默认20条）
venv/bin/python3 fzw.py --show

# 查看最近50条
venv/bin/python3 fzw.py --show --limit 50

# 保存验证码图片，便于排查是“拿不到图”还是“OCR识别差”
venv/bin/python3 fzw.py --card 110101199001011234 --debug --save-captcha
```

---

## 数据库说明

每次查询都会写入新记录（含时间戳），不会覆盖历史记录，适合追踪状态变化。

数据库路径：`skills/bot-fzw/fzw.db`

| 字段         | 说明                        |
| ------------ | --------------------------- |
| queried_at   | 查询时间（精确到秒）        |
| query_name   | 查询的姓名/企业名           |
| query_card   | 查询的身份证号              |
| result_count | 本次查询结果总数            |
| rec_name     | 被执行人名称                |
| card_num     | 被执行人身份证/统一信用代码 |
| exec_court   | 执行法院                    |
| case_code    | 案号                        |
| duty         | 履行情况                    |
| raw_json     | 原始 JSON 备份              |

---

## 技术说明

- **验证码识别**：ddddocr 本地识别，建议 Python 3.10 + `ddddocr==1.0.6` + `Pillow<10`
- **重试策略**：验证码请求与识别会自动重试，多次失败时终止查询，不再误写“无结果”
- **排障增强**：支持 `--save-captcha` 将验证码图片落盘到 `captcha-debug/`，方便人工核对 OCR 质量
- **反爬处理**：完整模拟浏览器 headers，维护 WAF session cookie
- **接口**：
  - 主页：`GET /zhzxgk/`（获取 WAF cookie + JSESSIONID）
  - 验证码：`GET /zhzxgk/captcha.do?captchaId={id}&random={r}`
  - 验证码校验：`GET /zhzxgk/checkyzm?captchaId={id}&pCode={code}`
  - 查询：`POST /zhzxgk/searchZhcx.do`（返回 JSON）
- **限速**：若遭遇 IP 限速（502），脚本会自动等待后重试

---

## 注意事项

1. 该网站有 WAF 防护，短时间内频繁查询会触发 IP 封禁，建议查询间隔 ≥ 30 秒
2. 查询结果仅反映当前时刻数据，不同时间查询结果可能不同
3. 本工具仅供合法合规用途，请遵守相关法律法规
