from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.tools import tool

import generic_parent_runner as base


ROOT = base.ROOT
WORK_DIR = base.WORK_DIR
OUTPUTS_ROOT = base.OUTPUTS_ROOT

SUBTASK_CALLS: list[dict[str, Any]] = []
DEEP_CALL_LOCK = threading.Lock()


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    slug = slug.strip("._-")
    return slug[:80] or "subtask"


def normalize_artifact_list(paths: list[str]) -> list[str]:
    return [base.normalize_expected_artifact(path) for path in paths]


def archive_self_check_artifacts(subtask_id: str, attempt: int) -> list[str]:
    if base.CONFIG is None:
        return []
    output_dir = base.CONFIG.output_dir
    slug = safe_slug(subtask_id)
    archived: list[str] = []
    candidates = [
        output_dir / "self_check_plan.md",
        output_dir / "self_check.py",
        output_dir / "self_check.js",
        output_dir / "self_check_report.md",
    ]
    for source in candidates:
        if not source.exists() or not source.is_file():
            continue
        target = output_dir / f"{slug}_attempt_{attempt}_{source.name}"
        shutil.copy2(source, target)
        archived.append("/outputs/" + target.relative_to(output_dir).as_posix())
    return archived


def clear_standard_self_check_artifacts() -> None:
    if base.CONFIG is None:
        return
    for name in [
        "self_check_plan.md",
        "self_check.py",
        "self_check.js",
        "self_check_report.md",
    ]:
        path = base.CONFIG.output_dir / name
        if path.exists() and path.is_file():
            path.unlink()


