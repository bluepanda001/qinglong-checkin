# QingLong Check-in

一个面向青龙面板的轻量每日签到脚本，目前支持：

- NodeSeek
- V2EX
- 恩山无线论坛
- 企业微信群机器人汇总通知

当前版本：**v1.2.0**

## 设计目标

- 不打印 Cookie
- 不使用账号密码登录
- 不自动安装 Python 依赖
- 不使用 `eval` / `exec` / `os.system` / `subprocess`
- 不加载第三方 `notify.py`
- 各站 Cookie 只发送给各自官方域名
- 企业微信通知只发送脱敏后的签到结果
- 恩山运行过程中刷新的 Cookie 会保存在本地状态文件中，权限为 `0600`

> 任何自动化脚本都不能承诺绝对安全。建议自行审查源码，并妥善保护青龙环境变量和服务器权限。

## 依赖

请在青龙 **依赖管理 → Python3** 中手动安装：

```text
requests
curl_cffi
```

脚本不会自行执行 `pip install`。

## 环境变量

| 变量 | 必需 | 说明 |
|---|---|---|
| `NS_COOKIE` | NodeSeek 使用时 | NodeSeek 完整 Cookie |
| `V2EX_COOKIES` | V2EX 使用时 | V2EX 完整 Cookie |
| `ENSHAN_COOKIE` | 恩山使用时 | 恩山完整 Cookie |
| `WECOM_WEBHOOK_KEY` | 可选 | 企业微信群机器人 Webhook 中 `?key=` 后面的值 |
| `CHECKIN_DELAY_MAX` | 可选 | 启动随机延迟秒数，默认 300；设为 0 可关闭 |
| `HTTP_TIMEOUT` | 可选 | HTTP 超时，默认 25 秒 |

至少配置一个站点的 Cookie 即可运行。

兼容变量名：

- `NODESEEK_COOKIE`
- `V2EX_COOKIE`
- `enshan_cookie`
- `QYWX_WEBHOOK_KEY`

## 多账号

每个站点支持多账号，可使用以下任一种方式：

```text
cookie1|||cookie2
```

或者每个 Cookie 一行。

脚本不会使用 `&` 作为账号分隔符，避免误拆 Cookie 内容。

## 青龙定时任务

推荐每天北京时间 09:20 执行：

```cron
20 9 * * *
```

如果脚本由青龙订阅拉取，常用命令：

```bash
task safe_checkin_wecom.py
```

如果是手动放入脚本目录，也可以按实际路径执行：

```bash
python3 /ql/data/scripts/safe_checkin_wecom.py
```

## 青龙订阅自动更新

仓库地址：

```text
https://github.com/bluepanda001/qinglong-checkin.git
```

建议：

- 更新订阅：每天 03:15，例如 `15 3 * * *`
- 签到任务：每天 09:20，例如 `20 9 * * *`
- 只拉取/执行 `safe_checkin_wecom.py`
- Cookie、Webhook Key 只保存在青龙环境变量中，不写入 GitHub

## 恩山状态文件

v1.2.0 起，脚本会在本地保存恩山运行过程中刷新的 Cookie：

```text
.safe_checkin_state.json
```

该文件权限会设置为 `0600`，不会打印到日志，也不会发送到企业微信，并已加入 `.gitignore`。

如果你手动更新了 `ENSHAN_COOKIE`，脚本会根据 Cookie 指纹自动放弃旧缓存并使用新 Cookie。

## 企业微信机器人

只填写 Webhook 中 `key=` 后的值：

```text
WECOM_WEBHOOK_KEY=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

不要填写完整 Webhook URL。

## 更新记录

见 [CHANGELOG.md](CHANGELOG.md)。

## 安全提示

不要把以下内容提交到 GitHub：

- 任意站点 Cookie
- 企业微信 Webhook Key
- `.safe_checkin_state.json`
- `.env`
- 青龙备份文件或日志
