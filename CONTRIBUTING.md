# 贡献指南

感谢帮助改进本地微信 / 企业微信监控器。请先阅读 `README.md`、`SECURITY.md` 和 `docs/` 内的接口约定；所有公开示例、测试和截图都只能使用合成数据。

## 开发环境

需要 Python 3.10+ 与 Git。核心命令不需要第三方包，也不需要账号或网络。项目使用源码入口，不要求 `pip install .`：

```sh
python3 bin/wechat-wecom-monitor demo --output /tmp/monitor-demo
python3 bin/wechat-wecom-monitor doctor
```

`/tmp/monitor-demo/wechat` 和 `/tmp/monitor-demo/wecom` 各自是 `chat-export/v1` 导出根目录。不要将两个平台合并到一个 manifest。

需要运行测试时，可以自行创建虚拟环境并安装依赖。以下 `pip` 命令可能联网，安装器不会替你运行：

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt -r requirements-live.txt -r requirements-files.txt
python -m pytest tests wechat/test_file_text.py wecom/collector
```

依赖分层：`requirements.txt` 是无第三方依赖的核心；`requirements-live.txt` 提供本地加密库读取与压缩解码；`requirements-files.txt` 提供附件正文解析；`requirements-dev.txt` 提供 pytest 与采集器合成图像测试所需的 Pillow。不要将可选依赖无条件导入核心入口。

## 安装器与配置

安装器只配置本地文件并执行诊断；不登录、不下载、不发消息、不创建定时任务。默认交互运行：

```sh
sh install.sh
```

自动化安装可以读取白名单 env 文件；先将模板复制到仓库外的私有目录：

```sh
mkdir -p "$HOME/.config/wechat-wecom-monitor"
chmod 700 "$HOME/.config/wechat-wecom-monitor"
cp examples/install.env.example "$HOME/.config/wechat-wecom-monitor/install.env"
chmod 600 "$HOME/.config/wechat-wecom-monitor/install.env"
# 在本机编辑 install.env，随后运行：
sh install.sh --non-interactive --config "$HOME/.config/wechat-wecom-monitor/install.env"
```

支持的变量只有 `MONITOR_CONFIG`（写入路径）、`EXPORT_ROOT`、`STATE_FILE`、`SEND_CMD`、`SEND_TO`。实际环境变量优先于 env 文件；env 文件不会被 source，不进行变量或命令替换，含空格的值须加引号。相对路径相对运行安装器时的工作目录，路径开头的 `~` 会展开。

`--config` 也可读取完整 JSON 监控配置，保留 `groups`、`key_people`、`user`、`rules` 和其他配置字段；输出位置仍由 `MONITOR_CONFIG` 指定。优先级为安全默认值、输出位置的已有 JSON、`--config` JSON、显式环境变量，交互输入最后覆盖。已有未知字段保留；已有 JSON 无法解析时退出，不覆盖。安装器以 `0600` 权限原子写入文件；新建私有目录受 `077` umask 约束，不修改已有目录的权限。

```sh
MONITOR_CONFIG="$HOME/.config/wechat-wecom-monitor/monitor-config.json" \
  sh install.sh --non-interactive --config /path/to/private/rules.json
```

默认配置位置为 `~/.config/wechat-wecom-monitor/monitor-config.json`，状态位置为 `~/.local/state/wechat-wecom-monitor/seen.json`。空 `EXPORT_ROOT` 可先运行 demo；空发送命令与目标不会启用发送。`bark_url` 和 `local_db` 等额外字段通过私有 JSON 配置，不放进公开 env 模板。未通过 doctor 时安装器返回其退出码并保留配置，不把诊断失败当作成功。

Python 缺失时安装器打印手动安装提示；可用 `PYTHON=/path/to/python3 sh install.sh` 选择解释器。缺少可选依赖时仅打印可复制的 `python -m pip install -r ...` 命令；离线环境可由使用者预先准备可信的 Python 与依赖轮子，安装器不会自动获取。

## 代码与数据约束

- 对源数据库、WAL、SHM 保持只读；只处理你有权读取的数据，不增加注入、绕过权限或自动寻找他人账号的功能。
- 核心保持标准库可运行；将账号、设备与路径差异留在配置，不写入源码。
- 规则结果必须可解释；变更默认分类时同时更新合成测试和中文说明，不把真实群名、联系人或历史消息作为测试样本。
- `chat-export/v1`、`chat-send/v1` 的变更需要同步消费者与生产者；发送结果 `unknown` 不得自动重发。
- 不通过 shell 执行外部消息内容或 env 配置。涉及发送与 Bark 的变更须覆盖 dry-run 不发送的行为。
- 私有导出、配置、状态、数据库、密钥、日志和本机绝对路径不得加入提交；忽略规则并不等于安全审计。

## 提交与拉取请求

保持修改聚焦，说明行为变化、适用平台、兼容边界和实际执行过的检查。没有运行的检查应明确写“未运行”，不要声称已通过。永久行为变更同时维护文档和 `CHANGELOG.md`；删除失效入口，不添加只为旧路径保留的空壳。

提交前检查暂存内容并执行隐私审计（只检查 Git 已跟踪文件，因此新文件需先加入暂存区再审计）：

```sh
git diff --cached
python3 scripts/audit-release.py
```

`.gitleaks.toml` 为补充检测规则，可在自行安装 gitleaks 后使用；它不能替代人工检查或权威的 `audit-release.py`。审计脚本与相邻 `wecom-export` 项目保持字节级一致，不在单仓库擅自放宽规则。

## 发布

维护者先完成测试、离线 demo、doctor 与隐私审计，再提交所有应发布文件。`VERSION`、更新记录与标签必须一致。本版本标签为 `v0.1.0`。构建命令：

```sh
sh scripts/build-release.sh
```

脚本要求已跟踪工作区与提交一致，检查三段版本号及提交内版本，先运行隐私审计，再用 `git archive HEAD` 构建：

- `dist/wechat-wecom-monitor-0.1.0.tar.gz`
- `dist/SHA256SUMS`

归档带 `wechat-wecom-monitor-0.1.0/` 顶层目录；未跟踪文件不进入归档，源码归档不附带第三方包。已安装 GNU 校验工具时可用 `cd dist && sha256sum -c SHA256SUMS`；macOS 可用 `cd dist && shasum -a 256 -c SHA256SUMS`。标签工作流执行测试、演示、诊断与构建后，上传源码包和校验文件作为 Actions 制品；不会自动创建公开 GitHub Release、推送标签或部署生产环境。
