"""控制台共用的只读状态、操作结果和有界日志；不依赖图形会话。"""

import json
import os
import re
import selectors
import signal
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from kit_state import load as load_state


def load_env():
    path = Path(os.environ.get("KIT_ENV", Path.home() / ".local/share/openai-proxy-kit/kit.env"))
    if not path.is_file():
        return {}
    try:
        return load_state(path)
    except (OSError, ValueError):
        return {}


ENV = load_env()
ROOT = Path(__file__).resolve().parent.parent
INSTALL_HOME = Path(ENV.get("INSTALL_HOME", Path.home() / ".local/share/openai-proxy-kit"))
ASSETS = ROOT / "assets"
MIXED_PORT = ENV.get("MIXED_PORT", "7890")
API_PORT = ENV.get("API_PORT", "9090")
OPENAI_GROUP = ENV.get("OPENAI_GROUP", "OpenAI 自动选择")
UNITS = {
    "mihomo": ENV.get("UNIT_MIHOMO", "mihomo-kit.service"),
    "controller": ENV.get("UNIT_CONTROLLER", "openai-route-controller-kit.service"),
    "guard": ENV.get("UNIT_GUARD_TIMER", "proxy-guard-kit.timer"),
}
GUARD_SERVICE = ENV.get("UNIT_GUARD_SERVICE", "proxy-guard-kit.service")
SWITCH_BIN = os.environ.get("OPK_SWITCH_BIN", str(ROOT / "scripts/switch.sh"))
MIHOMO_API = f"http://127.0.0.1:{API_PORT}"
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def refresh_config():
    global INSTALL_HOME, MIXED_PORT, API_PORT, OPENAI_GROUP, MIHOMO_API, GUARD_SERVICE
    ENV.clear()
    ENV.update(load_env())
    INSTALL_HOME = Path(ENV.get('INSTALL_HOME', Path.home() / '.local/share/openai-proxy-kit'))
    MIXED_PORT = ENV.get('MIXED_PORT', '7890')
    API_PORT = ENV.get('API_PORT', '9090')
    OPENAI_GROUP = ENV.get('OPENAI_GROUP', 'OpenAI 自动选择')
    MIHOMO_API = f'http://127.0.0.1:{API_PORT}'
    for key, envkey, default in [('mihomo', 'UNIT_MIHOMO', 'mihomo-kit.service'), ('controller', 'UNIT_CONTROLLER', 'openai-route-controller-kit.service'), ('guard', 'UNIT_GUARD_TIMER', 'proxy-guard-kit.timer')]:
        UNITS[key] = ENV.get(envkey, default)
    GUARD_SERVICE = ENV.get('UNIT_GUARD_SERVICE', 'proxy-guard-kit.service')


def command(args, timeout=8, **kwargs):
    try:
        with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              start_new_session=True, **kwargs) as process:
            try:
                out, error = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.communicate()
                return subprocess.CompletedProcess(args, 124, '', f'操作超时（{timeout} 秒）：{args[0]}')
            return subprocess.CompletedProcess(args, process.returncode, out, error)
    except OSError as exc:
        return subprocess.CompletedProcess(args, 127, '', str(exc))


def unit_active(unit):
    return command(["systemctl", "--user", "is-active", "--quiet", unit], timeout=3).returncode == 0


def proxy_setting(schema, key):
    result = command(["gsettings", "get", schema, key], timeout=3)
    return result.stdout.strip().strip("'") if result.returncode == 0 else "unknown"


def sys_proxy_mode():
    return proxy_setting("org.gnome.system.proxy", "mode")


def count_injected():
    try:
        manifest = json.loads((INSTALL_HOME / "backup/desktop/manifest.json").read_text())
        return sum(Path(p).is_file() and "# openai-proxy-kit injected" in Path(p).read_text()
                   for p in manifest.get("injected", {}))
    except (OSError, ValueError):
        return 0


def api_headers():
    secret = ENV.get("MIHOMO_SECRET", "")
    return {"Authorization": "Bearer " + secret} if secret else {}


def mihomo_get(path, timeout=2):
    try:
        with HTTP.open(urllib.request.Request(MIHOMO_API + path, headers=api_headers()), timeout=timeout) as response:
            return json.load(response)
    except (OSError, ValueError):
        return None


