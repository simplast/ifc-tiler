from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ConverterResult:
    ok: bool
    command: str
    elapsed_seconds: float
    error_message: str | None = None


def run_native_converter(
    *,
    input_ifc: Path,
    output_dir: Path,
    log_file: Path,
    timeout_min: int,
) -> ConverterResult:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    command = "native-ifcopenshell-engine"
    started = time.time()
    deadline = started + max(1, timeout_min) * 60

    try:
        import numpy as np
        import trimesh
        import ifcopenshell
        import ifcopenshell.geom
    except Exception as exc:
        log_file.write_text(
            f"command={command}\nexit_code=-1\nelapsed_seconds=0.00\n\n"
            f"----- stderr -----\nFailed to import ifcopenshell: {exc}\n",
            encoding="utf-8",
        )
        return ConverterResult(
            ok=False,
            command=command,
            elapsed_seconds=0.0,
            error_message="ifcopenshell is not installed or unavailable in runtime.",
        )

    logs: list[str] = []
    tiles_dir = output_dir / "tiles"
    tiles_dir.mkdir(parents=True, exist_ok=True)

    def emit_progress(message: str) -> None:
        logs.append(message)
        # Keep console feedback live for long IFC conversions.
        print(f"[progress] {message}", flush=True)
        elapsed = time.time() - started
        log_file.write_text(
            "\n".join(
                [
                    f"command={command}",
                    "exit_code=running",
                    f"elapsed_seconds={elapsed:.2f}",
                    "",
                    "----- progress -----",
                    *logs,
                ]
            ),
            encoding="utf-8",
        )

    try:
        emit_progress(f"Loading IFC: {input_ifc}")
        model = ifcopenshell.open(str(input_ifc))
        settings = ifcopenshell.geom.settings()
        settings.set(settings.USE_WORLD_COORDS, True)
        if hasattr(settings, "APPLY_DEFAULT_MATERIALS"):
            settings.set(settings.APPLY_DEFAULT_MATERIALS, True)

        products = [
            element
            for element in model.by_type("IfcProduct")
            if getattr(element, "Representation", None) is not None
        ]
        emit_progress(f"Found products with geometry representations: {len(products)}")

        meshes: list[trimesh.Trimesh] = []
        skipped = 0
        first_errors: list[str] = []
        start_extract = time.time()

        # Prefer iterator for broad IFC compatibility (especially IFC2x3 samples).
        emit_progress("Extracting geometry with ifcopenshell iterator...")
        iterator_processed = 0
        iterator = ifcopenshell.geom.iterator(settings, model)
        if iterator.initialize():
            while True:
                if time.time() > deadline:
                    raise TimeoutError("Native conversion timed out.")
                shape = iterator.get()
                iterator_processed += 1
                try:
                    mesh = _shape_to_mesh(shape, np, trimesh)
                    if mesh is None:
                        skipped += 1
                    else:
                        meshes.append(mesh)
                except Exception as exc:
                    skipped += 1
                    if len(first_errors) < 5:
                        first_errors.append(f"iterator shape error: {exc}")

                if iterator_processed % 200 == 0:
                    elapsed_extract = max(0.001, time.time() - start_extract)
                    speed = iterator_processed / elapsed_extract
                    emit_progress(
                        f"Processed shapes: {iterator_processed} "
                        f"(ok={len(meshes)}, skipped={skipped}, {speed:.1f}/s)"
                    )

                if not iterator.next():
                    break
            emit_progress(f"Iterator completed with {iterator_processed} shapes.")
        else:
            emit_progress("Iterator failed to initialize; fallback to per-product create_shape.")
            for index, element in enumerate(products):
                if time.time() > deadline:
                    raise TimeoutError("Native conversion timed out.")
                try:
                    shape = ifcopenshell.geom.create_shape(settings, element)
                    mesh = _shape_to_mesh(shape, np, trimesh)
                    if mesh is None:
                        skipped += 1
                    else:
                        meshes.append(mesh)
                except Exception as exc:
                    skipped += 1
                    if len(first_errors) < 5:
                        first_errors.append(f"create_shape error: {exc}")
                if (index + 1) % 200 == 0:
                    processed = index + 1
                    elapsed_extract = max(0.001, time.time() - start_extract)
                    speed = processed / elapsed_extract
                    emit_progress(
                        f"Processed products: {processed}/{len(products)} "
                        f"(ok={len(meshes)}, skipped={skipped}, {speed:.1f}/s)"
                    )

        if not meshes:
            if first_errors:
                emit_progress("First geometry errors:")
                for item in first_errors:
                    emit_progress(f"  - {item}")
            raise RuntimeError("No mesh geometry extracted from IFC.")

        emit_progress(f"Extracted meshes: {len(meshes)}, skipped: {skipped}")
        grouped_meshes = _group_meshes_spatially(meshes)
        emit_progress(f"Generated spatial tile groups: {len(grouped_meshes)}")

        children = []
        for tile_index, tile_meshes in enumerate(grouped_meshes):
            merged_mesh = trimesh.util.concatenate(tile_meshes)
            tile_name = f"{tile_index:05d}.glb"
            tile_path = tiles_dir / tile_name
            tile_path.write_bytes(merged_mesh.export(file_type="glb"))
            children.append(
                {
                    "boundingVolume": {"box": _to_box(merged_mesh.bounds)},
                    "geometricError": 0,
                    "content": {"uri": f"tiles/{tile_name}"},
                    "refine": "ADD",
                }
            )
            if (tile_index + 1) % 20 == 0 or tile_index + 1 == len(grouped_meshes):
                emit_progress(
                    f"Written tiles: {tile_index + 1}/{len(grouped_meshes)}"
                )

        scene_bounds = _compute_union_bounds(meshes)
        tileset = {
            "asset": {"version": "1.1"},
            "geometricError": 0,
            "root": {
                "boundingVolume": {"box": _to_box(scene_bounds)},
                "geometricError": 0,
                "refine": "ADD",
                "children": children,
            },
        }
        (output_dir / "tileset.json").write_text(
            json.dumps(tileset, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
        emit_progress("Wrote tileset.json")

        elapsed = time.time() - started
        log_file.write_text(
            "\n".join(
                [
                    f"command={command}",
                    "exit_code=0",
                    f"elapsed_seconds={elapsed:.2f}",
                    "",
                    "----- progress -----",
                    *logs,
                ]
            ),
            encoding="utf-8",
        )
        return ConverterResult(ok=True, command=command, elapsed_seconds=elapsed)
    except TimeoutError as exc:
        elapsed = time.time() - started
        log_file.write_text(
            "\n".join(
                [
                    f"command={command}",
                    "exit_code=-2",
                    f"elapsed_seconds={elapsed:.2f}",
                    "",
                    "----- stderr -----",
                    str(exc),
                    "",
                    "----- progress -----",
                    *logs,
                ]
            ),
            encoding="utf-8",
        )
        return ConverterResult(
            ok=False,
            command=command,
            elapsed_seconds=elapsed,
            error_message="Native converter timed out.",
        )
    except Exception as exc:
        elapsed = time.time() - started
        log_file.write_text(
            "\n".join(
                [
                    f"command={command}",
                    "exit_code=1",
                    f"elapsed_seconds={elapsed:.2f}",
                    "",
                    "----- stderr -----",
                    str(exc),
                    "",
                    "----- progress -----",
                    *logs,
                ]
            ),
            encoding="utf-8",
        )
        return ConverterResult(
            ok=False,
            command=command,
            elapsed_seconds=elapsed,
            error_message=f"Native converter failed: {exc}",
        )


def _group_meshes_spatially(meshes: list) -> list[list]:
    import numpy as np

    if len(meshes) <= 48:
        return [[mesh] for mesh in meshes]

    bounds = _compute_union_bounds(meshes)
    mins = bounds[0]
    maxs = bounds[1]
    target_tile_count = max(8, min(512, math.ceil(len(meshes) / 120)))
    grid_size = max(2, math.ceil(target_tile_count ** (1.0 / 3.0)))
    step = np.maximum((maxs - mins) / grid_size, np.array([1e-6, 1e-6, 1e-6]))

    groups: dict[tuple[int, int, int], list[trimesh.Trimesh]] = {}
    for mesh in meshes:
        centroid = mesh.bounds.mean(axis=0)
        idx = tuple(
            int(
                min(
                    grid_size - 1,
                    max(0, math.floor((centroid[axis] - mins[axis]) / step[axis])),
                )
            )
            for axis in range(3)
        )
        groups.setdefault(idx, []).append(mesh)

    return list(groups.values())


def _compute_union_bounds(meshes: list):
    import numpy as np

    mins = np.min(np.stack([mesh.bounds[0] for mesh in meshes], axis=0), axis=0)
    maxs = np.max(np.stack([mesh.bounds[1] for mesh in meshes], axis=0), axis=0)
    return np.stack([mins, maxs], axis=0)


def _to_box(bounds) -> list[float]:
    mins, maxs = bounds[0], bounds[1]
    center = ((mins + maxs) * 0.5).astype(float)
    half = ((maxs - mins) * 0.5).astype(float)
    return [
        center[0],
        center[1],
        center[2],
        half[0],
        0.0,
        0.0,
        0.0,
        half[1],
        0.0,
        0.0,
        0.0,
        half[2],
    ]


def _extract_face_colors(geometry, face_count: int):
    import numpy as np

    material_ids_raw = getattr(geometry, "material_ids", None)
    materials_raw = getattr(geometry, "materials", None)
    if material_ids_raw is None or materials_raw is None or face_count <= 0:
        return None

    try:
        material_ids = np.array(material_ids_raw, dtype=np.int64).reshape(-1)
    except Exception:
        return None
    if material_ids.size == 0:
        return None
    if material_ids.size < face_count:
        return None

    materials = list(materials_raw)
    if len(materials) == 0:
        return None

    colors = np.zeros((face_count, 4), dtype=np.uint8)
    has_color = False
    for i in range(face_count):
        material_index = int(material_ids[i])
        rgba = None
        try:
            rgba = _material_to_rgba(
                materials[material_index] if 0 <= material_index < len(materials) else None
            )
        except Exception:
            rgba = None
        if rgba is None:
            rgba = (180, 180, 180, 255)
        else:
            has_color = True
        colors[i, :] = rgba
    return colors if has_color else None


def _shape_to_mesh(shape, np, trimesh):
    geometry = getattr(shape, "geometry", None)
    if geometry is None:
        return None
    vertices = np.array(getattr(geometry, "verts", []), dtype=np.float32).reshape((-1, 3))
    faces = np.array(getattr(geometry, "faces", []), dtype=np.int64).reshape((-1, 3))
    if vertices.size == 0 or faces.size == 0:
        return None
    face_colors = _extract_face_colors(geometry, len(faces))
    mesh = trimesh.Trimesh(
        vertices=vertices,
        faces=faces,
        process=False,
        face_colors=face_colors,
    )
    # IFC geometry is usually Z-up; convert to Y-up for Three.js/glTF workflows.
    mesh.apply_transform(
        trimesh.transformations.rotation_matrix(-math.pi / 2, [1, 0, 0])
    )
    return mesh


def _material_to_rgba(material) -> tuple[int, int, int, int] | None:
    if material is None:
        return None

    rgb = _extract_rgb(material)
    if rgb is None:
        return None

    transparency = getattr(material, "transparency", 0.0)
    alpha = int(max(0.0, min(1.0, 1.0 - float(transparency))) * 255)
    return (
        int(max(0.0, min(1.0, rgb[0])) * 255),
        int(max(0.0, min(1.0, rgb[1])) * 255),
        int(max(0.0, min(1.0, rgb[2])) * 255),
        alpha,
    )


def _extract_rgb(material) -> tuple[float, float, float] | None:
    candidates = [
        "diffuse",
        "surface_colour",
        "surface_color",
        "color",
        "diffuse_colour",
        "diffuse_color",
    ]
    for attr in candidates:
        if not hasattr(material, attr):
            continue
        value = getattr(material, attr)
        rgb = _coerce_rgb(value)
        if rgb is not None:
            return rgb
    return None


def _coerce_rgb(value) -> tuple[float, float, float] | None:
    if isinstance(value, (tuple, list)) and len(value) >= 3:
        r = _safe_to_float(value[0])
        g = _safe_to_float(value[1])
        b = _safe_to_float(value[2])
        if r is None or g is None or b is None:
            return None
        return (r, g, b)
    r = getattr(value, "r", None)
    g = getattr(value, "g", None)
    b = getattr(value, "b", None)
    rr = _safe_to_float(r)
    gg = _safe_to_float(g)
    bb = _safe_to_float(b)
    if rr is not None and gg is not None and bb is not None:
        return (rr, gg, bb)
    x = getattr(value, "x", None)
    y = getattr(value, "y", None)
    z = getattr(value, "z", None)
    xx = _safe_to_float(x)
    yy = _safe_to_float(y)
    zz = _safe_to_float(z)
    if xx is not None and yy is not None and zz is not None:
        return (xx, yy, zz)
    return None


def _safe_to_float(value):
    if value is None:
        return None
    candidate = value
    if callable(candidate):
        try:
            candidate = candidate()
        except Exception:
            return None
    try:
        return float(candidate)
    except (TypeError, ValueError):
        return None
