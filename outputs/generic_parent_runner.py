from __future__ import annotations

import argparse
import base64
import csv
import json
import mimetypes
import os
import posixpath
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.tools import tool
from openai import OpenAI

from deepagents import create_deep_agent


THIS_FILE = Path(__file__).resolve()
ROOT = THIS_FILE.parents[1]
WORK_DIR = ROOT / "work"
OUTPUTS_ROOT = ROOT / "outputs"

for import_dir in (ROOT, THIS_FILE.parent, WORK_DIR):
    if str(import_dir) not in sys.path:
        sys.path.insert(0, str(import_dir))

from podman_sandbox_backend import PodmanRunLimits, PodmanSandboxBackend  # noqa: E402
from sandbox_tool.controller_sandbox_backend import ControllerSandboxBackend  # noqa: E402
from sandbox_tool.output_gate import (  # noqa: E402
    ALLOWED_EXTENSIONS,
    run_output_gate as run_output_gate_artifacts,
    sha256_file,
)


@dataclass
class InputMapping:
    host_path: Path
    sandbox_path: str
    staged_path: Path


@dataclass
class RunnerConfig:
    prompt: str
    run_root: Path
    output_dir: Path
    clean_export_dir: Path
    quarantine_dir: Path
    gate_log_dir: Path
    runner_log_dir: Path
    input_dir: Path
    workspace_dir: Path
    input_mappings: list[InputMapping]
    expected_artifacts: list[str]
    skill_sources: list[str]
    image: str
    wsl_distro: str
    parent_model: str
    deep_model: str
    parent_recursion_limit: int
    deep_recursion_limit: int
    max_review_rounds: int
    host_os: str
    podman_bin: str
    wsl_use_sudo: bool
    selinux_relabel: bool
    clear_output: bool
    keep_staged_input: bool
    allow_raw_parent_inspection: bool
    sandbox_backend: str
    sandbox_controller_url: str
    sandbox_controller_token: str
    xlsx_dangerous_formula_action: str


CONFIG: RunnerConfig | None = None
DEEP_AGENT_TRACE: list[dict[str, Any]] = []
DEEP_AGENT_EVALUATIONS: list[dict[str, Any]] = []
DEEP_REVIEW_REQUESTS: list[dict[str, Any]] = []
ALLOWED_EXPORT_EXTENSIONS_TEXT = ", ".join(sorted(ALLOWED_EXTENSIONS))


