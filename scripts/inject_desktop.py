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
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from kit_state import private_write

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
    private_write(backup_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))



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

    existing = manifest["injected"].get(str(target))
    if target.exists() and is_injected(target.read_text(encoding='utf-8')):
        if not existing:
            die(f"启动器已有代理标记但缺少恢复记录，保留文件：{target.name}")
        digest = existing.get('injected_sha256')
        if digest and hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            die(f"启动器注入后已被编辑，保留文件：{target.name}")
        print(f"[inject_desktop] 跳过（用户覆盖已注入）: {target.name}")
        return
    if existing and target.exists() and not is_injected(target.read_text(encoding='utf-8')):
        backup = Path(existing['backup'])
        if backup.exists() and target.read_bytes() != backup.read_bytes():
            die(f"启动器已被用户修改，保留原文件，请重新选择适配：{target.name}")
    original_exists = target.exists()
    original = target if original_exists else src
    text = original.read_text(encoding='utf-8')
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    suffix = '.user.orig' if original_exists and not in_place else '.orig'
    backup_path = backup_dir / f"{src.name}{suffix}"
    if not backup_path.exists():
        shutil.copy2(original, backup_path)
        backup_path.chmod(0o600)
    new_text = inject_text(text, proxy)
    private_write(target, new_text)
    manifest["injected"][str(target)] = {
        "source": str(src), "in_place": in_place,
        "restore_original": original_exists, "backup": str(backup_path),
        "injected_sha256": hashlib.sha256(new_text.encode()).hexdigest(),
    }
    print(f"[inject_desktop] 已注入: {src.name} → {target}"
          f"{'（用户级覆盖）' if not in_place else '（原地，已备份）'}")


def do_disable(user_apps: Path, backup_dir: Path, manifest: dict) -> int:
    """只替换仍属于本工具且未被用户编辑的文件。"""
    count = 0
    for target, info in list(manifest["injected"].items()):
        t = Path(target)
        b = Path(info["backup"])
        restore = info.get('restore_original', info.get('in_place', False))
        if t.is_file():
            data = t.read_bytes()
            if not is_injected(data.decode('utf-8')):
                continue
            digest = info.get('injected_sha256')
            if digest and hashlib.sha256(data).hexdigest() != digest:
                die(f"启动器注入后已被编辑，保留文件，请手动处理：{t.name}")
            if restore:
                if not b.is_file():
                    die(f"启动器备份缺失，保留文件：{t.name}")
                private_write(t, b.read_text(encoding='utf-8'))
            else:
                t.unlink()
            count += 1
        elif restore and b.is_file():
            private_write(t, b.read_text(encoding='utf-8'))
    print(f"[inject_desktop] 已停用 {count} 个启动器，用户原配置保留")
    return count


def do_restore(user_apps: Path, backup_dir: Path, manifest: dict) -> int:
    count = do_disable(user_apps, backup_dir, manifest)
    manifest["injected"] = {}
    save_manifest(backup_dir, manifest)
    return count


def do_enable(args, user_apps: Path, backup_dir: Path, manifest: dict) -> int:
    """恢复被 disable 停用的注入: 按 manifest 重新生成（幂等）"""
    count = 0
    for target, info in list(manifest["injected"].items()):
        src = Path(info["source"])
        if not src.is_file():
            print(f"[inject_desktop] 跳过（源文件已不存在）: {src}")
            continue
        original_record = dict(info)
        do_inject(args, src, args.proxy, user_apps, backup_dir, manifest)
        manifest['injected'][target]['restore_original'] = original_record.get('restore_original', original_record.get('in_place', False))
        manifest['injected'][target]['backup'] = original_record['backup']
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
