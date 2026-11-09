# 企业微信本地增量采集器（collector/）

本目录包含运行在 Windows 虚拟机内部（同时支持在 Linux/macOS 下执行离线 `demo` 与 `doctor`）的企业微信只读数据库增量采集器核心模块。

## 模块列表

| 文件 | 作用 |
|---|---|
| `collect-wecom.py` | CLI 主入口（`collect`、`query`、`events`、`snapshot`、`verify`、`export-v1`、`status`、`demo`、`doctor`） |
| `wxwork_crypto.py` | 依据 `utelle/SQLite3MultipleCiphers`（MIT）clean-room 实现的 wxSQLite3 AES-128-CBC 页面加解密与 SQLite 第 1 页/B-tree 校验 |
| `wxwork_keys.py` | 基于 Win32 `VirtualQueryEx` + `ReadProcessMemory` 的进程内存候选密钥扫描与第 1 页校验器 |
| `wxwork_snapshot.py` | 只读数据库与 `-wal`/`-shm` 稳定捕获、WAL 盐值代际与提交边界恢复、`PRAGMA integrity_check` 原子发布 |
| `proto.py` | 严格边界检查的精简 Protobuf wire 解析器 |
| `messages.py` | 企业微信消息类型（0/2/4/13/14/15/36/38/40/78/123/561/1002/1011/1022/2101）解码、URL 清洗与会话元数据关联 |
| `media.py` | 限定于当前账号 `Cache` 目录下的附件解析、MD5/SHA-256 校验、原图与客户端缓存重编码缩略图区分 |
| `archive_store.py` | 基于 SQLite 的归一化消息归档、首次运行基线隔离、去重与单调递增事件游标 |
| `chat_export.py` | 将归一化记录增量导出为共享契约格式 `chat-export/v1` |
| `demo_synthetic.py` | 离线合成加密数据库构造与端到端演示 |
| `doctor.py` | 运行环境、密码学测试向量与配置自检 |
| `deployment/install-task.ps1` | 注册/更新当前用户交互令牌下的每分钟 Windows 计划任务（带所有权标记与备份） |
| `deployment/remove-task.ps1` | 安全卸载计划任务或从备份 XML 恢复 |

## 常用命令（Windows PowerShell）

```powershell
$r = Join-Path $env:LOCALAPPDATA 'WeComCollector'

# 手动执行一次增量采集（并同步更新 chat-export/v1 目录）
& "$r\runtime\python.exe" "$r\collect-wecom.py" --config "$r\config.json" collect

# 查看采集状态与健康检查
& "$r\runtime\python.exe" "$r\collect-wecom.py" --config "$r\config.json" status
& "$r\runtime\python.exe" "$r\collect-wecom.py" --config "$r\config.json" status --check --max-age-minutes 15

# 查询最近 1 小时新增或附件状态更新的消息
& "$r\runtime\python.exe" "$r\collect-wecom.py" --config "$r\config.json" query --since 1h --include-updates

# 校验归档完整性与所有已就绪附件的 SHA-256
& "$r\runtime\python.exe" "$r\collect-wecom.py" --config "$r\config.json" verify --hashes
```