def load_env_local(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def require_safe_output_dir(output_dir: Path, *, sandbox_backend: str = "podman") -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    resolved = output_dir.resolve()
    outputs_root = (
        Path(os.getenv("RUNS_ROOT", "/srv/sandbox-tool/runs")).resolve()
        if sandbox_backend == "controller"
        else OUTPUTS_ROOT.resolve()
    )
    if resolved == outputs_root:
        raise ValueError(f"Refusing to use the run root itself as --output-dir: {outputs_root}")
    if not is_relative_to(resolved, outputs_root):
        raise ValueError(f"--output-dir must be under {outputs_root}: {resolved}")
    return resolved


def prepare_run_directories(run_root: Path) -> dict[str, Path]:
    dirs = {
        "run_root": run_root,
        "input_dir": run_root / "input",
        "workspace_dir": run_root / "workspace",
        "raw_output_dir": run_root / "raw_outputs",
        "clean_export_dir": run_root / "clean_exports",
        "quarantine_dir": run_root / "quarantine",
        "gate_log_dir": run_root / "gate_logs",
        "runner_log_dir": run_root / "runner_logs",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return {name: path.resolve() for name, path in dirs.items()}


def clear_directory_contents(directory: Path) -> None:
    root = directory.resolve()
    for child in directory.iterdir():
        resolved = child.resolve()
        if not is_relative_to(resolved, root):
            raise RuntimeError(f"Refusing to delete outside output dir: {resolved}")
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def normalize_expected_artifact(path: str) -> str:
    raw = path.replace("\\", "/")
    if not raw.startswith("/outputs/"):
        raise ValueError(f"Expected artifacts must be under /outputs/: {path}")
    normalized = posixpath.normpath(raw)
    if normalized == "/outputs" or normalized.startswith("/outputs/../"):
        raise ValueError(f"Invalid expected artifact path: {path}")
    suffix = posixpath.splitext(normalized)[1].lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError(
            "Expected/review artifacts must use an output-gate allowed extension "
            f"({ALLOWED_EXPORT_EXTENSIONS_TEXT}): {path}"
        )
    return normalized


def normalize_tool_expected_artifacts(paths: list[str]) -> list[str]:
    if CONFIG is None:
        raise RuntimeError("Runner config is not initialized.")
    normalized = [normalize_expected_artifact(path) for path in paths]
    if not normalized:
        raise ValueError("expected_artifacts must include at least one artifact")
    allowed_final = set(CONFIG.expected_artifacts)
    invalid = [
        path
        for path in normalized
        if path not in allowed_final and not path.startswith("/outputs/subtasks/")
    ]
    if invalid:
        raise ValueError(
            "Expected artifacts passed to the Deep Agent tool must be either the "
            f"configured final artifacts or intermediate /outputs/subtasks/* artifacts: {invalid}"
        )
    return normalized


def sandbox_input_relative(target: str) -> str:
    raw = target.replace("\\", "/")
    if raw.startswith("/input/"):
        raw = raw.removeprefix("/input/")
    elif raw.startswith("input/"):
        raw = raw.removeprefix("input/")
    raw = posixpath.normpath(raw)
    if raw in {"", "."} or raw.startswith("../") or raw == ".." or raw.startswith("/"):
        raise ValueError(f"Invalid /input target path: {target}")
    return raw


def parse_input_spec(spec: str) -> tuple[Path, str]:
    if "=" in spec:
        host, target = spec.split("=", 1)
    else:
        host = spec
        target = Path(spec).name
    host_path = Path(host).expanduser().resolve()
    if not host_path.exists():
        raise FileNotFoundError(f"Input file does not exist: {host_path}")
    rel = sandbox_input_relative(target)
    return host_path, "/input/" + rel


def stage_inputs(input_specs: list[str], staging_dir: Path) -> list[InputMapping]:
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)
    mappings: list[InputMapping] = []
    for spec in input_specs:
        host_path, sandbox_path = parse_input_spec(spec)
        rel = sandbox_path.removeprefix("/input/")
        destination = staging_dir / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        if host_path.is_dir():
            shutil.copytree(host_path, destination)
        else:
            shutil.copy2(host_path, destination)
        mappings.append(
            InputMapping(
                host_path=host_path,
                sandbox_path=sandbox_path,
                staged_path=destination,
            )
        )
    return mappings


def stage_skill_sources(skill_specs: list[str], input_dir: Path) -> list[str]:
    """Stage host skill source directories into /input and return sandbox source paths."""
    staged_sources: list[str] = []
    for spec in skill_specs:
        if "=" in spec:
            host, target = spec.split("=", 1)
        else:
            host = spec
            target = "skills"
        host_path = Path(host).expanduser().resolve()
        if not host_path.exists() or not host_path.is_dir():
            raise FileNotFoundError(f"Skill source must be an existing directory: {host_path}")
        rel = sandbox_input_relative(target)
        destination = input_dir / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(host_path, destination)
        staged_sources.append("/input/" + rel)
    return staged_sources


def resolve_sandbox_path(path: str | Path) -> Path:
    if CONFIG is None:
        raise RuntimeError("Runner config is not initialized.")
    raw = str(path).replace("\\", "/")
    if raw == "/outputs":
        return CONFIG.output_dir
    if raw.startswith("/outputs/"):
        return CONFIG.output_dir / raw.removeprefix("/outputs/")
    if raw == "/exports":
        return CONFIG.clean_export_dir
    if raw.startswith("/exports/"):
        return CONFIG.clean_export_dir / raw.removeprefix("/exports/")
    if raw == "/input":
        return CONFIG.input_dir
    if raw.startswith("/input/"):
        return CONFIG.input_dir / raw.removeprefix("/input/")
    return Path(path)


def normalize_readable_virtual_path(path: str | Path) -> str:
    raw = str(path).replace("\\", "/")
    normalized = posixpath.normpath(raw)
    if normalized in {"/outputs", "/exports", "/input"}:
        return normalized
    if (
        normalized.startswith("/outputs/")
        or normalized.startswith("/exports/")
        or normalized.startswith("/input/")
    ):
        return normalized
    raise ValueError(f"Path must be under /input, /outputs, or /exports: {path}")


def resolve_readable_virtual_path(path: str | Path) -> tuple[str, Path]:
    normalized = normalize_readable_virtual_path(path)
    host_path = resolve_sandbox_path(normalized).resolve()
    if CONFIG is None:
        raise RuntimeError("Runner config is not initialized.")
    allowed_roots = [
        CONFIG.output_dir.resolve(),
        CONFIG.clean_export_dir.resolve(),
        CONFIG.input_dir.resolve(),
    ]
    if not any(host_path == root or is_relative_to(host_path, root) for root in allowed_roots):
        raise ValueError(f"Resolved path escaped /input or /outputs: {path}")
    return normalized, host_path


def timestamp_iso(path: Path) -> str | None:
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")


def artifact_info(path: str | Path) -> dict[str, Any]:
    host_path = resolve_sandbox_path(path)
    info: dict[str, Any] = {
        "sandbox_path": str(path),
        "host_path": str(host_path),
        "exists": host_path.exists(),
    }
    if host_path.exists():
        info["mtime"] = timestamp_iso(host_path)
        info["is_file"] = host_path.is_file()
        info["is_dir"] = host_path.is_dir()
        if host_path.is_file():
            info["bytes"] = host_path.stat().st_size
    return info


def check_expected_artifacts(paths: list[str | Path]) -> dict[str, Any]:
    artifacts = [artifact_info(path) for path in paths]
    return {
        "artifacts": artifacts,
        "ok": all(item["exists"] for item in artifacts),
        "missing": [item for item in artifacts if not item["exists"]],
    }


def wsl_podman_image_available(image: str, distro: str) -> bool:
    if shutil.which("wsl") is None:
        return False
    return (
        subprocess.run(
            ["wsl", "-d", distro, "--", "sudo", "podman", "image", "exists", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


def native_podman_image_available(image: str, podman_bin: str) -> bool:
    if shutil.which(podman_bin) is None:
        return False
    return (
        subprocess.run(
            [podman_bin, "image", "exists", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


def resolve_host_os(host_os: str) -> str:
    if host_os != "auto":
        return host_os
    return "windows" if os.name == "nt" else "linux"


def configured_image_available() -> bool:
    if CONFIG is None:
        raise RuntimeError("Runner config is not initialized.")
    if CONFIG.sandbox_backend == "controller":
        return True
    if CONFIG.host_os == "windows":
        return wsl_podman_image_available(CONFIG.image, CONFIG.wsl_distro)
    return native_podman_image_available(CONFIG.image, CONFIG.podman_bin)


def create_configured_backend() -> Any:
    if CONFIG is None:
        raise RuntimeError("Runner config is not initialized.")

    if CONFIG.sandbox_backend == "controller":
        if not CONFIG.sandbox_controller_url:
            raise RuntimeError("sandbox_controller_url is required for controller backend")
        CONFIG.workspace_dir.mkdir(parents=True, exist_ok=True)
        return ControllerSandboxBackend(
            image=CONFIG.image,
            controller_url=CONFIG.sandbox_controller_url,
            run_id=CONFIG.run_root.name,
            input_dir=CONFIG.input_dir,
            output_dir=CONFIG.output_dir,
            workspace_dir=CONFIG.workspace_dir,
            token=CONFIG.sandbox_controller_token,
            timeout_seconds=300,
            max_output_bytes=450_000,
        )

    limits = PodmanRunLimits(
        enforce_cgroups=CONFIG.host_os != "windows",
        shell_cpu_seconds=300,
        shell_virtual_memory_kb=4 * 1024 * 1024,
    )

    if CONFIG.host_os == "windows":
        return PodmanSandboxBackend.for_wsl(
            image=CONFIG.image,
            distro=CONFIG.wsl_distro,
            input_dir=CONFIG.input_dir,
            output_dir=CONFIG.output_dir,
            use_sudo=CONFIG.wsl_use_sudo,
            limits=limits,
            max_output_bytes=450_000,
        )

    from podman_sandbox_backend import PodmanSecurityOptions

    return PodmanSandboxBackend(
        image=CONFIG.image,
        input_dir=CONFIG.input_dir,
        output_dir=CONFIG.output_dir,
        podman=CONFIG.podman_bin,
        host_path_mode="native",
        limits=limits,
        security=PodmanSecurityOptions(
            pull="never",
            selinux_relabel=CONFIG.selinux_relabel,
            userns="keep-id",
            user=None,
        ),
        max_output_bytes=450_000,
    )


def message_role(message: Any) -> str:
    return getattr(message, "type", message.__class__.__name__)


def content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def tool_call_dicts(message: Any) -> list[dict[str, Any]]:
    tool_calls = getattr(message, "tool_calls", None) or []
    normalized = []
    for call in tool_calls:
        if isinstance(call, dict):
            normalized.append({"name": call.get("name", ""), "args": call.get("args", {})})
        else:
            normalized.append(
                {"name": getattr(call, "name", ""), "args": getattr(call, "args", {})}
            )
    return normalized


def trace_messages(messages: list[Any], *, content_limit: int = 2200) -> list[dict[str, Any]]:
    trace = []
    for index, message in enumerate(messages, start=1):
        calls = tool_call_dicts(message)
        trace.append(
            {
                "index": index,
                "role": message_role(message),
                "tool_calls": [call["name"] for call in calls],
                "tool_call_args": calls,
                "content_preview": content_text(getattr(message, "content", ""))[:content_limit],
            }
        )
    return trace


def input_manifest() -> list[dict[str, str]]:
    if CONFIG is None:
        raise RuntimeError("Runner config is not initialized.")
    return [
        {
            "host_path": str(item.host_path),
            "sandbox_path": item.sandbox_path,
            "staged_path": str(item.staged_path),
        }
        for item in CONFIG.input_mappings
    ]


def build_parent_prompt() -> str:
    if CONFIG is None:
        raise RuntimeError("Runner config is not initialized.")
    return (
        "Run this task by delegating to the Deep Agent tool. The Deep Agent must request "
        "parent review through its request_parent_review tool before the parent can close "
        "the task.\n\n"
        "Inputs available in the sandbox:\n"
        + "\n".join(f"- {item.sandbox_path}" for item in CONFIG.input_mappings)
        + "\n\nExpected artifacts:\n"
        + "\n".join(f"- {path}" for path in CONFIG.expected_artifacts)
        + "\n\nOutput-gate allowed review/export extensions:\n"
        + ALLOWED_EXPORT_EXTENSIONS_TEXT
        + "\nDo not ask the Deep Agent to create final or review artifacts with any "
        "other extension. Helper scripts or working files may exist under /outputs, "
        "but they must not be passed as expected_artifacts or request_parent_review artifacts."
        + (
            "\n\nDeep Agent skill sources:\n"
            + "\n".join(f"- {path}" for path in CONFIG.skill_sources)
            if CONFIG.skill_sources
            else ""
        )
        + f"\n\nMaximum Deep Agent attempts allowed: {CONFIG.max_review_rounds}"
        + "\n\nUser task:\n"
        + CONFIG.prompt.strip()
    )


def text_file_preview(path: Path, max_chars: int) -> dict[str, Any]:
    size = path.stat().st_size
    max_bytes = max(max_chars * 4, 4096)
    with path.open("rb") as handle:
        raw = handle.read(max_bytes + 1)
    truncated = len(raw) > max_bytes or size > max_bytes
    text = raw[:max_bytes].decode("utf-8", errors="replace")
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True
    return {
        "kind": "text",
        "bytes": size,
        "truncated": truncated,
        "preview": text,
    }


def csv_file_preview(path: Path, max_rows: int, max_cols: int) -> dict[str, Any]:
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    rows: list[list[str]] = []
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        for index, row in enumerate(reader):
            if index >= max_rows:
                break
            rows.append(row[:max_cols])
    return {
        "kind": "csv",
        "bytes": path.stat().st_size,
        "delimiter": delimiter,
        "preview_rows": rows,
        "preview_row_count": len(rows),
        "max_cols_returned": max_cols,
    }


def json_file_preview(path: Path, max_chars: int) -> dict[str, Any]:
    preview = text_file_preview(path, max_chars=max_chars)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        preview["json_parse_error"] = f"{exc.__class__.__name__}: {str(exc)[:500]}"
        return preview

    if isinstance(parsed, dict):
        preview["json_type"] = "object"
        preview["top_level_keys"] = list(parsed.keys())[:50]
    elif isinstance(parsed, list):
        preview["json_type"] = "array"
        preview["array_length"] = len(parsed)
        preview["first_item_type"] = type(parsed[0]).__name__ if parsed else None
    else:
        preview["json_type"] = type(parsed).__name__
    return preview


def image_file_preview(path: Path) -> dict[str, Any]:
    mime = image_mime_type(path)
    info: dict[str, Any] = {
        "kind": "image",
        "bytes": path.stat().st_size,
        "mime": mime,
        "semantic_read_supported": mime.startswith("image/"),
        "semantic_read_tool": "read_sandbox_file(question=...) or inspect_sandbox_image",
    }
    try:
        from PIL import Image
    except Exception as exc:  # pragma: no cover - depends on local environment
        info["metadata_error"] = f"pillow_unavailable: {exc.__class__.__name__}: {exc}"
        return info

    try:
        with Image.open(path) as image:
            info.update(
                {
                    "format": image.format,
                    "width": image.width,
                    "height": image.height,
                    "mode": image.mode,
                    "frames": getattr(image, "n_frames", 1),
                    "has_alpha": image.mode in {"RGBA", "LA"}
                    or ("transparency" in image.info),
                }
            )
    except Exception as exc:
        info["metadata_error"] = f"{exc.__class__.__name__}: {exc}"
    return info


def pdf_file_preview(path: Path, max_chars: int, max_pages: int) -> dict[str, Any]:
    info: dict[str, Any] = {
        "kind": "pdf",
        "bytes": path.stat().st_size,
        "text_extraction": "pypdf",
    }
    try:
        from pypdf import PdfReader
    except Exception as exc:  # pragma: no cover - depends on local environment
        info["error"] = f"pypdf_unavailable: {exc.__class__.__name__}: {exc}"
        return info

    try:
        reader = PdfReader(str(path))
        info["page_count"] = len(reader.pages)
        if reader.metadata:
            info["metadata"] = {
                str(key).lstrip("/"): str(value)
                for key, value in reader.metadata.items()
                if value is not None
            }

        remaining_chars = max_chars
        page_previews: list[dict[str, Any]] = []
        pages_to_scan = min(max_pages, len(reader.pages))
        for page_index in range(pages_to_scan):
            if remaining_chars <= 0:
                break
            try:
                text = reader.pages[page_index].extract_text() or ""
            except Exception as exc:
                page_previews.append(
                    {
                        "page": page_index + 1,
                        "error": f"{exc.__class__.__name__}: {exc}",
                    }
                )
                continue
            if len(text) > remaining_chars:
                text = text[:remaining_chars]
            remaining_chars -= len(text)
            page_previews.append(
                {
                    "page": page_index + 1,
                    "chars_returned": len(text),
                    "text_preview": text,
                }
            )

        info.update(
            {
                "pages_scanned": pages_to_scan,
                "preview_pages": page_previews,
                "truncated": len(reader.pages) > pages_to_scan or remaining_chars <= 0,
            }
        )
        if not any(page.get("text_preview") for page in page_previews):
            info["note"] = (
                "No extractable text was found in the scanned preview pages. "
                "For scanned PDFs, render pages to images inside the sandbox and "
                "call read_sandbox_file with a visual question on those images."
            )
    except Exception as exc:
        info["error"] = f"{exc.__class__.__name__}: {exc}"
    return info


def media_file_preview(path: Path) -> dict[str, Any]:
    mime, _ = mimetypes.guess_type(path.name)
    kind = "binary_or_unsupported"
    if mime:
        if mime.startswith("audio/"):
            kind = "audio"
        elif mime.startswith("video/"):
            kind = "video"
        elif mime.startswith("application/"):
            kind = "application_binary"
    return {
        "kind": kind,
        "bytes": path.stat().st_size,
        "mime": mime or "application/octet-stream",
        "semantic_read_supported": False,
        "note": (
            "This runner reports metadata for this file type. If semantic reading is "
            "needed, create a task-specific extractor inside the sandbox and save a "
            "text/JSON artifact for review."
        ),
    }


def xlsx_file_preview(path: Path, max_rows: int, max_cols: int) -> dict[str, Any]:
    try:
        import openpyxl
    except Exception as exc:  # pragma: no cover - depends on local environment
        return {
            "kind": "xlsx",
            "bytes": path.stat().st_size,
            "error": f"openpyxl_unavailable: {exc.__class__.__name__}: {exc}",
        }

    workbook_values = openpyxl.load_workbook(path, data_only=True, read_only=True)
    workbook_formulas = openpyxl.load_workbook(path, data_only=False, read_only=True)
    sheets: list[dict[str, Any]] = []
    error_strings: list[dict[str, str]] = []
    scan_cell_limit = 200_000

    try:
        for sheet_name in workbook_values.sheetnames:
            ws_values = workbook_values[sheet_name]
            ws_formulas = workbook_formulas[sheet_name]
            preview_rows: list[list[Any]] = []
            nonempty_cells = 0
            scanned_cells = 0
            scan_truncated = False

            for row_index, row in enumerate(ws_values.iter_rows(values_only=True), start=1):
                row_values = list(row[:max_cols])
                if row_index <= max_rows:
                    preview_rows.append(row_values)
                for value in row:
                    scanned_cells += 1
                    if value not in (None, ""):
                        nonempty_cells += 1
                    if scanned_cells >= scan_cell_limit:
                        scan_truncated = True
                        break
                if scan_truncated:
                    break

            error_scan_count = 0
            for row in ws_formulas.iter_rows():
                for cell in row:
                    error_scan_count += 1
                    value = cell.value
                    if isinstance(value, str) and value.startswith("#"):
                        error_strings.append(
                            {"sheet": sheet_name, "cell": cell.coordinate, "value": value}
                        )
                        if len(error_strings) >= 100:
                            break
                    if error_scan_count >= scan_cell_limit:
                        break
                if len(error_strings) >= 100 or error_scan_count >= scan_cell_limit:
                    break

            sheets.append(
                {
                    "name": sheet_name,
                    "max_row": ws_values.max_row,
                    "max_column": ws_values.max_column,
                    "nonempty_cells_scanned": nonempty_cells,
                    "scan_truncated": scan_truncated,
                    "preview_rows": preview_rows,
                }
            )
    finally:
        workbook_values.close()
        workbook_formulas.close()

    return {
        "kind": "xlsx",
        "bytes": path.stat().st_size,
        "sheet_count": len(sheets),
        "sheets": sheets,
        "excel_error_strings": error_strings,
        "excel_error_count_returned": len(error_strings),
    }


def inspect_file_by_type(
    path: Path,
    max_chars: int,
    max_rows: int,
    max_cols: int,
    max_pages: int = 5,
) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False}
    info: dict[str, Any] = {
        "exists": True,
        "is_file": path.is_file(),
        "is_dir": path.is_dir(),
        "mtime": timestamp_iso(path),
    }
    if path.is_dir():
        children = sorted(path.iterdir(), key=lambda child: child.name.lower())[:100]
        info["children"] = [
            {
                "name": child.name,
                "is_file": child.is_file(),
                "is_dir": child.is_dir(),
                "bytes": child.stat().st_size if child.is_file() else None,
            }
            for child in children
        ]
        info["child_count_returned"] = len(children)
        return info

    suffix = path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}:
        info.update(image_file_preview(path))
    elif suffix == ".pdf":
        info.update(pdf_file_preview(path, max_chars=max_chars, max_pages=max_pages))
    elif suffix in {".xlsx", ".xlsm"}:
        info.update(xlsx_file_preview(path, max_rows=max_rows, max_cols=max_cols))
    elif suffix in {".csv", ".tsv"}:
        info.update(csv_file_preview(path, max_rows=max_rows, max_cols=max_cols))
    elif suffix == ".json":
        info.update(json_file_preview(path, max_chars=max_chars))
    elif suffix in {
        ".txt",
        ".md",
        ".log",
        ".py",
        ".js",
        ".ts",
        ".html",
        ".css",
        ".xml",
        ".yaml",
        ".yml",
    }:
        info.update(text_file_preview(path, max_chars=max_chars))
    else:
        info.update(media_file_preview(path))
    return info


@tool
def list_sandbox_files(root: str = "/outputs", max_entries: int = 100) -> dict[str, Any]:
    """List files under /input or /outputs for parent review."""
    if CONFIG is None:
        return {"ok": False, "error": "runner_config_missing"}
    try:
        normalized, host_root = resolve_readable_virtual_path(root)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    max_entries = max(1, min(max_entries, 500))
    if not host_root.exists():
        return {"ok": False, "root": normalized, "exists": False, "files": []}

    files: list[dict[str, Any]] = []
    for child in sorted(host_root.rglob("*"), key=lambda item: str(item).lower()):
        if len(files) >= max_entries:
            break
        resolved = child.resolve()
        if not is_relative_to(resolved, host_root.resolve()):
            continue
        rel = child.relative_to(host_root).as_posix()
        virtual_path = normalized.rstrip("/") + "/" + rel
        files.append(
            {
                "path": virtual_path,
                "is_file": child.is_file(),
                "is_dir": child.is_dir(),
                "bytes": child.stat().st_size if child.is_file() else None,
                "mtime": timestamp_iso(child),
            }
        )
    return {
        "ok": True,
        "root": normalized,
        "host_root": str(host_root),
        "entries_returned": len(files),
        "truncated": len(files) >= max_entries,
        "files": files,
    }


@tool
def inspect_sandbox_file(
    path: str,
    max_chars: int = 12000,
    max_rows: int = 20,
    max_cols: int = 12,
    max_pages: int = 5,
) -> dict[str, Any]:
    """Inspect a file under /input or /outputs, including text/csv/json/xlsx/pdf/image previews."""
    if CONFIG is None:
        return {"ok": False, "error": "runner_config_missing"}
    try:
        normalized, host_path = resolve_readable_virtual_path(path)
        max_chars = max(1000, min(max_chars, 50000))
        max_rows = max(1, min(max_rows, 100))
        max_cols = max(1, min(max_cols, 50))
        max_pages = max(1, min(max_pages, 20))
        info = inspect_file_by_type(
            host_path,
            max_chars=max_chars,
            max_rows=max_rows,
            max_cols=max_cols,
            max_pages=max_pages,
        )
        info.update(
            {
                "ok": bool(info.get("exists", True)),
                "path": normalized,
                "host_path": str(host_path),
            }
        )
        return info
    except Exception as exc:
        return {"ok": False, "path": path, "error": f"{exc.__class__.__name__}: {exc}"}


def openai_model_name(model: str) -> str:
    if model.startswith("openai:"):
        return model.split(":", 1)[1]
    return model


def image_mime_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed and guessed.startswith("image/"):
        return guessed
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    return "application/octet-stream"


@tool
def inspect_sandbox_image(
    path: str,
    question: str,
    max_output_tokens: int = 1200,
) -> dict[str, Any]:
    """Inspect an image under /input or /outputs using a vision-capable OpenAI model."""
    if CONFIG is None:
        return {"ok": False, "error": "runner_config_missing"}
    try:
        normalized, host_path = resolve_readable_virtual_path(path)
        if not host_path.is_file():
            return {"ok": False, "path": normalized, "error": "not_a_file"}
        if host_path.stat().st_size > 20 * 1024 * 1024:
            return {
                "ok": False,
                "path": normalized,
                "error": "image_too_large",
                "bytes": host_path.stat().st_size,
            }
        mime = image_mime_type(host_path)
        if not mime.startswith("image/"):
            return {"ok": False, "path": normalized, "error": f"unsupported_mime:{mime}"}

        max_output_tokens = max(200, min(max_output_tokens, 4000))
        data_url = (
            f"data:{mime};base64,"
            + base64.b64encode(host_path.read_bytes()).decode("ascii")
        )
        client = OpenAI()
        response = client.responses.create(
            model=openai_model_name(CONFIG.deep_model),
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Inspect this image carefully and answer the question. "
                                "If the image is ambiguous, say so and provide ranked candidates "
                                "with visual evidence instead of overclaiming.\n\n"
                                f"Question: {question}"
                            ),
                        },
                        {"type": "input_image", "image_url": data_url},
                    ],
                }
            ],
            max_output_tokens=max_output_tokens,
        )
        text = getattr(response, "output_text", "") or ""
        return {
            "ok": True,
            "path": normalized,
            "mime": mime,
            "bytes": host_path.stat().st_size,
            "model": openai_model_name(CONFIG.deep_model),
            "answer": text,
        }
    except Exception as exc:
        return {"ok": False, "path": path, "error": f"{exc.__class__.__name__}: {exc}"}


