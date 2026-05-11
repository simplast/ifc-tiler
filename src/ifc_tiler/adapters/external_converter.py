from __future__ import annotations

import gc
import json
import math
import shutil
import statistics
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

MAX_TILE_TRIANGLES = 180_000
BYTES_PER_MB = 1024 * 1024
LOD_MAX_CHILDREN = 16
LOD_MAX_DEPTH = 8
LOD_COARSE_TRIANGLE_BUDGET = {0: 1_500_000, 1: 900_000}
LOD_MIN_BUDGETED_ITEMS = 1200
LOD_MAX_BUDGET_DEPTH = 1
CACHE_METADATA_VERSION = 1
LOD_MIN_ROOT_RATIO = 0.35
LOD_MIN_ROOT_FACES = 350_000
LOD_DEFAULT_COARSE_RGBA = (200, 200, 200, 255)


@dataclass(frozen=True)
class ConverterResult:
    ok: bool
    command: str
    elapsed_seconds: float
    error_message: str | None = None
    compression: dict[str, object] | None = None


@dataclass
class MeshBuildResult:
    mesh: object
    timings: dict[str, float]
    vertex_count: int
    triangle_count: int
    estimated_bytes: int
    has_face_colors: bool


@dataclass
class ShapeMetadata:
    shape_index: int
    product_id: int | None
    global_id: str | None
    ifc_type: str | None
    triangle_count: int
    vertex_count: int
    estimated_bytes: int
    bounds: object
    centroid: object
    staging_path: Path
    has_face_colors: bool