def query_status():
    services = {key: unit_active(unit) for key, unit in UNITS.items()}
    mode = sys_proxy_mode()
    targets = [(proxy_setting(f"org.gnome.system.proxy.{kind}", "host"),
                proxy_setting(f"org.gnome.system.proxy.{kind}", "port"))
               for kind in ("http", "https", "ftp", "socks")] if mode == "manual" else []
    owned = mode == "manual" and any(host == "127.0.0.1" and port == MIXED_PORT
                                      for host, port in targets)
    group = mihomo_get("/proxies/" + urllib.parse.quote(OPENAI_GROUP, safe=""))
    group = group if isinstance(group, dict) else {}
    running = services["mihomo"] and mode == "manual" and all(
        h == "127.0.0.1" and p == MIXED_PORT for h, p in targets[:2]) and bool(group.get("all"))
    phase = "on" if running else "partial" if any(services.values()) or owned else "off"
    return {"services": services, "sys_proxy": mode, "proxy_owned": owned,
            "proxy_target": " / ".join(f"{h}:{p}" for h, p in targets),
            "injected": count_injected(), "running": running, "phase": phase,
            "group_now": group.get("now"), "nodes": group.get("all", []),
            "installed": bool(ENV), "checked_at": time.strftime("%H:%M:%S")}


def run_switch(on):
    return command(["bash", SWITCH_BIN, "start" if on else "stop"], timeout=75)


def put_node(node):
    req = urllib.request.Request(
        MIHOMO_API + "/proxies/" + urllib.parse.quote(OPENAI_GROUP, safe=""),
        method="PUT", data=json.dumps({"name": node}).encode(),
        headers={"Content-Type": "application/json", **api_headers()})
    try:
        with HTTP.open(req, timeout=5) as response:
            return response.status == 204
    except OSError:
        return False


def delay_test():
    query = urllib.parse.urlencode({"url": "https://chatgpt.com", "timeout": 5000})
    data = mihomo_get("/group/" + urllib.parse.quote(OPENAI_GROUP, safe="") + "/delay?" + query, 30)
    return data if isinstance(data, dict) else {}


def redact(message):
    text = str(message)
    if ENV.get("SUB_URL"):
        text = text.replace(ENV["SUB_URL"], "[订阅已隐藏]")
    text = re.sub(r"(?im)(\b(?:cookie|set-cookie)\s*[:=]\s*)[^\r\n]+", r"\1[已隐藏]", text)
    text = re.sub(r"(?i)\b(https?://)[^\s<>\"']+", "[URL 已隐藏]", text)
    text = re.sub(r'''(?ix)(\b(?:authorization|password|secret|(?:access[_-]|refresh[_-])?token|cookie|set-cookie|api[_-]?key)["']?\s*[:=]\s*)
                  (?:"[^"]*"|'[^']*'|(?:Bearer\s+)?[^\s,;}]+)''', r"\1[已隐藏]", text)
    return text[:8000]


@dataclass(frozen=True)
class LogEntry:
    timestamp: str
    source: str
    level: str
    message: str

    def line(self):
        return f"{self.timestamp}  {self.level:<5}  [{self.source}] {self.message}"


class LogStore:
    def __init__(self, limit=1200):
        self.entries = deque(maxlen=limit)
        self.lock = threading.Lock()
        self.revision = 0

    def add(self, source, message, level="INFO", timestamp=None):
        entry = LogEntry(timestamp or time.strftime("%H:%M:%S"), source, level, redact(message))
        with self.lock:
            self.entries.append(entry)
            self.revision += 1

    def snapshot(self, source="全部来源", level="全部级别", query=""):
        with self.lock:
            entries = list(self.entries)
        return [e for e in entries if (source == "全部来源" or e.source == source)
                and (level == "全部级别" or e.level == level)
                and query.casefold() in e.line().casefold()]

    def clear(self):
        with self.lock:
            self.entries.clear()
            self.revision += 1