@tool
def read_sandbox_file(
    path: str,
    question: str = "",
    max_chars: int = 12000,
    max_rows: int = 20,
    max_cols: int = 12,
    max_pages: int = 5,
    max_output_tokens: int = 1200,
) -> dict[str, Any]:
    """
    Read a sandbox file under /input or /outputs with type-aware handling.

    Text, CSV, JSON, Excel, and PDF files return structured previews. Image files
    return metadata, and when `question` is supplied the tool also performs a
    vision read. Audio/video and other binaries return metadata only.
    """
    file_info = inspect_sandbox_file.invoke(
        {
            "path": path,
            "max_chars": max_chars,
            "max_rows": max_rows,
            "max_cols": max_cols,
            "max_pages": max_pages,
        }
    )
    if not file_info.get("ok"):
        return file_info

    kind = file_info.get("kind")
    result: dict[str, Any] = {
        "ok": True,
        "path": file_info.get("path", path),
        "kind": kind,
        "read_mode": "typed_preview",
        "file": file_info,
    }
    if kind == "image" and question.strip():
        vision = inspect_sandbox_image.invoke(
            {
                "path": path,
                "question": question,
                "max_output_tokens": max_output_tokens,
            }
        )
        result["read_mode"] = "image_vision"
        result["vision"] = vision
        result["ok"] = bool(vision.get("ok"))
    elif question.strip():
        result["question_note"] = (
            "The file content was returned as structured preview data. Answer the "
            "question from the preview, or generate a task-specific extractor inside "
            "the sandbox if the preview is insufficient."
        )
    return result