def run_native_converter(
    *,
    input_ifc: Path,
    output_dir: Path,
    log_file: Path,
    timeout_min: int,
    diagnostics: bool = False,
    color_mode: str = "materials",
    transform_mode: str = "mesh",
    max_shapes: int | None = None,
    shape_range: tuple[int, int] | None = None,
    tile_target_mb: float = 4.0,
    tile_max_mb: float = 20.0,
    max_tile_triangles: int = MAX_TILE_TRIANGLES,
    lod_mode: str = "flat",
    lod_target_ratio: float = 0.1,
    lod_min_faces: int = 2_000,
    meshopt: bool = False,
    keep_temp: bool = False,
    skip_proxy_body_items_ge: int = 500,
    skip_brep_body_items_ge: int = 50,
    skip_surface_body_items_ge: int = 50,
    use_cache: bool = True,
    rebuild_cache: bool = False,
    cache_dir: Path | None = None,
) -> ConverterResult:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    tiles_dir = output_dir / "tiles"
    tiles_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = output_dir / ".staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    mesh_storage_dir = cache_dir if (use_cache and cache_dir is not None) else staging_dir
    mesh_storage_dir.mkdir(parents=True, exist_ok=True)
    _assert_staging_disk_space(input_ifc, mesh_storage_dir.parent)
    command = "native-ifcopenshell-engine"
    started = time.time()
    deadline = started + max(1, timeout_min) * 60
    color_mode = color_mode if color_mode in {"none", "uniform", "materials"} else "materials"
    transform_mode = transform_mode if transform_mode in {"mesh", "tile", "tileset", "none"} else "mesh"
    target_bytes = max(1.0, tile_target_mb) * BYTES_PER_MB
    max_bytes = max(target_bytes, tile_max_mb * BYTES_PER_MB)
    max_tile_triangles = max(1, max_tile_triangles)
    lod_mode = lod_mode if lod_mode in {"flat", "lod"} else "flat"
    lod_target_ratio = min(0.95, max(0.01, float(lod_target_ratio)))
    lod_min_faces = max(100, int(lod_min_faces))

    try:
        import ifcopenshell
        import ifcopenshell.geom
        import numpy as np
        import trimesh
    except Exception as exc:
        log_file.write_text(
            f"command={command}\nexit_code=-1\nelapsed_seconds=0.00\n\n"
            f"----- stderr -----\nFailed to import ifcopenshell: {exc}\n",
            encoding="utf-8",
        )
        return ConverterResult(False, command, 0.0, "ifcopenshell is not installed or unavailable in runtime.")

    log_file.write_text(
        f"command={command}\nexit_code=running\nelapsed_seconds=0.00\n\n----- progress -----\n",
        encoding="utf-8",
    )

    def emit_progress(message: str) -> None:
        print(f"[progress] {message}", flush=True)
        with log_file.open("a", encoding="utf-8") as stream:
            stream.write(f"{message}\n")

    def finish_log(exit_code: int, elapsed: float, error_message: str | None = None) -> None:
        with log_file.open("a", encoding="utf-8") as stream:
            stream.write(f"\n----- result -----\nexit_code={exit_code}\nelapsed_seconds={elapsed:.2f}\n")
            if error_message:
                stream.write(f"\n----- stderr -----\n{error_message}\n")

    stats: dict[str, object] = {
        "input_ifc": str(input_ifc),
        "options": {
            "diagnostics": diagnostics,
            "color_mode": color_mode,
            "transform_mode": transform_mode,
            "max_shapes": max_shapes,
            "shape_range": shape_range,
            "tile_target_mb": tile_target_mb,
            "tile_max_mb": tile_max_mb,
            "max_tile_triangles": max_tile_triangles,
            "lod_mode": lod_mode,
            "lod_target_ratio": lod_target_ratio,
            "lod_min_faces": lod_min_faces,
            "meshopt": meshopt,
            "skip_proxy_body_items_ge": skip_proxy_body_items_ge,
            "skip_brep_body_items_ge": skip_brep_body_items_ge,
            "skip_surface_body_items_ge": skip_surface_body_items_ge,
            "use_cache": use_cache,
            "rebuild_cache": rebuild_cache,
            "cache_dir": str(cache_dir) if cache_dir else None,
        },
        "slow_shapes": [],
        "large_shapes": [],
        "shape_type_counts": {},
        "diagnostic_shape_samples": [],
        "tile_details": [],
        "complexity_skip_samples": [],
    }

    def record_shape(item: ShapeMetadata, timings: dict[str, float]) -> None:
        stat = _shape_stat_dict(item, timings, _current_rss_mb())
        if item.ifc_type:
            counts = stats["shape_type_counts"]
            assert isinstance(counts, dict)
            counts[item.ifc_type] = int(counts.get(item.ifc_type, 0)) + 1
        _push_top(stats["slow_shapes"], stat, "total_ms", 40)
        _push_top(stats["large_shapes"], stat, "triangle_count", 40)
        if diagnostics and 15_000 <= item.shape_index <= 20_500:
            samples = stats["diagnostic_shape_samples"]
            assert isinstance(samples, list)
            samples.append(stat)

    def write_stats() -> None:
        (output_dir / "stats.json").write_text(
            json.dumps(_json_safe(stats), ensure_ascii=True, indent=2),
            encoding="utf-8",
        )

    try:
        shape_items: list[ShapeMetadata] = []
        skipped = 0
        selected_shapes = 0
        iterator_processed = 0
        first_errors: list[str] = []
        start_extract = time.time()

        if use_cache and mesh_storage_dir is not None:
            if rebuild_cache and mesh_storage_dir.exists():
                emit_progress(f"Rebuilding persistent geometry cache: {mesh_storage_dir}")
                shutil.rmtree(mesh_storage_dir, ignore_errors=True)
                mesh_storage_dir.mkdir(parents=True, exist_ok=True)
            if not rebuild_cache:
                cached_items = _load_shape_metadata_cache(mesh_storage_dir, np=np, log=emit_progress)
                if cached_items is not None:
                    shape_items = cached_items
                    selected_shapes = len(shape_items)
                    stats["cache"] = {
                        "enabled": True,
                        "status": "hit",
                        "cache_dir": str(mesh_storage_dir),
                        "cached_meshes": len(shape_items),
                    }
                    stats["complexity_filter"] = {
                        "enabled": None,
                        "thresholds": {
                            "proxy_body_items_ge": max(0, int(skip_proxy_body_items_ge)),
                            "brep_body_items_ge": max(0, int(skip_brep_body_items_ge)),
                            "surface_body_items_ge": max(0, int(skip_surface_body_items_ge)),
                        },
                        "skipped_products": None,
                        "reason_counts": None,
                    }
                    emit_progress(f"Persistent geometry cache hit: {len(shape_items)} meshes")
        if not shape_items:
            if use_cache:
                stats["cache"] = {
                    "enabled": True,
                    "status": "miss",
                    "cache_dir": str(mesh_storage_dir),
                    "cached_meshes": 0,
                }
            else:
                stats["cache"] = {"enabled": False, "status": "disabled", "cache_dir": None, "cached_meshes": 0}
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
            complexity_filter = {
                "proxy_body_items_ge": max(0, int(skip_proxy_body_items_ge)),
                "brep_body_items_ge": max(0, int(skip_brep_body_items_ge)),
                "surface_body_items_ge": max(0, int(skip_surface_body_items_ge)),
            }
            complexity_filter_enabled = any(value > 0 for value in complexity_filter.values())
            skip_product_ids: set[int] = set()
            skip_reason_counts = {
                "proxy_body_items": 0,
                "brep_body_items": 0,
                "surface_body_items": 0,
            }
            if complexity_filter_enabled:
                skip_product_ids, skip_reason_counts, skip_samples = _precompute_complexity_skips(
                    products,
                    complexity_filter=complexity_filter,
                )
                stats["complexity_filter"] = {
                    "enabled": True,
                    "thresholds": complexity_filter,
                    "skipped_products": len(skip_product_ids),
                    "reason_counts": skip_reason_counts,
                }
                samples = stats["complexity_skip_samples"]
                assert isinstance(samples, list)
                samples.extend(skip_samples)
                emit_progress(
                    f"Complexity pre-filter: skipped {len(skip_product_ids)}/{len(products)} products "
                    f"(proxy={skip_reason_counts['proxy_body_items']}, "
                    f"brep={skip_reason_counts['brep_body_items']}, "
                    f"surface={skip_reason_counts['surface_body_items']})"
                )
            else:
                stats["complexity_filter"] = {
                    "enabled": False,
                    "thresholds": complexity_filter,
                    "skipped_products": 0,
                    "reason_counts": skip_reason_counts,
                }

            if complexity_filter_enabled:
                emit_progress("Extracting geometry with per-product create_shape (pre-filter enforced)...")
                for index, element in enumerate(products):
                    if time.time() > deadline:
                        raise TimeoutError("Native conversion timed out.")
                    shape_index = index + 1
                    product_id = _safe_element_id(element)
                    if product_id is not None and product_id in skip_product_ids:
                        continue
                    if _shape_is_selected(shape_index, shape_range):
                        selected_shapes += 1
                        try:
                            get_started = time.perf_counter()
                            shape = ifcopenshell.geom.create_shape(settings, element)
                            get_ms = (time.perf_counter() - get_started) * 1000.0
                            build_started = time.perf_counter()
                            result = _shape_to_mesh(
                                shape,
                                np,
                                trimesh,
                                color_mode=color_mode,
                                transform_mode=transform_mode,
                            )
                            build_total_ms = (time.perf_counter() - build_started) * 1000.0
                            if result is None:
                                skipped += 1
                            else:
                                result.timings["iterator_get_ms"] = get_ms
                                result.timings["total_ms"] = build_total_ms + get_ms
                                item = _make_shape_metadata(
                                    result,
                                    shape_index,
                                    _element_info(element),
                                    staging_dir=mesh_storage_dir,
                                    np=np,
                                )
                                shape_items.append(item)
                                record_shape(item, result.timings)
                                del result.mesh
                        except Exception as exc:
                            skipped += 1
                            if len(first_errors) < 5:
                                first_errors.append(f"create_shape error: {exc}")
                    if shape_index % 200 == 0:
                        elapsed_extract = max(0.001, time.time() - start_extract)
                        emit_progress(
                            f"Processed products: {shape_index}/{len(products)} "
                            f"(selected={selected_shapes}, ok={len(shape_items)}, skipped={skipped}, "
                            f"{selected_shapes / elapsed_extract:.1f}/s, rss={_current_rss_mb():.0f}MB)"
                        )
                    if max_shapes is not None and selected_shapes >= max_shapes:
                        emit_progress(f"Stopped after selected max shapes: {max_shapes}")
                        break
                iterator_processed = len(products)
            else:
                emit_progress("Extracting geometry with ifcopenshell iterator...")
                iterator = ifcopenshell.geom.iterator(settings, model)
                if iterator.initialize():
                    while True:
                        if time.time() > deadline:
                            raise TimeoutError("Native conversion timed out.")
                        get_started = time.perf_counter()
                        shape = iterator.get()
                        get_ms = (time.perf_counter() - get_started) * 1000.0
                        iterator_processed += 1
                        if _shape_is_selected(iterator_processed, shape_range):
                            selected_shapes += 1
                            try:
                                build_started = time.perf_counter()
                                result = _shape_to_mesh(
                                    shape,
                                    np,
                                    trimesh,
                                    color_mode=color_mode,
                                    transform_mode=transform_mode,
                                )
                                build_total_ms = (time.perf_counter() - build_started) * 1000.0
                                if result is None:
                                    skipped += 1
                                else:
                                    result.timings["iterator_get_ms"] = get_ms
                                    result.timings["total_ms"] = build_total_ms + get_ms
                                    item = _make_shape_metadata(
                                        result,
                                        iterator_processed,
                                        _shape_info(model, shape),
                                        staging_dir=mesh_storage_dir,
                                        np=np,
                                    )
                                    shape_items.append(item)
                                    record_shape(item, result.timings)
                                    del result.mesh
                            except Exception as exc:
                                skipped += 1
                                if len(first_errors) < 5:
                                    first_errors.append(f"iterator shape error: {exc}")
                        if iterator_processed % 200 == 0:
                            elapsed_extract = max(0.001, time.time() - start_extract)
                            emit_progress(
                                f"Processed shapes: {iterator_processed} "
                                f"(selected={selected_shapes}, ok={len(shape_items)}, skipped={skipped}, "
                                f"{selected_shapes / elapsed_extract:.1f}/s, rss={_current_rss_mb():.0f}MB)"
                            )
                        if max_shapes is not None and selected_shapes >= max_shapes:
                            emit_progress(f"Stopped after selected max shapes: {max_shapes}")
                            break
                        if not iterator.next():
                            break
                    emit_progress(f"Iterator completed with {iterator_processed} shapes.")
                else:
                    raise RuntimeError("ifcopenshell iterator failed to initialize.")

            if use_cache and mesh_storage_dir is not None:
                _write_shape_metadata_cache(
                    mesh_storage_dir,
                    shape_items,
                    np=np,
                    log=emit_progress,
                )

        if not shape_items:
            if first_errors:
                emit_progress("First geometry errors:")
                for item in first_errors:
                    emit_progress(f"  - {item}")
            raise RuntimeError("No mesh geometry extracted from IFC.")

        extraction_elapsed = time.time() - start_extract
        stats["extraction_summary"] = {
            "iterator_shapes": iterator_processed,
            "selected_shapes": selected_shapes,
            "meshes": len(shape_items),
            "skipped": skipped,
            "elapsed_seconds": round(extraction_elapsed, 2),
            "selected_shapes_per_second": round(selected_shapes / max(0.001, extraction_elapsed), 2),
            "rss_mb": round(_current_rss_mb(), 1),
        }

        emit_progress(f"Extracted meshes: {len(shape_items)}, skipped: {skipped}")
        grouped_items = _group_items_spatially(
            shape_items,
            target_bytes=target_bytes,
            max_bytes=max_bytes,
            max_triangles=max_tile_triangles,
            np=np,
        )
        emit_progress(f"Generated spatial tile groups: {len(grouped_items)}")

        tile_records: list[dict] = []
        for tile_index, tile_group in enumerate(grouped_items):
            # One merged mesh per GLB keeps draw calls low (60fps class). Exporting each IFC
            # shape as its own glTF node (py3dtiles-style) cuts z-fighting slightly but explodes
            # draw count when a tile holds hundreds of products — GPU-bound to ~10–20fps.
            merged_mesh = _hydrate_tile_group(tile_group, np=np, trimesh=trimesh)
            if transform_mode == "tile":
                _apply_y_up_transform(merged_mesh, trimesh)
            tile_name = f"{tile_index:05d}.glb"
            tile_path = tiles_dir / tile_name
            tile_path.write_bytes(merged_mesh.export(file_type="glb"))
            tile_size = tile_path.stat().st_size
            tile_record = {
                "tile_index": tile_index,
                "uri": f"tiles/{tile_name}",
                "byte_size": tile_size,
                "triangle_count": sum(item.triangle_count for item in tile_group),
                "vertex_count": sum(item.vertex_count for item in tile_group),
                "shape_count": len(tile_group),
                "bounds": merged_mesh.bounds,
                "center": merged_mesh.bounds.mean(axis=0),
                "items": tile_group,
            }
            tile_records.append(tile_record)
            details = stats["tile_details"]
            assert isinstance(details, list)
            details.append(_tile_detail_dict(tile_record, tile_group))
            if (tile_index + 1) % 20 == 0 or tile_index + 1 == len(grouped_items):
                emit_progress(f"Written tiles: {tile_index + 1}/{len(grouped_items)}")
            del merged_mesh
            gc.collect()

        scene_bounds = _bounds_union([record["bounds"] for record in tile_records], np)
        stats["tile_summary"] = _tile_summary([record["byte_size"] for record in tile_records])
        if lod_mode == "lod":
            root = _build_lod_tree(
                tile_records,
                scene_bounds,
                np=np,
                trimesh=trimesh,
                tiles_dir=tiles_dir,
                transform_mode=transform_mode,
                lod_target_ratio=lod_target_ratio,
                lod_min_faces=lod_min_faces,
                depth=0,
                parent_index=None,
                node_counter=[0],
            )
        else:
            root = _build_flat_tileset_tree(tile_records, scene_bounds)
        if transform_mode == "tileset":
            root["transform"] = _y_up_transform_matrix()
        scene_geometric_error = _geometric_error_for_bounds(scene_bounds)
        # 3d-tiles-renderer (TilesRenderer.loadRootTileset) applies an extra +90° about X to
        # each glTF tile when asset.gltfUpAxis is omitted or "Y". Our mesh/tile modes already
        # bake the same IFC Z-up → Y-up fix as py3dtiles’ Z_UP_MATRIX into vertex data
        # (_apply_y_up_transform), and boundingVolume is computed from those same vertices — a
        # second rotation mis-orients the mesh relative to the tileset OBBs. Declaring "Z"
        # skips that branch (no x/y match) so content and bounds stay aligned in three.js.
        # (py3dtiles uses b3dm + the same matrix on the glTF root; it does not omit this step.)
        asset: dict[str, object] = {"version": "1.1"}
        if transform_mode in ("mesh", "tile"):
            asset["gltfUpAxis"] = "Z"
        tileset = {
            "asset": asset,
            "geometricError": scene_geometric_error,
            "root": root,
        }
        _validate_tileset_invariants(tileset)
        (output_dir / "tileset.json").write_text(
            json.dumps(tileset, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
        compression_stats = _maybe_meshopt_pack(tiles_dir, enabled=meshopt, log=emit_progress)
        stats["compression"] = compression_stats
        write_stats()
        emit_progress("Wrote tileset.json")

        elapsed = time.time() - started
        finish_log(0, elapsed)
        if not keep_temp and staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        return ConverterResult(
            ok=True,
            command=command,
            elapsed_seconds=elapsed,
            compression=compression_stats,
        )
    except TimeoutError as exc:
        elapsed = time.time() - started
        finish_log(-2, elapsed, str(exc))
        if not keep_temp and staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        return ConverterResult(False, command, elapsed, "Native converter timed out.")
    except Exception as exc:
        elapsed = time.time() - started
        finish_log(1, elapsed, str(exc))
        if not keep_temp and staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)
        return ConverterResult(False, command, elapsed, f"Native converter failed: {exc}")


def _shape_is_selected(shape_index: int, shape_range: tuple[int, int] | None) -> bool:
    return shape_range is None or shape_range[0] <= shape_index <= shape_range[1]


def _load_shape_metadata_cache(cache_dir: Path, *, np, log) -> list[ShapeMetadata] | None:
    metadata_path = cache_dir / "metadata.json"
    if not metadata_path.exists():
        return None
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"[warn] cache metadata unreadable ({exc}); cache will be rebuilt")
        return None
    if payload.get("schema_version") != CACHE_METADATA_VERSION:
        log("[warn] cache metadata schema mismatch; cache will be rebuilt")
        return None
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return None
    items: list[ShapeMetadata] = []
    try:
        for raw in raw_items:
            if not isinstance(raw, dict):
                return None
            npz_name = raw.get("npz_name")
            if not isinstance(npz_name, str) or not npz_name.endswith(".npz"):
                return None
            staging_path = cache_dir / npz_name
            if not staging_path.exists():
                return None
            bounds_min = np.array(raw.get("bounds_min", [0.0, 0.0, 0.0]), dtype=np.float32)
            bounds_max = np.array(raw.get("bounds_max", [0.0, 0.0, 0.0]), dtype=np.float32)
            centroid = np.array(raw.get("centroid", [0.0, 0.0, 0.0]), dtype=np.float32)
            items.append(
                ShapeMetadata(
                    shape_index=int(raw.get("shape_index")),
                    product_id=_safe_int(raw.get("product_id")),
                    global_id=_safe_str(raw.get("global_id")),
                    ifc_type=_safe_str(raw.get("ifc_type")),
                    triangle_count=int(raw.get("triangle_count", 0)),
                    vertex_count=int(raw.get("vertex_count", 0)),
                    estimated_bytes=int(raw.get("estimated_bytes", 0)),
                    bounds=np.stack([bounds_min, bounds_max], axis=0),
                    centroid=centroid,
                    staging_path=staging_path,
                    has_face_colors=bool(raw.get("has_face_colors", False)),
                )
            )
    except Exception:
        return None
    return items if items else None


