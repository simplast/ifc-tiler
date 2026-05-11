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
    diagnostics: bool
    color_mode: str
    transform_mode: str
    max_shapes: int | None
    shape_range: tuple[int, int] | None
    tile_target_mb: float
    tile_max_mb: float
    max_tile_triangles: int
    lod_mode: str
    lod_target_ratio: float
    lod_min_faces: int
    meshopt: bool
    skip_proxy_body_items_ge: int
    skip_brep_body_items_ge: int
    skip_surface_body_items_ge: int
    use_cache: bool
    rebuild_cache: bool
    cache_dir: Path | None
    backend: str = "native"
