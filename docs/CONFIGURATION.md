# 配置说明

以 monitor-config-TEMPLATE.json 为起点，复制到仓库外或未跟踪的私有目录。UTF-8 JSON，不写注释。配置文件权限 600，父目录 700。模板里所有名称和 ID 均为合成占位符，不代表真实账号。

## 字段

| 字段 | 类型 / 含义 |
|---|---|
| groups | 群配置列表 |
| groups[].id | 导出 manifest 中稳定的会话 ID，不是凭显示名猜测 |
| groups[].name | 群显示名 |
| groups[].tier | P0、P1、P2 或 IGNORE |
| groups[].authorities | 权威发送者 ID/名称列表，或分隔字符串 |
| groups[].call_when | 触发关键词列表，或分隔字符串；不是自然语言推理指令 |
| key_people | 关键人对象列表，每项 id、name |
| user.ids | 自己的稳定发送者 ID 列表 |
| user.aliases | 自己的名字 / 昵称别名列表，用于点名识别 |
| rules.urgent_keywords | 紧急关键词列表 |
| rules.notification_keywords | 通知关键词列表 |
| rules.private | CALL / DIGEST / IGNORE，默认 DIGEST |
| rules.mention | CALL / DIGEST / IGNORE，默认 CALL |
| export_root | chat-export/v1 导出目录 |
| state_file | 本工具消费状态文件；不要复用生产方 state/ |
| send_cmd | chat-send/v1 命令前缀，不是聊天正文 |
| send_to | 明确的提醒目标会话 |
| bark_url | 可选 Bark 接口 URL，包含私密凭据，勿公开 |
| local_db | 本地数据库配置对象，accounts 为账号配置列表 |

对于 authorities、call_when，优先 JSON 数组，避免分隔符与关键词本身冲突；字符串可按逗号、中文逗号、分号、顿号或换行分隔。不要把学校课表、地址或原部署身份抄进模板。

## 默认决策语义

先排除自己发送的消息和 IGNORE 群；这两项优先于紧急和点名规则。

| 条件 | 默认动作 |
|---|---|
| 自己发送 / IGNORE 群 | IGNORE |
| 私聊 | rules.private，默认 DIGEST |
| @自己 / 命中本人点名规则 | rules.mention，默认 CALL |
| 紧急关键词 | CALL |
| P0 群权威发言或命中 call_when | CALL |
| P1 群命中 call_when 或通知关键词 | DIGEST |
| P2 群命中通知关键词 | DIGEST |
| 其余 | IGNORE |

P1 的 call_when 名称沿用配置字段，**并不意味着升为 CALL**。key_people 描述关键联系人身份，不应把它理解成“其所有群发言无条件 CALL”。规则是确定性匹配，不会判断真实截止时间、付款合法性、事实真伪或任务是否已完成；不要依赖它承担紧急安全保障。

## CLI 与 Python

~~~sh
python3 bin/wechat-wecom-monitor check --export-root /path/to/private-export --config /path/to/private/monitor.json --state-file /path/to/private/state.json --dry-run
python3 bin/wechat-wecom-monitor check --local-db --config /path/to/private/monitor.json --dry-run
~~~

第二条需要先完成 local_db 配置与密钥准备，不是自动取密钥。local_db.accounts 每项包含 base_dir（本地库根目录）、key_file（已有密钥文件路径）、label（账号标签）、self_id（自己的稳定 ID）；lookback、friends、seen_file 为可选采集配置。所有路径与身份只放在本机私有配置，文件权限 600。示意结构如下，路径均须替换：

~~~json
{
  "local_db": {
    "accounts": [
      {
        "base_dir": "/path/to/private-client-data",
        "key_file": "/path/to/private/keys.json",
        "label": "example-account",
        "self_id": "acct_main_example"
      }
    ]
  }
}
~~~

Python 入口：load_config(path=None) 返回配置字典；classify_message(message, conversation, config) 返回含 action、reason 的字典；check_export(export_root, config=None, state_file=None, send_cmd=None, send_to=None, bark_url=None, dry_run=False) 返回检查结果字典。

安装任务使用 MONITOR_CONFIG、EXPORT_ROOT、STATE_FILE、SEND_CMD、SEND_TO 环境变量，详见 INSTALL。不要把密钥或 Bark URL 放进公开 shell 示例。
