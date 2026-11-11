"""VPN 连接管理：用 OpenConnect（Fortinet 协议）独立建立隧道。

不依赖 FortiClient。调用方式（已在真实环境验证）：
    openconnect --protocol=fortinet <server>:<port> --user=<user> \
        --passwd-on-stdin --non-inter [--servercert=pin-sha256:<pin>]

密码只经 stdin 传入，不出现在命令行/日志中。
"""

import json
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_DEFAULT_PATHS = [
    r"C:\Program Files\OpenConnect\openconnect.exe",
    r"C:\Program Files (x86)\OpenConnect\openconnect.exe",
]

# openconnect 隧道真正建立的输出特征（"Using Wintun device" 仅表示网卡
# 已创建、"PPP state transition" 也可能在后续 403/断线前出现，均不可靠）
_SUCCESS_MARKERS = (
    "Configured as ",
)

# 证书不匹配时 openconnect 会打印实际指纹，用于首次信任（TOFU）
_PIN_RE = re.compile(r"match server's certificate:\s*(pin-sha256:[A-Za-z0-9+/=]+)")

# 占位指纹：OpenConnect 在未提供 --servercert 时只会报 "signer not found"，
# 不会输出实际指纹；先给一个必不匹配的指纹，让它打印真实指纹后再重试
_DUMMY_PIN = "pin-sha256:" + "A" * 43 + "="


def _resolve_private_host(host: str) -> str:
    """把主机名/IP 解析为私有 IPv4；公网/本地回环返回空串。"""
    import ipaddress
    if not host:
        return ""
    value = str(host).strip()
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        try:
            value = socket.gethostbyname(value)
            addr = ipaddress.ip_address(value)
        except (OSError, ValueError):
            return ""
    if not addr.is_private or addr.is_loopback:
        return ""
    return str(addr)


def collect_llm_bypass_hosts(config: dict) -> list:
    """从分类器/AI 分析配置提取需要绕过 VPN 的内网 LLM 中转站 IP。"""
    config = config or {}
    bases = []

    cl = config.get("classifier") or {}
    if isinstance(cl, dict):
        if "llm" not in cl and isinstance(cl.get("classifier"), dict):
            cl = cl["classifier"]
        llm = cl.get("llm") if isinstance(cl, dict) else {}
        if isinstance(llm, dict):
            bases.append(llm.get("base_url"))

    ai = config.get("ai_analysis") or {}
    if isinstance(ai, dict):
        llm = ai.get("llm")
        if isinstance(llm, dict):
            bases.append(llm.get("base_url"))

    hosts = []
    for base in bases:
        if not base:
            continue
        host = urlparse(str(base)).hostname
        ip = _resolve_private_host(host)
        if ip and ip not in hosts:
            hosts.append(ip)
    return hosts


