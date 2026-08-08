#!/usr/bin/env python3
"""Independent operations controller for a Docker Compose deployment.

The controller only stores low-cardinality operational data. It never reads
application payloads, user records, request bodies, or container logs.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable


VERSION = "1.0"
USER_AGENT = f"ConsolidatorMonitor/{VERSION}"


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return utc_now().replace(microsecond=0).isoformat()


@dataclass
class CheckResult:
    name: str
    ok: bool
    summary: str
    value: int | float | str | None = None
    component: str = ""
    duration_ms: int = 0


class MonitorError(RuntimeError):
    pass


class Controller:
    def __init__(self, *, app_dir: Path, state_path: Path, external_only: bool = False):
        self.app_dir = app_dir
        self.state_path = state_path
        self.external_only = external_only
        self.public_url = (
            os.getenv("MONITOR_PUBLIC_URL")
            or os.getenv("SITE_URL")
            or "http://127.0.0.1:8001"
        ).rstrip("/")
        self.internal_url = os.getenv(
            "MONITOR_INTERNAL_URL", "http://127.0.0.1:8001/readyz/"
        )
        self.timeout = env_int("MONITOR_HTTP_TIMEOUT_SECONDS", 10, 2)
        self.alert_after = env_int("MONITOR_ALERT_AFTER", 2, 1)
        self.alert_cooldown = env_int(
            "MONITOR_ALERT_COOLDOWN_SECONDS", 1800, 60
        )
        self.autoheal = env_bool("MONITOR_AUTOHEAL", True)
        self.autoheal_after = env_int("MONITOR_AUTOHEAL_AFTER", 3, 2)
        self.autoheal_cooldown = env_int(
            "MONITOR_AUTOHEAL_COOLDOWN_SECONDS", 900, 120
        )
        self.hostname = socket.gethostname()

    def _run(self, command: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
        return subprocess.run(
            command,
            cwd=self.app_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )

    def _timed(self, name: str, callback: Callable[[], CheckResult]) -> CheckResult:
        started = time.monotonic()
        try:
            result = callback()
        except Exception as exc:
            result = CheckResult(name, False, f"{exc.__class__.__name__}: {exc}")
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result

    def _http(self, url: str, headers: dict[str, str] | None = None):
        request = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, **(headers or {})},
        )
        return urllib.request.urlopen(request, timeout=self.timeout)

    def check_internal(self) -> CheckResult:
        with self._http(self.internal_url) as response:
            payload = json.loads(response.read(4096))
            ok = response.status == 200 and bool(payload.get("ok"))
            return CheckResult(
                "internal_readiness",
                ok,
                "application, database and cache are ready" if ok else "readiness failed",
                response.status,
                "web",
            )

    def check_public(self) -> CheckResult:
        with self._http(self.public_url + "/readyz/") as response:
            payload = json.loads(response.read(4096))
            ok = response.status == 200 and bool(payload.get("ok"))
            return CheckResult(
                "public_readiness",
                ok,
                "public endpoint is reachable" if ok else "public endpoint is unavailable",
                response.status,
                "web",
            )

    def check_security_headers(self) -> CheckResult:
        with self._http(self.public_url + "/") as response:
            headers = {name.lower(): value for name, value in response.headers.items()}
        required = {
            "content-security-policy",
            "x-content-type-options",
            "x-frame-options",
            "referrer-policy",
        }
        if urllib.parse.urlparse(self.public_url).scheme == "https":
            required.add("strict-transport-security")
        missing = sorted(required - headers.keys())
        return CheckResult(
            "security_headers",
            not missing,
            "required browser security headers are present"
            if not missing
            else "missing: " + ", ".join(missing),
            len(missing),
            "proxy",
        )

    def check_dns(self) -> CheckResult:
        host = urllib.parse.urlparse(self.public_url).hostname
        if not host:
            raise MonitorError("public URL has no host")
        addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, None)})
        return CheckResult(
            "dns",
            bool(addresses),
            f"resolved to {len(addresses)} address(es)",
            len(addresses),
            "dns",
        )

    def check_tls(self) -> CheckResult:
        parsed = urllib.parse.urlparse(self.public_url)
        if parsed.scheme != "https" or not parsed.hostname:
            return CheckResult("tls_certificate", True, "TLS check is not applicable")
        context = ssl.create_default_context()
        with socket.create_connection(
            (parsed.hostname, parsed.port or 443), timeout=self.timeout
        ) as raw_socket:
            with context.wrap_socket(raw_socket, server_hostname=parsed.hostname) as tls_socket:
                certificate = tls_socket.getpeercert()
        expires = dt.datetime.strptime(
            certificate["notAfter"], "%b %d %H:%M:%S %Y %Z"
        ).replace(tzinfo=dt.timezone.utc)
        days = int((expires - utc_now()).total_seconds() // 86400)
        minimum = env_int("MONITOR_TLS_MIN_DAYS", 14, 1)
        return CheckResult(
            "tls_certificate",
            days >= minimum,
            f"certificate expires in {days} day(s)",
            days,
            "proxy",
        )

    def check_websocket_route(self) -> CheckResult:
        parsed = urllib.parse.urlparse(self.public_url)
        if not parsed.hostname:
            raise MonitorError("public URL has no host")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        websocket_path = os.getenv(
            "MONITOR_WEBSOCKET_PATH", "/ws/assistant/"
        ).strip()
        if not websocket_path.startswith("/"):
            raise MonitorError("MONITOR_WEBSOCKET_PATH must start with a slash")
        origin = f"{parsed.scheme}://{parsed.netloc}"
        request = (
            f"GET {websocket_path} HTTP/1.1\r\n"
            f"Host: {parsed.netloc}\r\n"
            f"Origin: {origin}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        with socket.create_connection((parsed.hostname, port), timeout=self.timeout) as sock:
            stream = sock
            if parsed.scheme == "https":
                stream = ssl.create_default_context().wrap_socket(
                    sock, server_hostname=parsed.hostname
                )
            stream.sendall(request)
            response = stream.recv(1024).decode("latin-1", "replace")
        status_line = response.splitlines()[0] if response else ""
        try:
            status = int(status_line.split()[1])
        except (IndexError, ValueError):
            status = 0
        # An anonymous client may be upgraded and closed by ASGI, or rejected by auth.
        ok = status in {101, 401, 403}
        return CheckResult(
            "websocket_route",
            ok,
            f"WebSocket route answered with HTTP {status}",
            status,
            "web",
        )

    def check_disk(self) -> CheckResult:
        usage = shutil.disk_usage(self.app_dir)
        percent = round(usage.used * 100 / usage.total, 1)
        maximum = env_int("MONITOR_DISK_WARN_PERCENT", 85, 50)
        return CheckResult(
            "disk_space",
            percent < maximum,
            f"filesystem usage is {percent}%",
            percent,
            "host",
        )

    def check_inodes(self) -> CheckResult:
        usage = os.statvfs(self.app_dir)
        total = usage.f_files
        free = usage.f_ffree
        percent = round((total - free) * 100 / total, 1) if total else 0.0
        maximum = env_int("MONITOR_INODE_WARN_PERCENT", 85, 50)
        return CheckResult(
            "filesystem_inodes",
            percent < maximum,
            f"filesystem inode usage is {percent}%",
            percent,
            "host",
        )

    def check_memory(self) -> CheckResult:
        values = {}
        with open("/proc/meminfo", encoding="ascii") as source:
            for line in source:
                key, value = line.split(":", 1)
                values[key] = int(value.strip().split()[0])
        total = values["MemTotal"]
        available = values.get("MemAvailable", values.get("MemFree", 0))
        percent = round(available * 100 / total, 1)
        minimum = env_int("MONITOR_MEMORY_MIN_PERCENT", 10, 1)
        return CheckResult(
            "memory_available",
            percent >= minimum,
            f"available memory is {percent}%",
            percent,
            "host",
        )

    def check_load(self) -> CheckResult:
        load_1, _, _ = os.getloadavg()
        cpus = os.cpu_count() or 1
        ratio = round(load_1 / cpus, 2)
        maximum = float(os.getenv("MONITOR_LOAD_PER_CPU", "1.5"))
        return CheckResult(
            "system_load",
            ratio <= maximum,
            f"one-minute load is {ratio} per CPU",
            ratio,
            "host",
        )

    def check_docker(self) -> CheckResult:
        result = self._run(
            ["docker", "compose", "ps", "-a", "--format", "json"], timeout=20
        )
        if result.returncode:
            raise MonitorError("docker compose status command failed")
        rows = []
        stripped = result.stdout.strip()
        if stripped:
            try:
                parsed = json.loads(stripped)
                rows = parsed if isinstance(parsed, list) else [parsed]
            except json.JSONDecodeError:
                rows = [json.loads(line) for line in stripped.splitlines() if line.strip()]
        required = {
            item.strip()
            for item in os.getenv(
                "MONITOR_DOCKER_SERVICES", "db,redis,web,worker,beat,clamav"
            ).split(",")
            if item.strip()
        }
        found = {str(row.get("Service") or row.get("Name") or ""): row for row in rows}
        problems = []
        for service in sorted(required):
            row = found.get(service)
            if not row:
                problems.append(f"{service}:missing")
                continue
            state = str(row.get("State", "")).lower()
            health = str(row.get("Health", "")).lower()
            if state != "running" or health in {"unhealthy", "starting"}:
                problems.append(f"{service}:{state or 'unknown'}/{health or 'no-health'}")
        container_ids = [str(row.get("ID") or "") for row in rows if row.get("ID")]
        restart_total = 0
        if container_ids:
            restart_result = self._run(
                [
                    "docker",
                    "inspect",
                    "--format",
                    "{{.Name}} {{.RestartCount}}",
                    *container_ids,
                ],
                timeout=20,
            )
            if restart_result.returncode:
                problems.append("container restart counters unavailable")
            else:
                maximum_restarts = env_int("MONITOR_MAX_CONTAINER_RESTARTS", 3, 0)
                for line in restart_result.stdout.splitlines():
                    name, _, raw_count = line.rpartition(" ")
                    try:
                        count = int(raw_count)
                    except ValueError:
                        problems.append("invalid container restart counter")
                        continue
                    restart_total += count
                    if count > maximum_restarts:
                        problems.append(
                            f"{name.lstrip('/')}:restarted-{count}-times"
                        )
        return CheckResult(
            "docker_services",
            not problems,
            "all required containers are running"
            if not problems
            else "; ".join(problems),
            restart_total,
            "docker",
        )

    def check_operations(self) -> CheckResult:
        command = [
            "docker", "compose", "exec", "-T", "web", "python", "manage.py",
            "check_operations", "--require-worker", "--require-heartbeat", "--json",
        ]
        result = self._run(command, timeout=35)
        payload = None
        for line in reversed(result.stdout.splitlines()):
            try:
                payload = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
        if not isinstance(payload, dict):
            raise MonitorError("application operations check returned no JSON")
        failures = payload.get("failures") or []
        return CheckResult(
            "application_operations",
            result.returncode == 0 and bool(payload.get("ok")),
            "database, cache, queue and tasks are healthy"
            if not failures
            else "; ".join(str(item) for item in failures)[:500],
            len(failures),
            "application",
        )

    def check_migrations(self) -> CheckResult:
        result = self._run(
            [
                "docker", "compose", "exec", "-T", "web", "python", "manage.py",
                "migrate", "--check", "--noinput",
            ],
            timeout=40,
        )
        return CheckResult(
            "database_migrations",
            result.returncode == 0,
            "all database migrations are applied"
            if result.returncode == 0
            else "unapplied database migrations detected",
            result.returncode,
            "application",
        )

    def check_backup(self) -> CheckResult:
        if not env_bool("MONITOR_REQUIRE_BACKUP", False):
            return CheckResult("database_backup", True, "backup age check is disabled")
        directory = Path(os.getenv("MONITOR_BACKUP_DIR", "/var/backups/consolidator"))
        files = list(directory.glob("*.dump")) if directory.is_dir() else []
        if not files:
            return CheckResult("database_backup", False, "no database backup found", component="backup")
        newest = max(files, key=lambda item: item.stat().st_mtime)
        age_hours = round((time.time() - newest.stat().st_mtime) / 3600, 1)
        maximum = env_int("MONITOR_MAX_BACKUP_AGE_HOURS", 30, 1)
        return CheckResult(
            "database_backup",
            age_hours <= maximum,
            f"latest backup is {age_hours} hour(s) old",
            age_hours,
            "backup",
        )

    def checks(self) -> list[CheckResult]:
        callbacks = [
            ("public_readiness", self.check_public),
            ("security_headers", self.check_security_headers),
            ("dns", self.check_dns),
            ("tls_certificate", self.check_tls),
            ("websocket_route", self.check_websocket_route),
        ]
        if not self.external_only:
            callbacks.extend(
                [
                    ("internal_readiness", self.check_internal),
                    ("disk_space", self.check_disk),
                    ("filesystem_inodes", self.check_inodes),
                    ("memory_available", self.check_memory),
                    ("system_load", self.check_load),
                    ("docker_services", self.check_docker),
                    ("application_operations", self.check_operations),
                    ("database_migrations", self.check_migrations),
                    ("database_backup", self.check_backup),
                ]
            )
        return [self._timed(name, callback) for name, callback in callbacks]

    def _load_state(self) -> dict:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            dir=self.state_path.parent, prefix=".monitor-", text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as target:
                json.dump(state, target, ensure_ascii=True, indent=2, sort_keys=True)
                target.write("\n")
            os.chmod(temporary, 0o640)
            os.replace(temporary, self.state_path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)

    def _notify_local(self, level: str, message: str) -> None:
        print(f"[{level}] {message}", flush=True)
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["logger", "-t", "consolidator-monitor", f"{level}: {message}"],
                timeout=3,
                check=False,
            )

    def _post_json(self, url: str, payload: dict) -> bool:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return 200 <= response.status < 300
        except (OSError, urllib.error.URLError):
            return False

    def notify(self, level: str, message: str, results: list[CheckResult]) -> None:
        self._notify_local(level, message)
        compact = [
            {"name": result.name, "summary": result.summary}
            for result in results
            if not result.ok
        ][:10]
        webhook = os.getenv("MONITOR_WEBHOOK_URL", "").strip()
        if webhook:
            self._post_json(
                webhook,
                {
                    "service": "consolidator",
                    "host": self.hostname,
                    "level": level,
                    "text": f"Consolidator [{level}] {self.hostname}: {message}",
                    "content": f"Consolidator [{level}] {self.hostname}: {message}",
                    "message": message,
                    "failed_checks": compact,
                    "timestamp": iso_now(),
                },
            )
        telegram_token = os.getenv("MONITOR_TELEGRAM_BOT_TOKEN", "").strip()
        telegram_chat = os.getenv("MONITOR_TELEGRAM_CHAT_ID", "").strip()
        if telegram_token and telegram_chat:
            self._post_json(
                f"https://api.telegram.org/bot{telegram_token}/sendMessage",
                {
                    "chat_id": telegram_chat,
                    "text": f"Consolidator [{level}] {self.hostname}\n{message}"[:4000],
                    "disable_web_page_preview": True,
                },
            )

    def _heartbeat(self, ok: bool) -> None:
        name = "UPTIME_HEARTBEAT_URL" if ok else "UPTIME_HEARTBEAT_FAIL_URL"
        url = os.getenv(name, "").strip()
        if not url:
            return
        try:
            with self._http(url):
                pass
        except (OSError, urllib.error.URLError):
            self._notify_local("warning", f"could not send {name}")

    def _autoheal(self, result: CheckResult, check_state: dict) -> str | None:
        if not self.autoheal or self.external_only:
            return None
        failures = int(check_state.get("consecutive_failures", 0))
        if failures < self.autoheal_after:
            return None
        now = time.time()
        last_heal = float(check_state.get("last_heal_epoch", 0) or 0)
        if now - last_heal < self.autoheal_cooldown:
            return None
        services: list[str] = []
        if result.name in {"public_readiness", "internal_readiness", "websocket_route"}:
            services = ["web"]
        elif result.name == "application_operations":
            services = ["worker", "beat"]
        if not services:
            return None
        command = ["docker", "compose", "restart", *services]
        completed = self._run(command, timeout=90)
        check_state["last_heal_epoch"] = now
        check_state["last_heal_at"] = iso_now()
        check_state["last_heal_services"] = services
        return (
            f"restarted {', '.join(services)}"
            if completed.returncode == 0
            else f"restart failed for {', '.join(services)}"
        )

    def run_once(self, *, disable_heal: bool = False) -> tuple[dict, int]:
        if disable_heal:
            self.autoheal = False
        results = self.checks()
        previous = self._load_state()
        previous_checks = previous.get("check_state", {})
        next_checks: dict[str, dict] = {}
        notifications: list[tuple[str, str]] = []
        now_epoch = time.time()

        for result in results:
            old = previous_checks.get(result.name, {})
            entry = {
                "ok": result.ok,
                "summary": result.summary,
                "value": result.value,
                "component": result.component,
                "duration_ms": result.duration_ms,
                "checked_at": iso_now(),
                "consecutive_failures": 0,
                "alerted": bool(old.get("alerted", False)),
                "last_alert_epoch": float(old.get("last_alert_epoch", 0) or 0),
            }
            for key in ("last_heal_epoch", "last_heal_at", "last_heal_services"):
                if key in old:
                    entry[key] = old[key]
            if result.ok:
                if old.get("alerted"):
                    notifications.append(("recovery", f"{result.name}: {result.summary}"))
                entry["alerted"] = False
                entry["last_alert_epoch"] = 0.0
            else:
                entry["consecutive_failures"] = int(old.get("consecutive_failures", 0)) + 1
                due = now_epoch - entry["last_alert_epoch"] >= self.alert_cooldown
                if entry["consecutive_failures"] >= self.alert_after and due:
                    notifications.append(("critical", f"{result.name}: {result.summary}"))
                    entry["alerted"] = True
                    entry["last_alert_epoch"] = now_epoch
                heal_message = self._autoheal(result, entry)
                if heal_message:
                    notifications.append(("autoheal", f"{result.name}: {heal_message}"))
            next_checks[result.name] = entry

        healthy = all(result.ok for result in results)
        state = {
            "version": VERSION,
            "host": self.hostname,
            "mode": "external" if self.external_only else "local",
            "ok": healthy,
            "checked_at": iso_now(),
            "failed_checks": [result.name for result in results if not result.ok],
            "check_state": next_checks,
        }
        self._save_state(state)
        self._heartbeat(healthy)
        for level, message in notifications:
            self.notify(level, message, results)
        self._notify_local(
            "ok" if healthy else "failed",
            f"{len(results)} checks, {len(state['failed_checks'])} failed",
        )
        return state, 0 if healthy else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", default=os.getenv("APP_DIR", os.getcwd()))
    parser.add_argument(
        "--state-file",
        default=os.getenv(
            "MONITOR_STATE_FILE", "/var/lib/consolidator-monitor/status.json"
        ),
    )
    parser.add_argument("--external-only", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--no-heal", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    state_path = Path(args.state_file).resolve()
    if args.status:
        try:
            print(state_path.read_text(encoding="utf-8"), end="")
            return 0
        except OSError as exc:
            print(f"monitor state is unavailable: {exc}", file=sys.stderr)
            return 2

    lock_path = state_path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="ascii") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("monitoring check is already running", file=sys.stderr)
            return 3
        controller = Controller(
            app_dir=Path(args.app_dir).resolve(),
            state_path=state_path,
            external_only=args.external_only,
        )
        _, exit_code = controller.run_once(disable_heal=args.no_heal)
        return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
