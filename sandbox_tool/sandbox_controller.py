from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field


RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")


class GateRequest(BaseModel):
    artifacts: list[str] = Field(default_factory=list)
    xlsx_dangerous_formula_action: str = "reject"


class ExecuteRequest(BaseModel):
    image: str
    command: str
    timeout_seconds: int = 120


def runs_root() -> Path:
    return Path(os.getenv("RUNS_ROOT", "/srv/sandbox-tool/runs")).resolve()


def host_runs_root() -> Path:
    return Path(os.getenv("RUNS_ROOT_HOST", str(runs_root()))).resolve()


def controller_token() -> str:
    return os.getenv("SANDBOX_CONTROLLER_TOKEN", "")


def output_gate_image() -> str:
    return os.getenv("OUTPUT_GATE_IMAGE", "sandbox-tool-agent:local")


def sandbox_image_allowlist() -> set[str]:
    raw = os.getenv("SANDBOX_IMAGE_ALLOWLIST", "localhost/python-data-sandbox:latest")
    return {item.strip() for item in raw.split(",") if item.strip()}


def verify_auth(authorization: str | None = Header(default=None)) -> None:
    token = controller_token()
    if not token:
        return
    if authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="invalid controller token")


def validate_run_id(run_id: str) -> str:
    if not RUN_ID_RE.match(run_id):
        raise HTTPException(status_code=400, detail="invalid run_id")
    return run_id


def run_path(run_id: str) -> Path:
    validate_run_id(run_id)
    root = runs_root()
    path = (root / run_id).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="run path escaped root") from exc
    return path


def ensure_run_dirs(run_id: str) -> dict[str, Path]:
    root = run_path(run_id)
    dirs = {
        "run_root": root,
        "input": root / "input",
        "workspace": root / "workspace",
        "raw_outputs": root / "raw_outputs",
        "clean_exports": root / "clean_exports",
        "quarantine": root / "quarantine",
        "gate_logs": root / "gate_logs",
        "runner_logs": root / "runner_logs",
    }
    mode = int(os.getenv("RUN_DIR_MODE", "0777"), 8)
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
        try:
            path.chmod(mode)
        except OSError:
            pass
    return dirs


def docker_client() -> Any:
    try:
        import docker
    except Exception as exc:  # pragma: no cover
        raise HTTPException(
            status_code=500,
            detail=f"docker sdk unavailable: {exc.__class__.__name__}: {exc}",
        ) from exc
    socket = os.getenv("SANDBOX_SOCKET")
    if socket:
        return docker.DockerClient(base_url=f"unix://{socket}")
    return docker.from_env()


def docker_mount(source: Path, target: str, read_only: bool) -> Any:
    import docker

    mount_source = source.resolve()
    root = runs_root()
    try:
        rel = mount_source.relative_to(root)
        mount_source = host_runs_root() / rel
    except ValueError:
        pass
    return docker.types.Mount(
        target=target,
        source=str(mount_source),
        type="bind",
        read_only=read_only,
    )


def hardened_run_kwargs(*, working_dir: str) -> dict[str, Any]:
    writable_home = "/workspace" if working_dir == "/workspace" else "/tmp"
    return {
        "network_disabled": True,
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges"],
        "user": os.getenv("SANDBOX_CONTAINER_USER", "1000:1000"),
        "mem_limit": os.getenv("SANDBOX_MEMORY_LIMIT", "2g"),
        "pids_limit": int(os.getenv("SANDBOX_PIDS_LIMIT", "256")),
        "nano_cpus": int(float(os.getenv("SANDBOX_CPUS", "2")) * 1_000_000_000),
        "tmpfs": {
            "/tmp": "rw,nosuid,nodev,noexec,size=256m",
            "/run": "rw,nosuid,nodev,noexec,size=64m",
        },
        "working_dir": working_dir,
        "environment": {
            "PYTHONUNBUFFERED": "1",
            "LANG": "C.UTF-8",
            "HOME": writable_home,
            "XDG_CACHE_HOME": f"{writable_home}/.cache",
            "MPLCONFIGDIR": f"{writable_home}/.matplotlib",
            "PYTENSOR_FLAGS": f"base_compiledir={writable_home}/.pytensor",
            "MPLBACKEND": "Agg",
        },
    }


def run_hardened_container(
    *,
    image: str,
    command: list[str],
    mounts: list[Any],
    timeout_seconds: int,
    working_dir: str,
) -> tuple[int, bytes]:
    client = docker_client()
    container = client.containers.create(
        image,
        command=command,
        mounts=mounts,
        detach=True,
        **hardened_run_kwargs(working_dir=working_dir),
    )
    try:
        container.start()
        result = container.wait(timeout=timeout_seconds)
        output = container.logs(stdout=True, stderr=True)
        status_code = int(result.get("StatusCode", 1))
        return status_code, output
    except Exception:
        try:
            container.kill()
        except Exception:
            pass
        raise
    finally:
        try:
            container.remove(force=True)
        except Exception:
            pass


