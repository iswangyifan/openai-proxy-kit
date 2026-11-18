# GitHub 开源发布流程

仓库目标：[iswangyifan/openai-proxy-kit](https://github.com/iswangyifan/openai-proxy-kit)。先发布候选版，完成干净系统验收后再发布稳定版。

## 一、本地准备与代码门禁

1. 所有必要源码、图标、文档、构建锁定输入与许可证进入 Git；缓存、个人配置和发行包不进入 Git。
2. 执行 `bash tests/check.sh`，覆盖安全回归、启停与首装失败路径、托盘往返和 controller 影子探测。
3. 运行 `bash packaging/fetch-core.sh` 和 `bash packaging/build-linux.sh`，验证锁定的核心及其对应源代码。
4. 从干净 Git 检出重新测试、构建，确认包内没有真实订阅、内部路径、节点凭据或上游开发目录。
5. 检查包的移除脚本、许可、实际来源清单、源码包和 SHA256SUMS。源码与二进制应对应同一提交。

## 二、GitHub 仓库设置（首次发布一次性完成）

- 检查仓库可见性及开源授权：维护者有权公开本项目代码、图标和截图；不公开企业私有配置、账号或第三方订阅。
- 启用 Issues、Private vulnerability reporting、Dependabot alerts 和 Secret scanning（按账户功能可用范围）；启用推送保护。
- 默认分支开启保护：PR 合并、CI 必须通过；个人维护时保持可用的紧急修复流程。
- Actions 使用最小只读权限；审查外部贡献的 workflow 改动，不向 fork PR 提供发布密钥。
- 仓库 About 填写用途、MIT 主项目许可、目标系统，并链接 README；不要宣传为 OpenAI 官方工具或保证任何地区可访问。

## 三、候选版与干净系统验收

1. 仅用 `git push origin main` 和明确的 tag 推送已审查内容，不使用 `--mirror` 或推送本地 AI 工具 notes。等待 GitHub CI 通过。手动运行 Release candidate 工作流，取得构建产物。
2. 在干净 Ubuntu 24.04 / GNOME amd64 虚拟机中验收：apt 安装能拉齐依赖，无 Node/Git/controller 开发目录仍可运行。
3. 使用测试订阅验收首次配置、GUI/托盘启停、日志、系统重启、核心崩溃与更新失败。确认不下载地理数据，不接管系统 DNS。
4. 验收已有手工代理、仅 HTTP 或 HTTPS 被其他软件修改、已有自定义启动器、同时发出两个操作，均不遗留死代理或丢失用户文件。
5. 分别验证在线/未登录用户的直接 apt remove、软件中心卸载、重新安装；验证 apt upgrade 保留状态且新版本能够完成认证迁移。
6. 对照旧版本迁移记录确认已丢失的旧启动器无法凭空恢复；对现有备份进行人工检查，避免声称修复能够恢复历史丢失数据。

本地替身/隔离测试和干净系统验收分别记录。没有完成虚拟机验收时，标注候选版，不把脚本全绿写成“所有 Linux 已验证”。

## 四、正式发布

- 版本号、变更记录、tag 和软件包版本对应；建议先 `v0.3.0-rc.1`，稳定后 `v0.3.0`。
- Release 附 `.deb`、SHA256SUMS、CORE-ORIGIN.json、对应 mihomo 源码、Go 模块源码包、MIHOMO-COMPONENTS.json 及项目源码入口；明确系统依赖与不支持的平台。
- 在下载说明旁给出准确的 GPL 对应源码位置和获取方式；保持分发期间可用。主项目 MIT 不替代第三方许可。
- 下载页注明签名状态。当前 SHA256 用于完整性校验，没有独立签名；后续使用维护者控制的密钥或 GitHub artifact attestations，并公开验证方法，不能将同站哈希宣称为签名。
- 发布后从 Release 重新下载并验 SHA256；安装、首次启动和卸载再抽检一次，建立反馈入口。

## 五、发布后维护

优先处理安全与恢复问题，再考虑独立自启动开关、企业域名直连配置和更多发行版。每次更新核心都重新核对来源、摘要、许可证和安全公告；执行 `packaging/audit_core_licenses.py prepare` 从实际固定核心收集并校验 Go 依赖许可，审查其差异后入库。不可直接取消锁定以绕过下载失败。

安全修复按 [SECURITY](../SECURITY.md) 私密协调，再发布补丁与公告。明确支持最新补丁版本，避免承诺无法持续维护的多平台范围。
