# 安装、使用与开发

## 支持范围

Ubuntu 24.04 / GNOME / amd64，Python 3.12、GTK4 ≥4.10、libadwaita ≥1.4。其他发行版与桌面暂未验收。GNOME 托盘依赖 StatusNotifierItem 扩展；缺少托盘时窗口仍提供明确退出方式。

软件包内置 mihomo 和本项目 Python 路由引擎；使用端不需要 Node、Git 或另一个源码仓库。系统 GTK/Python 等依赖由 apt 安装。

## 安装

从 [GitHub Releases](https://github.com/iswangyifan/openai-proxy-kit/releases) 下载软件包和 SHA256SUMS，在下载所在目录执行：

```bash
ls -lh ./openai-proxy-kit_0.3.0_amd64.deb ./SHA256SUMS &&
sha256sum --ignore-missing -c SHA256SUMS &&
sudo apt install ./openai-proxy-kit_0.3.0_amd64.deb &&
openai-proxy-kit
```

SHA256 检查完整性，不代表独立数字签名。路径必须指向实际下载文件；出现“不支持的文件”时先运行 `ls -l ./openai-proxy-kit_0.3.0_amd64.deb` 检查位置。

`./` 是终端当前目录。源码仓库不包含 `.deb`；如果是自行构建，在项目根目录使用 `sudo apt install ./dist/openai-proxy-kit_0.3.0_amd64.deb`。也可直接使用安装包的绝对路径。安装失败时请先解决错误，再运行应用命令。

首次配置输入可信 Clash YAML 订阅，核对页面的系统改动和可选启动器适配后启动。GUI 默认不适配任何应用；开启可选适配时会显示候选应用并保留现有用户参数。服务启停同时启用/取消登录自启动，独立自启设置尚未实现。已开启意图会在核心重启时核验端口后恢复；遇到其他手动/PAC代理则保留其设置。

## 使用与恢复

```bash
openai-proxy-kit start
openai-proxy-kit stop
openai-proxy-kit status
openai-proxy-kit --logs
openai-proxy-kit logs --follow
openai-proxy-kit diagnose
openai-proxy-kit update
openai-proxy-kit upgrade   # 旧配置迁移到随机 API 密钥与新守护
openai-proxy-kit reset     # 恢复用户集成，保留私有配置和备份
```

默认仅明确列出的 OpenAI 相关域名走代理，其余直连；不启用 TUN、不修改系统 DNS、不下载 GeoIP/GeoSite。内网登录、企业 DNS 和 VPN 兼容性仍取决于环境；需要时关闭代理。

关闭会逐协议恢复接管前的设置，保留其他程序后续修改的目标；因此原先配置了其他代理时，恢复结果也可能是该代理。已经运行的应用环境变量不会被改写，需要完全退出后重开。

新配置位于 `~/.config/openai-proxy-kit/mihomo/`；安装状态、系统代理快照和启动器备份位于 `~/.local/share/openai-proxy-kit/`。状态文件仅作数据解析，不能作为可执行 shell 脚本使用。软件包通过受保护的 UID 登记目录识别已配置账号；root 只验证文件归属并降权，实际状态由各用户读取。旧安装继续使用记录的路径，不递归删除共享 mihomo 目录。

软件包升级后请完全退出并重开控制台。旧配置迁移前保留 `.pre-auth` 备份，升级后在当前用户会话执行 `openai-proxy-kit upgrade`。API 密钥无需手工复制给 GUI。可选网页面板需要自行安装并输入密钥；不要把密钥放在 URL 中分享。

## 卸载

```bash
sudo apt remove openai-proxy-kit
```

正常包移除会在当前及其他已配置用户身份下清理服务、恢复代理和启动器。清理失败会阻止移除，先按提示恢复再重试，不能强制跳过脚本。apt upgrade 不执行移除清理。

GUI 的“工具与设置 → 恢复用户集成”与 `reset` 使用同一清理逻辑。

`reset` 和卸载保留订阅、节点配置及备份，状态归档为 `kit.env.removed-*`。如需删除敏感数据，先检查私有配置目录与备份，确认没有其他部署使用后自行删除；程序不会递归删除共享目录。自定义安装位置通过用户级位置记录定位；影子测试不登记为正常安装。

## 开发与构建

```bash
sudo apt install python3 python3-gi python3-yaml gir1.2-gtk-4.0 gir1.2-adw-1 \
  gir1.2-gdkpixbuf-2.0 librsvg2-common curl systemd iproute2 libglib2.0-bin \
  util-linux dbus dconf-gsettings-backend dpkg-dev binutils xvfb
bash packaging/fetch-core.sh
bash tests/check.sh
bash packaging/build-linux.sh
```

核心版本和可信摘要由 [core.lock.json](../packaging/core.lock.json) 固定；发行包仅接受锁定且已审核的核心；自定义版本须同步更新二进制摘要、准确源码包及源码摘要，不能覆盖一个路径就绕过门禁。首版只提供 amd64 锁定输入。构建会访问 GitHub 和 proxy.golang.org 获取固定依赖与源码；下载失败不跳过校验，离线构建需要先准备对应的校验缓存。不在安装阶段下载额外运行时。

产物位于 `dist/`。包内 CORE-ORIGIN.json 从实际入包文件生成；对应 GPL 核心及实际 Go 模块源码一并生成供发布；模块 ZIP 与二进制 buildinfo 中的 h1 校验一致。许可清单与全文随包提供。默认 `tests/check.sh` 使用隔离状态、合成凭据或本地假 API，不访问真实账号；可选真实 VM 验收的结果和边界见[发行验收记录](acceptance-2026-11-18.md)。

发布步骤与验收边界见[发布流程](releasing.md)，隐私说明见[安全策略](../SECURITY.md)，许可见[NOTICE](../NOTICE)。

路由策略及验证边界见[路由引擎设计](route-engine.md)。
