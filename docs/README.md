# openai-proxy-kit

Linux 上一站式 **ChatGPT / Codex 无感代理**部署工具：填一个机场订阅 URL，跑一次
`install.sh`，后台全自动——不用再"挂梯子"。

```bash
git clone <本仓库>
cd openai-proxy-kit
SUB_URL="https://你的机场订阅" ./install.sh
```

## 它解决什么问题

在 Linux 上让 ChatGPT / Codex "像没被墙一样"使用，需要同时打通五层，任何一层
缺失都会表现为"卡、慢、转圈、加载不出账号"：

| 层 | 问题 | 本工具 |
|---|---|---|
| 代理核心 | 没有常驻代理，每次手动开 | mihomo systemd 用户服务，开机自启 |
| 分流 | 全局代理会拖慢国内流量 | 规则分流：仅 OpenAI 系域名走代理，国内直连 |
| 系统代理 | 应用退出时把 GNOME 代理开关关掉导致全断网 | proxy-guard 每分钟自动恢复 |
| 应用注入 | **VSCode 的 Codex 插件、ChatGPT 桌面版不读系统代理、只认环境变量**（Rust/Electron 网络栈） | 桌面启动器自动注入 `http_proxy` |
| 节点保活 | 机场节点失效要手动换 | 集成 [openai-route-controller](https://github.com/tianwdong/openai-route-controller)（MIT），每 20s 完整路径探测自动切换/回滚/隔离 |

第 4 层是 Linux 上最隐蔽的坑：系统代理设置看着一切正常，但 Codex 插件每个请求
都在直连等超时。诊断方法见 `./diagnose.sh --pid <插件进程号>`。

## 与 openai-route-controller 的分工

上游项目（MIT）只做"路由熔断"一个环节，且仅支持 macOS/Windows，前提是用户已
装好 Clash Verge Rev 并手动合并扩展脚本。本项目补齐 Linux 全链路：

```
本工具:  mihomo 安装 → 订阅解析生成配置(含 OpenAI 动态分组+probe 监听)
          → systemd 常驻 → 系统代理守护 → 桌面启动器注入 → 诊断
上游组件: OpenAI 路由熔断（候选验真/自动切换/失败回滚/节点隔离）
```

上游在安装时自动 `git clone` 到 `~/.local/share/openai-proxy-kit/openai-route-controller/`，
版权与来源声明见 [NOTICE](NOTICE)。

## 架构

```
浏览器 ──────────┐                        ┌─> OpenAI 域名 ──> 「OpenAI 自动选择」组 ──> 机场节点
                  ├─ GNOME 系统代理 ──> mihomo (7890)
VSCode/Codex ────┤   (proxy-guard 守护)   │
ChatGPT 桌面版 ──┘                        └─> 国内域名/IP ──> DIRECT 直连

openai-route-controller ── mihomo API(9090) 控制组选择
                          probe 端口(17900+) 逐节点完整路径验真
```

- 订阅凭据只存在于 `proxy-providers` 的 URL 引用中，生成的配置文件零凭据
- probe 监听（17900 起）与上游 controller 的"候选名去重+字典序排序+端口偏移"
  约定严格一致，由 `gen_config.py` 自动生成

## 快速开始

依赖：Linux + systemd 用户服务 + GNOME 桌面（非 GNOME 亦可，跳过守护/注入段）+
python3 + python3-yaml；熔断器需 Node.js >= 22（缺失会跳过，可后补）。

```bash
# 安装（交互式，会询问订阅 URL、要注入哪些桌面应用）
SUB_URL="https://..." ./install.sh

# 日常
./diagnose.sh                     # 全链路诊断（PASS/FAIL 清单）
./install.sh start                # 一键开启整条代理链路
./install.sh stop                 # 一键关闭（公司内网/仓库恢复直连）
./install.sh status               # 查看各层状态
./install.sh update               # 机场节点变更后重新生成配置
./install.sh desktop              # 补充注入桌面启动器
./install.sh controller           # 补装路由熔断器（装好 Node 后）
./install.sh gui                  # 补装桌面控制台（装好 GTK4 后）

# 卸载（逐项确认，完整回滚）
./uninstall.sh
```

管理面板：`http://127.0.0.1:9090/ui/`（metacubexd，可切组内节点）

### 桌面控制台（FlClash 风格 GUI）

安装后应用菜单出现「代理控制台」——一个 GTK4 + libadwaita 原生窗口（与系统设置
同风格），不是网页：

- **大电源开关**：一键启停整条代理链路（服务 + 系统代理 + 桌面注入）
- 服务状态卡片（mihomo / 熔断器 / 守护 / 系统代理 / 桌面注入）
- OpenAI 组节点列表：点击切换、批量延迟测试
- 一键跳转 metacubexd 管理面板

开关按钮背后的实现与 CLI 子命令 `./install.sh start|stop|status` 是同一份
（`scripts/switch.sh`），GUI 只是壳。`start` 有防断网保护：mihomo 端口未就绪时
不会把系统代理指过去（比如 FlClash 抢占 7890 的场景）。

依赖 `python3-gi` + GTK4 + libadwaita（GNOME 桌面自带）。缺失时安装自动跳过
GUI，补装依赖后运行 `./install.sh gui` 即可：

```bash
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adwaita-1
```

### 影子验证（不动现有代理环境）

本机已有其他代理部署、想先验证本工具时：

```bash
INSTALL_HOME=~/.local/share/openai-proxy-kit-test \
MIHOMO_CONF_DIR=$HOME/.config/mihomo-kit-shadow \
MIXED_PORT=17890 API_PORT=19090 DNS_PORT=1153 \
ASSUME_YES=1 SUB_URL="https://..." SHADOW=1 ./install.sh
```

影子模式：配置目录/端口全部隔离，systemd unit 渲染为 `*-test` 名但不启动，
跳过系统代理守护与桌面注入（唯二会写真实系统状态的动作）。

## 可配置项（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `SUB_URL` | 交互询问 | 机场订阅 URL（Clash yaml 格式，机场后台均提供） |
| `MIXED_PORT` / `API_PORT` / `DNS_PORT` | 7890 / 9090 / 1053 | mihomo 端口 |
| `PROBE_PORT_BASE` | 17900 | **必须 17900**（上游 controller 硬编码约定） |
| `OPENAI_GROUP` | OpenAI 自动选择 | 组名 |
| `OPENAI_FILTER` | 空（全部节点） | 正则，筛选进入 OpenAI 组的候选（如 `HY2|JP|KR|TW`） |
| `MAX_NODES` | 60 | 节点数上限（防 probe 监听撑爆端口段） |
| `INSTALL_HOME` | ~/.local/share/openai-proxy-kit | 安装目录 |
| `MIHOMO_CONF_DIR` | ~/.config/mihomo | mihomo 配置目录 |
| `UPSTREAM_REF` | main | 上游熔断器版本 |

## 风险声明

- 仅对 5 条 OpenAI 系域名（openai.com / chatgpt.com / oaistatic.com /
  oaiusercontent.com / cdn.auth0.com）做接管分流，不碰其他流量策略；国内域名
  与 IP 全部直连
- 订阅节点名是快照：机场改了节点名后 probe 引用会失效，运行 `./install.sh update`
  重新生成即可（mihomo 对组内不存在的节点名会启动失败，update 是修复手段）
- systemd 服务以 `*-kit` 后缀命名，与用户已有的手工部署（裸名 unit）可共存；
  若要切换管理权，先停用旧 unit 再安装本工具
- 熔断器的能力边界：它验证"页面可达"，探不出节点出口 IP 被 OpenAI 登录风控
  拒绝（Cloudflare 403 地区限制）。此时在管理面板手动切换组内其他节点

## 测试

```bash
bash tests/run_tests.sh
```

自包含单元测试（合成订阅数据、不联网、不写真实系统状态）：配置生成的
probe 排序约定/筛选/报错/清洗/截断、桌面注入全生命周期（注入/幂等/还原/
disable/enable 启停循环）、switch.sh 未安装拒绝、gui.py headless 冒烟。
完整的影子安装冒烟见上文"影子验证"。

## 常见问题

见 [troubleshooting.md](troubleshooting.md)——收录实战踩过的坑：FlClash 端口
冲突、Codex 插件不读系统代理、登录 403 风控、订阅节点名变更等。

## 许可证

[MIT](LICENSE)。集成 [tianwdong/openai-route-controller](https://github.com/tianwdong/openai-route-controller)（MIT），见 [NOTICE](NOTICE)。
