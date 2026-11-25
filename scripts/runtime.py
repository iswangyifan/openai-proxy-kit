"""控制台共用的只读状态、操作结果和有界日志；不依赖图形会话。"""

import json
import os
import re
import selectors
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from kit_state import load as load_state
from mihomo_mode import saved_mode


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
    "controller": ENV.get("UNIT_CONTROLLER", "opk-route.service"),
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
    for key, envkey, default in [('mihomo', 'UNIT_MIHOMO', 'mihomo-kit.service'), ('controller', 'UNIT_CONTROLLER', 'opk-route.service'), ('guard', 'UNIT_GUARD_TIMER', 'proxy-guard-kit.timer')]:
        UNITS[key] = ENV.get(envkey, default)
    GUARD_SERVICE = ENV.get('UNIT_GUARD_SERVICE', 'proxy-guard-kit.service')


def command(args, timeout=8, input_text=None, termination_grace=5, **kwargs):
    try:
        if input_text is not None:
            kwargs['stdin'] = subprocess.PIPE
        with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              start_new_session=True, **kwargs) as process:
            try:
                out, error = process.communicate(input=input_text, timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.communicate(timeout=termination_grace)
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


def listener_issues():
    """状态刷新仅做本机短时核验，共用一次 IPv4 监听快照。"""
    result = command(['systemctl', '--user', 'show', UNITS['mihomo'],
                      '-p', 'MainPID', '--value'], timeout=1)
    pid = result.stdout.strip()
    if result.returncode or not pid.isdecimal() or int(pid) <= 0:
        return ['无法确认代理核心 MainPID，端口归属核验失败']
    result = command(['ss', '-4', '-tlnpH',
                      f'( sport = :{API_PORT} or sport = :{MIXED_PORT} )'], timeout=1)
    if result.returncode:
        return ['无法读取本机监听端口，管理 API 与代理入口归属核验失败']
    issues = []
    for title, port in (('管理 API', API_PORT), ('代理入口', MIXED_PORT)):
        rows = []
        for row in result.stdout.splitlines():
            fields = row.split()
            if len(fields) >= 5 and fields[3] in (
                    f'127.0.0.1:{port}', f'0.0.0.0:{port}', f'*:{port}'):
                rows.append(row)
        if not rows:
            issues.append(f'{title}端口 {port} 未在本机 IPv4 回环地址监听')
        elif any(set(re.findall(r'\bpid=(\d+),', row)) != {pid} for row in rows):
            issues.append(f'{title}端口 {port} 不属于代理核心 MainPID 或无法确认归属')
    return issues


def query_status():
    # 配置可能由终端升级或另一个进程更新，长驻 GUI 不能一直沿用导入时的快照。
    refresh_config()
    services = {key: unit_active(unit) for key, unit in UNITS.items()}
    port_issues = listener_issues() if services['mihomo'] else []
    mode = sys_proxy_mode()
    targets = [(proxy_setting(f"org.gnome.system.proxy.{kind}", "host"),
                proxy_setting(f"org.gnome.system.proxy.{kind}", "port"))
               for kind in ("http", "https", "ftp", "socks")] if mode == "manual" else []
    lease = bool(ENV) and (ENV.get('STATE_VERSION') != '2' or services['mihomo'] or
                           (INSTALL_HOME / 'system-proxy-before.json').exists() or
                           (INSTALL_HOME / 'proxy-enabled').exists())
    owned = lease and mode == "manual" and any(host == "127.0.0.1" and port == MIXED_PORT
                                      for host, port in targets)
    injected = count_injected()
    invalid_disk_mode = False
    try:
        disk_mode = saved_mode(dict(ENV)) if ENV.get('MIHOMO_CONF_DIR') else None
    except (OSError, ValueError):
        disk_mode = None
        invalid_disk_mode = True
    configs = mihomo_get("/configs")
    config_api_available = isinstance(configs, dict)
    api_mode = configs.get("mode") if config_api_available else None
    group = mihomo_get("/proxies/" + urllib.parse.quote(OPENAI_GROUP, safe=""))
    api_available = isinstance(group, dict)
    group = group if api_available else {}

    proxy_ready = mode == "manual" and all(
        h == "127.0.0.1" and p == MIXED_PORT for h, p in targets[:2])
    running = (all(services.values()) and disk_mode == api_mode == "rule" and
               proxy_ready and bool(group.get("all")) and not port_issues)
    direct_idle = (services["mihomo"] and not services["controller"] and not services["guard"] and
                   disk_mode == api_mode == "direct" and not owned and not injected and not port_issues)
    fully_stopped = not any(services.values()) and not owned and not injected and not invalid_disk_mode
    phase = "on" if running else "off" if direct_idle or fully_stopped else "partial"

    issues = list(port_issues)
    if phase == 'partial':
        if invalid_disk_mode:
            issues.append('磁盘配置模式无效，请检查 config.yaml')
        if services['mihomo'] and disk_mode in ('rule', 'direct') and api_mode in ('rule', 'direct') \
                and disk_mode != api_mode:
            issues.append(f'磁盘配置与核心运行模式不一致（{disk_mode} / {api_mode}）')
        if services['mihomo'] and not config_api_available:
            issues.append('核心模式读取失败，请核对管理 API 端口及密钥')
        elif services['mihomo'] and api_mode not in ('rule', 'direct'):
            issues.append('核心运行模式无效或缺失')
        if services['mihomo'] and disk_mode not in ('rule', 'direct') and not invalid_disk_mode:
            issues.append('磁盘配置缺少 rule/direct 模式')
        if not services['mihomo']:
            issues.append('代理核心未运行')
        if services['mihomo'] and not api_available:
            issues.append('节点读取失败，请核对管理 API 端口、密钥及代理组配置')
        elif services['mihomo'] and not group.get('all') and 'rule' in (disk_mode, api_mode):
            issues.append('当前代理组没有候选节点，请检查订阅')
        rule_expected = disk_mode == api_mode == 'rule'
        for key, title in (('controller', '路由控制器'), ('guard', '代理守护')):
            if rule_expected and not services[key]:
                issues.append(title + '未运行')
            elif services[key] and not running:
                issues.append(title + '仍在运行')
        if owned and not rule_expected:
            issues.append('系统代理仍指向当前核心端口 ' + MIXED_PORT)
        elif services['mihomo'] and rule_expected and not proxy_ready:
            issues.append('系统代理未指向当前核心端口 ' + MIXED_PORT)
        if injected and not running:
            issues.append(f'桌面启动器仍有 {injected} 个注入残留')
    from node_checks import check_context
    generation = (check_context(dict(ENV)) if services['mihomo'] and
                  (config_api_available or api_available) else None)
    return {"services": services, "sys_proxy": mode, "proxy_owned": owned,
            "proxy_target": " / ".join(f"{h}:{p}" for h, p in targets),
            "injected": injected, "running": running, "phase": phase,
            "disk_mode": disk_mode, "api_mode": api_mode,
            "config_api_available": config_api_available,
            "group_now": group.get("now"), "nodes": group.get("all", []),
            "api_available": api_available, "issues": issues, "node_check_generation": generation,
            "installed": bool(ENV), "checked_at": time.strftime("%H:%M:%S")}


def run_switch(on):
    # 模式事务收到 TERM 后需要恢复磁盘、重载并回读，不能在 5 秒内强杀。
    return command(["bash", SWITCH_BIN, "start" if on else "stop"], timeout=75, termination_grace=60)


def website_profile():
    from websites import current_profile
    return current_profile(dict(ENV))


def save_websites(profile):
    state = os.environ.get('KIT_ENV', str(Path.home() / '.local/share/openai-proxy-kit/kit.env'))
    return command([sys.executable, str(ROOT / 'scripts/websites.py'), '--state', state, '--apply'],
                   input_text=json.dumps(profile, ensure_ascii=False), timeout=90)


def check_websites():
    from websites import probe_services, current_profile
    env = dict(ENV)
    return probe_services(env, current_profile(env))


def check_openai():
    return check_websites()['openai']


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


def check_nodes():
    from node_checks import NodeChecker
    return NodeChecker(load_env()).run()


def panel_connection_url():
    """只连接本机管理页；认证信息放在片段中，不进入 HTTP 请求或操作日志。"""
    refresh_config()
    env = dict(ENV)
    port = env.get('API_PORT', '9090')
    address = 'http://127.0.0.1:' + port
    if not env or not unit_active(env.get('UNIT_MIHOMO', 'mihomo-kit.service')):
        raise RuntimeError('请先开启代理，再打开高级管理。')
    directory = Path(env.get('MIHOMO_CONF_DIR', Path.home() / '.config/mihomo'))
    if not (directory / 'ui/index.html').is_file():
        raise RuntimeError('尚未安装高级管理页面；日常使用可直接在本软件中切换节点。')
    secret = env.get('MIHOMO_SECRET', '')
    request = urllib.request.Request(address + '/version',
                                     headers={'Authorization': 'Bearer ' + secret} if secret else {})
    try:
        with HTTP.open(request, timeout=5) as response:
            value = json.loads(response.read(65536))
            if not isinstance(value, dict) or not value.get('version'):
                raise ValueError()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise RuntimeError('本机管理连接验证失败。请到“工具与设置”运行诊断后重试。') from None
        raise RuntimeError('本机管理服务响应异常，请稍后重试或运行诊断。') from None
    except (OSError, ValueError):
        raise RuntimeError('暂时无法连接本机管理服务，请确认代理已开启后重试。') from None
    # MetaCubeXD 的 setup 路由会更新旧端点密钥，并在成功后 replace 到 overview。
    # 不能改用首页路由：浏览器已有失效端点时，首页会跳过自动连接。
    fragment = urllib.parse.urlencode({'hostname': '127.0.0.1', 'port': port,
                                      'http': 'true', 'secret': secret})
    return address + '/ui/#/setup?' + fragment


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
