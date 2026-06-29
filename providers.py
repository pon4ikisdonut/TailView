from __future__ import annotations

import asyncio
import os
import platform
import re
import shlex
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Callable

_paramiko_available = False
try:
    import paramiko  # type: ignore
    _paramiko_available = True
except ImportError:
    pass

LineCallback = Callable[[str], None]

IGNORED_PATTERNS: list[str] = [
    "*.gz", "*.zip", "*.bz2", "*.xz", "*.zst",
    "*.tar", "*.tar.*",
    "lastlog", "wtmp", "btmp", "faillog", "utmp",
    "*.journal",
    "*.bin", "*.dat", "*.db", "*.sqlite",
    "*.png", "*.jpg", "*.svg", "*.ico",
    "*.so", "*.pyc", "*.pyo",
    "pacman.log",
]

_IGNORED_RE = re.compile(
    "|".join(
        re.escape(p).replace(r"\*", ".*")
        for p in IGNORED_PATTERNS
    ),
    re.IGNORECASE,
)


def _is_ignored(path: str) -> bool:
    name = Path(path).name
    return bool(_IGNORED_RE.fullmatch(name))


@dataclass
class LocalFileConfig:
    path: str
    tail_lines: int = 200
    use_sudo: bool = False


@dataclass
class SSHConfig:
    host: str
    port: int = 22
    username: str = ""
    password: str = ""
    key_path: str = ""
    remote_path: str = ""
    remote_os: str = "linux"
    tail_lines: int = 200


@dataclass
class DockerConfig:
    container_id: str
    container_name: str = ""
    tail_lines: int = 200


@dataclass
class K8sConfig:
    namespace: str = "default"
    pod_name: str = ""
    container_name: str = ""
    kubeconfig: str = ""
    tail_lines: int = 200


@dataclass
class KVMConfig:
    domain_name: str
    log_path: str = ""


class TailCommandStrategy(ABC):
    @abstractmethod
    def build_command(self, path: str, tail_lines: int, use_sudo: bool = False) -> list[str]:
        ...


class LinuxTailStrategy(TailCommandStrategy):
    def build_command(self, path: str, tail_lines: int, use_sudo: bool = False) -> list[str]:
        cmd = ["tail", f"-n{tail_lines}", "-f", path]
        if use_sudo:
            cmd = ["sudo", "-n", "--"] + cmd
        return cmd


class WindowsTailStrategy(TailCommandStrategy):
    def build_command(self, path: str, tail_lines: int, use_sudo: bool = False) -> list[str]:
        ps_cmd = f"Get-Content -Path '{path}' -Wait -Tail {tail_lines} -Encoding UTF8"
        return ["powershell", "-NonInteractive", "-Command", ps_cmd]


def _get_strategy(os_type: str) -> TailCommandStrategy:
    if os_type.lower() == "windows":
        return WindowsTailStrategy()
    return LinuxTailStrategy()


def _local_os() -> str:
    return "windows" if platform.system().lower() == "windows" else "linux"


def _check_readable(path: str) -> bool:
    return os.access(path, os.R_OK)


class BaseProvider(ABC):
    def __init__(self) -> None:
        self._running = False
        self._proc: subprocess.Popen | None = None  # type: ignore

    @property
    def source_id(self) -> str:
        raise NotImplementedError

    @abstractmethod
    async def stream(self) -> AsyncIterator[str]:
        ...

    def stop(self) -> None:
        self._running = False
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()


class LocalFileProvider(BaseProvider):
    def __init__(self, config: LocalFileConfig) -> None:
        super().__init__()
        self._config = config
        self._strategy = _get_strategy(_local_os())

    @property
    def source_id(self) -> str:
        return f"local:{self._config.path}"

    async def stream(self) -> AsyncIterator[str]:
        self._running = True
        path = self._config.path

        if not Path(path).exists():
            yield f"[TailView] File not found: {path}"
            return

        if not _check_readable(path) and not self._config.use_sudo:
            yield f"[TailView] Permission denied: {path}"
            yield f"[TailView] Tip: re-add this source and enable 'Use sudo' option"
            return

        cmd = self._strategy.build_command(path, self._config.tail_lines, self._config.use_sudo)
        loop = asyncio.get_event_loop()

        try:
            self._proc = await loop.run_in_executor(
                None,
                lambda: subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                ),
            )
        except FileNotFoundError as e:
            yield f"[TailView] Command not found: {e}"
            return

        assert self._proc.stdout is not None
        while self._running:
            line = await loop.run_in_executor(None, self._proc.stdout.readline)
            if not line:
                if self._proc.poll() is not None:
                    break
                await asyncio.sleep(0.05)
                continue
            yield line.rstrip("\n\r")


