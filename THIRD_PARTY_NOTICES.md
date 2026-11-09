# 第三方说明

## 本项目代码与源码包

本仓库项目代码采用 MIT 许可证，完整条款见 `LICENSE`，版权声明为 2026 年。发布源码包由 `git archive` 从提交构建，不打包虚拟环境、Python 解释器、数据库、运行数据或通过 pip 安装的第三方依赖。

`wecom/collector/` 为本项目保留的本地采集器源码；采集器接口及 `scripts/audit-release.py` 与相邻 `wecom-export` 项目同步，审计脚本须保持字节一致。`chat-export/v1` 和 `chat-send/v1` 为与 `wechat-export-send` 等生产 / 消费工具互通的接口约定，不表示捆绑了这些工具、客户端或服务。既有文件中的作者及许可声明必须保留；引入其他上游代码时，贡献者应补充其来源和实际许可证，不得仅用本项目 MIT 覆盖上游权利。

## Python 与可选依赖

以下项目不随本源码包分发；使用者按所需功能手动安装。许可证摘要方便识别，具体版本的 LICENSE / NOTICE 与上游源代码才是权威依据，重新分发其二进制、依赖或源码时必须另行遵守相应条款。

| 项目 | 用途 / 声明文件 | 主要许可证 | 上游 |
|---|---|---|---|
| CPython | Python 3.10+ 解释器及标准库；核心所需 | PSF License Agreement；部分内含组件另有声明 | <https://www.python.org/> |
| PyCryptodome | 本地加密库读取；`requirements-live.txt`，导入名 `Crypto` | BSD 2-Clause 与公有领域组件，依上游文件而定 | <https://www.pycryptodome.org/> |
| python-zstandard | 压缩消息解码；`requirements-live.txt` | BSD 3-Clause；底层 Zstandard 另有上游许可 | <https://github.com/indygreg/python-zstandard> |
| pypdf | PDF 正文；`requirements-files.txt` | BSD 3-Clause | <https://github.com/py-pdf/pypdf> |
| python-docx | DOCX 正文；`requirements-files.txt`，导入名 `docx` | MIT | <https://github.com/python-openxml/python-docx> |
| openpyxl | XLSX 正文；`requirements-files.txt` | MIT | <https://openpyxl.readthedocs.io/> |
| pytest | 合成数据测试；`requirements-dev.txt` | MIT | <https://github.com/pytest-dev/pytest> |
| Pillow | 采集器合成图像与测试；`requirements-dev.txt`，导入名 `PIL` | MIT-CMU（HPND 风格） | <https://python-pillow.github.io/> |

标准库 CLI、离线 demo 与配置安装不需要安装这些第三方 Python 包。安装器仅报告缺失项并打印手动安装命令，不自动连接包索引。附件解析器和依赖可能带入传递依赖，其许可与安全更新同样由使用者和再分发者负责核实。声明的是兼容版本区间，不是供应链锁定清单。

## 开发与发布工具

- Git（GPL-2.0）仅用于开发、隐私审计列举已跟踪文件及源码归档，不随发布包分发：<https://git-scm.com/>。
- GitHub Actions 工作流引用 `actions/checkout`、`actions/setup-python`、`actions/upload-artifact`（上游项目均为 MIT），作为托管 CI 服务中的工具使用，并非打包进程序：<https://github.com/actions>。
- 可选 gitleaks（MIT）可使用本仓库 `.gitleaks.toml` 补充检查，但未被安装器下载或捆绑：<https://github.com/gitleaks/gitleaks>。

## 平台与服务

微信、企业微信及相关名称和商标属于各自权利人。本项目与其运营方无隶属、授权或背书关系，不分发客户端、账号、数据库密钥或官方 SDK。

Bark 和 `chat-send/v1` 发送器是由用户显式选择的外部服务 / 程序；本项目不会捆绑或自动部署它们。使用时请自行核实其许可证、服务条款与隐私政策；协议兼容不代表安全审查通过。