def _write_shape_metadata_cache(cache_dir: Path, items: list[ShapeMetadata], *, np, log) -> None:
    if not items:
        return
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": CACHE_METADATA_VERSION,
        "item_count": len(items),
        "items": [
            {
                "shape_index": item.shape_index,
                "product_id": item.product_id,
                "global_id": item.global_id,
                "ifc_type": item.ifc_type,
                "triangle_count": item.triangle_count,
                "vertex_count": item.vertex_count,
                "estimated_bytes": item.estimated_bytes,
                "bounds_min": [float(v) for v in np.asarray(item.bounds[0], dtype=np.float64)],
                "bounds_max": [float(v) for v in np.asarray(item.bounds[1], dtype=np.float64)],
                "centroid": [float(v) for v in np.asarray(item.centroid, dtype=np.float64)],
                "npz_name": item.staging_path.name,
                "has_face_colors": item.has_face_colors,
            }
            for item in items
        ],
    }
    temp_path = cache_dir / "metadata.json.tmp"
    final_path = cache_dir / "metadata.json"
    temp_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    temp_path.replace(final_path)
    log(f"Persistent geometry cache stored: {final_path}")


def _precompute_complexity_skips(
    products: list[object],
    *,
    complexity_filter: dict[str, int],
) -> tuple[set[int], dict[str, int], list[dict[str, object]]]:
    skip_ids: set[int] = set()
    reason_counts = {
        "proxy_body_items": 0,
        "brep_body_items": 0,
        "surface_body_items": 0,
    }
    samples: list[dict[str, object]] = []

    for element in products:
        product_id = _safe_element_id(element)
        if product_id is None:
            continue
        metrics = _body_complexity_metrics(element)
        reason_key = None
        threshold = None

        is_proxy = element.is_a() == "IfcBuildingElementProxy"
        if is_proxy:
            if (
                complexity_filter["proxy_body_items_ge"] > 0
                and metrics["max_body_items"] >= complexity_filter["proxy_body_items_ge"]
            ) or (
                complexity_filter["brep_body_items_ge"] > 0
                and metrics["proxy_underlying_type"] == "Brep"
                and metrics["max_body_items"] >= complexity_filter["brep_body_items_ge"]
            ) or (
                complexity_filter["surface_body_items_ge"] > 0
                and metrics["proxy_underlying_type"] == "SurfaceModel"
                and metrics["max_body_items"] >= complexity_filter["surface_body_items_ge"]
            ):
                reason_key = "proxy_body_items"
                threshold = complexity_filter["proxy_body_items_ge"]
        elif (
            complexity_filter["brep_body_items_ge"] > 0
            and metrics["max_brep_body_items"] >= complexity_filter["brep_body_items_ge"]
        ):
            reason_key = "brep_body_items"
            threshold = complexity_filter["brep_body_items_ge"]
        elif (
            complexity_filter["surface_body_items_ge"] > 0
            and metrics["max_surface_body_items"] >= complexity_filter["surface_body_items_ge"]
        ):
            reason_key = "surface_body_items"
            threshold = complexity_filter["surface_body_items_ge"]

        if reason_key is None:
            continue

        skip_ids.add(product_id)
        reason_counts[reason_key] += 1
        if len(samples) < 80:
            samples.append(
                {
                    "product_id": product_id,
                    "global_id": _safe_str(getattr(element, "GlobalId", None)),
                    "ifc_type": _safe_str(element.is_a()),
                    "reason": reason_key,
                    "threshold": threshold,
                    "max_body_items": metrics["max_body_items"],
                    "max_body_items_direct": metrics["max_body_items_direct"],
                    "max_brep_body_items": metrics["max_brep_body_items"],
                    "max_surface_body_items": metrics["max_surface_body_items"],
                }
            )

    return skip_ids, reason_counts, samples