@tool
def run_deep_agent_subtask(
    subtask_id: str,
    objective: str,
    expected_artifacts: list[str],
) -> dict[str, Any]:
    """Delegate one subtask to a Deep Agent with output-gate allowed expected artifacts.

    expected_artifacts must be files under /outputs with one of these extensions:
    .csv, .html, .md, .xlsx. Intermediate artifacts should normally be under
    /outputs/subtasks/. Do not request .json, .py, .js, .png, .pdf, .docx,
    .pptx, .xlsm, or directory artifacts as review/export artifacts.
    """
    if base.CONFIG is None:
        return {"ok": False, "error": "runner_config_missing"}

    normalized_expected = normalize_artifact_list(expected_artifacts)
    subtask_prompt = (
        f"You are working on parent-decomposed subtask `{subtask_id}`.\n\n"
        "Inputs are under /input. Prior subtask outputs, if any, are under /outputs. "
        "Do not use the network. Produce only the requested artifacts for this subtask "
        "unless the objective explicitly asks for final synthesis.\n\n"
        "Subtask objective:\n"
        f"{objective.strip()}\n\n"
        "Subtask expected artifacts:\n"
        + "\n".join(f"- {path}" for path in normalized_expected)
        + "\n\n"
        "After producing the artifacts, run the mandatory autonomous self-check required "
        "by your system prompt, then call request_parent_review."
    )

    # Parent models may issue several tool calls in one turn. The underlying generic
    # runner uses global round files, so serialize calls to keep traces and self-checks
    # attributable to the correct subtask.
    with DEEP_CALL_LOCK:
        attempt_number = len(base.DEEP_AGENT_EVALUATIONS) + 1
        clear_standard_self_check_artifacts()
        result = base.run_deep_agent_task.invoke(
            {"task": subtask_prompt, "expected_artifacts": normalized_expected}
        )
        subtask_check = base.check_expected_artifacts(normalized_expected)
        archived_self_checks = archive_self_check_artifacts(subtask_id, attempt_number)
        call_record = {
            "subtask_id": subtask_id,
            "attempt": attempt_number,
            "objective": objective,
            "expected_artifacts": normalized_expected,
            "subtask_artifact_check": subtask_check,
            "review_requested": result.get("review_requested"),
            "deep_error": result.get("deep_error"),
            "archived_self_checks": archived_self_checks,
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
        }
        SUBTASK_CALLS.append(call_record)

        history_path = base.CONFIG.runner_log_dir / "multi_deep_subtask_history.json"
        history_path.write_text(
            json.dumps(SUBTASK_CALLS, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {
            **result,
            "subtask_id": subtask_id,
            "subtask_expected_artifacts": normalized_expected,
            "subtask_artifact_check": subtask_check,
            "archived_self_checks": archived_self_checks,
        }


@tool
def inspect_subtask_history() -> dict[str, Any]:
    """Return the parent-visible history of Deep Agent subtask calls."""
    return {"ok": True, "subtask_count": len(SUBTASK_CALLS), "subtasks": SUBTASK_CALLS}


def build_multi_parent_prompt(min_subtasks: int) -> str:
    if base.CONFIG is None:
        raise RuntimeError("Runner config is not initialized.")
    return (
        "You are testing whether a parent agent can decompose a vague request, delegate "
        "multiple subtasks to Deep Agents, inspect the results, and produce one integrated "
        "deliverable through a final Deep Agent synthesis call.\n\n"
        "Inputs available in the sandbox:\n"
        + "\n".join(f"- {item.sandbox_path}" for item in base.CONFIG.input_mappings)
        + "\n\nFinal expected artifact(s):\n"
        + "\n".join(f"- {path}" for path in base.CONFIG.expected_artifacts)
        + "\n\nOutput-gate allowed review/export extensions:\n"
        + base.ALLOWED_EXPORT_EXTENSIONS_TEXT
        + "\nDo not ask Deep Agents to create expected/review artifacts with other "
        "extensions. Helper files may exist under /outputs, but reviewed artifacts "
        "must be .md, .csv, .xlsx, or .html."
        + (
            "\n\nDeep Agent skill sources:\n"
            + "\n".join(f"- {path}" for path in base.CONFIG.skill_sources)
            if base.CONFIG.skill_sources
            else ""
        )
        + f"\n\nDeep Agent call budget: {base.CONFIG.max_review_rounds}. "
        + f"You must call run_deep_agent_subtask at least {min_subtasks} times unless the "
        "input is unreadable. At least one call must be a final synthesis subtask that "
        "reads prior /outputs/subtasks artifacts and creates the final expected artifact(s). "
        "For intermediate subtasks, set expected_artifacts to specific paths under "
        "`/outputs/subtasks/` using only output-gate allowed extensions, usually "
        ".md for narrative or .csv/.xlsx for structured data. "
        "After each subtask review request, inspect the subtask artifacts with "
        "read_sandbox_file, inspect_sandbox_file, or list_sandbox_files; use "
        "read_sandbox_file with a question or inspect_sandbox_image when visual "
        "artifacts need semantic review. Do not write artifacts yourself. "
        "If a subtask fails materially and budget remains, create a repair or replacement "
        "subtask. In the final response, list the decomposition, Deep Agent call count, "
        "which artifacts were inspected, whether the final artifact exists, self-check "
        "status, and residual risks. Tie claims to inspected files.\n\n"
        "User's intentionally vague request:\n"
        + base.CONFIG.prompt.strip()
    )


def run_parent_agent(min_subtasks: int) -> dict[str, Any]:
    if base.CONFIG is None:
        raise RuntimeError("Runner config is not initialized.")

    parent_prompt = build_multi_parent_prompt(min_subtasks)
    (base.CONFIG.runner_log_dir / "parent_prompt.txt").write_text(
        parent_prompt,
        encoding="utf-8",
    )
    (base.CONFIG.runner_log_dir / "input_manifest.json").write_text(
        json.dumps(base.input_manifest(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    parent_tools = [
        run_deep_agent_subtask,
        inspect_subtask_history,
        base.run_output_gate,
        base.inspect_gate_manifest,
        base.inspect_exported_artifacts,
        base.read_exported_file,
        base.list_exported_files,
    ]
    if base.CONFIG.allow_raw_parent_inspection:
        parent_tools.extend(
            [
                base.list_sandbox_files,
                base.read_sandbox_file,
                base.inspect_sandbox_file,
                base.inspect_sandbox_image,
                base.inspect_expected_artifacts,
                base.inspect_self_check_artifacts,
            ]
        )

    parent_agent = create_agent(
        model=base.CONFIG.parent_model,
        tools=parent_tools,
        system_prompt=(
            "You are a parent decomposition and review agent. You do not implement "
            "analysis yourself and you do not edit files yourself. Your job is to split "
            "the vague request into meaningful subtasks, call run_deep_agent_subtask for "
            f"output-gate allowed artifacts ({base.ALLOWED_EXPORT_EXTENSIONS_TEXT}) for "
            "each, review subtask status through inspect_subtask_history"
            + (
                " and raw inspection tools when needed"
                if base.CONFIG.allow_raw_parent_inspection
                else ""
            )
            + ", then delegate one final "
            "synthesis subtask that creates the final expected artifact. Intermediate "
            "subtasks are allowed to have their own artifacts under /outputs/subtasks. "
            "After the final synthesis subtask requests review, call run_output_gate and "
            "review the final clean exports with inspect_exported_artifacts/read_exported_file. "
            "Do not treat the final artifact being absent as a failure until after the "
            "synthesis subtask. Do treat missing subtask artifacts, missing review requests, "
            "or weak self-checks as material issues. Never exceed the Deep Agent call budget."
        ),
    )
    parent_error: dict[str, str] | None = None
    messages: list[Any] = []
    try:
        parent_result = parent_agent.invoke(
            {"messages": [{"role": "user", "content": parent_prompt}]},
            config={
                "configurable": {"thread_id": "parent-agent-multi-deep-task"},
                "recursion_limit": base.CONFIG.parent_recursion_limit,
            },
        )
        messages = parent_result["messages"]
    except Exception as exc:
        parent_error = {
            "type": exc.__class__.__name__,
            "message": str(exc)[:3000],
        }

    parent_trace = base.trace_messages(messages, content_limit=3200) if messages else []
    (base.CONFIG.runner_log_dir / "parent_agent_trace.json").write_text(
        json.dumps(parent_trace, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    final_text = (
        base.content_text(getattr(messages[-1], "content", ""))
        if messages
        else "Parent agent stopped before producing a final message."
    )
    parent_tool_calls = [
        name for item in parent_trace for name in item.get("tool_calls", [])
    ]
    final_check = base.check_expected_artifacts(base.CONFIG.expected_artifacts)
    report = [
        "# Multi Deep Parent Agent Run",
        "",
        "## Inputs",
        "",
        "```json",
        json.dumps(base.input_manifest(), ensure_ascii=False, indent=2),
        "```",
        "",
        "## Final Expected Artifacts",
        "",
        "```json",
        json.dumps(base.CONFIG.expected_artifacts, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Parent Tool Calls",
        "",
        "```json",
        json.dumps(parent_tool_calls, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Deep Agent Subtask History",
        "",
        "```json",
        json.dumps(SUBTASK_CALLS, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Final Artifact Check",
        "",
        "```json",
        json.dumps(final_check, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Parent Error",
        "",
        "```json",
        json.dumps(parent_error, ensure_ascii=False, indent=2),
        "```",
        "",
        "## Parent Final Response",
        "",
        final_text,
    ]
    (base.CONFIG.runner_log_dir / "parent_agent_report.md").write_text(
        "\n".join(report),
        encoding="utf-8",
    )
    return {
        "parent_tool_calls": parent_tool_calls,
        "final_text": final_text,
        "deep_agent_attempts": list(base.DEEP_AGENT_EVALUATIONS),
        "subtasks": list(SUBTASK_CALLS),
        "final_check": final_check,
        "parent_error": parent_error,
        "output_dir": str(base.CONFIG.run_root),
        "raw_output_dir": str(base.CONFIG.output_dir),
        "clean_export_dir": str(base.CONFIG.clean_export_dir),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Parent runner that decomposes one vague task into multiple Deep Agent subtasks."
    )
    prompt_group = parser.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt-file", help="UTF-8 text file containing the task prompt.")
    prompt_group.add_argument("--prompt", help="Task prompt text.")
    parser.add_argument("--input", action="append", default=[])
    parser.add_argument("--expected-artifact", action="append", required=True)
    parser.add_argument(
        "--skill-source",
        action="append",
        default=[],
        help="Skill source directory to expose to Deep Agent, e.g. outputs/skills=/input/skills.",
    )
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--image", default="localhost/python-data-sandbox:latest")
    parser.add_argument(
        "--sandbox-backend",
        choices=["podman", "controller"],
        default=os.getenv("SANDBOX_BACKEND", "podman"),
    )
    parser.add_argument(
        "--sandbox-controller-url",
        default=os.getenv("SANDBOX_CONTROLLER_URL", ""),
    )
    parser.add_argument(
        "--sandbox-controller-token",
        default=os.getenv("SANDBOX_CONTROLLER_TOKEN", ""),
    )
    parser.add_argument("--host-os", choices=["auto", "windows", "linux"], default="auto")
    parser.add_argument("--wsl-distro", default="Ubuntu-22.04")
    parser.add_argument("--wsl-no-sudo", action="store_true")
    parser.add_argument("--podman-bin", default="podman")
    parser.add_argument("--selinux-relabel", action="store_true")
    parser.add_argument("--parent-model", default="openai:gpt-5.2")
    parser.add_argument("--deep-model", default="openai:gpt-5.2")
    parser.add_argument("--parent-recursion-limit", type=int, default=28)
    parser.add_argument("--deep-recursion-limit", type=int, default=100)
    parser.add_argument("--max-deep-calls", type=int, default=5)
    parser.add_argument("--min-deep-calls", type=int, default=3)
    parser.add_argument("--no-clear-output", action="store_true")
    parser.add_argument("--keep-staged-input", action="store_true")
    parser.add_argument("--allow-raw-parent-inspection", action="store_true")
    parser.add_argument(
        "--xlsx-dangerous-formula-action",
        choices=["reject", "stringify"],
        default="reject",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    load_dotenv(ROOT / ".env.local", override=False)
    base.load_env_local(ROOT / ".env.local")
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
    run_root = base.require_safe_output_dir(
        output_arg.resolve() if output_arg.is_absolute() else (output_base / output_arg).resolve(),
        sandbox_backend=args.sandbox_backend,
    )
    if not args.no_clear_output:
        base.clear_directory_contents(run_root)
    run_dirs = base.prepare_run_directories(run_root)

    input_dir = run_dirs["input_dir"]
    input_mappings = base.stage_inputs(args.input, input_dir)
    skill_sources = base.stage_skill_sources(args.skill_source, input_dir)
    expected_artifacts = normalize_artifact_list(args.expected_artifact)
    host_os = base.resolve_host_os(args.host_os)

    base.CONFIG = base.RunnerConfig(
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
        max_review_rounds=max(1, args.max_deep_calls),
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
    base.DEEP_AGENT_TRACE.clear()
    base.DEEP_AGENT_EVALUATIONS.clear()
    base.DEEP_REVIEW_REQUESTS.clear()
    SUBTASK_CALLS.clear()

    try:
        result = run_parent_agent(min_subtasks=max(1, args.min_deep_calls))
    finally:
        if (
            base.CONFIG is not None
            and not base.CONFIG.keep_staged_input
            and base.CONFIG.input_dir.exists()
        ):
            shutil.rmtree(base.CONFIG.input_dir, ignore_errors=True)

    print("Parent tool calls:", result["parent_tool_calls"])
    print("Deep Agent calls:", len(result["deep_agent_attempts"]))
    print("Subtasks:", [item["subtask_id"] for item in result["subtasks"]])
    print("Final artifact ok:", result["final_check"].get("ok"))
    print("Parent error:", result.get("parent_error"))
    print("Output dir:", result["output_dir"])
    print("Final response:")
    print(result["final_text"])


if __name__ == "__main__":
    main()
