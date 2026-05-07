from __future__ import annotations

import hashlib
import json
import shutil
import time
import uuid
from pathlib import Path

from ifc_tiler.adapters.external_converter import run_native_converter
from ifc_tiler.config import (
    ConvertConfig,
    EXIT_CONVERTER_FAILED,
    EXIT_INPUT_INVALID,
    EXIT_OK,
    EXIT_OUTPUT_CONFLICT,
    EXIT_OUTPUT_INVALID,
)


def run_convert(config: ConvertConfig) -> int:
    input_ifc = config.input_ifc.expanduser().resolve()
    out_dir = config.out_dir.expanduser().resolve()
    job_name = config.job_name

    if not input_ifc.exists() or not input_ifc.is_file():
        print(f"[error] IFC file not found: {input_ifc}")
        return EXIT_INPUT_INVALID
    if input_ifc.suffix.lower() != ".ifc":
        print(f"[error] Input must be an .ifc file: {input_ifc}")
        return EXIT_INPUT_INVALID

    out_dir.mkdir(parents=True, exist_ok=True)
    final_dir = out_dir / job_name
    if final_dir.exists() and not config.overwrite:
        print(
            f"[error] Output already exists: {final_dir} "
            "(use --overwrite to replace)"
        )
        return EXIT_OUTPUT_CONFLICT

    job_id = f"{job_name}-{uuid.uuid4().hex[:8]}"
    temp_root = out_dir / ".tmp" / job_id
    temp_converter_output = temp_root / "converter_output"
    temp_normalized = temp_root / "normalized"
    temp_logs = temp_root / "logs"
    convert_log = temp_logs / "convert.log"

    started = time.time()
    input_hash = _sha256_file(input_ifc)

    converter_result = run_native_converter(
        input_ifc=input_ifc,
        output_dir=temp_converter_output,
        log_file=convert_log,
        timeout_min=config.timeout_min,
    )
    if not converter_result.ok:
        _persist_failure_logs(final_dir=final_dir, convert_log=convert_log, overwrite=config.overwrite)
        message = converter_result.error_message or "Unknown converter error."
        print(f"[error] {message}")
        return EXIT_CONVERTER_FAILED

    tileset_path = _find_tileset(temp_converter_output)
    if not tileset_path:
        _persist_failure_logs(final_dir=final_dir, convert_log=convert_log, overwrite=config.overwrite)
        print("[error] Converter output missing tileset.json")
        return EXIT_OUTPUT_INVALID

    normalized_root = tileset_path.parent
    shutil.copytree(normalized_root, temp_normalized, dirs_exist_ok=True)

    normalized_tileset = temp_normalized / "tileset.json"
    validation_error = _validate_tileset(normalized_tileset)
    if validation_error:
        _persist_failure_logs(final_dir=final_dir, convert_log=convert_log, overwrite=config.overwrite)
        print(f"[error] {validation_error}")
        return EXIT_OUTPUT_INVALID

    manifest = {
        "input_ifc": str(input_ifc),
        "input_sha256": input_hash,
        "job_name": job_name,
        "created_at_epoch": int(time.time()),
        "elapsed_seconds_total": round(time.time() - started, 2),
        "converter_elapsed_seconds": round(converter_result.elapsed_seconds, 2),
        "converter_command": converter_result.command,
        "config": {
            "input_ifc": str(config.input_ifc),
            "out_dir": str(config.out_dir),
            "job_name": config.job_name,
            "overwrite": config.overwrite,
            "keep_temp": config.keep_temp,
            "timeout_min": config.timeout_min,
        },
    }
    (temp_normalized / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )
    (temp_normalized / "logs").mkdir(parents=True, exist_ok=True)
    if convert_log.exists():
        shutil.copy2(convert_log, temp_normalized / "logs" / "convert.log")

    if final_dir.exists() and config.overwrite:
        shutil.rmtree(final_dir)
    temp_normalized.parent.mkdir(parents=True, exist_ok=True)
    temp_normalized.replace(final_dir)

    if not config.keep_temp and temp_root.exists():
        shutil.rmtree(temp_root, ignore_errors=True)

    print(f"[ok] Converted IFC: {input_ifc}")
    print(f"[ok] Output directory: {final_dir}")
    return EXIT_OK


def _persist_failure_logs(*, final_dir: Path, convert_log: Path, overwrite: bool) -> None:
    logs_dir = final_dir / "logs"
    if final_dir.exists() and overwrite:
        shutil.rmtree(final_dir, ignore_errors=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    if convert_log.exists():
        shutil.copy2(convert_log, logs_dir / "convert.log")


def _find_tileset(root: Path) -> Path | None:
    candidates = sorted(root.rglob("tileset.json"))
    if not candidates:
        return None
    return min(candidates, key=lambda p: len(p.parts))


def _validate_tileset(tileset_path: Path) -> str | None:
    if not tileset_path.exists():
        return "tileset.json not found after normalization."
    try:
        data = json.loads(tileset_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return f"tileset.json is invalid JSON: {exc}"

    root = data.get("root")
    if not isinstance(root, dict):
        return "tileset.json missing root node."

    refs = []
    _collect_content_refs(root, refs)
    for ref in refs:
        if "://" in ref:
            continue
        candidate = (tileset_path.parent / ref).resolve()
        if not candidate.exists():
            return f"Referenced content is missing: {ref}"
    return None


def _collect_content_refs(node: dict, refs: list[str]) -> None:
    content = node.get("content")
    if isinstance(content, dict):
        uri = content.get("uri") or content.get("url")
        if isinstance(uri, str) and uri.strip():
            refs.append(uri)

    contents = node.get("contents")
    if isinstance(contents, list):
        for item in contents:
            if not isinstance(item, dict):
                continue
            uri = item.get("uri") or item.get("url")
            if isinstance(uri, str) and uri.strip():
                refs.append(uri)

    children = node.get("children")
    if isinstance(children, list):
        for child in children:
            if isinstance(child, dict):
                _collect_content_refs(child, refs)


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()