def _body_complexity_metrics(
    element,
) -> dict[str, int]:
    representation = getattr(element, "Representation", None)
    representations = getattr(representation, "Representations", None) if representation is not None else None
    if not representations:
        return {
            "max_body_items": 0,
            "max_body_items_direct": 0,
            "max_brep_body_items": 0,
            "max_surface_body_items": 0,
            "proxy_underlying_type": None,
        }

    max_body_items = 0
    max_body_items_direct = 0
    max_brep_body_items = 0
    max_surface_body_items = 0
    proxy_underlying_type = None

    # Keep parity with ifc-extract proxy classifier:
    # Body representation is unwrapped once when RepresentationType == MappedRepresentation,
    # and complexity is measured on the underlying representation item count.
    if element.is_a("IfcBuildingElementProxy"):
        body_representation = _find_body_representation(element)
        if body_representation is None:
            return {
                "max_body_items": 0,
                "max_body_items_direct": 0,
                "max_brep_body_items": 0,
                "max_surface_body_items": 0,
            }
        body_items_direct = getattr(body_representation, "Items", None) or []
        underlying_representation, _is_mapped = _unwrap_body_representation(body_representation)
        body_items_underlying = getattr(underlying_representation, "Items", None) or []
        body_item_count_direct = len(body_items_direct)
        body_item_count = len(body_items_underlying)
        underlying_type = _safe_str(getattr(underlying_representation, "RepresentationType", None))
        proxy_underlying_type = underlying_type
        max_body_items = max(max_body_items, body_item_count)
        max_body_items_direct = max(max_body_items_direct, body_item_count_direct)
        if underlying_type == "Brep":
            max_brep_body_items = max(max_brep_body_items, body_item_count)
        if underlying_type == "SurfaceModel":
            max_surface_body_items = max(max_surface_body_items, body_item_count)
    else:
        for rep in representations:
            identifier = _safe_str(getattr(rep, "RepresentationIdentifier", None))
            if not identifier or identifier.lower() != "body":
                continue
            items = getattr(rep, "Items", None) or []
            body_item_count_direct = len(items)
            body_item_count = body_item_count_direct
            max_body_items = max(max_body_items, body_item_count)
            max_body_items_direct = max(max_body_items_direct, body_item_count_direct)
            has_brep = False
            has_surface = False
            for item in items:
                item_type = item.is_a() if hasattr(item, "is_a") else type(item).__name__
                if item_type.endswith("Brep"):
                    has_brep = True
                if item_type in {"IfcFaceBasedSurfaceModel", "IfcShellBasedSurfaceModel"}:
                    has_surface = True
            if has_brep:
                max_brep_body_items = max(max_brep_body_items, body_item_count)
            if has_surface:
                max_surface_body_items = max(max_surface_body_items, body_item_count)

    return {
        "max_body_items": max_body_items,
        "max_body_items_direct": max_body_items_direct,
        "max_brep_body_items": max_brep_body_items,
        "max_surface_body_items": max_surface_body_items,
        "proxy_underlying_type": proxy_underlying_type,
    }