class LogFollower:
    """关闭查看时终止 journalctl；有界读取并跟随旧部署的守护文件。"""

    def __init__(self, store):
        self.store = store
        self.process = None
        self.thread = None
        self.stop_event = threading.Event()
        self.cursor = None
        self.file_offset = None
        self.file_identity = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._follow, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=4)

    def _record(self, line):
        try:
            row = json.loads(line)
            self.cursor = row.get("__CURSOR", self.cursor)
            unit = row.get("_SYSTEMD_USER_UNIT", "")
            source = next((k for k, v in UNITS.items() if v == unit), "guard" if unit == GUARD_SERVICE else "系统")
            priority = int(row.get("PRIORITY", 6))
            level = "ERROR" if priority <= 3 else "WARN" if priority == 4 else "INFO"
            message = row.get("MESSAGE", "")
            # stdout 的 journal 优先级通常是 INFO，优先识别组件自己的日志级别。
            try:
                structured = json.loads(message)
                own_level = structured.get("level", "") if isinstance(structured, dict) else ""
            except (ValueError, TypeError):
                match = re.search(r"\blevel=(error|warning|warn|info|debug)\b", str(message), re.I)
                own_level = match.group(1) if match else ""
            level = {"error": "ERROR", "warning": "WARN", "warn": "WARN", "info": "INFO",
                     "debug": "DEBUG"}.get(str(own_level).lower(), level)
            stamp = time.strftime("%H:%M:%S", time.localtime(int(row.get("__REALTIME_TIMESTAMP", 0)) / 1e6))
            self.store.add(source, message, level, stamp)
        except (ValueError, TypeError):
            if line.strip():
                self.store.add("系统", line.strip(), "WARN")

    def _guard_file(self):
        path = Path(ENV.get("GUARD_LOG", INSTALL_HOME / "proxy-guard.log"))
        try:
            stat = path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if identity != self.file_identity:
                self.file_identity = identity
                self.file_offset = max(0, stat.st_size - 32768)
            if self.file_offset > stat.st_size:
                self.file_offset = 0
            with path.open("rb") as stream:
                stream.seek(self.file_offset)
                chunk = stream.read(32768)
                # 留下未写完的行，下一轮继续读取。
                end = chunk.rfind(b"\n") + 1
                if end == 0 and len(chunk) == 32768:
                    end = len(chunk)
                self.file_offset += end
                for line in chunk[:end].decode(errors="replace").splitlines():
                    self.store.add("guard", line)
        except OSError:
            pass

    def _follow(self):
        args = ["journalctl", "--user", "--follow", "--no-pager", "--output=json"]
        args += ["--after-cursor=" + self.cursor] if self.cursor else ["--lines=80"]
        for unit in dict.fromkeys([*UNITS.values(), GUARD_SERVICE]):
            args += ["--unit", unit]
        try:
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self.process = process
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                pending = b""
                while not self.stop_event.is_set():
                    self._guard_file()
                    for key, _ in selector.select(timeout=0.3):
                        chunk = os.read(key.fileobj.fileno(), 16384)
                        if not chunk:
                            if not self.stop_event.is_set():
                                self.store.add("系统", "日志读取已结束，可重新打开实时跟随", "WARN")
                            return
                        pending += chunk
                        lines = pending.split(b"\n")
                        pending = lines.pop()[-65536:]
                        for line in lines:
                            self._record(line.decode(errors="replace"))
        except OSError as exc:
            self.store.add("系统", f"无法读取服务日志：{exc}", "ERROR")
        finally:
            process = self.process
            if process:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                process.stdout.close()
            self.process = None


def main():
    """命令行日志入口，与 GUI 使用同一日志管道。"""
    import argparse
    parser = argparse.ArgumentParser(description="openai-proxy-kit 实时日志（自动脱敏）")
    parser.add_argument("--follow", action="store_true")
    args = parser.parse_args()
    store = LogStore()
    reader = LogFollower(store)
    if not args.follow:
        journal = ["journalctl", "--user", "--no-pager", "--output=json", "--lines=80"]
        for unit in dict.fromkeys([*UNITS.values(), GUARD_SERVICE]):
            journal += ["--unit", unit]
        result = command(journal)
        for line in result.stdout.splitlines():
            reader._record(line)
        if result.returncode:
            store.add("系统", result.stderr or "读取日志失败", "ERROR")
        reader._guard_file()
        for entry in store.snapshot():
            print(entry.line())
        return
    reader.start()
    seen = 0
    try:
        while reader.thread.is_alive():
            time.sleep(0.2)
            with store.lock:
                revision = store.revision
                entries = list(store.entries)[-min(revision - seen, len(store.entries)):]
            if revision != seen:
                for entry in entries:
                    print(entry.line(), flush=True)
                seen = revision
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()


if __name__ == "__main__":
    main()