def normalize_export_path(path: str | Path) -> tuple[str, Path]:
    if CONFIG is None:
        raise RuntimeError("Runner config is not initialized.")
    raw = str(path).replace("\\", "/")
    normalized = posixpath.normpath(raw)
    if normalized.startswith("/outputs/"):
        rel = normalized.removeprefix("/outputs/")
    elif normalized.startswith("/exports/"):
        rel = normalized.removeprefix("/exports/")
    else:
        raise ValueError(f"Exported artifact path must be under /outputs or /exports: {path}")
    if rel in {"", "."} or rel == ".." or rel.startswith("../"):
        raise ValueError(f"Invalid exported artifact path: {path}")
    host_path = (CONFIG.clean_export_dir / rel).resolve()
    clean_root = CONFIG.clean_export_dir.resolve()
    if not (host_path == clean_root or is_relative_to(host_path, clean_root)):
        raise ValueError(f"Resolved export path escaped clean export root: {path}")
    return "/exports/" + rel, host_path


def call_controller_gate(artifacts: list[str]) -> dict[str, Any]:
    if CONFIG is None:
        raise RuntimeError("Runner config is not initialized.")
    payload = {
        "artifacts": artifacts,
        "xlsx_dangerous_formula_action": CONFIG.xlsx_dangerous_formula_action,
    }
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if CONFIG.sandbox_controller_token:
        headers["Authorization"] = f"Bearer {CONFIG.sandbox_controller_token}"
    url = (
        CONFIG.sandbox_controller_url.rstrip("/")
        + f"/runs/{CONFIG.run_root.name}/gate"
    )
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"controller gate failed with HTTP {exc.code}: {detail}") from exc
    result = json.loads(body) if body else {}
    manifest = result.get("manifest")
    if not isinstance(manifest, dict):
        raise RuntimeError(f"controller gate returned no manifest: {result}")
    return manifest


