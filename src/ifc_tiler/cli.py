from __future__ import annotations

import argparse
from pathlib import Path

from ifc_tiler.config import ConvertConfig, EXIT_OK
from ifc_tiler.pipeline import run_convert


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

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command != "convert":
        parser.print_help()
        return EXIT_OK

    job_name = args.job_name or args.ifc_path.stem
    config = ConvertConfig(
        input_ifc=args.ifc_path,
        out_dir=args.out_dir,
        job_name=job_name,
        overwrite=args.overwrite,
        keep_temp=args.keep_temp,
        timeout_min=args.timeout_min,
    )
    return run_convert(config)


if __name__ == "__main__":
    raise SystemExit(main())
