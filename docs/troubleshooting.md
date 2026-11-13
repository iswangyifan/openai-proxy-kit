# troubleshooting — 实战踩坑记录

以下问题全部在一台 Ubuntu 24.04 (GNOME) + VSCode/Codex + ChatGPT 桌面版的
真实机器上踩过并定因，收录于此供排查参考。

## 1. VSCode 的 Codex 插件极卡/转圈，但系统代理"看着正常"

**现象**：插件加载账号失败、模型列表超时、每次请求等 30-60s；浏览器一切正常。

**根因**：Codex 插件是 Rust 网络栈，**不读 GNOME 系统代理设置，只认
`http_proxy`/`https_proxy` 环境变量**。从桌面图标启动的 VSCode 没有这些变量，
插件直连 OpenAI 被墙。

**排查**：
```bash
# 1. 代理链路本身通不通
curl -x http://127.0.0.1:7890 -sI https://chatgpt.com | head -1

# 2. 插件进程有没有代理变量（用 diagnose.sh --pid 或手工）
pgrep -af 'extensions/openai.chatgpt.*codex'   # 找到 pid
tr '\0' '\n' < /proc/<pid>/environ | grep -i proxy
```

**修复**：`./install.sh desktop` 注入桌面启动器（或对已运行实例：完全退出
VSCode 后从应用菜单重启——注意 VSCode 是单实例，旧实例活着时新启动只是给它
开窗口，代理变量不会生效）。

**注意**：改 VSCode 设置里的 `http.proxy` 没用——那只管 VSCode 自身网络，
管不到 Codex 插件这个独立子进程。

## 2. FlClash 冲突

**现象**：安装本工具后代理全瘫 / FlClash 退出后全网断开。

**根因**：FlClash 抢占 7890 端口；其退出时还会把 GNOME 系统代理开关改成
none，导致所有遵循系统代理的应用直连被墙。

**修复**：完全退出 FlClash 再安装本工具（install.sh 会检测并拦截）；已被
关掉的代理开关由 proxy-guard 每分钟自动恢复。

## 3. chatgpt.com 返回 Cloudflare 403 "Country, region, or territory not supported"

**现象**：节点延迟正常、api.openai.com 返回 401（正常），但网页/登录被拒，
cf-ray 落在 LAX 等风控出口。

**根因**：该节点出口 IP 段被 OpenAI 登录风控拉黑——**API 可用但登录被拒**。
熔断器的探测判据是页面可达，探不出这种风控拒绝（能力边界）。

**修复**：在管理面板（http://127.0.0.1:9090/ui/）的「OpenAI 自动选择」组里
手动切换其他节点。实测规律：FDCservers 系（无论叫什么地区）多被拒；
Amazon/Wap.ac 出口的 JP/KR/TW 节点多为可用。

## 4. 机场更新节点后 mihomo 起不来 / probe 引用失效

**现象**：`mihomo-kit.service` 启动失败，日志报组内引用了不存在的节点名；
或 controller 报 `No isolated probe route`。

**根因**：订阅节点名是生成配置时的快照。机场改名/下线节点后，
`OpenAI 自动选择` 组和 probe listeners 里的引用就悬空了。

**修复**：`./install.sh update`（重拉订阅、重新生成配置、重启服务）。

## 5. 注入后应用没生效

**清单**：
- 应用是否**完全退出**过？（托盘里活着不算退出；单实例应用新启动只是开窗口）
- 是否从**应用菜单**启动？（终端里手动 `code` 启动不带注入——除非终端本身
  有代理变量）
- 验证：`tr '\0' '\n' < /proc/<pid>/environ | grep -i proxy` 应看到
  `http_proxy=http://127.0.0.1:7890`

## 6. 注销/重启后服务停了

systemd 用户服务在用户会话结束时会停止。若需常驻：

```bash
loginctl enable-linger $USER
```

install.sh 检测到未开启 linger 时会提示。
