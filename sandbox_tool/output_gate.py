from __future__ import annotations

import argparse
import csv
import hashlib
import json
import posixpath
import re
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


POLICY_VERSION = "output-gate-v1"
ALLOWED_EXTENSIONS = {".md", ".csv", ".xlsx", ".html"}
MAX_TEXT_BYTES = 10 * 1024 * 1024
MAX_XLSX_BYTES = 100 * 1024 * 1024
MAX_ZIP_ENTRIES = 20_000
MAX_ZIP_UNCOMPRESSED_BYTES = 250 * 1024 * 1024
MAX_ZIP_COMPRESSION_RATIO = 100
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
CSV_ESCAPE_POLICY = "apostrophe-prefix"
BINARY_MAGIC_SIGNATURES = [
    (b"PK\x03\x04", "zip_ooxml"),
    (b"PK\x05\x06", "zip_empty"),
    (b"PK\x07\x08", "zip_spanned"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole_compound_document"),
    (b"%PDF-", "pdf"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"7z\xbc\xaf\x27\x1c", "7zip"),
    (b"Rar!\x1a\x07\x00", "rar"),
    (b"Rar!\x1a\x07\x01\x00", "rar5"),
    (b"\x1f\x8b", "gzip"),
    (b"BZh", "bzip2"),
    (b"SQLite format 3\x00", "sqlite"),
    (b"PAR1", "parquet"),
]
HTML_DOCUMENT_START_RE = re.compile(r"^\s*(?:<!doctype\s+html\b|<html\b)", re.IGNORECASE)
HTML_DOCUMENT_MARKER_RE = re.compile(
    r"<\s*(?:!doctype\s+html\b|html\b|head\b|body\b)",
    re.IGNORECASE,
)
NUMERIC_LITERAL_RE = re.compile(
    r"^[+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))(?:[eE][+-]?\d+)?$"
)
HTML_ACTIVE_PATTERNS = [
    (re.compile(r"<\s*script\b", re.IGNORECASE), "script_tag"),
    (re.compile(r"<\s*iframe\b", re.IGNORECASE), "iframe_tag"),
    (re.compile(r"<\s*object\b", re.IGNORECASE), "object_tag"),
    (re.compile(r"<\s*embed\b", re.IGNORECASE), "embed_tag"),
    (re.compile(r"<\s*base\b", re.IGNORECASE), "base_tag"),
    (re.compile(r"<\s*svg\b", re.IGNORECASE), "inline_svg"),
    (re.compile(r"<\s*foreignObject\b", re.IGNORECASE), "svg_foreign_object"),
    (re.compile(r"\son[a-z0-9_-]+\s*=", re.IGNORECASE), "event_handler"),
    (re.compile(r"javascript\s*:", re.IGNORECASE), "javascript_url"),
    (re.compile(r"data\s*:\s*image/svg\+xml", re.IGNORECASE), "svg_data_uri"),
    (
        re.compile(
            r"\b(?:src|href|srcset)\s*=\s*['\"]?\s*(?:https?:|//|ftp:|mailto:)",
            re.IGNORECASE,
        ),
        "external_resource_or_link",
    ),
    (
        re.compile(
            r"<\s*meta\b[^>]*http-equiv\s*=\s*['\"]?refresh",
            re.IGNORECASE,
        ),
        "meta_refresh",
    ),
]
MARKDOWN_ACTIVE_PATTERNS = HTML_ACTIVE_PATTERNS
HTML_CSP = (
    "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
    "font-src 'self' data:; base-uri 'none'; form-action 'none'"
)
DANGEROUS_XLSX_FUNCTIONS = {
    "WEBSERVICE",
    "FILTERXML",
    "RTD",
    "CALL",
    "REGISTER.ID",
    "IMAGE",
}
XLSX_DANGEROUS_PART_PATTERNS = [
    "vbaProject.bin",
    "/externalLinks/",
    "/connections.xml",
    "/oleObjects/",
    "/activeX/",
    "/embeddings/",
]


@dataclass
class GatePaths:
    raw_root: Path
    clean_root: Path
    quarantine_root: Path
    log_root: Path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_output_artifact(path: str) -> tuple[str, str]:
    raw = path.replace("\\", "/")
    normalized = posixpath.normpath(raw)
    if not normalized.startswith("/outputs/"):
        raise ValueError(f"artifact must be under /outputs: {path}")
    rel = normalized.removeprefix("/outputs/")
    if rel in {"", "."} or rel == ".." or rel.startswith("../"):
        raise ValueError(f"invalid artifact path: {path}")
    return normalized, rel


def safe_join(root: Path, rel: str) -> Path:
    target = (root / rel).resolve()
    root_resolved = root.resolve()
    try:
        target.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"path escaped root: {rel}") from exc
    return target


