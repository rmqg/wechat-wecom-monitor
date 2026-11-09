# 接口约定：chat-export/v1 与 chat-send/v1

本文件由 `wechat-export-send` 和 `chat-digest` 共用，两边 `docs/CONTRACT.md` 内容相同。要修改时两边同时改，并升 schema 版本。其他导出工具只要产出同样的格式，也可以被消费方读取。

## 1. chat-export/v1（导出格式，生产方：wechat-export-send 或其他遵守本格式的导出工具；消费方：chat-digest）

导出是一个目录，可以增量追加：

```
<export-root>/
  manifest.json
  members/<conversation_id>.json        # 可选
  conversations/<conversation_id>.jsonl # 每行一条消息，按 ts 递增追加
  media/<sha256 前两位>/<sha256>.<ext>  # 可选，按内容寻址
  state/                                # 生产方自用的游标，消费方不读
```

`conversation_id` 只能含 `[A-Za-z0-9_.-]`。生产方把平台原始 ID 做成稳定的 ID，可以哈希，也可以原样保留；同一个会话不论哪次运行都必须得到同一个 ID。

### manifest.json

```json
{
  "schema": "chat-export/v1",
  "producer": {"name": "wechat-export-send", "version": "1.0.0"},
  "source": {"platform": "wechat", "account_id": "<稳定的不透明 ID，可以是哈希>"},
  "generated_at": "2026-11-08T12:00:00+08:00",
  "timezone": "Asia/Shanghai",
  "conversations": [
    {"id": "g_1a2b3c", "kind": "group", "name": "示例群", "file": "conversations/g_1a2b3c.jsonl",
     "members_file": "members/g_1a2b3c.json", "last_ts": 1794043483, "message_count": 1234}
  ]
}
```

- `platform` 取值：`wechat`、`wecom`、`qq`、`other`。
- `kind` 取值：`group`、`private`、`channel`、`other`。
- manifest 必须原子替换：先写临时文件，再 rename。
- `conversations[]` 可以带可选字段 `avatar`：会话自己的头像（群头像），相对 export-root 的路径，例如 `"avatar": "media/ab/<sha256>.jpg"`。旧版生产方不写这个字段；消费方读不到或文件不存在时按“没有头像”处理，不得报错，也不得按名字猜。

### conversations/*.jsonl（每行一个 JSON 对象）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | 是 | 会话内唯一且稳定。重复出现时以最后一行为准（消息被编辑、撤回时会这样） |
| `ts` | integer | 是 | Unix 秒（UTC） |
| `sender_id` | string | 是 | 会话内稳定的发送者 ID；系统消息用 `"system"` |
| `sender_name` | string | 否 | 当时的显示名（优先用群昵称） |
| `is_self` | boolean | 否 | 是不是导出账号自己发的 |
| `type` | string | 是 | `text`、`image`、`file`、`link`、`voice`、`video`、`sticker`、`quote`、`forward`、`system`、`revoke`、`other` |
| `text` | string | 否 | 可读正文。没有正文时不写，或写空串。不得用占位文字冒充正文 |
| `reply_to` | object | 否 | `{"id": "<被引用消息 id，可缺>", "sender_name": "...", "text": "..."}` |
| `forward` | array | 否 | 合并转发的条目，每项 `{"sender_name","ts","type","text"}` |
| `link` | object | 否 | `{"url","title","description"}` |
| `media` | array | 否 | 每项 `{"kind":"image|file|voice|video|sticker","path":"media/ab/<sha256>.jpg","sha256","mime","bytes","name","status":"ready|pending|missing","is_thumbnail":false,"source_quality":"original|client_full"}`；`path` 相对 export-root；没下载到原图时 `status` 写 `pending` 或 `missing`，**不得**拿缩略图冒充原图（是缩略图就写 `is_thumbnail: true`）。`source_quality` 可选：长度和 md5 都与消息记录对上写 `original`，否则写 `client_full` |
| `mentions` | array | 否 | 被 @ 的 `sender_id` 列表；@所有人写 `"@all"` |
| `raw_type` | string | 否 | 平台原始类型号，便于排查 |
| `extra` | object | 否 | 生产方私有字段，消费方必须忽略不认识的字段 |

### members/*.json

```json
{"conversation_id": "g_1a2b3c", "updated_at": 1794043483,
 "members": [{"id": "u_9f8e", "name": "群昵称", "display_name": "本名或备注",
              "avatar": "media/cd/<sha256>.jpg"}]}
```

`avatar` 可选，相对 export-root；生产方没有这个人的头像时不写。头像文件和其他媒体一样属于隐私数据，只留在本机。

### 规矩

- 只追加；消费方按 `(conversation_id, id)` 去重，后出现的行覆盖先出现的。
- 消费方用 `last_ts` 和自己记的游标做增量，不读生产方的 `state/`。
- 文件编码 UTF-8，不带 BOM，每行以 `\n` 结尾；生产方写行时先写完整行再 flush，消费方忽略最后一行不完整的情况。
- 时间一律存 UTC 秒，显示时区看 manifest 的 `timezone`。
- 导出里含真实聊天内容和媒体，属于隐私数据：目录权限 700，不放进任何仓库。

### 头像刷新（可选）

头像文件按内容寻址，换头像后 `avatar` 的路径会变，消费方每次读取最新路径，不要按成员长期缓存。已生成的报告保留当时的头像。

生产方可以额外提供一个刷新命令，消费方在生成报告前调用，配置里写完整命令前缀，调用时追加 `--ids-file <路径>`（每行一个成员 ID 或会话 ID）。退出码 0 表示已刷新完成；非 0 或超时，消费方继续用导出里现有的头像并记录日志，不得让报告失败。

## 2. chat-send/v1（发送接口，提供方：wechat-export-send；调用方：chat-digest）

命令行（调用方通过配置项指定命令前缀，例如 `wechat-send` 或 `docker exec wechat wechat-send`）：

```
wechat-send --to <会话名或 conversation_id> (--text <文字> | --text-file <路径> | --image <路径>) \
            [--request-id <幂等键>] [--timeout <秒>] [--dry-run] --json
```

- `--request-id`：同一个键已经确认发送过的，直接返回上一次的结果，不会重发。
- `--dry-run`：只定位目标会话、校验输入，不提交。
- 标准输出是一个 JSON 对象：

```json
{"schema": "chat-send/v1", "status": "sent", "request_id": "...", "to": "...",
 "conversation_id": "...", "submitted_at": 1794043483, "detail": "...", "extra": {}}
```

| status | 退出码 | 含义 | 调用方该怎么做 |
|---|---|---|---|
| `sent` | 0 | 已提交并确认送达 | 记为完成 |
| `failed` | 1 | 在提交**之前**失败（目标会话找不到、窗口不对、文件不合法等），确定没发出去 | 可以重试 |
| `unknown` | 2 | 已经提交或可能已经提交，但没能确认 | **不许自动重试**，交给人工确认 |
| `invalid` | 3 | 参数错误 | 修配置，不重试 |
| `dry_run` | 0 | 只在 `--dry-run` 时出现：目标会话和输入都校验通过，没有提交 | 不算发送，按需再正式发送 |
| `calibrated` | 0 | 只在 `--calibrate` 时出现：已选中会话并保存标题模板 | 不算发送 |

- `extra` 是提供方私有字段，调用方必须忽略不认识的字段。
- 提供方必须保证：凡是可能已经点了「发送」的情况，一律报 `unknown`，不报 `failed`。