@tool
def run_output_gate(export_artifacts: list[str] | None = None) -> dict[str, Any]:
    """Run the deterministic output allowlist gate for declared /outputs artifacts."""
    if CONFIG is None:
        return {"ok": False, "error": "runner_config_missing"}
    if export_artifacts is None or not export_artifacts:
        if not DEEP_REVIEW_REQUESTS:
            return {"ok": False, "error": "no_review_request_to_export"}
        export_artifacts = list(DEEP_REVIEW_REQUESTS[-1].get("artifacts", []))
    normalized: list[str] = []
    errors: list[str] = []
    for artifact in export_artifacts:
        try:
            normalized.append(normalize_expected_artifact(artifact))
        except Exception as exc:
            errors.append(f"{artifact}: {exc}")
    if errors:
        return {"ok": False, "error": "invalid_export_artifacts", "details": errors}
    try:
        if CONFIG.sandbox_backend == "controller" and CONFIG.sandbox_controller_url:
            manifest = call_controller_gate(normalized)
        else:
            manifest = run_output_gate_artifacts(
                raw_root=CONFIG.output_dir,
                clean_root=CONFIG.clean_export_dir,
                quarantine_root=CONFIG.quarantine_dir,
                log_root=CONFIG.gate_log_dir,
                artifacts=normalized,
                run_id=CONFIG.run_root.name,
                xlsx_dangerous_formula_action=CONFIG.xlsx_dangerous_formula_action,
            )
    except Exception as exc:
        return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
    latest_manifest = CONFIG.runner_log_dir / "latest_gate_manifest.json"
    latest_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "ok": manifest.get("overall_status") == "pass",
        "overall_status": manifest.get("overall_status"),
        "manifest_path": "/gate_logs/gate_manifest.json",
        "clean_export_root": str(CONFIG.clean_export_dir),
        "quarantine_root": str(CONFIG.quarantine_dir),
        "artifacts": manifest.get("artifacts", []),
    }


@tool
def inspect_gate_manifest() -> dict[str, Any]:
    """Read the latest output-gate manifest."""
    if CONFIG is None:
        return {"ok": False, "error": "runner_config_missing"}
    path = CONFIG.gate_log_dir / "gate_manifest.json"
    if not path.exists():
        return {"ok": False, "exists": False, "path": str(path)}
    try:
        return {"ok": True, "exists": True, "manifest": json.loads(path.read_text(encoding="utf-8"))}
    except Exception as exc:
        return {"ok": False, "exists": True, "error": f"{exc.__class__.__name__}: {exc}"}


@tool
def list_exported_files(root: str = "/exports", max_entries: int = 100) -> dict[str, Any]:
    """List files under the clean export area after output-gate processing."""
    if CONFIG is None:
        return {"ok": False, "error": "runner_config_missing"}
    try:
        normalized, host_root = normalize_export_path(root) if root != "/exports" else ("/exports", CONFIG.clean_export_dir)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    max_entries = max(1, min(max_entries, 500))
    if not host_root.exists():
        return {"ok": False, "root": normalized, "exists": False, "files": []}
    files: list[dict[str, Any]] = []
    clean_root = host_root.resolve()
    for child in sorted(host_root.rglob("*"), key=lambda item: str(item).lower()):
        if len(files) >= max_entries:
            break
        resolved = child.resolve()
        if not is_relative_to(resolved, clean_root):
            continue
        rel = child.relative_to(host_root).as_posix()
        files.append(
            {
                "path": normalized.rstrip("/") + "/" + rel,
                "is_file": child.is_file(),
                "is_dir": child.is_dir(),
                "bytes": child.stat().st_size if child.is_file() else None,
                "mtime": timestamp_iso(child),
            }
        )
    return {
        "ok": True,
        "root": normalized,
        "entries_returned": len(files),
        "truncated": len(files) >= max_entries,
        "files": files,
    }


@tool
def read_exported_file(
    path: str,
    max_chars: int = 12000,
    max_rows: int = 20,
    max_cols: int = 12,
    max_pages: int = 5,
) -> dict[str, Any]:
    """Inspect a clean exported file after output-gate processing."""
    if CONFIG is None:
        return {"ok": False, "error": "runner_config_missing"}
    try:
        normalized, host_path = normalize_export_path(path)
        max_chars = max(1000, min(max_chars, 50000))
        max_rows = max(1, min(max_rows, 100))
        max_cols = max(1, min(max_cols, 50))
        max_pages = max(1, min(max_pages, 20))
        info = inspect_file_by_type(
            host_path,
            max_chars=max_chars,
            max_rows=max_rows,
            max_cols=max_cols,
            max_pages=max_pages,
        )
        info.update({"ok": bool(info.get("exists", True)), "path": normalized, "host_path": str(host_path)})
        return info
    except Exception as exc:
        return {"ok": False, "path": path, "error": f"{exc.__class__.__name__}: {exc}"}


@tool
def inspect_exported_artifacts(max_rows: int = 20, max_cols: int = 12) -> dict[str, Any]:
    """Inspect configured expected artifacts from the clean export area."""
    if CONFIG is None:
        return {"ok": False, "error": "runner_config_missing"}
    inspections = [
        read_exported_file.invoke(
            {"path": path, "max_rows": max_rows, "max_cols": max_cols}
        )
        for path in CONFIG.expected_artifacts
    ]
    return {
        "ok": all(item.get("ok") for item in inspections),
        "inspections": inspections,
    }


@tool
def list_quarantine_metadata() -> dict[str, Any]:
    """List quarantined raw artifacts and output-gate findings."""
    if CONFIG is None:
        return {"ok": False, "error": "runner_config_missing"}
    manifest_path = CONFIG.gate_log_dir / "gate_manifest.json"
    manifest = None
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    quarantined = []
    for path in sorted(CONFIG.quarantine_dir.rglob("*"), key=lambda item: str(item).lower()):
        if path.is_file():
            quarantined.append(
                {
                    "path": str(path),
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path) if path.exists() else None,
                }
            )
    return {
        "ok": True,
        "quarantine_root": str(CONFIG.quarantine_dir),
        "quarantined_files": quarantined,
        "manifest_rejections": [
            item
            for item in (manifest or {}).get("artifacts", [])
            if item.get("status") == "rejected"
        ],
    }


@tool
def inspect_expected_artifacts(max_rows: int = 20, max_cols: int = 12) -> dict[str, Any]:
    """Inspect every configured expected artifact for parent review."""
    if CONFIG is None:
        return {"ok": False, "error": "runner_config_missing"}
    inspections = [
        read_sandbox_file.invoke(
            {"path": path, "max_rows": max_rows, "max_cols": max_cols}
        )
        for path in CONFIG.expected_artifacts
    ]
    artifact_check = check_expected_artifacts(CONFIG.expected_artifacts)
    return {
        "ok": artifact_check["ok"] and all(item.get("ok") for item in inspections),
        "artifact_check": artifact_check,
        "inspections": inspections,
    }