def _find_body_representation(element):
    representation = getattr(element, "Representation", None)
    representations = getattr(representation, "Representations", None) if representation is not None else None
    if not representations:
        return None
    for rep in representations:
        identifier = _safe_str(getattr(rep, "RepresentationIdentifier", None))
        if identifier and identifier.lower() == "body":
            return rep
    return None


def _unwrap_body_representation(body_representation):
    representation_type = _safe_str(getattr(body_representation, "RepresentationType", None))
    if representation_type != "MappedRepresentation":
        return body_representation, False
    items = getattr(body_representation, "Items", None) or []
    for item in items:
        if not item.is_a("IfcMappedItem"):
            continue
        mapping_source = getattr(item, "MappingSource", None)
        mapped_representation = getattr(mapping_source, "MappedRepresentation", None) if mapping_source is not None else None
        if mapped_representation is not None:
            return mapped_representation, True
    return body_representation, False


def _safe_element_id(element) -> int | None:
    try:
        value = element.id()
    except Exception:
        return None
    return value if isinstance(value, int) else None


def _group_items_spatially(items: list[ShapeMetadata], *, target_bytes: float, max_bytes: float, max_triangles: int, np) -> list[list[ShapeMetadata]]:
    if len(items) <= 1:
        return [[item] for item in items]
    bounds = _compute_union_bounds_from_items(items, np)
    mins = bounds[0]
    maxs = bounds[1]
    total_bytes = sum(item.estimated_bytes for item in items)
    target_tile_count = max(8, min(2048, math.ceil(total_bytes / target_bytes)))
    grid_size = max(2, math.ceil(target_tile_count ** (1.0 / 3.0)))
    step = np.maximum((maxs - mins) / grid_size, np.array([1e-6, 1e-6, 1e-6]))
    buckets: dict[tuple[int, int, int], list[ShapeMetadata]] = {}
    for item in items:
        idx = tuple(
            int(min(grid_size - 1, max(0, math.floor((item.centroid[axis] - mins[axis]) / step[axis]))))
            for axis in range(3)
        )
        buckets.setdefault(idx, []).append(item)
    refined: list[list[ShapeMetadata]] = []
    for bucket in buckets.values():
        refined.extend(_split_group_by_budget(bucket, max_bytes=max_bytes, max_triangles=max_triangles, np=np))
    return _merge_small_groups(refined, target_bytes=target_bytes, max_bytes=max_bytes, max_triangles=max_triangles, np=np)


def _split_group_by_budget(group: list[ShapeMetadata], *, max_bytes: float, max_triangles: int, np) -> list[list[ShapeMetadata]]:
    queue = [group]
    output: list[list[ShapeMetadata]] = []
    while queue:
        current = queue.pop()
        if not current:
            continue
        triangles = sum(item.triangle_count for item in current)
        bytes_count = sum(item.estimated_bytes for item in current)
        if triangles <= max_triangles and bytes_count <= max_bytes:
            output.append(current)
            continue
        if len(current) <= 1:
            for item in current:
                output.extend(_split_large_item(item, max_triangles, max_bytes))
            continue
        centers = np.stack([item.centroid for item in current], axis=0)
        axis = int(np.argmax(centers.max(axis=0) - centers.min(axis=0)))
        median = float(np.median(centers[:, axis]))
        left = [item for item in current if item.centroid[axis] <= median]
        right = [item for item in current if item.centroid[axis] > median]
        if not left or not right:
            midpoint = max(1, len(current) // 2)
            left, right = current[:midpoint], current[midpoint:]
        queue.append(left)
        queue.append(right)
    return output


def _split_large_item(item: ShapeMetadata, max_triangles: int, max_bytes: float) -> list[list[ShapeMetadata]]:
    if item.triangle_count <= max_triangles and item.estimated_bytes <= max_bytes:
        return [[item]]
    return [[item]]


def _merge_small_groups(groups: list[list[ShapeMetadata]], *, target_bytes: float, max_bytes: float, max_triangles: int, np) -> list[list[ShapeMetadata]]:
    small_limit = target_bytes * 0.35
    sortable = sorted(groups, key=lambda group: tuple(_group_centroid(group, np)))
    merged: list[list[ShapeMetadata]] = []
    pending: list[ShapeMetadata] = []
    for group in sortable:
        group_bytes = sum(item.estimated_bytes for item in group)
        group_triangles = sum(item.triangle_count for item in group)
        pending_bytes = sum(item.estimated_bytes for item in pending)
        pending_triangles = sum(item.triangle_count for item in pending)
        if group_bytes <= small_limit and pending_bytes + group_bytes <= max_bytes and pending_triangles + group_triangles <= max_triangles:
            pending.extend(group)
            if pending_bytes + group_bytes >= target_bytes:
                merged.append(pending)
                pending = []
        else:
            if pending:
                merged.append(pending)
                pending = []
            merged.append(group)
    if pending:
        merged.append(pending)
    return merged


def _group_centroid(group: list[ShapeMetadata], np):
    return np.mean(np.stack([item.centroid for item in group], axis=0), axis=0)


def _compute_union_bounds_from_items(items: list[ShapeMetadata], np):
    mins = np.min(np.stack([item.bounds[0] for item in items], axis=0), axis=0)
    maxs = np.max(np.stack([item.bounds[1] for item in items], axis=0), axis=0)
    return np.stack([mins, maxs], axis=0)


def _to_box(bounds) -> list[float]:
    mins, maxs = bounds[0], bounds[1]
    center = ((mins + maxs) * 0.5).astype(float)
    half = ((maxs - mins) * 0.5).astype(float)
    return [float(center[0]), float(center[1]), float(center[2]), float(half[0]), 0.0, 0.0, 0.0, float(half[1]), 0.0, 0.0, 0.0, float(half[2])]


def _extract_face_colors(geometry, face_count: int, color_mode: str):
    import numpy as np
    if color_mode in {"none", "uniform"}:
        return None
    material_ids_raw = getattr(geometry, "material_ids", None)
    materials_raw = getattr(geometry, "materials", None)
    if material_ids_raw is None or materials_raw is None or face_count <= 0:
        return None
    try:
        material_ids = np.array(material_ids_raw, dtype=np.int64).reshape(-1)
    except Exception:
        return None
    if material_ids.size == 0 or material_ids.size < face_count:
        return None
    materials = list(materials_raw)
    if not materials:
        return None
    face_material_ids = material_ids[:face_count]
    colors = np.zeros((face_count, 4), dtype=np.uint8)
    colors[:, :] = (180, 180, 180, 255)
    has_color = False
    for material_index in np.unique(face_material_ids):
        material_int = int(material_index)
        rgba = _material_to_rgba(materials[material_int] if 0 <= material_int < len(materials) else None)
        if rgba is not None:
            has_color = True
            colors[face_material_ids == material_int, :] = rgba
    return colors if has_color else None


def _shape_to_mesh(shape, np, trimesh, *, color_mode: str, transform_mode: str):
    geometry = getattr(shape, "geometry", None)
    if geometry is None:
        return None
    arrays_started = time.perf_counter()
    vertices = np.array(getattr(geometry, "verts", []), dtype=np.float32).reshape((-1, 3))
    faces = np.array(getattr(geometry, "faces", []), dtype=np.int64).reshape((-1, 3))
    arrays_ms = (time.perf_counter() - arrays_started) * 1000.0
    if vertices.size == 0 or faces.size == 0:
        return None
    colors_started = time.perf_counter()
    face_colors = _extract_face_colors(geometry, len(faces), color_mode)
    colors_ms = (time.perf_counter() - colors_started) * 1000.0
    mesh_started = time.perf_counter()
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False, face_colors=face_colors)
    mesh_create_ms = (time.perf_counter() - mesh_started) * 1000.0
    transform_ms = 0.0
    if transform_mode == "mesh":
        transform_started = time.perf_counter()
        _apply_y_up_transform(mesh, trimesh)
        transform_ms = (time.perf_counter() - transform_started) * 1000.0
    vertex_count = len(mesh.vertices)
    triangle_count = len(mesh.faces)
    return MeshBuildResult(
        mesh=mesh,
        timings={"vertex_face_array_ms": arrays_ms, "face_colors_ms": colors_ms, "mesh_create_ms": mesh_create_ms, "transform_ms": transform_ms},
        vertex_count=vertex_count,
        triangle_count=triangle_count,
        estimated_bytes=_estimate_mesh_bytes(vertex_count, triangle_count, face_colors is not None),
        has_face_colors=face_colors is not None,
    )