def _powershell(script: str, timeout: int = 15):
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _json_first(stdout: str) -> dict:
    text = (stdout or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    if isinstance(data, list):
        data = data[0] if data else {}
    return data if isinstance(data, dict) else {}


def _find_net_route(ip: str) -> dict:
    """查找到目标 IP 的当前路由（应在 VPN 连接前调用）。"""
    script = (
        f"$r = Find-NetRoute -RemoteIPAddress '{ip}' -ErrorAction SilentlyContinue "
        f"| Where-Object {{ $_.NextHop -and $_.DestinationPrefix }} "
        f"| Select-Object -First 1 InterfaceIndex,NextHop,DestinationPrefix; "
        f"if ($r) {{ $r | ConvertTo-Json -Compress }}")
    try:
        return _json_first(_powershell(script).stdout)
    except Exception as e:
        logger.debug("查询 LLM 直连路由失败: %s", e)
        return {}


def _route_exists(ip: str) -> bool:
    script = (
        f"$r = Get-NetRoute -DestinationPrefix '{ip}/32' "
        f"-ErrorAction SilentlyContinue | Select-Object -First 1; "
        f"if ($r) {{ 'yes' }}")
    try:
        return "yes" in (_powershell(script, timeout=10).stdout or "")
    except Exception:
        return False


def _add_host_route(ip: str, next_hop: str, if_index: int) -> tuple:
    script = (
        f"New-NetRoute -DestinationPrefix '{ip}/32' -NextHop '{next_hop}' "
        f"-InterfaceIndex {int(if_index)} -RouteMetric 1 "
        f"-PolicyStore ActiveStore -ErrorAction Stop")
    try:
        r = _powershell(script)
        if r.returncode == 0:
            return True, ""
        return False, (r.stderr or r.stdout or "").strip()[:300]
    except Exception as e:
        return False, str(e)[:300]


def _remove_host_route(ip: str) -> bool:
    script = (
        f"Remove-NetRoute -DestinationPrefix '{ip}/32' -Confirm:$false "
        f"-ErrorAction SilentlyContinue")
    try:
        return _powershell(script, timeout=10).returncode == 0
    except Exception:
        return False


def _resource_root() -> str:
    """程序资源根目录（打包后为 _internal，源码环境为项目根）"""
    import sys
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS",
                       os.path.dirname(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def find_openconnect() -> Optional[str]:
    """按 打包内置 → 安装目录 → PATH 的顺序查找 openconnect.exe"""
    bundled = os.path.join(_resource_root(), "tools", "vpn", "openconnect.exe")
    for path in [bundled] + _DEFAULT_PATHS:
        if os.path.isfile(path):
            return path
    found = shutil.which("openconnect")
    return found or None


def is_reachable(host: str, port: int, timeout: float = 3.0) -> bool:
    """TCP 探测目标是否可达（判断 VPN 是否已生效）"""
    if not host or not port:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def parse_certificate_pin(output: str) -> Optional[str]:
    """从 openconnect 输出中提取服务器实际证书指纹"""
    m = _PIN_RE.search(output or "")
    return m.group(1) if m else None


def parse_host_port(value: str) -> tuple:
    """把 "host:port" / "http://host:port/path" 解析为 (host, port)"""
    value = (value or "").strip()
    if not value:
        return "", 0
    value = re.sub(r"^[a-zA-Z]+://", "", value)
    value = value.split("/", 1)[0]
    if value.startswith("["):  # IPv6
        host, _, rest = value.partition("]")
        host = host.lstrip("[")
        port = rest.lstrip(":")
        return host, int(port) if port.isdigit() else 0
    if ":" in value:
        host, _, port = value.rpartition(":")
        if port.isdigit():
            return host, int(port)
    return value, 0


def parse_probe_target(value: str) -> tuple:
    """解析探测地址；未写端口时按协议补默认端口（https→443 / http→80）"""
    raw = (value or "").strip()
    low = raw.lower()
    scheme = ("https" if low.startswith("https://")
              else "http" if low.startswith("http://") else "")
    host, port = parse_host_port(raw)
    if host and not port:
        if scheme == "https":
            port = 443
        elif scheme == "http":
            port = 80
    return host, port


class VpnClient:
    """单个 VPN 连接的生命周期管理（同一时间只保持一条隧道）"""

    def __init__(self, config: dict):
        cfg = config or {}
        self.enabled = bool(cfg.get("enabled"))
        self.server = str(cfg.get("server", "")).strip()
        self.port = int(cfg.get("port") or 0)
        self.username = str(cfg.get("username", "")).strip()
        self.password = str(cfg.get("password", ""))
        self.servercert = str(cfg.get("servercert", "")).strip()
        self.probe_host = str(cfg.get("probe_host", "")).strip()
        self.probe_port = int(cfg.get("probe_port") or 0)
        self.timeout = int(cfg.get("timeout") or 60)
        self.no_dtls = bool(cfg.get("no_dtls"))
        self.auto_trust_cert = bool(cfg.get("auto_trust_cert", True))
        # 内网 LLM 中转站等需要绕过 VPN 的主机（由调用方从完整配置提取）
        self.bypass_hosts = list(cfg.get("bypass_hosts") or [])
        self._bypass_routes = []
        self._added_bypass_ips = []
        self._proc: Optional[subprocess.Popen] = None
        self._output: list = []
        self._reader: Optional[threading.Thread] = None
        self._last_exit_code = None
        self._last_output_text = ""

    # ── 状态 ──────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def probe(self) -> bool:
        return is_reachable(self.probe_host, self.probe_port)

    def _prepare_bypass_routes(self):
        """在 VPN 连接前记录 LLM 中转站经物理网络的下一跳。"""
        self._bypass_routes = []
        for host in self.bypass_hosts:
            ip = _resolve_private_host(host)
            if not ip:
                continue
            info = _find_net_route(ip)
            next_hop = str(info.get("NextHop") or "").strip()
            if_index = info.get("InterfaceIndex")
            if not next_hop or not if_index:
                logger.info("LLM 中转站 %s 无需 bypass（本地路由已直连）", ip)
                continue
            self._bypass_routes.append((ip, next_hop, int(if_index)))
        if self._bypass_routes:
            logger.info(
                "VPN LLM 直连路由候选: %s",
                ", ".join(f"{ip} via {hop}"
                          for ip, hop, _ in self._bypass_routes))

    def _add_bypass_routes(self):
        for ip, next_hop, if_index in self._bypass_routes:
            if ip in self._added_bypass_ips:
                continue
            if _route_exists(ip):
                logger.info("LLM 直连路由已存在，跳过: %s", ip)
                continue
            ok, err = _add_host_route(ip, next_hop, if_index)
            if ok:
                self._added_bypass_ips.append(ip)
                logger.info("[VPN] 已添加 LLM 直连路由: %s via %s (if %s)",
                            ip, next_hop, if_index)
            else:
                logger.warning("[VPN] 添加 LLM 直连路由失败 %s: %s", ip, err)

    def _remove_bypass_routes(self):
        for ip in list(self._added_bypass_ips):
            if _remove_host_route(ip):
                logger.info("[VPN] 已删除 LLM 直连路由: %s", ip)
            else:
                logger.warning("[VPN] 删除 LLM 直连路由失败: %s", ip)
        self._added_bypass_ips = []

    # ── 连接 ──────────────────────────────────────────

    def connect(self) -> tuple:
        """确保 VPN 可用。返回 (ok, message)。"""
        if not self.enabled:
            return True, "未启用 VPN"
        if not self.server or not self.port:
            return False, "VPN 配置不完整（服务器/端口）"
        if self.probe_host and self.probe_port and self.probe():
            return True, "目标已可达（无需连接 VPN）"
        if self.bypass_hosts:
            self._prepare_bypass_routes()
        logger.info(
            "VPN 连接参数: server=%s:%s user=%s probe=%s:%s timeout=%ss "
            "dtls=%s cert=%s",
            self.server, self.port, self.username,
            self.probe_host or "-", self.probe_port or "-", self.timeout,
            "关闭(强制TCP)" if self.no_dtls else "自动",
            "已固定" if self.servercert else "首次信任(TOFU)")
        exe = find_openconnect()
        if not exe:
            return False, ("未检测到 OpenConnect，请先安装（管理员执行 "
                           "choco install openconnect -y）或手动连接 VPN")

        # 默认 DTLS 优先，失败自动回退 TCP；显式勾选强制 TCP 时只走 TCP
        modes = [True] if self.no_dtls else [False, True]
        last_msg = "VPN 连接失败"
        for no_dtls in modes:
            auto_fallback = no_dtls and not self.no_dtls
            retry_tcp = False
            for attempt in (1, 2):
                ok, msg, pin = self._try_connect(exe, no_dtls=no_dtls)
                if ok:
                    logger.info("VPN 连接成功: %s", msg)
                    if pin and pin != self.servercert:
                        self.servercert = pin
                        msg += f"（已记录证书指纹 {pin[:24]}…）"
                    if auto_fallback:
                        msg += "（DTLS 失败，已自动回退 TCP）"
                    self._add_bypass_routes()
                    return True, msg
                # 首次未见指纹 → 失败信息里带实际指纹 → 自动信任并重试一次
                if (attempt == 1 and self.auto_trust_cert
                        and not self.servercert and pin):
                    logger.warning("VPN 服务器证书未固定，自动信任指纹: %s",
                                   pin[:24] + "…")
                    self.servercert = pin
                    continue
                last_msg = msg
                # DTLS 自动模式失败且输出显示 DTLS 不可用 → 改用 TCP 重试
                if (not no_dtls and not self.no_dtls
                        and self._dtls_failure(self._last_output_text)):
                    logger.warning(
                        "DTLS 连接失败（退出码 %s），自动改用 TCP 重试: %s",
                        self._last_exit_code, msg)
                    self._log_output_tail()
                    retry_tcp = True
                    break
                logger.warning("VPN 连接失败（openconnect 退出码 %s）: %s",
                               self._last_exit_code, msg)
                self._log_output_tail()
                return False, msg
            if retry_tcp:
                continue
        self._log_output_tail()
        return False, last_msg

    @staticmethod
    def _dtls_failure(text: str) -> bool:
        """输出是否表明 DTLS 不可用（可自动回退 TCP）"""
        low = (text or "").lower()
        return ("detected dead peer" in low
                or "failed to connect dtls" in low
                or ("svrhello status was" in low and "fail" in low))

    def _try_connect(self, exe: str, no_dtls: Optional[bool] = None) -> tuple:
        """启动一次 openconnect，返回 (ok, message, detected_pin)"""
        cmd = [
            exe,
            "--protocol=fortinet",
            f"{self.server}:{self.port}",
            f"--user={self.username}",
            "--passwd-on-stdin",
            "--non-inter",
        ]
        # UDP/50443 被拦截时强制走 TCP/HTTPS，避免 DTLS dead peer 后的错误回退
        use_no_dtls = self.no_dtls if no_dtls is None else no_dtls
        if use_no_dtls:
            cmd.append("--no-dtls")
        # 无指纹时用占位指纹探测真实指纹（否则 openconnect 只报 signer not found）
        cmd.append(f"--servercert={self.servercert or _DUMMY_PIN}")
        # 显式指定路由脚本：内置版默认脚本路径编译为安装目录，
        # 未安装 OpenConnect 的机器会找不到脚本导致路由配置失败
        script = os.path.join(os.path.dirname(exe), "vpnc-script-win.js")
        if os.path.isfile(script):
            cmd.append(f"--script={script}")
        self._output = []
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, bufsize=0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as e:
            return False, f"启动 OpenConnect 失败: {e}", None
        self._proc = proc
        try:
            proc.stdin.write((self.password + "\n").encode("utf-8"))
            proc.stdin.flush()
        except OSError:
            pass
        self._reader = threading.Thread(target=self._read_output,
                                        daemon=True)
        self._reader.start()

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            text = "".join(self._output)
            if any(m in text for m in _SUCCESS_MARKERS):
                # 有探测目标时，继续等待真正可达
                if not (self.probe_host and self.probe_port):
                    return True, "VPN 已连接", self.servercert or None
                if self.probe():
                    return True, "VPN 已连接，目标可达", self.servercert or None
            if proc.poll() is not None:
                # 进程退出后读取线程可能还没把最后的错误行读完，等它排空
                if self._reader is not None:
                    self._reader.join(timeout=1.0)
                break
            time.sleep(0.5)

        if self._reader is not None:
            self._reader.join(timeout=1.0)
        self._last_exit_code = proc.poll()
        text = "".join(self._output)
        self._last_output_text = text
        pin = parse_certificate_pin(text)
        configured = any(m in text for m in _SUCCESS_MARKERS)
        self.disconnect()
        if configured and self.probe_host and self.probe_port:
            msg = (f"VPN 隧道已建立，但探测目标 "
                   f"{self.probe_host}:{self.probe_port} 不可达。"
                   f"请确认该 VPN 账号有内网访问权限，"
                   f"或在 VPN 设置中修改探测地址")
        elif configured:
            msg = "VPN 隧道已建立，但连接随后中断"
        else:
            msg = "VPN 连接超时或失败"
        hint = self._error_hint(text)
        if hint:
            msg += "：" + hint
        return False, msg, pin

    def _log_output_tail(self, limit: int = 40):
        """失败时把 openconnect 输出尾部写入日志，便于远程用户反馈。"""
        lines = [ln for ln in "".join(self._output).splitlines() if ln.strip()]
        if not lines:
            return
        tail = "\n".join(lines[-limit:])
        if self.password:
            tail = tail.replace(self.password, "***")
        logger.warning("[openconnect 输出]\n%s", tail)

    def _read_output(self):
        proc = self._proc
        if not proc or not proc.stdout:
            return
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    self._output.append(line + "\n")
                    logger.debug("[openconnect] %s", line)
        except (OSError, ValueError):
            pass

    @staticmethod
    def _error_hint(text: str) -> str:
        low = (text or "").lower()
        if "certificate" in low and "fingerprint" in low:
            return "服务器证书不匹配，请在 VPN 设置中更新证书指纹"
        if "administrator" in low or "privilege" in low or "access is denied" in low:
            return "需要以管理员身份运行本程序以创建虚拟网卡"
        if "403 forbidden" in low or "unexpected 403" in low:
            return ("VPN 服务器拒绝隧道配置（HTTP 403）：该账号可能未开通 "
                    "SSL VPN 隧道权限，或需要使用指定的认证组/门户")
        if "ancient fortinet server" in low:
            return "服务器未返回标准 Fortinet 隧道配置（账号权限/认证组不匹配）"
        if "detected dead peer" in low or "failed to connect dtls" in low:
            return "DTLS 连接失败并回退 HTTPS（可能 UDP 50443 被防火墙拦截）"
        if ("failed to create wintun" in low
                or "failed to open wintun" in low
                or ("wintun adapter" in low and "failed" in low)):
            return "虚拟网卡创建失败，请确认以管理员运行且 wintun.dll 可用"
        if any(k in low for k in ("session terminated", "session limit",
                                   "maximum number of sessions",
                                   "concurrent session")):
            return "VPN 会话被服务器终止（账号可能在其他设备同时登录）"
        # 注意：成功的会话也会输出 "Session authentication will expire"，
        # 不能只匹配 authentication/password，否则会误报为密码错误
        if any(k in low for k in ("failed to complete authentication",
                                   "login failed", "authentication failed",
                                   "invalid password", "invalid credentials",
                                   "password expired", "account locked")):
            return "VPN 账号或密码错误（或账号被锁定/密码过期）"
        return ""

    # ── 断开 ──────────────────────────────────────────

    def disconnect(self) -> tuple:
        proc = self._proc
        self._proc = None
        had_vpn = proc is not None and proc.poll() is None
        if had_vpn:
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        # 先清理 LLM 直连路由，再等待网卡/路由清理
        self._remove_bypass_routes()
        for _ in range(10):
            if not self.probe():
                break
            time.sleep(0.5)
        logger.info("VPN 已断开")
        return True, "VPN 已断开" if had_vpn else "VPN 未连接"


def ensure_vpn(config: dict) -> tuple:
    """按方案 VPN 配置确保连接可用（供同步流程调用）"""
    cfg = config or {}
    if not cfg.get("enabled"):
        return True, "未启用 VPN"
    client = VpnClient(cfg)
    return client.connect()


def current_connection(config: dict) -> Optional[VpnClient]:
    """构造客户端实例（不连接），供断开时复用"""
    return VpnClient(config) if (config or {}).get("enabled") else None
