from __future__ import annotations

import shutil
import shlex
import subprocess
import sys
import time
from pathlib import Path

from ifc_tiler.adapters.external_converter import ConverterResult


def run_py3dtiles_converter(
    *,
    input_ifc: Path,
    output_dir: Path,
    log_file: Path,
    timeout_min: int,
) -> ConverterResult:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    py3dtiles_bin = shutil.which("py3dtiles")
    if not py3dtiles_bin:
        local_bin = Path(sys.executable).with_name("py3dtiles")
        if local_bin.exists():
            py3dtiles_bin = str(local_bin)

    if not py3dtiles_bin:
        message = (
            "py3dtiles CLI was not found in PATH. Install it with "
            "`uv add py3dtiles` or run once with `uv run --with py3dtiles ...`."
        )
        log_file.write_text(
            f"command=py3dtiles convert --out {output_dir} {input_ifc}\n"
            "exit_code=-1\nelapsed_seconds=0.00\n\n"
            f"----- stderr -----\n{message}\n",
            encoding="utf-8",
        )
        return ConverterResult(False, "py3dtiles convert", 0.0, message)

    command_parts = [
        py3dtiles_bin,
        "convert",
        "--out",
        str(output_dir),
        str(input_ifc),
    ]
    command = shlex.join(command_parts)

    started = time.time()
    log_file.write_text(
        f"command={command}\nexit_code=running\nelapsed_seconds=0.00\n",
        encoding="utf-8",
    )

    try:
        completed = subprocess.run(
            command_parts,
            capture_output=True,
            text=True,
            timeout=max(1, timeout_min) * 60,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.time() - started
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        log_file.write_text(
            (
                f"command={command}\nexit_code=timeout\nelapsed_seconds={elapsed:.2f}\n\n"
                f"----- stdout -----\n{stdout}\n\n"
                f"----- stderr -----\n{stderr}\n"
            ),
            encoding="utf-8",
        )
        return ConverterResult(False, command, elapsed, "py3dtiles conversion timed out.")
    except Exception as exc:  # pragma: no cover
        elapsed = time.time() - started
        log_file.write_text(
            (
                f"command={command}\nexit_code=-1\nelapsed_seconds={elapsed:.2f}\n\n"
                f"----- stderr -----\n{exc}\n"
            ),
            encoding="utf-8",
        )
        return ConverterResult(False, command, elapsed, f"py3dtiles execution failed: {exc}")

    elapsed = time.time() - started
    log_file.write_text(
        (
            f"command={command}\nexit_code={completed.returncode}\nelapsed_seconds={elapsed:.2f}\n\n"
            f"----- stdout -----\n{completed.stdout}\n\n"
            f"----- stderr -----\n{completed.stderr}\n"
        ),
        encoding="utf-8",
    )

    if completed.returncode != 0:
        return ConverterResult(
            False,
            command,
            elapsed,
            "py3dtiles conversion failed. See logs/convert.log for details.",
        )
    return ConverterResult(True, command, elapsed)
