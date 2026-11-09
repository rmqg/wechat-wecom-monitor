# 消息监控人工复核手册（合成模板）

此模板用于复核规则，不是 agent 执行前提。机器配置见 monitor-config-TEMPLATE.json；确定性规则以 CONFIGURATION.md 为准。所有占位名称、ID 均须在本机私有配置替换，不提交真实身份或聊天。

## 用户与群

- 用户 ID：USER_SELF；别名：示例用户。
- P0：GROUP_PRIORITY，示例重点群；authorities 填示例负责人 ID；call_when 填需要提醒的关键词数组。
- P1：GROUP_REGULAR，示例常规群；call_when / 通知关键词命中后 DIGEST。
- P2：GROUP_LOW，示例低频群；仅通知关键词命中后 DIGEST。
- IGNORE：GROUP_MUTED，示例忽略群；即使 @ 或紧急词也忽略。
- key_people：以 id、name 描述示例联系人，不附身份证、手机号、课表或住址。

## 复核顺序

1. 自己发送或 IGNORE 群：IGNORE。
2. 私聊：rules.private，默认 DIGEST。
3. 群里 @自己 / 本人别名点名：rules.mention，默认 CALL。
4. 紧急关键词：CALL。
5. P0 权威发言或 call_when：CALL。
6. P1 call_when / 通知关键词、P2 通知关键词：DIGEST。
7. 其余：IGNORE。

CALL 表示提醒本人查看；DIGEST 表示留待汇总；IGNORE 表示不提醒。不要把规则配置当成自然语言 agent 指令。核心规则不承诺自动判断 48 小时截止、上课静默、跨群事件合并或延期二次提醒。

## 人工确认

提醒应最小化，只保留必要上下文。卡号、身份证号、密码、API 凭据、私有链接等不得复制进公开报告。不要代回复、接龙、点链接、付款或加好友。unknown 发送状态先核对目标会话，未确认前不得自动重试。排障只提供合成复现，不上传原始数据库、真实导出或密钥。
