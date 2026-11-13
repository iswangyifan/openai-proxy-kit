#!/usr/bin/env python3
"""openai-proxy-kit 桌面启动器代理注入器

背景（Linux 实战坑）: VSCode 的 Codex 插件、ChatGPT 桌面版等 Electron/Rust
应用的网络栈不读 GNOME 系统代理设置，只认 http_proxy/https_proxy 环境变量。
本脚本把用户级 .desktop 的 Exec 行改为 `Exec=env http_proxy=... 原命令`，
使应用从应用菜单启动时自动带代理。

安全设计:
  - 永不修改 /usr/share/applications 系统文件，只生成用户级覆盖版
  - 若目标 .desktop 原本就在用户目录，先备份到 backup 目录再原地修改
  - 注入/还原状态记录在 manifest.json，供 uninstall 还原
  - 幂等: 已注入（含 marker 注释）的文件跳过

动作语义:
  - inject / restore : 安装 / 卸载（restore 清空 manifest）
  - enable / disable : 日常开关（disable 保留 manifest，enable 按 manifest 重新注入）
    用于"一键关闭代理"时把应用还原为直连启动，之后可无损恢复
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

MARKER = "# openai-proxy-kit injected"
ENV_PREFIX_TMPL = (
    "http_proxy={proxy} https_proxy={proxy} HTTP_PROXY={proxy} HTTPS_PROXY={proxy} "
    "no_proxy=localhost,127.0.0.1 NO_PROXY=localhost,127.0.0.1"
)


def die(msg: str):
    print(f"[inject_desktop] 错误: {msg}", file=sys.stderr)
    sys.exit(1)


def load_manifest(backup_dir: Path) -> dict:
    mf = backup_dir / "manifest.json"
    if mf.is_file():
        return json.loads(mf.read_text(encoding="utf-8"))
    return {"injected": {}}


def save_manifest(backup_dir: Path, manifest: dict):
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def is_injected(text: str) -> bool:
    return MARKER in text


def inject_text(text: str, proxy: str) -> str:
    """给所有 Exec= 行加 env 前缀（含 [Desktop Action] 段内的）"""
    env_vars = ENV_PREFIX_TMPL.format(proxy=proxy)
    lines = text.splitlines(keepends=True)
    out, marker_added = [], False
    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith("Exec=") and not stripped.startswith("Exec=env http_proxy="):
            indent = ln[:len(ln) - len(ln.lstrip())]
            cmd = stripped[len("Exec="):]
            out.append(f"{MARKER}\n")
            marker_added = True
            out.append(f"{indent}Exec=env {env_vars} {cmd}\n")
        else:
            out.append(ln)
    if not marker_added:
        die("文件中没有可注入的 Exec= 行")
    return "".join(out)


def resolve_target(orig: Path, user_apps: Path) -> tuple[Path, bool]:
    """决定写入目标与是否原地修改。返回 (目标文件, 是否用户目录原地)"""
    if orig.parent == user_apps:
        return orig, True
    # 系统级 → 生成用户级覆盖
    return user_apps / orig.name, False


def do_inject(args, src: Path, proxy: str, user_apps: Path, backup_dir: Path, manifest: dict):
    if not src.is_file():
        die(f"desktop 文件不存在: {src}")

    text = src.read_text(encoding="utf-8")
    if is_injected(text):
        print(f"[inject_desktop] 跳过（已注入）: {src.name}")
        return

    target, in_place = resolve_target(src, user_apps)

    # 备份原文件（系统级备份源文件；用户级原地修改前也备份）
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{src.name}.orig"
    if not backup_path.exists():
        shutil.copy2(src, backup_path)

    new_text = inject_text(text, proxy)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(new_text, encoding="utf-8")

    manifest["injected"][str(target)] = {
        "source": str(src),
        "in_place": in_place,
        "backup": str(backup_path),
    }
    print(f"[inject_desktop] 已注入: {src.name} → {target}"
          f"{'（用户级覆盖）' if not in_place else '（原地，已备份）'}")


def do_restore(user_apps: Path, backup_dir: Path, manifest: dict) -> int:
    count = 0
    for target, info in list(manifest["injected"].items()):
        t = Path(target)
        if t.is_file():
            t.unlink()
            count += 1
            print(f"[inject_desktop] 已移除注入: {t.name}")
        # 原地修改的（如 ~/.local 下原生的 chatgpt.desktop）从备份恢复
        if info.get("in_place"):
            b = Path(info["backup"])
            if b.is_file() and not t.exists():
                shutil.copy2(b, t)
                print(f"[inject_desktop] 已从备份恢复: {t.name}")
    manifest["injected"] = {}
    save_manifest(backup_dir, manifest)
    print(f"[inject_desktop] 共处理 {count} 个文件（备份保留在 {backup_dir}）")
    return count


def do_disable(user_apps: Path, backup_dir: Path, manifest: dict) -> int:
    """临时停用注入（卸载代理开关用）: 删除注入版启动器，manifest 保留"""
    count = 0
    for target, info in list(manifest["injected"].items()):
        t = Path(target)
        # 只删除确认是注入版的文件，避免误删用户后续手动改过的
        if t.is_file() and is_injected(t.read_text(encoding="utf-8")):
            t.unlink()
            count += 1
            print(f"[inject_desktop] 已停用注入: {t.name}")
        if info.get("in_place"):
            b = Path(info["backup"])
            if b.is_file() and not t.exists():
                shutil.copy2(b, t)
                print(f"[inject_desktop] 已恢复原版: {t.name}")
    print(f"[inject_desktop] 停用完成（{count} 个，manifest 保留，enable 可恢复）")
    return count


def do_enable(args, user_apps: Path, backup_dir: Path, manifest: dict) -> int:
    """恢复被 disable 停用的注入: 按 manifest 重新生成（幂等）"""
    count = 0
    for target, info in list(manifest["injected"].items()):
        src = Path(info["source"])
        if not src.is_file():
            print(f"[inject_desktop] 跳过（源文件已不存在）: {src}")
            continue
        do_inject(args, src, args.proxy, user_apps, backup_dir, manifest)
        count += 1
    print(f"[inject_desktop] 恢复完成（{count} 个）")
    return count


def main():
    ap = argparse.ArgumentParser(description="openai-proxy-kit 桌面启动器代理注入器")
    ap.add_argument("action", choices=["inject", "restore", "scan", "enable", "disable"])
    ap.add_argument("--desktop-file", action="append", default=[],
                    help="要注入的 .desktop 文件路径（可多次指定）")
    ap.add_argument("--proxy", default="http://127.0.0.1:7890")
    ap.add_argument("--user-apps", default="~/.local/share/applications")
    ap.add_argument("--backup-dir", default="~/.local/share/openai-proxy-kit/backup/desktop")
    args = ap.parse_args()

    user_apps = Path(args.user_apps).expanduser()
    backup_dir = Path(args.backup_dir).expanduser()
    manifest = load_manifest(backup_dir)

    if args.action == "restore":
        do_restore(user_apps, backup_dir, manifest)
        return

    if args.action == "disable":
        do_disable(user_apps, backup_dir, manifest)
        return

    if args.action == "enable":
        do_enable(args, user_apps, backup_dir, manifest)
        save_manifest(backup_dir, manifest)
        return

    if args.action == "scan":
        print("常见候选（/usr/share/applications 下含 Exec 的）:")
        known = ["code.desktop", "code_code.desktop", "chatgpt.desktop",
                 "com.microsoft.VSCode.desktop", "com.microsoft.VSCode.UrlHandler.desktop"]
        sys_apps = Path("/usr/share/applications")
        for name in known:
            for cand in (sys_apps / name, user_apps / name):
                if cand.is_file():
                    print(f"  {cand}")
        extra = [p for p in sys_apps.glob("*.desktop")
                 if re.search(r"(?i)(chatgpt|vscode|cursor|codex)", p.name)]
        for p in sorted(set(extra)):
            print(f"  {p}")
        return

    if not args.desktop_file:
        die("inject 需要 --desktop-file（可先运行 scan 查看候选）")

    for f in args.desktop_file:
        do_inject(args, Path(f).expanduser(), args.proxy, user_apps, backup_dir, manifest)

    save_manifest(backup_dir, manifest)


if __name__ == "__main__":
    main()
