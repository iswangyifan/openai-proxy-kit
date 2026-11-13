#!/usr/bin/env python3
"""openai-proxy-kit 配置生成器: 机场订阅 → mihomo 配置

职责:
  1. 拉取订阅（http(s) URL 或本地文件路径，Clash yaml 格式）
  2. 提取全部 proxies 并清洗（去重/去空/截断上限）
  3. 生成 mihomo 配置: 固定段模板 + 静态 proxies + OpenAI 动态分组 + 置顶规则
     + probe listeners
     （组与 listeners 均为「节点名快照」设计——上游 controller 的 probe 端口
     映射依赖静态节点名；订阅节点变更后由 install.sh update 重新生成）

probe listeners 端口映射约定（必须与上游 openai-route-controller 一致）:
  controller.mjs 的 nodeProbeProxyUrl() 按候选名「去重 + 字典序排序」后的
  索引分配端口 PROBE_BASE_PORT + i（上游硬编码 17900）。本生成器对
  OpenAI 组候选做同样的 sorted(set(names)) 排序生成 listeners。
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

import yaml

PROBE_LISTEN_HOST = "127.0.0.1"
GENERATED_HEADER = (
    "# ============================================================\n"
    "# 本文件由 openai-proxy-kit 自动生成，请勿手工编辑\n"
    "# 重新生成会覆盖；节点名来自订阅快照，订阅节点变更后请运行:\n"
    "#   ./install.sh update\n"
    "# ============================================================\n"
)


def fetch_subscription(source: str) -> str:
    """拉取订阅: http(s) 用 curl，否则按本地文件读取"""
    if source.startswith(("http://", "https://")):
        r = subprocess.run(
            ["curl", "-fsSL", "--connect-timeout", "15", "--max-time", "60", source],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            die(f"订阅拉取失败: {source}\n{r.stderr.strip()}")
        return r.stdout
    p = Path(source).expanduser()
    if not p.is_file():
        die(f"订阅文件不存在: {source}")
    return p.read_text(encoding="utf-8", errors="replace")


def die(msg: str):
    print(f"[gen_config] 错误: {msg}", file=sys.stderr)
    sys.exit(1)


def extract_proxies(text: str) -> list[dict]:
    """从 Clash yaml 订阅提取 proxies（保持订阅原始顺序）"""
    stripped = text.lstrip()
    if not re.search(r"(?m)^proxies\s*:", stripped):
        die("订阅不是 Clash yaml 格式（缺少 proxies 段）。\n"
            "请向机场索取 Clash 格式的订阅链接（绝大多数机场后台均提供）；\n"
            "base64 分享链接格式不直接受支持")
    try:
        data = yaml.safe_load(stripped)
    except yaml.YAMLError as e:
        die(f"订阅 yaml 解析失败: {e}")
    proxies = data.get("proxies") if isinstance(data, dict) else None
    if not proxies:
        die("订阅 yaml 中 proxies 为空")
    result = [p for p in proxies if isinstance(p, dict) and p.get("name")]
    if not result:
        die("订阅 yaml 中未提取到任何节点")
    return result


def clean_proxies(proxies: list[dict], max_nodes: int) -> list[dict]:
    """清洗: 去空/去重（重名追加 -2 后缀，同步回节点 dict）、超长截断"""
    result, seen = [], set()
    for p in proxies:
        n = str(p["name"]).strip()
        if not n or len(n) > 120:
            continue
        base, dup = n, 2
        while n in seen:
            n = f"{base}-{dup}"
            dup += 1
        seen.add(n)
        p = dict(p)
        p["name"] = n
        result.append(p)
    if len(result) > max_nodes:
        truncated = len(result) - max_nodes
        result = result[:max_nodes]
        print(f"[gen_config] 警告: 节点数超过上限 {max_nodes}，已截断 {truncated} 个"
              f"（可用 --max-nodes 调整）", file=sys.stderr)
    return result


def build_config(proxies: list[dict], openai_names: list[str], args) -> dict:
    """拼装完整 mihomo 配置 dict"""
    tmpl = Path(args.templates_dir) / "config-base.yaml.tmpl"
    base_text = tmpl.read_text(encoding="utf-8")
    for var, val in (("MIXED_PORT", args.mixed_port), ("API_PORT", args.api_port),
                     ("DNS_PORT", args.dns_port)):
        base_text = base_text.replace(f"@{var}@", str(val))

    try:
        config = yaml.safe_load(base_text)
    except yaml.YAMLError as e:
        die(f"基础模板渲染后解析失败（模板损坏?）: {e}")

    # 静态 proxies: 组与 probe listeners 都按「节点名快照」设计（上游 controller
    # 的 probe 端口映射约定依赖静态节点名），订阅更新由 install.sh update 重新生成
    config["proxies"] = proxies

    # --- proxy-groups ---
    config["proxy-groups"] = [
        {
            "name": args.openai_group,
            "type": "select",
            "proxies": list(openai_names),
        },
        {
            "name": "自动选择",
            "type": "url-test",
            "proxies": [p["name"] for p in proxies],
            "url": "http://www.gstatic.com/generate_204",
            "interval": 300,
        },
        {
            "name": "漏网之鱼",
            "type": "select",
            "proxies": ["自动选择", "DIRECT"],
        },
    ]

    # --- rules: 渲染组名 ---
    rules_tmpl = (Path(args.templates_dir) / "rules-base.yaml").read_text(encoding="utf-8")
    rules_text = (rules_tmpl
                  .replace("@OPENAI_GROUP@", args.openai_group)
                  .replace("@FALLBACK_GROUP@", "漏网之鱼"))
    config["rules"] = yaml.safe_load(rules_text)

    # --- probe listeners: 与上游 controller 的「去重+排序+端口偏移」约定严格一致 ---
    listeners = []
    for i, name in enumerate(sorted(set(openai_names))):
        listeners.append({
            "name": f"openai-route-probe-{i}",
            "type": "mixed",
            "listen": PROBE_LISTEN_HOST,
            "port": args.probe_port_base + i,
            "udp": False,
            "proxy": name,
        })
    config["listeners"] = listeners
    return config


def main():
    ap = argparse.ArgumentParser(description="openai-proxy-kit 订阅配置生成器")
    ap.add_argument("--sub-url", required=True,
                    help="机场订阅 URL（http/https）或本地订阅文件路径")
    ap.add_argument("--out", required=True, help="输出 mihomo 配置文件路径")
    ap.add_argument("--templates-dir", default=None,
                    help="模板目录（默认取脚本同级 ../templates）")
    ap.add_argument("--mixed-port", type=int, default=7890)
    ap.add_argument("--api-port", type=int, default=9090)
    ap.add_argument("--dns-port", type=int, default=1053)
    ap.add_argument("--probe-port-base", type=int, default=17900,
                    help="probe 监听端口基址（须与 openai-route-controller 约定一致，默认 17900）")
    ap.add_argument("--openai-group", default="OpenAI 自动选择")
    ap.add_argument("--openai-filter", default=None,
                    help="正则，从全部节点中筛选 OpenAI 组候选（默认全部节点）")
    ap.add_argument("--max-nodes", type=int, default=60,
                    help="节点数上限，防止 listeners 撑爆端口段（默认 60）")
    ap.add_argument("--mihomo-bin", default=None,
                    help="mihomo 二进制路径，提供则生成后自动做 -t 语法校验")
    ap.add_argument("--mihomo-conf-dir", default=None,
                    help="-t 校验用的 mihomo 配置目录（geodata 所在）")
    args = ap.parse_args()

    if args.templates_dir is None:
        args.templates_dir = str(Path(__file__).resolve().parent.parent / "templates")

    print(f"[gen_config] 拉取订阅: {args.sub_url[:60]}{'...' if len(args.sub_url) > 60 else ''}")
    text = fetch_subscription(args.sub_url)

    proxies = extract_proxies(text)
    print(f"[gen_config] 提取到 {len(proxies)} 个节点")
    proxies = clean_proxies(proxies, args.max_nodes)
    print(f"[gen_config] 清洗后保留 {len(proxies)} 个节点")
    names = [p["name"] for p in proxies]

    if args.openai_filter:
        try:
            pat = re.compile(args.openai_filter)
        except re.error as e:
            die(f"--openai-filter 正则无效: {e}")
        openai_names = [n for n in names if pat.search(n)]
    else:
        openai_names = list(names)
    if not openai_names:
        die("OpenAI 组候选为空（--openai-filter 过滤后无匹配节点）")
    print(f"[gen_config] OpenAI 组候选 {len(openai_names)} 个，"
          f"probe 端口 {args.probe_port_base}-{args.probe_port_base + len(openai_names) - 1}")

    config = build_config(proxies, openai_names, args)

    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)

    tmp = out.with_suffix(out.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(GENERATED_HEADER)
        yaml.dump(config, f, allow_unicode=True, sort_keys=False,
                  default_flow_style=False, width=200)

    # 可选语法校验
    if args.mihomo_bin:
        cmd = [args.mihomo_bin, "-t", "-d",
               args.mihomo_conf_dir or str(out.parent), "-f", str(tmp)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            tmp.unlink(missing_ok=True)
            die(f"mihomo -t 校验失败:\n{r.stdout}\n{r.stderr}")
        print("[gen_config] mihomo -t 语法校验通过")

    tmp.replace(out)
    print(f"[gen_config] 已生成: {out}")


if __name__ == "__main__":
    main()
