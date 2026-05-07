from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

EXIT_OK = 0
EXIT_INPUT_INVALID = 2
EXIT_CONVERTER_FAILED = 3
EXIT_OUTPUT_CONFLICT = 4
EXIT_OUTPUT_INVALID = 5


@dataclass(frozen=True)
class ConvertConfig:
    input_ifc: Path
    out_dir: Path
    job_name: str
    overwrite: bool
    keep_temp: bool
    timeout_min: int