class SSHProvider(BaseProvider):
    def __init__(self, config: SSHConfig) -> None:
        super().__init__()
        self._config = config
        self._channel: "paramiko.Channel | None" = None  # type: ignore

    @property
    def source_id(self) -> str:
        return f"ssh:{self._config.host}:{self._config.remote_path}"

    async def stream(self) -> AsyncIterator[str]:
        if not _paramiko_available:
            yield "[TailView] paramiko not installed. Run: pip install paramiko"
            return

        self._running = True
        cfg = self._config
        strategy = _get_strategy(cfg.remote_os)
        cmd_parts = strategy.build_command(cfg.remote_path, cfg.tail_lines)
        cmd = " ".join(shlex.quote(p) for p in cmd_parts)
        loop = asyncio.get_event_loop()

        client: paramiko.SSHClient = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs: dict = dict(hostname=cfg.host, port=cfg.port, username=cfg.username, timeout=15)
        if cfg.key_path:
            connect_kwargs["key_filename"] = cfg.key_path
        elif cfg.password:
            connect_kwargs["password"] = cfg.password

        try:
            await loop.run_in_executor(None, lambda: client.connect(**connect_kwargs))
        except Exception as e:
            yield f"[TailView] SSH connect error: {e}"
            return

        transport = client.get_transport()
        assert transport is not None
        self._channel = transport.open_session()
        self._channel.exec_command(cmd)
        self._channel.setblocking(False)

        buf = ""
        try:
            while self._running:
                chunk = await loop.run_in_executor(None, self._safe_recv, 4096)
                if chunk is None:
                    await asyncio.sleep(0.05)
                    continue
                if chunk == b"":
                    break
                buf += chunk.decode("utf-8", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    yield line.rstrip("\r")
        finally:
            self._channel.close()
            client.close()

    def _safe_recv(self, nbytes: int) -> bytes | None:
        assert self._channel is not None
        try:
            self._channel.setblocking(False)
            return self._channel.recv(nbytes)
        except Exception:
            return None

    def stop(self) -> None:
        self._running = False
        if self._channel:
            try:
                self._channel.close()
            except Exception:
                pass


class DockerProvider(BaseProvider):
    def __init__(self, config: DockerConfig) -> None:
        super().__init__()
        self._config = config

    @property
    def source_id(self) -> str:
        name = self._config.container_name or self._config.container_id[:12]
        return f"docker:{name}"

    async def stream(self) -> AsyncIterator[str]:
        self._running = True
        cfg = self._config
        target = cfg.container_name or cfg.container_id
        cmd = ["docker", "logs", "--follow", f"--tail={cfg.tail_lines}", target]
        loop = asyncio.get_event_loop()

        try:
            self._proc = await loop.run_in_executor(
                None,
                lambda: subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                ),
            )
        except FileNotFoundError:
            yield "[TailView] docker not found in PATH"
            return

        assert self._proc.stdout is not None
        while self._running:
            line = await loop.run_in_executor(None, self._proc.stdout.readline)
            if not line:
                if self._proc.poll() is not None:
                    break
                await asyncio.sleep(0.05)
                continue
            yield line.rstrip("\n\r")


@dataclass
class DockerContainerInfo:
    container_id: str
    name: str
    image: str
    status: str


def discover_docker_containers() -> list[DockerContainerInfo]:
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}"],
            capture_output=True, text=True, timeout=5,
        )
        containers = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 4:
                containers.append(DockerContainerInfo(
                    container_id=parts[0], name=parts[1],
                    image=parts[2], status=parts[3],
                ))
        return containers
    except Exception:
        return []


class K8sProvider(BaseProvider):
    def __init__(self, config: K8sConfig) -> None:
        super().__init__()
        self._config = config

    @property
    def source_id(self) -> str:
        return f"k8s:{self._config.namespace}/{self._config.pod_name}"

    async def stream(self) -> AsyncIterator[str]:
        self._running = True
        cfg = self._config
        cmd = [
            "kubectl", "logs", "--follow",
            f"--tail={cfg.tail_lines}",
            f"--namespace={cfg.namespace}",
            cfg.pod_name,
        ]
        if cfg.container_name:
            cmd += ["-c", cfg.container_name]
        if cfg.kubeconfig:
            cmd += [f"--kubeconfig={cfg.kubeconfig}"]

        loop = asyncio.get_event_loop()
        try:
            self._proc = await loop.run_in_executor(
                None,
                lambda: subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                ),
            )
        except FileNotFoundError:
            yield "[TailView] kubectl not found in PATH"
            return

        assert self._proc.stdout is not None
        while self._running:
            line = await loop.run_in_executor(None, self._proc.stdout.readline)
            if not line:
                if self._proc.poll() is not None:
                    break
                await asyncio.sleep(0.05)
                continue
            yield line.rstrip("\n\r")