def _apply_y_up_transform(mesh, _trimesh) -> None:
    """IFC world Z-up → glTF Y-up (same 4×4 as py3dtiles IfcTilerWorker.Z_UP_MATRIX_4X4).

    py3dtiles applies this matrix on the embedded glTF root when packing ``b3dm``; the native
    engine bakes it into vertex positions before writing ``.glb`` tiles. Numerically it matches
    ``trimesh.transformations.rotation_matrix(-pi/2, [1, 0, 0])``.
    """
    import numpy as np

    z_up = np.array(
        ((1.0, 0.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, -1.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0)),
        dtype=np.float64,
    )
    mesh.apply_transform(z_up)


def _estimate_mesh_bytes(vertex_count: int, triangle_count: int, has_face_colors: bool) -> int:
    return vertex_count * 3 * 4 + triangle_count * 3 * 4 + (triangle_count * 4 if has_face_colors else 0)


def _make_shape_metadata(
    result: MeshBuildResult,
    shape_index: int,
    shape_info: dict[str, object],
    *,
    staging_dir: Path,
    np,
) -> ShapeMetadata:
    mesh = result.mesh
    mesh.metadata.update({"shape_index": shape_index, "product_id": shape_info.get("product_id"), "global_id": shape_info.get("global_id"), "ifc_type": shape_info.get("ifc_type")})
    staging_path = staging_dir / f"shape_{shape_index:08d}.npz"
    _write_mesh_staging(mesh, staging_path, np=np)
    return ShapeMetadata(
        shape_index=shape_index,
        product_id=_safe_int(shape_info.get("product_id")),
        global_id=_safe_str(shape_info.get("global_id")),
        ifc_type=_safe_str(shape_info.get("ifc_type")),
        triangle_count=result.triangle_count,
        vertex_count=result.vertex_count,
        estimated_bytes=result.estimated_bytes,
        bounds=mesh.bounds,
        centroid=mesh.bounds.mean(axis=0),
        staging_path=staging_path,
        has_face_colors=result.has_face_colors,
    )


def _shape_info(model, shape) -> dict[str, object]:
    global_id = _safe_str(getattr(shape, "guid", None))
    product = None
    if global_id:
        try:
            product = model.by_guid(global_id)
        except Exception:
            product = None
    info = _element_info(product)
    if global_id and not info.get("global_id"):
        info["global_id"] = global_id
    return info


def _element_info(element) -> dict[str, object]:
    if element is None:
        return {"product_id": None, "global_id": None, "ifc_type": None}
    try:
        product_id = element.id()
    except Exception:
        product_id = None
    try:
        ifc_type = element.is_a()
    except Exception:
        ifc_type = type(element).__name__
    return {"product_id": product_id, "global_id": _safe_str(getattr(element, "GlobalId", None)), "ifc_type": ifc_type}


def _shape_stat_dict(item: ShapeMetadata, timings: dict[str, float], rss_mb: float) -> dict[str, object]:
    return {"shape_index": item.shape_index, "product_id": item.product_id, "global_id": item.global_id, "ifc_type": item.ifc_type, "triangle_count": item.triangle_count, "vertex_count": item.vertex_count, "estimated_mb": round(item.estimated_bytes / BYTES_PER_MB, 3), "bbox_min": [float(v) for v in item.bounds[0]], "bbox_max": [float(v) for v in item.bounds[1]], "rss_mb": round(rss_mb, 1), **{key: round(value, 3) for key, value in timings.items()}}


def _tile_detail_dict(tile_record: dict, tile_group: list[ShapeMetadata]) -> dict[str, object]:
    return {"tile_index": tile_record["tile_index"], "uri": tile_record["uri"], "byte_size": tile_record["byte_size"], "size_mb": round(tile_record["byte_size"] / BYTES_PER_MB, 3), "triangle_count": tile_record["triangle_count"], "vertex_count": tile_record["vertex_count"], "shape_count": tile_record["shape_count"], "bbox_min": [float(v) for v in tile_record["bounds"][0]], "bbox_max": [float(v) for v in tile_record["bounds"][1]], "source_shapes": [{"shape_index": item.shape_index, "product_id": item.product_id, "ifc_type": item.ifc_type, "triangles": item.triangle_count} for item in sorted(tile_group, key=lambda value: value.triangle_count, reverse=True)[:20]]}


def _leaf_tile_node(tile_record: dict) -> dict:
    return {
        "boundingVolume": {"box": _to_box(tile_record["bounds"])},
        "geometricError": 0,
        "content": {"uri": tile_record["uri"]},
        "refine": "REPLACE",
        "extras": {
            "tileIndex": tile_record["tile_index"],
            "byteSize": tile_record["byte_size"],
            "triangleCount": tile_record["triangle_count"],
            "shapeCount": tile_record["shape_count"],
        },
    }


def _build_flat_tileset_tree(tile_records: list[dict], bounds) -> dict:
    return {
        "boundingVolume": {"box": _to_box(bounds)},
        "geometricError": _geometric_error_for_bounds(bounds),
        "refine": "ADD",
        "children": [_leaf_tile_node(record) for record in tile_records],
    }


