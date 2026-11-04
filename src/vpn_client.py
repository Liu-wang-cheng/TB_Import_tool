"""VPN 连接管理：用 OpenConnect（Fortinet 协议）独立建立隧道。

不依赖 FortiClient。调用方式（已在真实环境验证）：
    openconnect --protocol=fortinet <server>:<port> --user=<user> \
        --passwd-on-stdin --non-inter [--servercert=pin-sha256:<pin>]

密码只经 stdin 传入，不出现在命令行/日志中。
"""

import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_PATHS = [
    r"C:\Program Files\OpenConnect\openconnect.exe",
    r"C:\Program Files (x86)\OpenConnect\openconnect.exe",
]

# openconnect 隧道真正建立的输出特征（"Using Wintun device" 等仅表示
# 网卡已创建，不能作为连接成功依据）
_SUCCESS_MARKERS = (
    "Configured as ",
    "PPP state transition from OPENED to NETWORK",
)

# 证书不匹配时 openconnect 会打印实际指纹，用于首次信任（TOFU）
_PIN_RE = re.compile(r"match server's certificate:\s*(pin-sha256:[A-Za-z0-9+/=]+)")

# 占位指纹：OpenConnect 在未提供 --servercert 时只会报 "signer not found"，
# 不会输出实际指纹；先给一个必不匹配的指纹，让它打印真实指纹后再重试
_DUMMY_PIN = "pin-sha256:" + "A" * 43 + "="


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
        self.auto_trust_cert = bool(cfg.get("auto_trust_cert", True))
        self._proc: Optional[subprocess.Popen] = None
        self._output: list = []
        self._reader: Optional[threading.Thread] = None
        self._last_exit_code = None

    # ── 状态 ──────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def probe(self) -> bool:
        return is_reachable(self.probe_host, self.probe_port)

    # ── 连接 ──────────────────────────────────────────

    def connect(self) -> tuple:
        """确保 VPN 可用。返回 (ok, message)。"""
        if not self.enabled:
            return True, "未启用 VPN"
        if not self.server or not self.port:
            return False, "VPN 配置不完整（服务器/端口）"
        if self.probe_host and self.probe_port and self.probe():
            return True, "目标已可达（无需连接 VPN）"
        logger.info(
            "VPN 连接参数: server=%s:%s user=%s probe=%s:%s timeout=%ss cert=%s",
            self.server, self.port, self.username,
            self.probe_host or "-", self.probe_port or "-", self.timeout,
            "已固定" if self.servercert else "首次信任(TOFU)")
        exe = find_openconnect()
        if not exe:
            return False, ("未检测到 OpenConnect，请先安装（管理员执行 "
                           "choco install openconnect -y）或手动连接 VPN")

        for attempt in (1, 2):
            ok, msg, pin = self._try_connect(exe)
            if ok:
                logger.info("VPN 连接成功: %s", msg)
                if pin and pin != self.servercert:
                    self.servercert = pin
                    msg += f"（已记录证书指纹 {pin[:24]}…）"
                return True, msg
            # 首次未见指纹 → 失败信息里带实际指纹 → 自动信任并重试一次
            if (attempt == 1 and self.auto_trust_cert and not self.servercert
                    and pin):
                logger.warning("VPN 服务器证书未固定，自动信任指纹: %s",
                               pin[:24] + "…")
                self.servercert = pin
                continue
            logger.warning("VPN 连接失败（openconnect 退出码 %s）: %s",
                           self._last_exit_code, msg)
            self._log_output_tail()
            return False, msg
        self._log_output_tail()
        return False, "VPN 连接失败"

    def _try_connect(self, exe: str) -> tuple:
        """启动一次 openconnect，返回 (ok, message, detected_pin)"""
        cmd = [
            exe,
            "--protocol=fortinet",
            f"{self.server}:{self.port}",
            f"--user={self.username}",
            "--passwd-on-stdin",
            "--non-inter",
        ]
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

    def _log_output_tail(self, limit: int = 20):
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
        if "failed to create wintun" in low or (
                "wintun" in low and ("failed" in low or "error" in low)):
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
        if proc is None or proc.poll() is not None:
            return True, "VPN 未连接"
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        # 等待网卡/路由清理
        for _ in range(10):
            if not self.probe():
                break
            time.sleep(0.5)
        logger.info("VPN 已断开")
        return True, "VPN 已断开"


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
