# 排障

先停止重叠定时任务，保留私有状态；不要删水位“试试”。只在本机诊断，不上传真实数据库、日志全文、账号 ID、密钥或截图。

| 现象 | 处理 |
|---|---|
| 找不到命令 | 从仓库根目录用 python3 bin/wechat-wecom-monitor；定时任务填写绝对路径 |
| doctor 报配置失败 | 检查 JSON、字段类型与文件权限；用合成模板比对，不公开实际配置 |
| manifest 不存在 / 格式不符 | --export-root 指向含 manifest.json 的导出根目录，而不是 conversations 子目录；生产方须产出 chat-export/v1 |
| 群消息全 IGNORE | 核对 groups[].id 与 manifest、tier、关键词以及 user.ids；P1 call_when 默认 DIGEST，不是 CALL |
| 自己消息未忽略 / @未识别 | 核对 is_self、sender_id、mentions 和 user.ids/aliases；显示名不是可靠身份凭证 |
| 私聊未 CALL | 默认 private=DIGEST；需要改变时显式设置 rules.private |
| 真实采集缺群 / 缺消息 | 检查客户端同步、采集器支持范围、已有密钥与数据库结构；不要修改源库或声称完整覆盖 |
| 本地库解密失败 | 密钥必须与账号/库对应；工具不会自动取密钥，不把密钥贴到 Issue |
| dry-run 后重复看到同样消息 | 预期行为：dry-run 不推进消费水位 |
| 正式运行重复提醒 | 检查是否换了 state_file、是否并发任务、生产方消息 ID 是否稳定；先人工核对，不盲目重发 |
| 发送 unknown / 超时 | 可能已提交，先查目标会话与 request_id，绝不自动重发或删除状态绕过保护 |
| 发送 failed / invalid | failed 才表示确定提交前失败；invalid 修参数；按 CONTRACT 区分，不能把任意异常当 failed |
| Bark 失败 | 检查本机网络与私有 URL，避免把 token 打印进日志；Bark 不是 chat-send/v1 送达证明 |

报告问题请提供工具版本、平台、脱敏错误类型和可重复的合成输入。真实聊天、媒体、客户端数据库、私有网络地址或身份信息不得进入公共 Issue。