def _build_lod_tree(
    tile_records: list[dict],
    bounds,
    *,
    np,
    trimesh,
    tiles_dir: Path,
    transform_mode: str,
    lod_target_ratio: float,
    lod_min_faces: int,
    depth: int = 0,
    parent_index: list[int] | None = None,
    node_counter: list[int] | None = None,
) -> dict:
    del parent_index
    if node_counter is None:
        node_counter = [0]
    if len(tile_records) == 1:
        return _leaf_tile_node(tile_records[0])

    node_bounds = _bounds_union([record["bounds"] for record in tile_records], np)
    should_split = len(tile_records) > LOD_MAX_CHILDREN and depth < LOD_MAX_DEPTH
    children: list[dict]
    if should_split:
        centers = np.stack([record["center"] for record in tile_records], axis=0)
        axis = int(np.argmax(centers.max(axis=0) - centers.min(axis=0)))
        median = float(np.median(centers[:, axis]))
        left = [record for record in tile_records if record["center"][axis] <= median]
        right = [record for record in tile_records if record["center"][axis] > median]
        if not left or not right:
            midpoint = max(1, len(tile_records) // 2)
            left, right = tile_records[:midpoint], tile_records[midpoint:]
        children = [
            _build_lod_tree(
                child_records,
                _bounds_union([record["bounds"] for record in child_records], np),
                np=np,
                trimesh=trimesh,
                tiles_dir=tiles_dir,
                transform_mode=transform_mode,
                lod_target_ratio=lod_target_ratio,
                lod_min_faces=lod_min_faces,
                depth=depth + 1,
                parent_index=None,
                node_counter=node_counter,
            )
            for child_records in (left, right)
            if child_records
        ]
    else:
        children = [_leaf_tile_node(record) for record in tile_records]

    node = {
        "boundingVolume": {"box": _to_box(node_bounds)},
        "geometricError": _lod_node_geometric_error(
            node_bounds=node_bounds,
            tile_records=tile_records,
            children=children,
            depth=depth,
        ),
        "refine": "REPLACE",
        "children": children,
    }
    # Parent tiles carry no coarse mesh content.
    # This keeps LOD as pure hierarchy-over-leaves to avoid white-shell loading.
    return node


def _validate_tileset_invariants(tileset: dict) -> None:
    root = tileset.get("root")
    if not isinstance(root, dict):
        raise RuntimeError("Tileset root is missing or malformed.")
    if not (isinstance(root.get("geometricError"), (int, float)) and root["geometricError"] > 0):
        raise RuntimeError("Tileset root.geometricError must be > 0 to allow renderer traversal.")
    children = root.get("children") or []
    if not children:
        raise RuntimeError("Tileset root must have at least one child tile with content.")

    def walk(node: dict, depth: int) -> None:
        has_content = "content" in node
        sub_children = node.get("children") or []
        if not has_content and not sub_children:
            raise RuntimeError(f"Tile at depth={depth} has neither content nor children.")
        for child in sub_children:
            walk(child, depth + 1)

    walk(root, depth=0)


def _bounds_union(bounds_list: list, np):
    mins = np.min(np.stack([bounds[0] for bounds in bounds_list], axis=0), axis=0)
    maxs = np.max(np.stack([bounds[1] for bounds in bounds_list], axis=0), axis=0)
    return np.stack([mins, maxs], axis=0)


def _write_mesh_staging(mesh, path: Path, *, np) -> None:
    face_colors = getattr(getattr(mesh, "visual", None), "face_colors", None)
    if face_colors is not None and len(face_colors) == len(mesh.faces):
        np.savez_compressed(
            path,
            vertices=np.asarray(mesh.vertices, dtype=np.float32),
            faces=np.asarray(mesh.faces, dtype=np.int64),
            face_colors=np.asarray(face_colors, dtype=np.uint8),
        )
    else:
        np.savez_compressed(
            path,
            vertices=np.asarray(mesh.vertices, dtype=np.float32),
            faces=np.asarray(mesh.faces, dtype=np.int64),
        )


def _load_shape_mesh(item: ShapeMetadata, *, np, trimesh):
    data = np.load(item.staging_path, allow_pickle=False)
    face_colors = data["face_colors"] if "face_colors" in data.files else None
    return trimesh.Trimesh(
        vertices=data["vertices"],
        faces=data["faces"],
        face_colors=face_colors,
        process=False,
    )


def _hydrate_tile_group(group: list[ShapeMetadata], *, np, trimesh):
    meshes = [_load_shape_mesh(item, np=np, trimesh=trimesh) for item in group]
    merged = trimesh.util.concatenate(meshes)
    del meshes
    return merged


def _write_lod_content(
    items: list[ShapeMetadata],
    *,
    np,
    trimesh,
    tiles_dir: Path,
    transform_mode: str,
    depth: int,
    node_counter: list[int],
    lod_target_ratio: float,
    lod_min_faces: int,
) -> str | None:
    if not items:
        return None
    selected_items = _select_lod_items_for_coarse_content(items, depth=depth)
    meshes = [_load_shape_mesh(item, np=np, trimesh=trimesh) for item in selected_items]
    merged_mesh = trimesh.util.concatenate(meshes)
    del meshes
    if transform_mode == "tile":
        _apply_y_up_transform(merged_mesh, trimesh)
    original_has_face_colors = _mesh_has_face_colors(merged_mesh)
    dominant_rgba = _mesh_dominant_face_rgba(merged_mesh)
    try:
        merged_mesh.merge_vertices()
    except Exception:
        pass
    face_count = len(merged_mesh.faces)
    ratio = min(0.98, max(0.20, lod_target_ratio * (1.0 + depth * 0.2)))
    if depth == 0:
        ratio = max(ratio, LOD_MIN_ROOT_RATIO)
    target_faces = max(lod_min_faces, int(face_count * ratio))
    if depth == 0:
        target_faces = max(target_faces, min(face_count, LOD_MIN_ROOT_FACES))
    if face_count > target_faces and face_count >= lod_min_faces:
        try:
            simplified = merged_mesh.simplify_quadric_decimation(face_count=target_faces)
            if simplified is not None and len(simplified.faces) > 0:
                simplified_has_colors = _mesh_has_face_colors(simplified)
                if not (original_has_face_colors and not simplified_has_colors):
                    merged_mesh = simplified
        except Exception:
            pass
    if not _mesh_has_face_colors(merged_mesh):
        fallback_rgba = dominant_rgba if dominant_rgba is not None else LOD_DEFAULT_COARSE_RGBA
        _paint_mesh_uniform_face_color(merged_mesh, fallback_rgba, np=np, trimesh=trimesh)
    node_index = node_counter[0]
    node_counter[0] += 1
    lod_name = f"lod_{depth}_{node_index:05d}.glb"
    lod_path = tiles_dir / lod_name
    lod_path.write_bytes(merged_mesh.export(file_type="glb"))
    del merged_mesh
    gc.collect()
    return f"tiles/{lod_name}"


def _maybe_meshopt_pack(tiles_dir: Path, *, enabled: bool, log) -> dict[str, object]:
    if not enabled:
        return {"skipped": True}
    if shutil.which("gltfpack") is None:
        log("[warn] gltfpack not found, skipping meshopt compression")
        return {"missing_tool": True}
    before = sum(path.stat().st_size for path in tiles_dir.glob("*.glb"))
    for glb in sorted(tiles_dir.glob("*.glb")):
        subprocess.run(
            ["gltfpack", "-i", str(glb), "-o", str(glb), "-cc", "-kn"],
            check=True,
            capture_output=True,
        )
    after = sum(path.stat().st_size for path in tiles_dir.glob("*.glb"))
    ratio = (after / before * 100.0) if before > 0 else 0.0
    log(f"[ok] meshopt: {before / BYTES_PER_MB:.1f}MB -> {after / BYTES_PER_MB:.1f}MB ({ratio:.0f}%)")
    return {"before_bytes": before, "after_bytes": after}


def _assert_staging_disk_space(input_ifc: Path, parent_dir: Path) -> None:
    expected = int(max(1, input_ifc.stat().st_size) * 1.5)
    free_bytes = shutil.disk_usage(parent_dir).free
    if free_bytes < expected:
        raise RuntimeError(
            "Not enough disk space for streaming staging cache. "
            f"Need >= {expected / BYTES_PER_MB:.1f}MB free near {parent_dir}."
        )


def _lod_node_geometric_error(*, node_bounds, tile_records: list[dict], children: list[dict], depth: int) -> float:
    base_error = _geometric_error_for_bounds(node_bounds) / max(1.0, depth + 1.0)
    total_triangles = max(0, sum(int(record.get("triangle_count", 0)) for record in tile_records))
    # Keep dense small regions refinable without forcing full-tree refinement.
    complexity_error = math.sqrt(float(max(1, total_triangles))) / 170.0
    child_error = max(
        (float(child.get("geometricError", 0.0)) for child in children if isinstance(child, dict)),
        default=0.0,
    )
    return max(1.0, base_error, complexity_error, child_error + 0.25)


def _lod_triangle_budget(depth: int) -> int:
    max_depth_key = max(LOD_COARSE_TRIANGLE_BUDGET)
    key = min(max(0, depth), max_depth_key)
    return LOD_COARSE_TRIANGLE_BUDGET.get(key, LOD_COARSE_TRIANGLE_BUDGET[max_depth_key])


def _select_lod_items_for_coarse_content(items: list[ShapeMetadata], *, depth: int) -> list[ShapeMetadata]:
    if not items:
        return items
    if depth == 0:
        # Root coarse tile should represent the whole model envelope.
        return items
    if depth > LOD_MAX_BUDGET_DEPTH:
        # Keep full coverage on deeper nodes; aggressive budgeting here can hide
        # large parts of buildings when parent coarse tiles are visible.
        return items
    budget = _lod_triangle_budget(depth)
    total = sum(item.triangle_count for item in items)
    if total <= budget:
        return items

    selected: list[ShapeMetadata] = []
    running = 0
    for item in sorted(items, key=lambda entry: entry.triangle_count, reverse=True):
        if running >= budget and len(selected) >= LOD_MIN_BUDGETED_ITEMS:
            break
        selected.append(item)
        running += item.triangle_count
    return selected or [max(items, key=lambda entry: entry.triangle_count)]


def _mesh_has_face_colors(mesh) -> bool:
    face_colors = getattr(getattr(mesh, "visual", None), "face_colors", None)
    return face_colors is not None and len(face_colors) == len(mesh.faces)


def _mesh_dominant_face_rgba(mesh) -> tuple[int, int, int, int] | None:
    face_colors = getattr(getattr(mesh, "visual", None), "face_colors", None)
    if face_colors is None or len(face_colors) == 0:
        return None
    try:
        color = face_colors[:, :4].mean(axis=0)
        return (
            int(max(0, min(255, round(float(color[0]))))),
            int(max(0, min(255, round(float(color[1]))))),
            int(max(0, min(255, round(float(color[2]))))),
            int(max(0, min(255, round(float(color[3]))))),
        )
    except Exception:
        return None


def _paint_mesh_uniform_face_color(mesh, rgba: tuple[int, int, int, int], *, np, trimesh) -> None:
    if len(mesh.faces) == 0:
        return
    colors = np.tile(np.array([list(rgba)], dtype=np.uint8), (len(mesh.faces), 1))
    mesh.visual = trimesh.visual.ColorVisuals(mesh=mesh, face_colors=colors)


def _geometric_error_for_bounds(bounds) -> float:
    return max(1.0, math.dist([float(v) for v in bounds[0]], [float(v) for v in bounds[1]]) / 24.0)


def _y_up_transform_matrix() -> list[float]:
    return [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0]


def _tile_summary(sizes: list[int]) -> dict[str, object]:
    if not sizes:
        return {}
    sorted_sizes = sorted(sizes)
    return {"count": len(sorted_sizes), "total_mb": round(sum(sorted_sizes) / BYTES_PER_MB, 3), "min_mb": round(sorted_sizes[0] / BYTES_PER_MB, 3), "p25_mb": round(_percentile(sorted_sizes, 0.25) / BYTES_PER_MB, 3), "median_mb": round(statistics.median(sorted_sizes) / BYTES_PER_MB, 3), "p75_mb": round(_percentile(sorted_sizes, 0.75) / BYTES_PER_MB, 3), "p90_mb": round(_percentile(sorted_sizes, 0.90) / BYTES_PER_MB, 3), "p95_mb": round(_percentile(sorted_sizes, 0.95) / BYTES_PER_MB, 3), "max_mb": round(sorted_sizes[-1] / BYTES_PER_MB, 3), "histogram": {"lt_100kb": sum(size < 100 * 1024 for size in sorted_sizes), "100kb_1mb": sum(100 * 1024 <= size < BYTES_PER_MB for size in sorted_sizes), "1mb_8mb": sum(BYTES_PER_MB <= size < 8 * BYTES_PER_MB for size in sorted_sizes), "8mb_20mb": sum(8 * BYTES_PER_MB <= size < 20 * BYTES_PER_MB for size in sorted_sizes), "gte_20mb": sum(size >= 20 * BYTES_PER_MB for size in sorted_sizes)}}


def _percentile(values: list[int], fraction: float) -> float:
    index = min(len(values) - 1, max(0, int(round((len(values) - 1) * fraction))))
    return float(values[index])


def _push_top(container: object, item: dict[str, object], key: str, limit: int) -> None:
    assert isinstance(container, list)
    value = item.get(key)
    if not isinstance(value, (int, float)):
        return
    container.append(item)
    container.sort(key=lambda entry: float(entry.get(key, 0)), reverse=True)
    del container[limit:]


def _current_rss_mb() -> float:
    try:
        import resource
        rss = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return rss / (1024 * 1024) if rss > 10_000_000 else rss / 1024
    except Exception:
        return 0.0


def _safe_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _safe_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _json_safe(value):
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return str(value)
    return value


def _material_to_rgba(material) -> tuple[int, int, int, int] | None:
    if material is None:
        return None
    rgb = _extract_rgb(material)
    if rgb is None:
        return None
    transparency = getattr(material, "transparency", 0.0)
    alpha = int(max(0.0, min(1.0, 1.0 - float(transparency))) * 255)
    return (int(max(0.0, min(1.0, rgb[0])) * 255), int(max(0.0, min(1.0, rgb[1])) * 255), int(max(0.0, min(1.0, rgb[2])) * 255), alpha)


def _extract_rgb(material) -> tuple[float, float, float] | None:
    for attr in ["diffuse", "surface_colour", "surface_color", "color", "diffuse_colour", "diffuse_color"]:
        if hasattr(material, attr):
            rgb = _coerce_rgb(getattr(material, attr))
            if rgb is not None:
                return rgb
    return None


def _coerce_rgb(value) -> tuple[float, float, float] | None:
    if isinstance(value, (tuple, list)) and len(value) >= 3:
        parts = [_safe_to_float(value[i]) for i in range(3)]
        return None if any(part is None for part in parts) else (parts[0], parts[1], parts[2])
    for names in [("r", "g", "b"), ("x", "y", "z")]:
        parts = [_safe_to_float(getattr(value, name, None)) for name in names]
        if all(part is not None for part in parts):
            return (parts[0], parts[1], parts[2])
    return None


def _safe_to_float(value):
    if value is None:
        return None
    if callable(value):
        try:
            value = value()
        except Exception:
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
