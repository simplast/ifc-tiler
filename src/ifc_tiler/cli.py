from __future__ import annotations

import argparse
from pathlib import Path

from ifc_tiler.config import ConvertConfig, EXIT_INPUT_INVALID, EXIT_OK, EXIT_OUTPUT_CONFLICT
from ifc_tiler.ifc_preclean import preclean_ifc_for_py3dtiles
from ifc_tiler.pipeline import run_convert


def _parse_shape_range(value: str) -> tuple[int, int]:
    try:
        start_text, end_text = value.split(":", 1)
        start = int(start_text)
        end = int(end_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("shape range must be START:END") from exc
    if start < 1 or end < start:
        raise argparse.ArgumentTypeError("shape range must satisfy 1 <= START <= END")
    return (start, end)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ifc-tiler",
        description="Convert IFC files into a 3D Tiles output directory.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    convert_parser = subparsers.add_parser("convert", help="Convert a single IFC file.")
    convert_parser.add_argument("ifc_path", type=Path, help="Absolute or relative IFC path.")
    convert_parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("./outputs"),
        help="Output root directory. Default: ./outputs",
    )
    convert_parser.add_argument(
        "--job-name",
        type=str,
        default=None,
        help="Output job name. Default: IFC filename stem.",
    )
    convert_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing output directory if present.",
    )
    convert_parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep temporary files for troubleshooting.",
    )
    convert_parser.add_argument(
        "--timeout-min",
        type=int,
        default=120,
        help="Converter timeout in minutes. Default: 120",
    )
    convert_parser.add_argument(
        "--diagnostics",
        action="store_true",
        help="Write detailed stats.json with slow shape and tile diagnostics.",
    )
    convert_parser.add_argument(
        "--color-mode",
        choices=("none", "uniform", "materials"),
        default="materials",
        help="Color extraction mode. Default: materials",
    )
    convert_parser.add_argument(
        "--no-face-colors",
        action="store_true",
        help="Shortcut for --color-mode none.",
    )
    convert_parser.add_argument(
        "--transform-mode",
        choices=("mesh", "tile", "tileset", "none"),
        default="mesh",
        help="Where to apply IFC Z-up to glTF Y-up correction. Default: mesh",
    )
    convert_parser.add_argument(
        "--no-y-up-transform",
        action="store_true",
        help="Shortcut for --transform-mode none.",
    )
    convert_parser.add_argument(
        "--max-shapes",
        type=int,
        default=None,
        help="Stop after converting this many selected shapes.",
    )
    convert_parser.add_argument(
        "--shape-range",
        type=_parse_shape_range,
        default=None,
        metavar="START:END",
        help="Only build meshes for this 1-based iterator shape range.",
    )
    convert_parser.add_argument(
        "--tile-target-mb",
        type=float,
        default=4.0,
        help="Target GLB tile size in MB before merging small groups. Default: 4",
    )
    convert_parser.add_argument(
        "--tile-max-mb",
        type=float,
        default=20.0,
        help="Soft maximum GLB tile size in MB. Default: 20",
    )
    convert_parser.add_argument(
        "--max-tile-triangles",
        type=int,
        default=180_000,
        help="Maximum triangles per leaf tile before splitting. Default: 180000",
    )
    convert_parser.add_argument(
        "--lod-mode",
        choices=("flat", "lod"),
        default="flat",
        help="Tileset structure mode. flat keeps a single-level tree; lod builds hierarchical coarse content.",
    )
    convert_parser.add_argument(
        "--lod-target-ratio",
        type=float,
        default=0.1,
        help="Base triangle ratio kept for generated LOD coarse meshes. Default: 0.1",
    )
    convert_parser.add_argument(
        "--lod-min-faces",
        type=int,
        default=2_000,
        help="Skip decimation for merged meshes with fewer faces than this threshold. Default: 2000",
    )
    convert_parser.add_argument(
        "--meshopt",
        action="store_true",
        help="Run gltfpack meshopt compression on exported GLBs if gltfpack is installed.",
    )
    convert_parser.add_argument(
        "--skip-proxy-body-items-ge",
        type=int,
        default=500,
        help="Skip IfcBuildingElementProxy before meshing when Body item count is >= this threshold. Set <=0 to disable. Default: 500",
    )
    convert_parser.add_argument(
        "--skip-brep-body-items-ge",
        type=int,
        default=50,
        help="Skip products before meshing when a Body rep contains Brep and Body item count is >= this threshold. Set <=0 to disable. Default: 50",
    )
    convert_parser.add_argument(
        "--skip-surface-body-items-ge",
        type=int,
        default=50,
        help="Skip products before meshing when a Body rep contains SurfaceModel and Body item count is >= this threshold. Set <=0 to disable. Default: 50",
    )
    convert_parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable persistent native geometry cache reuse.",
    )
    convert_parser.add_argument(
        "--rebuild-cache",
        action="store_true",
        help="Rebuild persistent native geometry cache for this IFC/options key.",
    )
    convert_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Optional cache base directory (default: <out-dir>/.cache/native-shapes).",
    )

    py3dtiles_parser = subparsers.add_parser(
        "convert-py3dtiles",
        help="Convert a single IFC file with py3dtiles backend.",
    )
    py3dtiles_parser.add_argument("ifc_path", type=Path, help="Absolute or relative IFC path.")
    py3dtiles_parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("./outputs"),
        help="Output root directory. Default: ./outputs",
    )
    py3dtiles_parser.add_argument(
        "--job-name",
        type=str,
        default=None,
        help="Output job name. Default: IFC filename stem.",
    )
    py3dtiles_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing output directory if present.",
    )
    py3dtiles_parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep temporary files for troubleshooting.",
    )
    py3dtiles_parser.add_argument(
        "--timeout-min",
        type=int,
        default=120,
        help="Converter timeout in minutes. Default: 120",
    )
    py3dtiles_parser.add_argument(
        "--skip-proxy-body-items-ge",
        type=int,
        default=500,
        help="Preclean: drop IfcBuildingElementProxy when Body item count is >= this threshold. Set <=0 to disable. Default: 500",
    )
    py3dtiles_parser.add_argument(
        "--skip-brep-body-items-ge",
        type=int,
        default=50,
        help="Preclean: drop products when a Body rep contains Brep and Body item count is >= this threshold. Set <=0 to disable. Default: 50",
    )
    py3dtiles_parser.add_argument(
        "--skip-surface-body-items-ge",
        type=int,
        default=50,
        help="Preclean: drop products when a Body rep contains SurfaceModel and Body item count is >= this threshold. Set <=0 to disable. Default: 50",
    )

    preclean_parser = subparsers.add_parser(
        "preclean",
        help="Preclean IFC and write a cleaned IFC file.",
    )
    preclean_parser.add_argument("ifc_path", type=Path, help="Absolute or relative IFC path.")
    preclean_parser.add_argument(
        "output_ifc",
        type=Path,
        help="Output path for precleaned IFC.",
    )
    preclean_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite output_ifc if it already exists.",
    )
    preclean_parser.add_argument(
        "--skip-proxy-body-items-ge",
        type=int,
        default=500,
        help="Drop IfcBuildingElementProxy when Body item count is >= this threshold. Set <=0 to disable. Default: 500",
    )
    preclean_parser.add_argument(
        "--skip-brep-body-items-ge",
        type=int,
        default=50,
        help="Drop products when a Body rep contains Brep and Body item count is >= this threshold. Set <=0 to disable. Default: 50",
    )
    preclean_parser.add_argument(
        "--skip-surface-body-items-ge",
        type=int,
        default=50,
        help="Drop products when a Body rep contains SurfaceModel and Body item count is >= this threshold. Set <=0 to disable. Default: 50",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command not in {"convert", "convert-py3dtiles", "preclean"}:
        parser.print_help()
        return EXIT_OK

    if args.command == "preclean":
        input_ifc = args.ifc_path.expanduser().resolve()
        output_ifc = args.output_ifc.expanduser().resolve()
        if not input_ifc.exists() or not input_ifc.is_file():
            print(f"[error] IFC file not found: {input_ifc}")
            return EXIT_INPUT_INVALID
        if input_ifc.suffix.lower() != ".ifc":
            print(f"[error] Input must be an .ifc file: {input_ifc}")
            return EXIT_INPUT_INVALID
        if output_ifc.exists() and not args.overwrite:
            print(
                f"[error] Output already exists: {output_ifc} "
                "(use --overwrite to replace)"
            )
            return EXIT_OUTPUT_CONFLICT
        result = preclean_ifc_for_py3dtiles(
            input_ifc=input_ifc,
            output_ifc=output_ifc,
            skip_proxy_body_items_ge=args.skip_proxy_body_items_ge,
            skip_brep_body_items_ge=args.skip_brep_body_items_ge,
            skip_surface_body_items_ge=args.skip_surface_body_items_ge,
        )
        print(f"[ok] Precleaned IFC: {result.cleaned_ifc}")
        print(
            "[ok] Removed products: "
            f"{result.removed_total} "
            f"(no-body={result.removed_no_body_representation}, "
            f"empty-body={result.removed_empty_body_items}, "
            f"proxy={result.removed_proxy_body_items}, "
            f"brep={result.removed_brep_body_items}, "
            f"surface={result.removed_surface_body_items})"
        )
        return EXIT_OK

    job_name = args.job_name or args.ifc_path.stem
    if args.command == "convert":
        color_mode = "none" if args.no_face_colors else args.color_mode
        transform_mode = "none" if args.no_y_up_transform else args.transform_mode
        backend = "native"
        max_shapes = args.max_shapes
        shape_range = args.shape_range
        tile_target_mb = args.tile_target_mb
        tile_max_mb = args.tile_max_mb
        max_tile_triangles = args.max_tile_triangles
        lod_mode = args.lod_mode
        lod_target_ratio = args.lod_target_ratio
        lod_min_faces = args.lod_min_faces
        meshopt = args.meshopt
        diagnostics = args.diagnostics
        skip_proxy_body_items_ge = args.skip_proxy_body_items_ge
        skip_brep_body_items_ge = args.skip_brep_body_items_ge
        skip_surface_body_items_ge = args.skip_surface_body_items_ge
        use_cache = not args.no_cache
        rebuild_cache = args.rebuild_cache
        cache_dir = args.cache_dir
    else:
        color_mode = "materials"
        transform_mode = "mesh"
        backend = "py3dtiles"
        max_shapes = None
        shape_range = None
        tile_target_mb = 4.0
        tile_max_mb = 20.0
        max_tile_triangles = 180_000
        lod_mode = "flat"
        lod_target_ratio = 0.1
        lod_min_faces = 2_000
        meshopt = False
        diagnostics = False
        skip_proxy_body_items_ge = args.skip_proxy_body_items_ge
        skip_brep_body_items_ge = args.skip_brep_body_items_ge
        skip_surface_body_items_ge = args.skip_surface_body_items_ge
        use_cache = False
        rebuild_cache = False
        cache_dir = None

    config = ConvertConfig(
        input_ifc=args.ifc_path,
        out_dir=args.out_dir,
        job_name=job_name,
        overwrite=args.overwrite,
        keep_temp=args.keep_temp,
        timeout_min=args.timeout_min,
        diagnostics=diagnostics,
        color_mode=color_mode,
        transform_mode=transform_mode,
        max_shapes=max_shapes,
        shape_range=shape_range,
        tile_target_mb=tile_target_mb,
        tile_max_mb=tile_max_mb,
        max_tile_triangles=max_tile_triangles,
        lod_mode=lod_mode,
        lod_target_ratio=lod_target_ratio,
        lod_min_faces=lod_min_faces,
        meshopt=meshopt,
        skip_proxy_body_items_ge=skip_proxy_body_items_ge,
        skip_brep_body_items_ge=skip_brep_body_items_ge,
        skip_surface_body_items_ge=skip_surface_body_items_ge,
        use_cache=use_cache,
        rebuild_cache=rebuild_cache,
        cache_dir=cache_dir,
        backend=backend,
    )
    return run_convert(config)


if __name__ == "__main__":
    raise SystemExit(main())