@tool
def inspect_self_check_artifacts(max_chars: int = 16000) -> dict[str, Any]:
    """Inspect Deep Agent-generated self-check plan, script candidates, and report."""
    if CONFIG is None:
        return {"ok": False, "error": "runner_config_missing"}

    output_dir = CONFIG.output_dir
    script_candidates = sorted(
        [
            path
            for path in output_dir.glob("self_check.*")
            if path.name not in {"self_check_plan.md", "self_check_report.md"}
            and path.is_file()
        ],
        key=lambda path: path.name.lower(),
    )
    paths = [
        "/outputs/self_check_plan.md",
        *[
            "/outputs/" + path.relative_to(output_dir).as_posix()
            for path in script_candidates[:10]
        ],
        "/outputs/self_check_report.md",
    ]
    inspections = [
        read_sandbox_file.invoke(
            {"path": path, "max_chars": max_chars, "max_rows": 30, "max_cols": 20}
        )
        for path in paths
    ]
    plan_exists = (output_dir / "self_check_plan.md").exists()
    report_exists = (output_dir / "self_check_report.md").exists()
    return {
        "ok": plan_exists and report_exists and bool(script_candidates),
        "plan_exists": plan_exists,
        "report_exists": report_exists,
        "script_candidates": [path.name for path in script_candidates],
        "inspections": inspections,
    }


@tool
def request_parent_review(
    artifacts: list[str],
    summary: str,
    known_issues: str = "",
) -> dict[str, Any]:
    """Ask the parent agent to review output-gate allowed artifacts before final closure."""
    if CONFIG is None:
        return {"ok": False, "error": "runner_config_missing"}
    normalized_artifacts: list[str] = []
    errors: list[str] = []
    for artifact in artifacts:
        try:
            normalized_artifacts.append(normalize_expected_artifact(artifact))
        except Exception as exc:
            errors.append(f"{artifact}: {exc}")
    if errors:
        return {
            "ok": False,
            "review_requested": False,
            "error": "invalid_review_artifacts",
            "details": errors,
            "allowed_extensions": sorted(ALLOWED_EXTENSIONS),
            "message": (
                "Review artifacts must be under /outputs and use only output-gate "
                "allowed extensions. Do not include executable self-check scripts, "
                "images, PDFs, or other helper files in request_parent_review."
            ),
        }
    request = {
        "attempt": len(DEEP_AGENT_EVALUATIONS) + 1,
        "artifacts": normalized_artifacts,
        "summary": summary,
        "known_issues": known_issues,
        "recorded_at": datetime.now().isoformat(timespec="seconds"),
    }
    DEEP_REVIEW_REQUESTS.append(request)
    return {
        "ok": True,
        "review_requested": True,
        "message": (
            "Parent review request recorded. Stop further work for this attempt and "
            "return a concise message listing the artifacts awaiting review."
        ),
        "request": request,
    }


