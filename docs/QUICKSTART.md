# 5 分钟离线快速开始

离线演示仅使用合成会话，不要求微信、企微、数据库密钥或 agent。真实数据库接入不是这个五分钟流程的一部分。

## 1. 生成演示

在仓库根目录：

~~~sh
umask 077
python3 bin/wechat-wecom-monitor demo --output ./demo-output
python3 bin/wechat-wecom-monitor doctor
~~~

输出目录下分别生成 wechat/manifest.json 和 wecom/manifest.json，均采用 chat-export/v1，两个平台是独立导出源。demo 不运行真实采集或网络请求；未指定 --output 时默认不保留临时文件。只使用演示目录，不把真实导出覆盖进去。

## 2. 预览检查

~~~sh
python3 bin/wechat-wecom-monitor check --export-root ./demo-output/wechat --dry-run
python3 bin/wechat-wecom-monitor check --export-root ./demo-output/wecom --dry-run
~~~

查看 JSON 中的分类和原因。未配置群时不能期待每个合成群自动 CALL：配置中的群 ID 必须与导出 manifest 对应。dry-run 不发送、不推进消费水位，可重复预览。

## 3. 使用自己的规则

~~~sh
mkdir -p ./private
chmod 700 ./private
cp docs/monitor-config-TEMPLATE.json ./private/monitor.json
chmod 600 ./private/monitor.json
python3 bin/wechat-wecom-monitor doctor --config ./private/monitor.json
python3 bin/wechat-wecom-monitor check --export-root ./demo-output/wechat --config ./private/monitor.json --state-file ./private/wechat-state.json --dry-run
python3 bin/wechat-wecom-monitor check --export-root ./demo-output/wecom --config ./private/monitor.json --state-file ./private/wecom-state.json --dry-run
~~~

编辑 private/monitor.json，按 [配置说明](CONFIGURATION.md) 填合成群 ID 与关键字。演示目录也不要混入真实聊天。

## 4. 切换真实部署前

先完成 [安装](INSTALL.md)、[兼容性检查](COMPATIBILITY.md) 和 [人工验收](VALIDATION.md)。通过独立导出器准备 chat-export/v1 后，将 --export-root 指向私有导出目录；明确选择 send_cmd/send_to 或 bark_url 才接入提醒。第一次只 dry-run；删除 --dry-run 才是正式检查。发送 unknown 时停止自动重发并人工核对。
