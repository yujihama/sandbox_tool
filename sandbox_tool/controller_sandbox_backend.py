from __future__ import annotations

import json
import posixpath
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox


@dataclass
class ControllerSandboxBackend(BaseSandbox):
    """Deep Agents sandbox backend that delegates execution to sandbox-controller."""

    image: str
    controller_url: str
    run_id: str
    input_dir: Path
    output_dir: Path
    workspace_dir: Path
    token: str = ""
    timeout_seconds: int = 120
    max_output_bytes: int = 450_000

    @property
    def id(self) -> str:
        return f"controller-{self.run_id}"

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        payload = {
            "image": self.image,
            "command": command,
            "timeout_seconds": int(timeout or self.timeout_seconds),
        }
        try:
            response = self._request(
                "POST",
                f"/runs/{self.run_id}/execute",
                payload,
                timeout_seconds=int(timeout or self.timeout_seconds) + 30,
            )
        except Exception as exc:
            return ExecuteResponse(
                output=f"sandbox-controller execute failed: {exc.__class__.__name__}: {exc}",
                exit_code=1,
                truncated=False,
            )
        output = str(response.get("output", ""))
        truncated = False
        if len(output.encode("utf-8", errors="replace")) > self.max_output_bytes:
            encoded = output.encode("utf-8", errors="replace")[: self.max_output_bytes]
            output = encoded.decode("utf-8", errors="replace")
            output += "\n[output truncated by ControllerSandboxBackend]"
            truncated = True
        return ExecuteResponse(
            output=output,
            exit_code=int(response.get("exit_code", 0 if response.get("ok") else 1)),
            truncated=truncated,
        )

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
                    responses.append(FileDownloadResponse(path=source, error="file_not_found"))
                else:
                    responses.append(FileDownloadResponse(path=source, content=path.read_bytes()))
            except PermissionError:
                responses.append(FileDownloadResponse(path=source, error="permission_denied"))
            except ValueError:
                responses.append(FileDownloadResponse(path=source, error="invalid_path"))
        return responses

    def cleanup(self) -> None:
        try:
            self._request("POST", f"/runs/{self.run_id}/cleanup", {}, timeout_seconds=30)
        except Exception:
            pass

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        url = self.controller_url.rstrip("/") + path
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(
                request,
                timeout=max(timeout_seconds or self.timeout_seconds + 30, 60),
            ) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
        return json.loads(body) if body else {}

    def _resolve_virtual_path(self, path: str) -> Path:
        normalized = _normalize_sandbox_path(path)
        if normalized == ".":
            return self.workspace_dir.resolve()
        first, _, rest = normalized.partition("/")
        if first == "workspace":
            return _join_under(self.workspace_dir, rest or ".")
        if first == "input":
            return _join_under(self.input_dir, rest or ".")
        if first in {"outputs", "output"}:
            return _join_under(self.output_dir, rest or ".")
        return _join_under(self.workspace_dir, normalized)


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