@tool
def run_deep_agent_task(task: str, expected_artifacts: list[str]) -> dict[str, Any]:
    """Run one sandboxed Deep Agent attempt for allowed /outputs artifacts.

    expected_artifacts must be files under /outputs using only output-gate
    allowed extensions: .csv, .html, .json, .md, .xlsx, .yaml, .yml. For the
    generic runner, pass the configured final artifacts exactly. For multi-step
    runs, intermediate expected artifacts may be under /outputs/subtasks/. Do
    not request .py, .js, .png, .pdf, .docx, .pptx, .xlsm, or directory
    artifacts as review/export artifacts.
    """
    if CONFIG is None:
        return {"ok": False, "error": "runner_config_missing"}
    try:
        effective_expected_artifacts = normalize_tool_expected_artifacts(expected_artifacts)
    except Exception as exc:
        return {
            "ok": False,
            "error": "invalid_expected_artifacts",
            "message": str(exc),
            "allowed_extensions": sorted(ALLOWED_EXTENSIONS),
        }

    attempt = len(DEEP_AGENT_EVALUATIONS) + 1
    review_start = len(DEEP_REVIEW_REQUESTS)
    if attempt > CONFIG.max_review_rounds:
        return {
            "ok": False,
            "error": "max_review_rounds_exceeded",
            "attempt": attempt,
            "max_review_rounds": CONFIG.max_review_rounds,
        }

    log_dir = CONFIG.runner_log_dir
    deep_prompt_path = log_dir / f"deep_agent_prompt_round_{attempt}.txt"
    deep_trace_path = log_dir / f"deep_agent_trace_round_{attempt}.json"
    cleanup_path = log_dir / f"cleanup_report_round_{attempt}.json"
    evaluation_path = log_dir / f"parent_tool_evaluation_round_{attempt}.json"
    latest_deep_prompt_path = log_dir / "deep_agent_prompt.txt"
    latest_deep_trace_path = log_dir / "deep_agent_trace.json"
    latest_cleanup_path = log_dir / "cleanup_report.json"
    latest_evaluation_path = log_dir / "parent_tool_evaluation.json"

    task_with_contract = (
        "Expected review/export artifacts for this invocation:\n"
        + "\n".join(f"- {path}" for path in effective_expected_artifacts)
        + "\n\nAllowed review/export extensions: "
        + ALLOWED_EXPORT_EXTENSIONS_TEXT
        + "\nDo not pass helper scripts, images, PDFs, or other non-allowed files "
        "to request_parent_review. If structured data is needed as a reviewed artifact, "
        "write CSV/XLSX/JSON/YAML or include it in a Markdown report.\n\n"
        "Task instructions:\n"
        + task
    )

    deep_prompt_path.write_text(task_with_contract, encoding="utf-8")
    latest_deep_prompt_path.write_text(task_with_contract, encoding="utf-8")

    if not configured_image_available():
        evaluation = {
            "ok": False,
            "attempt": attempt,
            "error": "sandbox_image_missing",
            "sandbox_backend": CONFIG.sandbox_backend,
            "image": CONFIG.image,
            "host_os": CONFIG.host_os,
        }
        DEEP_AGENT_EVALUATIONS.append(evaluation)
        evaluation_path.write_text(
            json.dumps(evaluation, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        latest_evaluation_path.write_text(
            json.dumps(evaluation, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return evaluation

    backend = create_configured_backend()

    deep_error: dict[str, Any] | None = None
    try:
        deep_agent = create_deep_agent(
            model=CONFIG.deep_model,
            tools=[request_parent_review, read_sandbox_file, inspect_sandbox_image],
            backend=backend,
            skills=CONFIG.skill_sources or None,
            system_prompt=(
                "You are a task execution agent running in an isolated sandbox. "
                "Read inputs only from /input and write final artifacts under /outputs. "
                f"Review/export artifacts may only use these extensions: {ALLOWED_EXPORT_EXTENSIONS_TEXT}. "
                "Do not include helper scripts, JSON files, images, PDFs, or other non-allowed "
                "files in request_parent_review. "
                "Save larger scripts under /outputs before executing them. "
                "Do not use the network from the sandbox. Use installed local libraries when helpful. "
                "Use read_sandbox_file when you need a type-aware read of /input or "
                "/outputs files: it can preview text, CSV, JSON, Excel, PDFs, image "
                "metadata, and can perform a vision read of images when you pass a "
                "question. You may still call inspect_sandbox_image directly for focused "
                "visual review after creating crops, contact sheets, plots, or screenshots. "
                "Cite inspected file paths and any uncertainty in your artifacts. "
                "Before requesting parent review, you must perform an autonomous self-check. "
                "This self-check is task-specific and you must design it yourself; do not "
                "wait for a prebuilt validator. Required self-check artifacts: "
                "`/outputs/self_check_plan.md`, one executable check script such as "
                "`/outputs/self_check.py` or `/outputs/self_check.js`, and "
                "`/outputs/self_check_report.md`. The plan must explain what you will verify "
                "against the user task. The script must inspect the generated artifacts and, "
                "when relevant, execute code, parse files, load workbooks, validate CSV/JSON, "
                "or run smoke tests using available local tools. If a headless browser is "
                "available for HTML tasks, use it; otherwise run the strongest available "
                "syntax/reference checks and state the limitation. Execute the self-check "
                "script with the `execute` tool. If it fails, fix the artifact and rerun the "
                "self-check before requesting review. The report must include command(s) run, "
                "pass/fail status, checked files, limitations, and remaining known issues. "
                "When you believe the expected artifacts are ready for review, you must "
                "call request_parent_review with the final artifact paths, a concise summary of "
                "what you produced, and any known issues. Include `/outputs/self_check_plan.md` "
                "and `/outputs/self_check_report.md` in the review request artifacts list, but "
                "do not include the executable self-check script because code is not an allowed "
                "export format. Call review after creating or updating "
                "the artifacts and completing the self-check, not before. After the review request tool returns, stop work "
                "for this attempt and respond concisely with the paths awaiting review. "
                "If this invocation contains parent correction feedback from a previous "
                "review, fix the issues first, rerun self-check, then call "
                "request_parent_review again."
            ),
        )
        deep_result = deep_agent.invoke(
            {"messages": [{"role": "user", "content": task_with_contract}]},
            config={
                "configurable": {"thread_id": "deep-agent-generic-task"},
                "recursion_limit": CONFIG.deep_recursion_limit,
            },
        )
        messages = deep_result["messages"]
        DEEP_AGENT_TRACE.clear()
        DEEP_AGENT_TRACE.extend(trace_messages(messages, content_limit=2600))
        deep_trace_path.write_text(
            json.dumps(DEEP_AGENT_TRACE, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        latest_deep_trace_path.write_text(
            json.dumps(DEEP_AGENT_TRACE, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        deep_error = {"type": exc.__class__.__name__, "message": str(exc)[:2000]}
        deep_trace_path.write_text(
            json.dumps({"error": deep_error}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        latest_deep_trace_path.write_text(
            json.dumps({"error": deep_error}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    finally:
        workspace_dir = backend.workspace_dir
        backend.cleanup()
        workspace_exists = workspace_dir.exists()
        cleanup_ok = (
            not workspace_exists
            if CONFIG.sandbox_backend == "podman"
            else True
        )
        cleanup_payload = {
            "sandbox_id": backend.id,
            "workspace_dir": str(workspace_dir),
            "workspace_exists_after_cleanup": workspace_exists,
            "cleanup_policy": CONFIG.sandbox_backend,
            "cleanup_ok": cleanup_ok,
        }
        cleanup_path.write_text(
            json.dumps(cleanup_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        latest_cleanup_path.write_text(
            json.dumps(cleanup_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    artifact_check = check_expected_artifacts(effective_expected_artifacts)
    cleanup = json.loads(cleanup_path.read_text(encoding="utf-8"))
    attempt_review_requests = DEEP_REVIEW_REQUESTS[review_start:]
    self_check_scripts = sorted(
        [
            path.name
            for path in CONFIG.output_dir.glob("self_check.*")
            if path.name not in {"self_check_plan.md", "self_check_report.md"}
            and path.is_file()
        ]
    )
    evaluation = {
        "attempt": attempt,
        "max_review_rounds": CONFIG.max_review_rounds,
        "runtime": {
            "host_os": CONFIG.host_os,
            "sandbox_backend": CONFIG.sandbox_backend,
            "image": CONFIG.image,
            "podman_bin": CONFIG.podman_bin,
            "wsl_distro": CONFIG.wsl_distro if CONFIG.host_os == "windows" else None,
            "selinux_relabel": CONFIG.selinux_relabel,
        },
        "expected_artifacts": effective_expected_artifacts,
        "configured_final_artifacts": CONFIG.expected_artifacts,
        "parent_provided_expected_artifacts": expected_artifacts,
        "artifact_check": artifact_check,
        "deep_error": deep_error,
        "cleanup": cleanup,
        "review_requested": bool(attempt_review_requests),
        "review_requests": attempt_review_requests,
        "self_check": {
            "plan_exists": (CONFIG.output_dir / "self_check_plan.md").exists(),
            "report_exists": (CONFIG.output_dir / "self_check_report.md").exists(),
            "script_candidates": self_check_scripts,
        },
        "deep_tool_calls": [
            name for item in DEEP_AGENT_TRACE for name in item.get("tool_calls", [])
        ],
    }
    evaluation["ok"] = (
        artifact_check["ok"]
        and deep_error is None
        and cleanup.get("cleanup_ok") is True
    )
    evaluation_path.write_text(
        json.dumps(evaluation, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    latest_evaluation_path.write_text(
        json.dumps(evaluation, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    DEEP_AGENT_EVALUATIONS.append(evaluation)
    return evaluation


def run_parent_agent() -> dict[str, Any]:
    if CONFIG is None:
        raise RuntimeError("Runner config is not initialized.")

    parent_prompt = build_parent_prompt()
    (CONFIG.runner_log_dir / "parent_prompt.txt").write_text(parent_prompt, encoding="utf-8")
    (CONFIG.runner_log_dir / "input_manifest.json").write_text(
        json.dumps(input_manifest(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    parent_tools = [
        run_deep_agent_task,
        run_output_gate,
        inspect_gate_manifest,
        list_exported_files,
        read_exported_file,
        inspect_exported_artifacts,
        list_quarantine_metadata,
    ]
    if CONFIG.allow_raw_parent_inspection:
        parent_tools.extend(
            [
                list_sandbox_files,
                read_sandbox_file,
                inspect_sandbox_file,
                inspect_sandbox_image,
                inspect_expected_artifacts,
                inspect_self_check_artifacts,
            ]
        )

    parent_agent = create_agent(
        model=CONFIG.parent_model,
        tools=parent_tools,
        system_prompt=(
            "You are a parent HITL reviewer and orchestrator. The Deep Agent does the "
            "implementation and must explicitly request parent review by calling its "
            "request_parent_review tool. Do not edit files yourself and do not perform the "
            "implementation yourself. Required workflow: (1) call run_deep_agent_task, "
            f"passing only output-gate allowed expected artifacts ({ALLOWED_EXPORT_EXTENSIONS_TEXT}), "
            "(2) check whether its result has review_requested=true, (3) if review was "
            "requested, call run_output_gate for the declared review artifacts before "
            "reading any produced files, (4) inspect the gate manifest and only read clean "
            "exports with inspect_exported_artifacts, read_exported_file, or list_exported_files. "
            "Do not read raw /outputs in production mode. If the gate rejects files, use "
            "inspect_gate_manifest and list_quarantine_metadata to cite concrete findings, "
            "then ask the Deep Agent to repair them. (5) If self-check report/plan are missing "
            "from clean exports, the self-check did not execute, gate failures were ignored, "
            "or the inspected clean artifact materially fails the user task, and at least one "
            "Deep Agent attempt remains, "
            "call run_deep_agent_task again with concise correction instructions that include "
            "your findings, (6) run the output gate and inspect again after every review "
            "request, and close only when no material issues remain or no attempts remain. "
            "If the Deep Agent does not request review, treat that as a material protocol "
            "issue; if attempts remain, call run_deep_agent_task again instructing it to "
            "produce/update artifacts, run self-check, and call request_parent_review. "
            f"The maximum Deep Agent attempts is {CONFIG.max_review_rounds}. Never call "
            "run_deep_agent_task after the tool reports max_review_rounds_exceeded. "
            "In the final response, report attempts used, files inspected, material findings, "
            "gate status, self-check status, remaining issues if any, whether the last Deep "
            "Agent attempt requested review, and clean export paths. Keep claims tied to "
            "gate manifest and inspected clean-export evidence."
        ),
    )
    parent_result = parent_agent.invoke(
        {"messages": [{"role": "user", "content": parent_prompt}]},
        config={
            "configurable": {"thread_id": "parent-agent-generic-task"},
            "recursion_limit": CONFIG.parent_recursion_limit,
        },
    )
    messages = parent_result["messages"]
    parent_trace = trace_messages(messages, content_limit=2600)
    (CONFIG.runner_log_dir / "parent_agent_trace.json").write_text(
        json.dumps(parent_trace, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    final_text = content_text(getattr(messages[-1], "content", ""))
    parent_tool_calls = [name for item in parent_trace for name in item.get("tool_calls", [])]
    evaluation_path = CONFIG.runner_log_dir / "parent_tool_evaluation.json"
    evaluation = json.loads(evaluation_path.read_text(encoding="utf-8")) if evaluation_path.exists() else {}
    review_rounds = list(DEEP_AGENT_EVALUATIONS)
    report = [
        "# Generic Parent Agent Run",
        "",
        "## Inputs",
        "",
        "```json",
        json.dumps(input_manifest(), ensure_ascii=False, indent=2),
        "```",
        "",
        "## Expected Artifacts",
        "",
        "```json",
        json.dumps(CONFIG.expected_artifacts, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Parent Tool Calls",
        "",
        "```json",
        json.dumps(parent_tool_calls, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Parent Final Response",
        "",
        final_text,
        "",
        "## Latest Generic Artifact Evaluation",
        "",
        "```json",
        json.dumps(evaluation, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Deep Agent Attempt Evaluations",
        "",
        "```json",
        json.dumps(review_rounds, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    (CONFIG.runner_log_dir / "parent_agent_report.md").write_text(
        "\n".join(report),
        encoding="utf-8",
    )
    return {
        "parent_tool_calls": parent_tool_calls,
        "final_text": final_text,
        "evaluation": evaluation,
        "deep_agent_attempts": review_rounds,
        "output_dir": str(CONFIG.run_root),
        "raw_output_dir": str(CONFIG.output_dir),
        "clean_export_dir": str(CONFIG.clean_export_dir),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generic parent-agent runner: stage inputs, delegate one task to a Deep Agent "
            "through a tool, wait for a Deep Agent review request, inspect produced artifacts, "
            "and optionally ask for bounded repairs."
        )
    )
    prompt_group = parser.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt-file", help="UTF-8 text file containing the task prompt.")
    prompt_group.add_argument("--prompt", help="Task prompt text.")
    parser.add_argument(
        "--input",
        action="append",
        default=[],
        help="Input mapping. Use HOST_PATH=/input/name or just HOST_PATH to use the basename.",
    )
    parser.add_argument(
        "--expected-artifact",
        action="append",
        required=True,
        help="Expected output path under /outputs. Repeat for multiple artifacts.",
    )
    parser.add_argument(
        "--skill-source",
        action="append",
        default=[],
        help=(
            "Skill source directory to expose to Deep Agent. Use HOST_DIR=/input/skills "
            "or just HOST_DIR to stage it at /input/skills. The source should contain "
            "skill-name/SKILL.md directories."
        ),
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Host output directory under this workspace's outputs/ directory.",
    )
    parser.add_argument("--image", default="localhost/python-data-sandbox:latest")
    parser.add_argument(
        "--sandbox-backend",
        choices=["podman", "controller"],
        default=os.getenv("SANDBOX_BACKEND", "podman"),
        help="Sandbox backend. Use controller inside Docker Compose; use podman for direct host runs.",
    )
    parser.add_argument(
        "--sandbox-controller-url",
        default=os.getenv("SANDBOX_CONTROLLER_URL", ""),
        help="sandbox-controller base URL for --sandbox-backend controller.",
    )
    parser.add_argument(
        "--sandbox-controller-token",
        default=os.getenv("SANDBOX_CONTROLLER_TOKEN", ""),
        help="Bearer token for sandbox-controller, if configured.",
    )
    parser.add_argument(
        "--host-os",
        choices=["auto", "windows", "linux"],
        default="auto",
        help="Host OS/runtime mode. windows uses WSL Podman; linux uses native Podman.",
    )
    parser.add_argument("--wsl-distro", default="Ubuntu-22.04")
    parser.add_argument(
        "--wsl-no-sudo",
        action="store_true",
        help="On --host-os windows, call WSL podman without sudo.",
    )
    parser.add_argument(
        "--podman-bin",
        default="podman",
        help="Podman executable for --host-os linux.",
    )
    parser.add_argument(
        "--selinux-relabel",
        action="store_true",
        help="On --host-os linux, add SELinux relabeling to bind mounts.",
    )
    parser.add_argument("--parent-model", default="openai:gpt-5.2")
    parser.add_argument("--deep-model", default="openai:gpt-5.2")
    parser.add_argument("--parent-recursion-limit", type=int, default=12)
    parser.add_argument("--deep-recursion-limit", type=int, default=80)
    parser.add_argument(
        "--max-review-rounds",
        type=int,
        default=2,
        help="Maximum number of times the parent may call the Deep Agent tool.",
    )
    parser.add_argument(
        "--no-clear-output",
        action="store_true",
        help="Do not clear the output directory before running. Not recommended for evaluations.",
    )
    parser.add_argument(
        "--keep-staged-input",
        action="store_true",
        help="Keep the temporary staged input directory under work/ after the run.",
    )
    parser.add_argument(
        "--allow-raw-parent-inspection",
        action="store_true",
        help="Development-only: expose raw /outputs inspection tools to the parent agent.",
    )
    parser.add_argument(
        "--xlsx-dangerous-formula-action",
        choices=["reject", "stringify"],
        default="reject",
        help="Output-gate action for dangerous xlsx formulas. Default rejects for repair.",
    )
    return parser.parse_args()


def main() -> None:
    global CONFIG
    args = parse_args()

    load_dotenv(ROOT / ".env.local", override=False)
    load_env_local(ROOT / ".env.local")
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is missing.")

    prompt = (
        Path(args.prompt_file).read_text(encoding="utf-8")
        if args.prompt_file
        else args.prompt
    )
    output_arg = Path(args.output_dir)
    output_base = (
        Path(os.getenv("RUNS_ROOT", "/srv/sandbox-tool/runs"))
        if args.sandbox_backend == "controller"
        else ROOT
    )
    run_root = require_safe_output_dir(
        output_arg.resolve() if output_arg.is_absolute() else (output_base / output_arg).resolve(),
        sandbox_backend=args.sandbox_backend,
    )
    if not args.no_clear_output:
        clear_directory_contents(run_root)
    run_dirs = prepare_run_directories(run_root)

    input_dir = run_dirs["input_dir"]
    input_mappings = stage_inputs(args.input, input_dir)
    skill_sources = stage_skill_sources(args.skill_source, input_dir)
    expected_artifacts = [
        normalize_expected_artifact(path) for path in args.expected_artifact
    ]
    host_os = resolve_host_os(args.host_os)

    CONFIG = RunnerConfig(
        prompt=prompt,
        run_root=run_dirs["run_root"],
        output_dir=run_dirs["raw_output_dir"],
        clean_export_dir=run_dirs["clean_export_dir"],
        quarantine_dir=run_dirs["quarantine_dir"],
        gate_log_dir=run_dirs["gate_log_dir"],
        runner_log_dir=run_dirs["runner_log_dir"],
        input_dir=input_dir,
        workspace_dir=run_dirs["workspace_dir"],
        input_mappings=input_mappings,
        expected_artifacts=expected_artifacts,
        skill_sources=skill_sources,
        image=args.image,
        wsl_distro=args.wsl_distro,
        parent_model=args.parent_model,
        deep_model=args.deep_model,
        parent_recursion_limit=args.parent_recursion_limit,
        deep_recursion_limit=args.deep_recursion_limit,
        max_review_rounds=max(1, args.max_review_rounds),
        host_os=host_os,
        podman_bin=args.podman_bin,
        wsl_use_sudo=not args.wsl_no_sudo,
        selinux_relabel=args.selinux_relabel,
        clear_output=not args.no_clear_output,
        keep_staged_input=args.keep_staged_input,
        allow_raw_parent_inspection=args.allow_raw_parent_inspection,
        sandbox_backend=args.sandbox_backend,
        sandbox_controller_url=args.sandbox_controller_url,
        sandbox_controller_token=args.sandbox_controller_token,
        xlsx_dangerous_formula_action=args.xlsx_dangerous_formula_action,
    )

    try:
        result = run_parent_agent()
    finally:
        if CONFIG is not None and not CONFIG.keep_staged_input and CONFIG.input_dir.exists():
            shutil.rmtree(CONFIG.input_dir, ignore_errors=True)

    print("Parent tool calls:", result["parent_tool_calls"])
    print("Deep Agent attempts:", len(result["deep_agent_attempts"]))
    print("Evaluation ok:", result["evaluation"].get("ok"))
    print("Output dir:", result["output_dir"])
    print("Final response:")
    print(result["final_text"])


if __name__ == "__main__":
    main()
