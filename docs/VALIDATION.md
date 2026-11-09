# 人工验收清单

v0.1.0 集成验收使用 Python 3.12.13：全量 `python3 -m pytest -q` **87 passed**；显式 `demo` 在 0.4 秒内完成微信/企微合成采集、导出、分类、模拟发送与二次增量去重；`doctor` 返回 `ok: true`（未配置真实源/目标的离线警告符合预期）；跟踪源码隐私审计为 **0 处问题**。未接入真实账号或真实推送，以下仍是部署者应执行的清单。

## 离线与规则

1. 按 QUICKSTART 生成合成导出，运行 doctor 与 dry-run，确认无需客户端、密钥或 agent。
2. 用合成用户、会话和关键词覆盖：自己 IGNORE、IGNORE 群 IGNORE、私聊默认 DIGEST、@自己默认 CALL、紧急 CALL、P0 权威 / call_when CALL、P1 call_when / 通知 DIGEST、P2 通知 DIGEST、其余 IGNORE。
3. 分别检查 authorities 与 call_when 的数组、分隔字符串形式，以及 private / mention 三种动作设置。
4. 核对 classify_message 返回 action、reason，微信/企微 classify 接入不依赖 agent。

## 增量与发送

1. 仅用合成导出：重复 dry-run，消费水位应保持不变。
2. 使用隔离的假发送器验证 sent、failed、unknown、invalid、dry_run 分支；unknown 不得自动重发。不要用真实会话做自动测试。
3. 用独立私有 state_file 正式消费合成输入，再次检查不应把已处理输入当全新提醒；生产方与消费方状态隔离。
4. 接入真实发送之前，由本人核对 send_to 和消息内容；确认后才进行一次最小化真实提醒。

## 发布与隐私

- 目录 700；配置、密钥、状态、私有日志 600。
- 跟踪文件不包含真实账号 ID、SID、个人邮箱、内网地址、私有挂载路径、服务域名、内部主机名或部署身份词；按 wecom-export/scripts/audit-release.py 的规则审计。
- 不包含真实数据库、镜像、导出、媒体、密钥、运行状态或第三方私有配置。
- docs/CONTRACT.md 与 wechat-export-send/docs/CONTRACT.md 已分别计算 MD5，均为 `d003706b58eae98a9da3dd4e247af49a`。
- 文档不能把合成 demo 等同真实客户端兼容性；无客户端版本保证、无自动取密钥。

复验请使用 Python ≥3.10，并安装 `requirements-dev.txt`、`requirements-live.txt`、`requirements-files.txt`；依次运行 `python3 -m pytest -q`、`./bin/wechat-wecom-monitor demo`、`./bin/wechat-wecom-monitor doctor`、`python3 scripts/audit-release.py`。发布前提交全部源码，再运行 `sh scripts/build-release.sh`；`dist/SHA256SUMS` 记录当前归档的 SHA-256，不把归档本身提交进仓库。
