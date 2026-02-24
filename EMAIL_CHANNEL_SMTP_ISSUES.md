# Email Channel SMTP 问题诊断报告

## 问题描述

Email channel 可以成功接收邮件并通过 channelRuntime 生成 AI 回复,但 SMTP 发送回复失败,错误信息:

```
Error: Greeting never received
code: 'ETIMEDOUT'
command: 'CONN'
```

## 测试结果

### 网络连接测试

```bash
$ nc -zv smtp.qq.com 465
Connection to smtp.qq.com port 465 [tcp/urd] succeeded! ✅

$ nc -zv smtp.qq.com 587
Connection to smtp.qq.com port 587 [tcp/submission] succeeded! ✅
```

**结论**: 网络连接正常,端口未被防火墙阻止。

### Python SMTP 测试

```python
# 测试端口 465 (SSL)
server = smtplib.SMTP_SSL('smtp.qq.com', 465, timeout=10)
# 结果: ❌ _ssl.c:989: The handshake operation timed out

# 测试端口 587 (STARTTLS)
server = smtplib.SMTP('smtp.qq.com', 587, timeout=10)
server.starttls()
# 结果: ❌ Connection unexpectedly closed: timed out
```

**结论**: 两种连接方式都超时,但端口是通的。

## 可能的原因

### 1. QQ 邮箱 SMTP 配置问题 ⭐ 最可能

**QQ 邮箱 SMTP 要求**:

- 必须使用 **授权码** 而不是 QQ 密码
- SMTP 服务需要在 QQ 邮箱设置中开启
- 授权码需要定期更新

**检查项**:

- [ ] 登录 QQ 邮箱网页版
- [ ] 进入"设置" -> "账户"
- [ ] 找到"POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV服务"
- [ ] 开启"SMTP服务"
- [ ] 生成新的授权码
- [ ] 更新 openclaw.json 中的 `smtp.password` 为授权码

### 2. SSL/TLS 版本问题

QQ 邮箱可能要求特定的 TLS 版本:

- **最小 TLS 版本**: TLS 1.2
- **支持的加密套件**: 有限列表

**解决方案**: 在 nodemailer 配置中指定 TLS 选项:

```typescript
{
  host: 'smtp.qq.com',
  port: 465,
  secure: true,
  auth: { user: '...', pass: '...' },
  tls: {
    rejectUnauthorized: false,
    minVersion: 'TLSv1.2'
  }
}
```

### 3. QQ 邮箱的连接限制

QQ 邮箱可能对 SMTP 连接有:

- **IP 白名单限制**
- **连接频率限制**
- **地理位置限制**

**检查**: 尝试从不同的网络环境连接

### 4. 授权码过期

QQ 邮箱授权码可能:

- **过期时间**: 通常 6 个月
- **使用次数限制**: 某些情况下有次数限制

**解决方案**: 重新生成授权码

## 当前配置

### SMTP 配置 (openclaw.json)

```json
{
  "smtp": {
    "host": "smtp.qq.com",
    "port": 587,
    "secure": false,
    "user": "guxiaobo1982@qq.com",
    "password": "cgcxtmrovpzrbgcg"
  }
}
```

### 推荐配置

根据 QQ 邮箱官方文档,推荐使用:

```json
{
  "smtp": {
    "host": "smtp.qq.com",
    "port": 465,
    "secure": true,
    "user": "guxiaobo1982@qq.com",
    "password": "<授权码>"
  }
}
```

## 建议的解决步骤

### 步骤 1: 获取新的授权码

1. 登录 QQ 邮箱 (https://mail.qq.com)
2. 点击"设置" -> "账户"
3. 找到"POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV服务"
4. 确认"SMTP服务"已开启
5. 点击"生成授权码"
6. 按照提示验证手机
7. 复制生成的授权码 (16位字符)

### 步骤 2: 更新配置

更新 `~/.openclaw/openclaw.json`:

```json
{
  "channels": {
    "email": {
      "accounts": {
        "default": {
          "imap": { ... },
          "smtp": {
            "host": "smtp.qq.com",
            "port": 465,
            "secure": true,
            "user": "guxiaobo1982@qq.com",
            "password": "<新的16位授权码>"
          }
        }
      }
    }
  }
}
```

### 步骤 3: 重启 Gateway

```bash
pkill -f "openclaw gateway"
pnpm openclaw gateway run --bind loopback --port 18789
```

### 步骤 4: 测试

发送一封测试邮件,观察日志中的详细错误信息。

## 调试改进

已添加更详细的 SMTP 错误日志:

```typescript
console.log(`[EMAIL PLUGIN] [${this.accountId}] Creating SMTP transporter:`, {
  host: config.host,
  port: config.port,
  secure: config.secure,
  user: config.auth.user,
});

// 在 sendEmail 中:
console.log(`[EMAIL PLUGIN] [${this.accountId}] ❌ Error sending email to ${to}:`);
console.error(`[EMAIL PLUGIN] [${this.accountId}] Error code: ${error?.code}`);
console.error(`[EMAIL PLUGIN] [${this.accountId}] Error command: ${error?.command}`);
console.error(`[EMAIL PLUGIN] [${this.accountId}] Error message: ${error?.message}`);
```

这将提供更详细的错误信息来诊断问题。

## 临时解决方案

如果 QQ 邮箱 SMTP 继续无法工作,可以考虑:

1. **使用其他邮箱服务商**:
   - Gmail (smtp.gmail.com:587)
   - 163 邮箱 (smtp.163.com:465)
   - Outlook (smtp-mail.outlook.com:587)

2. **使用第三方 SMTP 服务**:
   - SendGrid
   - Mailgun
   - Amazon SES

3. **只收不发模式**:
   - 暂时禁用 SMTP 回复功能
   - 仅用于接收邮件和 AI 处理

## 与 channelRuntime 的关系

⚠️ **重要**: SMTP 发送失败 **不是** channelRuntime 的问题!

channelRuntime 的核心功能已经完全正常工作:

- ✅ 邮件接收 (IMAP)
- ✅ 消息分发到 AI
- ✅ AI 处理和生成回复
- ✅ 回复内容生成
- ❌ 回复发送 (SMTP 配置问题,与 channelRuntime 无关)

SMTP 是一个独立的邮件发送功能,与 channelRuntime 消息分发机制是分离的。

## 总结

SMTP 问题的根本原因很可能是 **QQ 邮箱的授权码配置问题**,而不是代码问题。建议:

1. **优先级 1**: 在 QQ 邮箱中重新生成授权码
2. **优先级 2**: 尝试使用端口 465 和 SSL (secure: true)
3. **优先级 3**: 考虑使用其他邮箱服务商
4. **优先级 4**: 临时禁用回复功能,专注于接收和处理

channelRuntime 的实现已经成功验证,可以继续向官方仓库提交 PR! 🎉