app = FastAPI(title="Sandbox Controller", version="0.1.0")


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"ok": True, "runs_root": str(runs_root())}


@app.post("/runs/{run_id}/gate", dependencies=[Depends(verify_auth)])
def run_gate(run_id: str, request: GateRequest) -> dict[str, Any]:
    if request.xlsx_dangerous_formula_action not in {"reject", "stringify"}:
        raise HTTPException(status_code=400, detail="invalid xlsx action")
    if not request.artifacts:
        raise HTTPException(status_code=400, detail="artifacts required")
    for artifact in request.artifacts:
        normalized = artifact.replace("\\", "/")
        if not normalized.startswith("/outputs/") or "/../" in normalized:
            raise HTTPException(status_code=400, detail=f"invalid artifact path: {artifact}")

    dirs = ensure_run_dirs(run_id)
    command = [
        "python",
        "-m",
        "sandbox_tool.output_gate",
        "--raw-root",
        "/raw_outputs",
        "--clean-root",
        "/clean_exports",
        "--quarantine-root",
        "/quarantine",
        "--log-root",
        "/gate_logs",
        "--run-id",
        run_id,
        "--xlsx-dangerous-formula-action",
        request.xlsx_dangerous_formula_action,
    ]
    for artifact in request.artifacts:
        command.extend(["--artifact", artifact])

    try:
        status_code, output = run_hardened_container(
            image=output_gate_image(),
            command=command,
            mounts=[
                docker_mount(dirs["raw_outputs"], "/raw_outputs", True),
                docker_mount(dirs["clean_exports"], "/clean_exports", False),
                docker_mount(dirs["quarantine"], "/quarantine", False),
                docker_mount(dirs["gate_logs"], "/gate_logs", False),
            ],
            timeout_seconds=120,
            working_dir="/app",
        )
        if status_code != 0:
            raise RuntimeError(
                f"gate container exited with status {status_code}: "
                f"{output.decode('utf-8', errors='replace')[-4000:]}"
            )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"gate container failed: {exc.__class__.__name__}: {exc}",
        ) from exc
    manifest_path = dirs["gate_logs"] / "gate_manifest.json"
    manifest = None
    if manifest_path.exists():
        import json

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        "ok": bool(manifest and manifest.get("overall_status") == "pass"),
        "run_id": run_id,
        "manifest": manifest,
        "container_output": output.decode("utf-8", errors="replace")[-4000:]
        if isinstance(output, bytes)
        else str(output)[-4000:],
    }


@app.post("/runs/{run_id}/execute", dependencies=[Depends(verify_auth)])
def run_sandbox(run_id: str, request: ExecuteRequest) -> dict[str, Any]:
    if request.image not in sandbox_image_allowlist():
        raise HTTPException(status_code=400, detail="image is not allowlisted")
    dirs = ensure_run_dirs(run_id)
    try:
        status_code, output = run_hardened_container(
            image=request.image,
            command=["/bin/sh", "-lc", request.command],
            mounts=[
                docker_mount(dirs["workspace"], "/workspace", False),
                docker_mount(dirs["input"], "/input", True),
                docker_mount(dirs["raw_outputs"], "/outputs", False),
            ],
            timeout_seconds=max(1, min(request.timeout_seconds, 3600)),
            working_dir="/workspace",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"sandbox container failed: {exc.__class__.__name__}: {exc}",
        ) from exc
    return {
        "ok": status_code == 0,
        "run_id": run_id,
        "exit_code": status_code,
        "output": output.decode("utf-8", errors="replace") if isinstance(output, bytes) else str(output),
    }


@app.post("/runs/{run_id}/cleanup", dependencies=[Depends(verify_auth)])
def cleanup(run_id: str) -> dict[str, Any]:
    # Container cleanup is handled by `remove=True`; run directory retention is a
    # policy decision, so this endpoint intentionally does not delete artifacts.
    dirs = ensure_run_dirs(run_id)
    return {"ok": True, "run_id": run_id, "run_root": str(dirs["run_root"])}


@app.get("/runs/{run_id}/status", dependencies=[Depends(verify_auth)])
def status(run_id: str) -> dict[str, Any]:
    dirs = ensure_run_dirs(run_id)
    return {
        "ok": True,
        "run_id": run_id,
        "dirs": {name: str(path) for name, path in dirs.items()},
        "gate_manifest_exists": (dirs["gate_logs"] / "gate_manifest.json").exists(),
    }
