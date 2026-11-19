# 代理网站与检测范围

在左侧“代理网站”中勾选 GitHub、YouTube，或每行填写一个自定义域名，点击“保存并应用”。无需修改安装目录的规则模板，也无需重新下载订阅。

## 保存与生效

- OpenAI / ChatGPT 始终启用。其他网站与它共用“代理节点”页的当前节点，不是各自独立选节点。
- 未配置订阅时可以先保存；核心关闭时保存不会启动服务；核心运行时校验后热重载，仅新连接使用新规则，必要时刷新网页。
- 用户设置保存在 `INSTALL_HOME/website-routing.json`，默认安装数据目录为 `~/.local/share/openai-proxy-kit/`，文件仅当前用户可读写。更新订阅、迁移配置、升级软件会保留设置。
- 保存失败时恢复配置、探测映射与设置。若恢复尚未完成，页面会提示重试；处理核心故障后再次保存可继续恢复。不要把私有恢复文件或核心配置上传到 Issue。
- 自定义域名包含其子域，独立于预设开关生效。旧手工配置的其他规则会保留；kit 管理的域名规则优先，关闭的预设显式直连，其他手工规则仍可能影响未管理域名的实际走向。
- 默认兜底保持 DIRECT，私有地址规则优先；不启用 TUN 或接管系统 DNS。

## 预设范围

GitHub 预设包含 `github.com`、`githubusercontent.com`、`githubassets.com`、`github.io` 及其子域，覆盖常见网页、API、资源和 Pages。

YouTube 预设包含 `youtube.com`、`youtu.be`、`youtube-nocookie.com`、`googlevideo.com`、`ytimg.com`、`ggpht.com`，以及部分 YouTube API、登录和资源主机名。不代理整个 `google.com`、`googleapis.com` 或所有 Google 服务。

这些是维护的有限静态域名集合，不能保证覆盖服务的所有功能和后续变化。缺少的公开目标可加入自定义域名，避免填写公司内网域名。参考：[GitHub 网络访问说明](https://docs.github.com/en/get-started/using-github/allowing-access-to-githubs-services-from-a-restricted-network)、[Google 防火墙与代理说明](https://knowledge.workspace.google.com/admin/security/firewall-and-proxy-settings)、[Cisco 服务关联域名参考](https://www.cisco.com/c/en/us/support/docs/security/umbrella/224706-add-required-domains-to-allow-list-for.pdf)。

## 检测结果的含义

检测按钮只发起固定公开 HTTPS 请求，不携带账号 Cookie、OpenAI 登录令牌或第三方订阅凭据：

| 预设 | 检测目标与判据 |
| --- | --- |
| OpenAI / ChatGPT | ChatGPT 的 trace、robots 公开端点，检查 HTTP 状态和响应语义。 |
| GitHub | `https://api.github.com/meta`，检查 HTTP 200 和预期 JSON 字段。 |
| YouTube | `https://www.youtube.com/robots.txt`，检查 HTTP 200 和 robots 响应。 |

“公开端点可达”不代表账号登录、Git 操作、完整下载或视频播放都成功。限流、验证码、节点出口与服务状态仍可能影响实际业务。检测不会写入节点选择；检测期间节点或规则变更会要求重新检测。自定义域名不在固定端点检测范围内。

自动故障切换仍由 OpenAI 探测驱动；YouTube 检测失败不会为它单独更换节点。

## 应用必须实际使用代理

网站勾选只决定进入本工具的流量如何分流。浏览器需启用系统代理，桌面应用可能需要启动器适配，终端 Git 的 HTTPS 代理需自行配置。SSH 不会因为勾选 GitHub 自动被 HTTP 系统代理接管。

高级用户可执行 `openai-proxy-kit sites` 查看设置，或通过标准输入向 `openai-proxy-kit sites --apply` 提交设置。一般用户直接使用 GUI，不需要运行 sudo 或手工填写 API 密钥。

## 本轮功能验证

0.4.0 开发时现场执行 102 项单元测试、15 项脚本测试、2 项集成测试及 GTK 实窗检查。覆盖关闭状态保存、非法域名拒绝、配置校验失败不写入、热重载失败恢复、未完成事务重试、并发互斥和订阅更新/升级后保留设置。

隔离运行固定版本 mihomo，验证规则实际重载成功、核心 PID 与当前节点保持不变。使用真实订阅中的当前节点，仅在私有临时核心中验证 OpenAI、GitHub、YouTube 三个公开端点，均通过；测试进程和临时配置结束后清理。GTK 还验证了按钮实际保存、逐服务检测结果、820/1080 宽度及固定操作栏。

此项是具体版本的功能与公开网络验收，不代表账号登录、完整 Git/SSH 工作流或视频播放均已验证。历史系统生命周期验收见[0.3.0 记录](acceptance-2026-11-18.md)。
