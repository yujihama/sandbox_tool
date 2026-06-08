from __future__ import annotations

import posixpath
import shutil
import subprocess
import tempfile
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox


@dataclass(frozen=True)
class PodmanRunLimits:
    cpus: str = "2"
    memory: str = "4g"
    memory_swap: str = "4g"
    pids_limit: int = 256
    timeout_seconds: int = 120
    tmp_size: str = "512m"
    run_size: str = "64m"
    shm_size: str = "128m"
    enforce_cgroups: bool = True
    shell_cpu_seconds: int | None = None
    shell_virtual_memory_kb: int | None = None


@dataclass(frozen=True)
class PodmanSecurityOptions:
    network: str = "none"
    read_only_rootfs: bool = True
    no_new_privileges: bool = True
    cap_drop_all: bool = True
    userns: str | None = "keep-id"
    user: str | None = "1000:1000"
    pull: str = "never"
    selinux_relabel: bool = False
    unsetenv_all: bool = True
    root_mount_dirs: tuple[str, ...] = (
        "large_tool_results",
        "conversation_history",
        "memories",
    )


@dataclass
class PodmanSandboxBackend(BaseSandbox):
    """Deep Agents sandbox backend backed by short-lived Podman containers.

    The backend keeps a per-sandbox host scratch directory, then starts a fresh
    container for each `execute()` call. Container cleanup is belt-and-suspenders:
    `podman run --rm` is used, and the generated container name is removed again
    in `finally`.

    This class intentionally does not expose local environment variables to the
    container. Seed data through `input_dir`, `upload_files()`, or explicit bind
    mounts, and collect artifacts through `output_dir` or `download_files()`.
    """

    image: str
    input_dir: Path | None = None
    output_dir: Path | None = None
    podman: str | tuple[str, ...] = "podman"
    host_path_mode: str = "native"
    limits: PodmanRunLimits = field(default_factory=PodmanRunLimits)
    security: PodmanSecurityOptions = field(default_factory=PodmanSecurityOptions)
    dry_run: bool = False
    keep_workspace: bool = False
    max_output_bytes: int = 200_000
    extra_run_args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        self._sandbox_id = f"podman-{uuid.uuid4().hex[:12]}"
        self._workspace_dir = Path(
            tempfile.mkdtemp(prefix=f"{self._sandbox_id}-")
        ).resolve()
        self._scratch_dir = self._workspace_dir / "workspace"
        self._scratch_dir.mkdir(parents=True, exist_ok=True)

        if self.input_dir is not None:
            self.input_dir = Path(self.input_dir).resolve()
            if not self.input_dir.exists():
                raise FileNotFoundError(f"input_dir does not exist: {self.input_dir}")
        if self.output_dir is not None:
            self.output_dir = Path(self.output_dir).resolve()
            self.output_dir.mkdir(parents=True, exist_ok=True)

        for name in self.security.root_mount_dirs:
            (self._scratch_dir / name).mkdir(parents=True, exist_ok=True)

        self._last_container_names: list[str] = []
        self._last_run_argv: list[str] | None = None

    @classmethod
    def for_wsl(
        cls,
        *,
        image: str,
        distro: str = "Ubuntu-22.04",
        input_dir: Path | None = None,
        output_dir: Path | None = None,
        use_sudo: bool = True,
        limits: PodmanRunLimits | None = None,
        security: PodmanSecurityOptions | None = None,
        **kwargs: object,
    ) -> "PodmanSandboxBackend":
        uid, gid = _wsl_user_ids(distro)
        _ensure_wsl_runtime_dir(distro, uid, gid)

        command_prefix = ["wsl", "-d", distro, "--"]
        if use_sudo:
            command_prefix.append("sudo")
        else:
            command_prefix.extend(["env", f"XDG_RUNTIME_DIR=/run/user/{uid}"])
        command_prefix.append("podman")

        if limits is None:
            limits = PodmanRunLimits(
                enforce_cgroups=False,
                shell_cpu_seconds=120,
                shell_virtual_memory_kb=4 * 1024 * 1024,
            )
        if security is None:
            security = PodmanSecurityOptions(
                pull="missing",
                unsetenv_all=False,
                user=f"{uid}:{gid}",
            )

        return cls(
            image=image,
            input_dir=input_dir,
            output_dir=output_dir,
            podman=tuple(command_prefix),
            host_path_mode="wsl",
            limits=limits,
            security=security,
            **kwargs,
        )

    @property
    def id(self) -> str:
        return self._sandbox_id

    @property
    def workspace_dir(self) -> Path:
        return self._workspace_dir

    @property
    def last_run_argv(self) -> list[str] | None:
        return self._last_run_argv

    def __enter__(self) -> "PodmanSandboxBackend":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        self.cleanup()

    def cleanup(self) -> None:
        """Remove any known leftover containers and the host scratch directory."""
        for name in list(dict.fromkeys(self._last_container_names)):
            try:
                with subprocess.Popen(
                    self._podman_argv("rm", "-f", name),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ) as proc:
                    proc.wait(timeout=10)
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
        self._last_container_names.clear()

        if not self.keep_workspace and self._workspace_dir.exists():
            shutil.rmtree(self._workspace_dir, ignore_errors=True)

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        effective_timeout = timeout or self.limits.timeout_seconds
        container_name = f"{self._sandbox_id}-{uuid.uuid4().hex[:8]}"
        self._last_container_names.append(container_name)
        argv = self._build_run_argv(command, container_name, effective_timeout)
        self._last_run_argv = argv

        if self.dry_run:
            return ExecuteResponse(
                output="DRY RUN: " + " ".join(_quote_arg(arg) for arg in argv),
                exit_code=0,
                truncated=False,
            )

        try:
            completed = subprocess.run(
                argv,
                cwd=str(self._workspace_dir),
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=effective_timeout + 15,
                check=False,
            )
            output = completed.stdout or ""
            truncated = False
            if len(output.encode("utf-8", errors="replace")) > self.max_output_bytes:
                encoded = output.encode("utf-8", errors="replace")[
                    : self.max_output_bytes
                ]
                output = encoded.decode("utf-8", errors="replace")
                output += "\n[output truncated by PodmanSandboxBackend]"
                truncated = True
            return ExecuteResponse(
                output=output,
                exit_code=completed.returncode,
                truncated=truncated,
            )
        except FileNotFoundError:
            return ExecuteResponse(
                output=f"Podman executable not found: {self.podman}",
                exit_code=127,
                truncated=False,
            )
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            return ExecuteResponse(
                output=output + f"\nTimed out after {effective_timeout}s",
                exit_code=124,
                truncated=False,
            )
        finally:
            try:
                subprocess.run(
                    self._podman_argv("rm", "-f", container_name),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            except FileNotFoundError:
                pass
            with suppress(ValueError):
                self._last_container_names.remove(container_name)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        responses: list[FileUploadResponse] = []
        for target, content in files:
            try:
                path = self._resolve_virtual_path(target)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
            except PermissionError:
                responses.append(FileUploadResponse(path=target, error="permission_denied"))
            except IsADirectoryError:
                responses.append(FileUploadResponse(path=target, error="is_directory"))
            except ValueError:
                responses.append(FileUploadResponse(path=target, error="invalid_path"))
            else:
                responses.append(FileUploadResponse(path=target))
        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        responses: list[FileDownloadResponse] = []
        for source in paths:
            try:
                path = self._resolve_virtual_path(source)
                if path.is_dir():
                    responses.append(FileDownloadResponse(path=source, error="is_directory"))
                elif not path.exists():
                    responses.append(
                        FileDownloadResponse(path=source, error="file_not_found")
                    )
                else:
                    responses.append(
                        FileDownloadResponse(path=source, content=path.read_bytes())
                    )
            except PermissionError:
                responses.append(
                    FileDownloadResponse(path=source, error="permission_denied")
                )
            except ValueError:
                responses.append(FileDownloadResponse(path=source, error="invalid_path"))
        return responses

    def _build_run_argv(
        self,
        command: str,
        container_name: str,
        timeout_seconds: int,
    ) -> list[str]:
        argv = self._podman_argv(
            "run",
            "--rm",
            "--name",
            container_name,
            "--pull",
            self.security.pull,
            "--network",
            self.security.network,
            "--workdir",
            "/workspace",
            "--timeout",
            str(timeout_seconds),
            "--shm-size",
            self.limits.shm_size,
        )
        if self.limits.enforce_cgroups:
            argv.extend(
                [
                    "--cpus",
                    self.limits.cpus,
                    "--memory",
                    self.limits.memory,
                    "--memory-swap",
                    self.limits.memory_swap,
                    "--pids-limit",
                    str(self.limits.pids_limit),
                ]
            )
        else:
            argv.append("--cgroups=disabled")
        if self.security.unsetenv_all:
            argv.append("--unsetenv-all")
        argv.extend(
            [
                "--env",
                "PATH=/usr/local/bin:/usr/bin:/bin",
                "--env",
                "LANG=C.UTF-8",
                "--env",
                "HOME=/workspace",
                "--env",
                "XDG_CACHE_HOME=/workspace/.cache",
                "--env",
                "MPLCONFIGDIR=/workspace/.matplotlib",
                "--env",
                "PYTENSOR_FLAGS=base_compiledir=/workspace/.pytensor",
                "--env",
                "PYTHONUNBUFFERED=1",
                "--env",
                "MPLBACKEND=Agg",
            ]
        )
        if self.security.read_only_rootfs:
            argv.extend(["--read-only", "--read-only-tmpfs=false"])
        if self.security.cap_drop_all:
            argv.append("--cap-drop=all")
        if self.security.no_new_privileges:
            argv.extend(["--security-opt", "no-new-privileges"])
        if self.security.userns:
            argv.extend(["--userns", self.security.userns])
        if self.security.user:
            argv.extend(["--user", self.security.user])
        argv.extend(
            [
                "--tmpfs",
                f"/tmp:rw,nosuid,nodev,noexec,size={self.limits.tmp_size}",
                "--tmpfs",
                f"/run:rw,nosuid,nodev,noexec,size={self.limits.run_size}",
            ]
        )

        for src, dst, readonly in self._mounts():
            argv.extend(
                [
                    "--mount",
                    _bind_mount_arg(
                        self._host_path(src),
                        dst,
                        readonly,
                        relabel=self.security.selinux_relabel,
                    ),
                ]
            )

        argv.extend(self.extra_run_args)
        argv.extend([self.image, "/bin/sh", "-lc", self._wrap_shell_command(command)])
        return argv

    def _podman_argv(self, *args: str) -> list[str]:
        if isinstance(self.podman, str):
            return [self.podman, *args]
        return [*self.podman, *args]

    def _wrap_shell_command(self, command: str) -> str:
        prefixes: list[str] = []
        if self.limits.shell_cpu_seconds is not None:
            prefixes.append(f"ulimit -t {int(self.limits.shell_cpu_seconds)}")
        if self.limits.shell_virtual_memory_kb is not None:
            prefixes.append(f"ulimit -v {int(self.limits.shell_virtual_memory_kb)}")
        if not prefixes:
            return command
        return "; ".join(prefixes + [command])

    def _mounts(self) -> Iterable[tuple[Path, str, bool]]:
        yield self._scratch_dir, "/workspace", False

        if self.input_dir is not None:
            yield self.input_dir, "/input", True
            yield self.input_dir, "/workspace/input", True

        if self.output_dir is not None:
            yield self.output_dir, "/outputs", False
            yield self.output_dir, "/workspace/outputs", False

        for name in self.security.root_mount_dirs:
            path = self._scratch_dir / name
            path.mkdir(parents=True, exist_ok=True)
            yield path, f"/{name}", False

    def _host_path(self, path: Path) -> str:
        if self.host_path_mode == "native":
            return str(path)
        if self.host_path_mode == "wsl":
            return _windows_path_to_wsl(path)
        raise ValueError(f"Unsupported host_path_mode: {self.host_path_mode}")

    def _resolve_virtual_path(self, path: str) -> Path:
        normalized = _normalize_sandbox_path(path)
        if normalized == ".":
            return self._scratch_dir

        first, _, rest = normalized.partition("/")
        if first == "workspace":
            return _join_under(self._scratch_dir, rest or ".")
        if first == "input":
            if self.input_dir is None:
                raise ValueError("input path requested but input_dir is not configured")
            return _join_under(self.input_dir, rest or ".")
        if first in {"outputs", "output"}:
            if self.output_dir is None:
                return _join_under(self._scratch_dir, normalized)
            return _join_under(self.output_dir, rest or ".")
        return _join_under(self._scratch_dir, normalized)


def _normalize_sandbox_path(path: str) -> str:
    raw = path.replace("\\", "/").strip()
    if not raw:
        raise ValueError("empty sandbox path")
    if ":" in raw.split("/", 1)[0]:
        raise ValueError("drive-qualified paths are not allowed")
    raw = raw.lstrip("/")
    normalized = posixpath.normpath(raw)
    if normalized in {"", "/"}:
        return "."
    if normalized == ".." or normalized.startswith("../"):
        raise ValueError("path traversal is not allowed")
    if normalized.startswith("~"):
        raise ValueError("home-relative paths are not allowed")
    return normalized


def _join_under(root: Path, relative: str) -> Path:
    candidate = (root / Path(relative)).resolve()
    resolved_root = root.resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("resolved path escapes sandbox root") from exc
    return candidate


def _bind_mount_arg(src: str, dst: str, readonly: bool, *, relabel: bool = False) -> str:
    options = ["type=bind", f"src={src}", f"dst={dst}"]
    if readonly:
        options.append("ro")
    else:
        options.append("rw")
    if relabel:
        options.append("relabel=private")
    return ",".join(options)


def _windows_path_to_wsl(path: Path) -> str:
    resolved = path.resolve()
    drive = resolved.drive.rstrip(":").lower()
    if drive:
        rest = "/".join(resolved.parts[1:]).replace("\\", "/")
        return f"/mnt/{drive}/{rest}"
    return resolved.as_posix()


def _wsl_user_ids(distro: str) -> tuple[int, int]:
    uid_result = subprocess.run(
        ["wsl", "-d", distro, "--", "id", "-u"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    gid_result = subprocess.run(
        ["wsl", "-d", distro, "--", "id", "-g"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    return int(uid_result.stdout.strip()), int(gid_result.stdout.strip())


def _ensure_wsl_runtime_dir(distro: str, uid: int, gid: int) -> None:
    script = (
        "set -e; "
        "sudo chmod 755 /run/user; "
        f"sudo mkdir -p /run/user/{uid}; "
        f"sudo chown {uid}:{gid} /run/user/{uid}; "
        f"sudo chmod 700 /run/user/{uid}"
    )
    subprocess.run(
        ["wsl", "-d", distro, "--", "bash", "-lc", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


def _quote_arg(arg: str) -> str:
    if not arg or any(ch.isspace() for ch in arg) or '"' in arg:
        return '"' + arg.replace('"', '\\"') + '"'
    return arg