def finding(code: str, message: str, *, severity: str = "high", **details: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"code": code, "severity": severity, "message": message}
    if details:
        payload["details"] = details
    return payload


def reject_result(
    *,
    requested_path: str,
    rel: str,
    raw_path: Path,
    quarantine_path: Path,
    kind: str | None,
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    quarantine_path.parent.mkdir(parents=True, exist_ok=True)
    if raw_path.exists() and raw_path.is_file():
        shutil.copy2(raw_path, quarantine_path)
    return {
        "requested_path": requested_path,
        "raw_path": str(raw_path),
        "clean_path": None,
        "quarantine_path": str(quarantine_path),
        "status": "rejected",
        "kind": kind,
        "raw_sha256": sha256_file(raw_path) if raw_path.exists() and raw_path.is_file() else None,
        "clean_sha256": None,
        "bytes_raw": raw_path.stat().st_size if raw_path.exists() and raw_path.is_file() else None,
        "bytes_clean": None,
        "findings": findings,
        "actions": ["quarantined"],
    }


def pass_result(
    *,
    requested_path: str,
    raw_path: Path,
    clean_path: Path,
    kind: str,
    findings: list[dict[str, Any]] | None = None,
    actions: list[str] | None = None,
) -> dict[str, Any]:
    raw_hash = sha256_file(raw_path)
    clean_hash = sha256_file(clean_path)
    status = "sanitized" if actions else "pass"
    return {
        "requested_path": requested_path,
        "raw_path": str(raw_path),
        "clean_path": str(clean_path),
        "quarantine_path": None,
        "status": status,
        "kind": kind,
        "raw_sha256": raw_hash,
        "clean_sha256": clean_hash,
        "bytes_raw": raw_path.stat().st_size,
        "bytes_clean": clean_path.stat().st_size,
        "findings": findings or [],
        "actions": actions or [],
    }


def read_text_strict(path: Path) -> str:
    if path.stat().st_size > MAX_TEXT_BYTES:
        raise ValueError(f"text file exceeds size limit: {path.stat().st_size}")
    raw = path.read_bytes()
    if b"\x00" in raw:
        raise ValueError("NUL byte detected in text file")
    return raw.decode("utf-8-sig")


def known_binary_magic(path: Path) -> str | None:
    with path.open("rb") as handle:
        prefix = handle.read(32)
    for signature, name in BINARY_MAGIC_SIGNATURES:
        if prefix.startswith(signature):
            return name
    return None


def format_declaration_findings(text: str, *, expected_kind: str) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if expected_kind in {"md", "csv"} and HTML_DOCUMENT_START_RE.match(text):
        findings.append(
            finding(
                "declared_format_mismatch",
                f"{expected_kind} artifact appears to be an HTML document",
                expected_kind=expected_kind,
                detected_kind="html",
            )
        )
    if expected_kind == "html" and not HTML_DOCUMENT_MARKER_RE.search(text):
        findings.append(
            finding(
                "html_document_marker_missing",
                "HTML artifact must contain a document marker such as <!doctype html>, <html>, <head>, or <body>",
                expected_kind=expected_kind,
            )
        )
    return findings


def read_text_artifact(path: Path, *, expected_kind: str) -> str:
    magic = known_binary_magic(path)
    if magic:
        raise GateReject(
            [
                finding(
                    "magic_extension_mismatch",
                    f"{expected_kind} artifact starts with {magic} magic bytes",
                    expected_kind=expected_kind,
                    detected_magic=magic,
                )
            ]
        )
    try:
        text = read_text_strict(path)
    except UnicodeDecodeError as exc:
        raise GateReject(
            [
                finding(
                    "text_decode_error",
                    f"{expected_kind} artifact is not valid UTF-8 text: {exc}",
                    expected_kind=expected_kind,
                )
            ]
        ) from exc
    except ValueError as exc:
        raise GateReject(
            [
                finding(
                    "text_artifact_invalid",
                    f"{expected_kind} artifact failed text validation: {exc}",
                    expected_kind=expected_kind,
                )
            ]
        ) from exc

    findings = format_declaration_findings(text, expected_kind=expected_kind)
    if findings:
        raise GateReject(findings)
    return text


def csv_cell_needs_formula_escape(value: str) -> bool:
    if not value:
        return False
    stripped = value.lstrip(" ")
    if not stripped:
        return False
    if stripped.startswith(("\t", "\r")):
        return True
    if stripped.startswith(("+", "-")) and NUMERIC_LITERAL_RE.fullmatch(stripped):
        return False
    return stripped.startswith(CSV_FORMULA_PREFIXES)


def active_content_findings(text: str, patterns: list[tuple[re.Pattern[str], str]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for pattern, code in patterns:
        match = pattern.search(text)
        if match:
            line = text.count("\n", 0, match.start()) + 1
            findings.append(
                finding(
                    code,
                    f"active or external content detected at line {line}",
                    line=line,
                    matched=text[match.start() : match.end()][:120],
                )
            )
    return findings


def sanitize_md(raw_path: Path, clean_path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    text = read_text_artifact(raw_path, expected_kind="md")
    findings = active_content_findings(text, MARKDOWN_ACTIVE_PATTERNS)
    if findings:
        raise GateReject(findings)
    clean_path.parent.mkdir(parents=True, exist_ok=True)
    clean_path.write_text(text, encoding="utf-8", newline="\n")
    return [], []


def sanitize_html(raw_path: Path, clean_path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    text = read_text_artifact(raw_path, expected_kind="html")
    findings = active_content_findings(text, HTML_ACTIVE_PATTERNS)
    if findings:
        raise GateReject(findings)
    csp_meta = f'<meta http-equiv="Content-Security-Policy" content="{HTML_CSP}">'
    if "Content-Security-Policy" not in text:
        if re.search(r"<\s*head\b[^>]*>", text, flags=re.IGNORECASE):
            text = re.sub(
                r"(<\s*head\b[^>]*>)",
                r"\1\n" + csp_meta,
                text,
                count=1,
                flags=re.IGNORECASE,
            )
        else:
            text = csp_meta + "\n" + text
        actions = ["injected_csp"]
    else:
        actions = []
    clean_path.parent.mkdir(parents=True, exist_ok=True)
    clean_path.write_text(text, encoding="utf-8", newline="\n")
    return [], actions


def sanitize_csv(raw_path: Path, clean_path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    text = read_text_artifact(raw_path, expected_kind="csv")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(text.splitlines(), dialect=dialect))
    changed_cells: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows, start=1):
        for col_index, value in enumerate(row, start=1):
            if csv_cell_needs_formula_escape(value):
                row[col_index - 1] = "'" + value
                changed_cells.append(
                    {
                        "row": row_index,
                        "column": col_index,
                        "original_prefix": value.lstrip(" ")[:1],
                        "policy": CSV_ESCAPE_POLICY,
                    }
                )
    clean_path.parent.mkdir(parents=True, exist_ok=True)
    with clean_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, dialect=dialect, lineterminator="\n")
        writer.writerows(rows)
    findings = []
    actions = []
    if changed_cells:
        findings.append(
            finding(
                "csv_formula_injection_cells",
                f"escaped {len(changed_cells)} formula-like CSV cells",
                severity="medium",
                cells=changed_cells[:100],
                truncated=len(changed_cells) > 100,
                policy=CSV_ESCAPE_POLICY,
            )
        )
        actions.append("escaped_csv_formula_cells")
    return findings, actions


class GateReject(Exception):
    def __init__(self, findings: list[dict[str, Any]]):
        super().__init__("gate rejected artifact")
        self.findings = findings


def check_zip_safety(path: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            findings.append(
                finding(
                    "zip_entry_count_limit",
                    f"zip entry count exceeds limit: {len(infos)}",
                    entries=len(infos),
                    limit=MAX_ZIP_ENTRIES,
                )
            )
        total_uncompressed = 0
        for info in infos:
            name = info.filename.replace("\\", "/")
            normalized = posixpath.normpath(name)
            if normalized.startswith("../") or normalized.startswith("/"):
                findings.append(
                    finding("zip_path_traversal", "zip entry path escapes archive root", entry=name)
                )
            total_uncompressed += info.file_size
            if info.compress_size > 0 and info.file_size / info.compress_size > MAX_ZIP_COMPRESSION_RATIO:
                findings.append(
                    finding(
                        "zip_compression_ratio_limit",
                        "zip entry compression ratio exceeds limit",
                        entry=name,
                        ratio=round(info.file_size / info.compress_size, 2),
                        limit=MAX_ZIP_COMPRESSION_RATIO,
                    )
                )
        if total_uncompressed > MAX_ZIP_UNCOMPRESSED_BYTES:
            findings.append(
                finding(
                    "zip_uncompressed_size_limit",
                    "zip uncompressed size exceeds limit",
                    total_uncompressed=total_uncompressed,
                    limit=MAX_ZIP_UNCOMPRESSED_BYTES,
                )
            )
    return findings


def xlsx_part_findings(path: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    with zipfile.ZipFile(path) as archive:
        names = ["/" + item.filename.replace("\\", "/") for item in archive.infolist()]
    required = {"/[Content_Types].xml", "/xl/workbook.xml"}
    for required_name in required:
        if required_name not in names:
            findings.append(
                finding("xlsx_required_part_missing", "required OOXML part missing", part=required_name)
            )
    for name in names:
        for pattern in XLSX_DANGEROUS_PART_PATTERNS:
            if pattern in name:
                findings.append(
                    finding(
                        "xlsx_dangerous_part",
                        "dangerous or active OOXML part detected",
                        part=name,
                        pattern=pattern,
                    )
                )
    return findings


def formula_findings(formula: str) -> list[dict[str, Any]]:
    upper = formula.upper()
    findings: list[dict[str, Any]] = []
    for function_name in DANGEROUS_XLSX_FUNCTIONS:
        if re.search(rf"(^|[^A-Z0-9_.]){re.escape(function_name)}\s*\(", upper):
            findings.append(
                finding(
                    "xlsx_dangerous_formula_function",
                    "dangerous Excel formula function detected",
                    function=function_name,
                    formula=formula[:500],
                )
            )
    if re.search(r"HYPERLINK\s*\(\s*['\"]\s*(?:https?:|//|ftp:|mailto:)", upper):
        findings.append(
            finding(
                "xlsx_external_hyperlink_formula",
                "external HYPERLINK formula detected",
                formula=formula[:500],
            )
        )
    if "[" in formula or re.search(r"(https?:|ftp:|mailto:|\\\\)", formula, re.IGNORECASE):
        findings.append(
            finding(
                "xlsx_external_reference_formula",
                "external reference-like formula detected",
                formula=formula[:500],
            )
        )
    return findings


def sanitize_xlsx(
    raw_path: Path,
    clean_path: Path,
    *,
    dangerous_formula_action: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    if raw_path.stat().st_size > MAX_XLSX_BYTES:
        raise GateReject(
            [
                finding(
                    "xlsx_size_limit",
                    "xlsx file exceeds size limit",
                    bytes=raw_path.stat().st_size,
                    limit=MAX_XLSX_BYTES,
                )
            ]
        )
    with raw_path.open("rb") as handle:
        if handle.read(4) != b"PK\x03\x04":
            raise GateReject([finding("magic_extension_mismatch", "xlsx file is not a ZIP/OOXML file")])
    findings = check_zip_safety(raw_path)
    findings.extend(xlsx_part_findings(raw_path))
    if findings:
        raise GateReject(findings)

    try:
        import openpyxl
    except Exception as exc:  # pragma: no cover
        raise GateReject(
            [finding("openpyxl_unavailable", f"openpyxl unavailable: {exc.__class__.__name__}: {exc}")]
        ) from exc

    workbook = openpyxl.load_workbook(raw_path, data_only=False, keep_links=False)
    try:
        formula_cell_findings: list[dict[str, Any]] = []
        stringified_cells: list[dict[str, Any]] = []
        for sheet in workbook.worksheets:
            for row in sheet.iter_rows():
                for cell in row:
                    value = cell.value
                    if isinstance(value, str) and value.startswith("="):
                        cell_findings = formula_findings(value)
                        if cell_findings:
                            for item in cell_findings:
                                item["details"] = {
                                    **item.get("details", {}),
                                    "sheet": sheet.title,
                                    "cell": cell.coordinate,
                                }
                            formula_cell_findings.extend(cell_findings)
                            if dangerous_formula_action == "stringify":
                                cell.value = "'" + value
                                stringified_cells.append(
                                    {"sheet": sheet.title, "cell": cell.coordinate}
                                )

        if formula_cell_findings and dangerous_formula_action == "reject":
            raise GateReject(formula_cell_findings)

        clean_path.parent.mkdir(parents=True, exist_ok=True)
        workbook.save(clean_path)
        actions = []
        findings_out = []
        if stringified_cells:
            actions.append("stringified_dangerous_xlsx_formulas")
            findings_out.append(
                finding(
                    "xlsx_dangerous_formula_stringified",
                    f"stringified {len(stringified_cells)} dangerous formula cells",
                    severity="medium",
                    cells=stringified_cells[:100],
                    truncated=len(stringified_cells) > 100,
                )
            )
        return findings_out, actions
    finally:
        workbook.close()


def process_artifact(
    paths: GatePaths,
    requested_path: str,
    *,
    xlsx_dangerous_formula_action: str = "reject",
) -> dict[str, Any]:
    normalized, rel = normalize_output_artifact(requested_path)
    raw_path = safe_join(paths.raw_root, rel)
    clean_path = safe_join(paths.clean_root, rel)
    quarantine_path = safe_join(paths.quarantine_root, rel)
    suffix = raw_path.suffix.lower()
    kind = suffix.removeprefix(".")

    if suffix == ".xlsm":
        return reject_result(
            requested_path=normalized,
            rel=rel,
            raw_path=raw_path,
            quarantine_path=quarantine_path,
            kind="xlsm",
            findings=[finding("xlsm_not_allowed", "macro-enabled workbook is not allowed")],
        )
    if suffix not in ALLOWED_EXTENSIONS:
        return reject_result(
            requested_path=normalized,
            rel=rel,
            raw_path=raw_path,
            quarantine_path=quarantine_path,
            kind=kind or None,
            findings=[
                finding(
                    "extension_not_allowed",
                    "artifact extension is not in output allowlist",
                    extension=suffix,
                    allowlist=sorted(ALLOWED_EXTENSIONS),
                )
            ],
        )
    if not raw_path.exists() or not raw_path.is_file():
        return reject_result(
            requested_path=normalized,
            rel=rel,
            raw_path=raw_path,
            quarantine_path=quarantine_path,
            kind=kind,
            findings=[finding("raw_artifact_missing", "raw artifact does not exist")],
        )

    try:
        if suffix == ".md":
            findings, actions = sanitize_md(raw_path, clean_path)
        elif suffix == ".csv":
            findings, actions = sanitize_csv(raw_path, clean_path)
        elif suffix == ".html":
            findings, actions = sanitize_html(raw_path, clean_path)
        elif suffix == ".xlsx":
            findings, actions = sanitize_xlsx(
                raw_path,
                clean_path,
                dangerous_formula_action=xlsx_dangerous_formula_action,
            )
        else:  # pragma: no cover - protected by allowlist
            raise GateReject([finding("unsupported_kind", "unsupported artifact kind")])
        return pass_result(
            requested_path=normalized,
            raw_path=raw_path,
            clean_path=clean_path,
            kind=kind,
            findings=findings,
            actions=actions,
        )
    except GateReject as exc:
        return reject_result(
            requested_path=normalized,
            rel=rel,
            raw_path=raw_path,
            quarantine_path=quarantine_path,
            kind=kind,
            findings=exc.findings,
        )
    except Exception as exc:
        return reject_result(
            requested_path=normalized,
            rel=rel,
            raw_path=raw_path,
            quarantine_path=quarantine_path,
            kind=kind,
            findings=[
                finding(
                    "gate_exception",
                    f"gate processing failed: {exc.__class__.__name__}: {exc}",
                )
            ],
        )


def run_output_gate(
    *,
    raw_root: Path,
    clean_root: Path,
    quarantine_root: Path,
    log_root: Path,
    artifacts: list[str],
    run_id: str | None = None,
    xlsx_dangerous_formula_action: str = "reject",
) -> dict[str, Any]:
    if xlsx_dangerous_formula_action not in {"reject", "stringify"}:
        raise ValueError("xlsx_dangerous_formula_action must be reject or stringify")
    for root in (raw_root, clean_root, quarantine_root, log_root):
        root.mkdir(parents=True, exist_ok=True)
    paths = GatePaths(
        raw_root=raw_root.resolve(),
        clean_root=clean_root.resolve(),
        quarantine_root=quarantine_root.resolve(),
        log_root=log_root.resolve(),
    )
    normalized_artifacts = []
    seen = set()
    for artifact in artifacts:
        normalized, _ = normalize_output_artifact(artifact)
        if normalized not in seen:
            normalized_artifacts.append(normalized)
            seen.add(normalized)

    results = [
        process_artifact(
            paths,
            artifact,
            xlsx_dangerous_formula_action=xlsx_dangerous_formula_action,
        )
        for artifact in normalized_artifacts
    ]
    pass_like = {"pass", "sanitized"}
    overall_status = "pass" if results and all(item["status"] in pass_like for item in results) else "fail"
    manifest = {
        "policy_version": POLICY_VERSION,
        "run_id": run_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "execution_mode": "local",
        "overall_status": overall_status,
        "xlsx_dangerous_formula_action": xlsx_dangerous_formula_action,
        "raw_root": str(paths.raw_root),
        "clean_root": str(paths.clean_root),
        "quarantine_root": str(paths.quarantine_root),
        "artifacts_requested": normalized_artifacts,
        "artifacts": results,
    }
    manifest_path = paths.log_root / "gate_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate and sanitize sandbox output artifacts.")
    parser.add_argument("--raw-root", required=True)
    parser.add_argument("--clean-root", required=True)
    parser.add_argument("--quarantine-root", required=True)
    parser.add_argument("--log-root", required=True)
    parser.add_argument("--run-id")
    parser.add_argument(
        "--artifact",
        action="append",
        default=[],
        help="Artifact path under /outputs. Repeat for multiple artifacts.",
    )
    parser.add_argument(
        "--xlsx-dangerous-formula-action",
        choices=["reject", "stringify"],
        default="reject",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest = run_output_gate(
        raw_root=Path(args.raw_root),
        clean_root=Path(args.clean_root),
        quarantine_root=Path(args.quarantine_root),
        log_root=Path(args.log_root),
        artifacts=args.artifact,
        run_id=args.run_id,
        xlsx_dangerous_formula_action=args.xlsx_dangerous_formula_action,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