def discover_k8s_pods(namespace: str = "", kubeconfig: str = "") -> list[dict[str, str]]:
    try:
        cmd = [
            "kubectl", "get", "pods", "--all-namespaces", "-o",
            "custom-columns=NS:.metadata.namespace,NAME:.metadata.name,"
            "READY:.status.containerStatuses[0].ready,STATUS:.status.phase",
            "--no-headers",
        ]
        if kubeconfig:
            cmd += [f"--kubeconfig={kubeconfig}"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        pods = []
        for line in result.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) >= 2:
                pods.append({"namespace": parts[0], "name": parts[1],
                             "ready": parts[2] if len(parts) > 2 else "?",
                             "status": parts[3] if len(parts) > 3 else "?"})
        return pods
    except Exception:
        return []


class KVMProvider(BaseProvider):
    def __init__(self, config: KVMConfig) -> None:
        super().__init__()
        self._config = config

    @property
    def source_id(self) -> str:
        return f"kvm:{self._config.domain_name}"

    def _resolve_log_path(self) -> str:
        if self._config.log_path:
            return self._config.log_path
        return f"/var/log/libvirt/qemu/{self._config.domain_name}.log"

    async def stream(self) -> AsyncIterator[str]:
        self._running = True
        path = self._resolve_log_path()
        cfg_local = LocalFileConfig(path=path, tail_lines=200)
        provider = LocalFileProvider(cfg_local)
        async for line in provider.stream():
            if not self._running:
                break
            yield line

    def stop(self) -> None:
        self._running = False


def discover_kvm_domains() -> list[str]:
    try:
        result = subprocess.run(
            ["virsh", "list", "--all", "--name"],
            capture_output=True, text=True, timeout=5,
        )
        return [l.strip() for l in result.stdout.splitlines() if l.strip()]
    except Exception:
        return []


_DEFAULT_LOG_DIRS_LINUX = [
    "/var/log",
    "/var/log/nginx",
    "/var/log/apache2",
    "/var/log/httpd",
    "/var/log/mysql",
    "/var/log/postgresql",
    os.path.expanduser("~/.local/share"),
]

_DEFAULT_LOG_DIRS_WINDOWS = [
    r"C:\Windows\Logs",
    r"C:\inetpub\logs",
    os.path.expandvars(r"%APPDATA%\logs"),
    os.path.expandvars(r"%LOCALAPPDATA%\logs"),
]

_LOG_EXTENSIONS = {".log", ".txt", ".out", ".err"}

_GROUP_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"nginx", re.I), "Nginx"),
    (re.compile(r"apache|httpd", re.I), "Apache"),
    (re.compile(r"mysql|mariadb", re.I), "MySQL"),
    (re.compile(r"postgres|pg_", re.I), "PostgreSQL"),
    (re.compile(r"kern|syslog|messages|dmesg", re.I), "System"),
    (re.compile(r"auth|secure|faillog", re.I), "Auth"),
    (re.compile(r"docker", re.I), "Docker"),
    (re.compile(r"kube|k8s", re.I), "Kubernetes"),
    (re.compile(r"libvirt|qemu|kvm", re.I), "KVM"),
]


@dataclass
class DiscoveredLog:
    path: str
    group: str
    name: str
    readable: bool = True


def discover_local_logs() -> list[DiscoveredLog]:
    dirs = _DEFAULT_LOG_DIRS_WINDOWS if platform.system().lower() == "windows" else _DEFAULT_LOG_DIRS_LINUX
    results: list[DiscoveredLog] = []
    seen: set[str] = set()

    for d in dirs:
        p = Path(d)
        if not p.exists():
            continue
        try:
            for f in p.rglob("*"):
                if f.suffix.lower() not in _LOG_EXTENSIONS:
                    continue
                if not f.is_file():
                    continue
                abs_path = str(f.resolve())
                if abs_path in seen:
                    continue
                if _is_ignored(abs_path):
                    continue
                seen.add(abs_path)

                group = "Other"
                for pattern, grp in _GROUP_PATTERNS:
                    if pattern.search(f.name) or pattern.search(str(f.parent)):
                        group = grp
                        break

                results.append(DiscoveredLog(
                    path=abs_path,
                    group=group,
                    name=f.name,
                    readable=_check_readable(abs_path),
                ))
        except PermissionError:
            continue

    return results