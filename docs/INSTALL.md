# 安装与真实部署

## 先区分两条路径

- 离线规则检查：Python 3，读取 chat-export/v1，不需要客户端或 agent。
- 本地数据库采集：高度依赖平台、客户端数据库结构、同步完整度与已有合法密钥；需要另行准备环境。具体依赖以 wechat/、wecom/collector/ 的说明与 doctor 结果为准。不要向系统 Python 盲目安装所有可选依赖。

从仓库根目录可直接运行 python3 bin/wechat-wecom-monitor；如使用发行包或安装脚本，先查看脚本说明，再在自己的用户权限下安装。不要把安装成功等同于采集成功。

## 私有目录

~~~sh
umask 077
mkdir -p /path/to/private-monitor
chmod 700 /path/to/private-monitor
cp docs/monitor-config-TEMPLATE.json /path/to/private-monitor/monitor.json
chmod 600 /path/to/private-monitor/monitor.json
~~~

以上 /path/to 是占位符。导出、状态、日志放私有目录，文件权限 600；不要把配置放共享 Web 目录。不要使用管理员权限读取不属于自己的账号。

## 配置与诊断

~~~sh
python3 bin/wechat-wecom-monitor doctor --config /path/to/private-monitor/monitor.json
python3 bin/wechat-wecom-monitor check --export-root /path/to/private-export --config /path/to/private-monitor/monitor.json --state-file /path/to/private-monitor/state.json --dry-run
~~~

本地库路径可改用 check --local-db --config /path/to/private-monitor/monitor.json --dry-run。doctor 不能保证客户端未来版本兼容，也不会自动获得密钥。

## 定时任务环境

安装器/任务环境变量约定：

| 变量 | 含义 |
|---|---|
| MONITOR_CONFIG | 私有配置 JSON 路径 |
| EXPORT_ROOT | 导出目录 |
| STATE_FILE | 独立消费状态文件 |
| SEND_CMD | chat-send/v1 命令前缀 |
| SEND_TO | 已确认的提醒目标 |

使用绝对路径；定时任务通常没有交互 shell 的 PATH。若手工编写调度器，请显式映射为 --config、--export-root、--state-file、--send-cmd、--send-to，不假定裸 CLI 自动读取所有环境变量。配置包含空格时正确引用。一个状态文件只交给一个串行任务，不重叠运行。

首次不要启用无人值守发送。先离线演示，再真实导出 dry-run，再由本人确认目标进行一次正式提醒，最后才设置 cron / systemd / Windows 计划任务。停用时先关闭调度器，保留私有状态以便排查；重新启用前人工检查 unknown 记录，不能删除水位强迫重发。

## 每 10 分钟定时扫描配置示例（Cron / Agent 巡检）

生产环境中推荐以 **10 分钟** 为周期扫描新消息并推送重要提醒：

### 方式一：由定时智能体（Agent）每 10 分钟扫描并按手册推送（原生产配置）

由定时 Agent 任务每 10 分钟执行一次增量拉取脚本，将输出的 JSON 结合 `docs/monitor-manual-TEMPLATE.md` 判定手册交由 Agent 过滤出 `CALL` / `DIGEST` 重要消息后推送：

~~~sh
# 每 10 分钟拉取微信新增消息（含 7200 秒回看窗口与状态去重）与企微增量事件
python3 /path/to/wechat-wecom-monitor/wechat/wechat_check.py --since "$(cat /path/to/private-monitor/last_ts)" --lookback 7200
python3 /path/to/wechat-wecom-monitor/wecom/wecom_check.py
~~~

### 方式二：由系统 Cron 每 10 分钟运行内置独立规则分类与推送（脱离 Agent）

~~~cron
*/10 * * * * umask 077; /usr/bin/python3 /path/to/wechat-wecom-monitor/bin/wechat-wecom-monitor check --export-root /path/to/private-export --config /path/to/private-monitor/monitor.json --state-file /path/to/private-monitor/state.json --send-cmd "/path/to/wechat-send --instance main" --send-to "filehelper" >> /path/to/private-monitor/cron.log 2>&1
~~~
