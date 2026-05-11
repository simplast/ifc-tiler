"""Patch an existing tileset.json so that it loads correctly in 3d-tiles-renderer.

This is a one-off fixer for outputs that were written before
`external_converter._build_tileset_tree` was changed to emit a flat single-level
hierarchy. It does two things:

1. Promote every descendant tile that has `content.uri` to be a direct child of root,
   discarding the empty-content internal nodes that the old recursive splitter
   produced. This guarantees there are no internal nodes that the renderer would
   stop traversal at without rendering anything.
2. Recompute root.geometricError (and the outer tileset.geometricError) from the
   axis-aligned bounding box of all leaves. Any value <= 0 makes the renderer
   refuse to descend past root, which was the root cause of "model invisible
   except at very specific zoom angles".

Usage:
    python scripts/flatten_tileset.py outputs/td/tileset.json
    python scripts/flatten_tileset.py outputs/td/tileset.json --in-place
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Iterator


def collect_leaves(node: dict) -> Iterator[dict]:
    if "content" in node and isinstance(node.get("content"), dict):
        yield node
        return
    for child in node.get("children", []) or []:
        if isinstance(child, dict):
            yield from collect_leaves(child)


def box_to_aabb(box: list[float]) -> tuple[list[float], list[float]]:
    cx, cy, cz = box[0:3]
    hx = abs(box[3]) + abs(box[6]) + abs(box[9])
    hy = abs(box[4]) + abs(box[7]) + abs(box[10])
    hz = abs(box[5]) + abs(box[8]) + abs(box[11])
    return [cx - hx, cy - hy, cz - hz], [cx + hx, cy + hy, cz + hz]


def union_bounds_from_leaves(leaves: list[dict]) -> tuple[list[float], list[float]]:
    mins = [math.inf, math.inf, math.inf]
    maxs = [-math.inf, -math.inf, -math.inf]
    for leaf in leaves:
        box = leaf.get("boundingVolume", {}).get("box")
        if not isinstance(box, list) or len(box) != 12:
            continue
        lo, hi = box_to_aabb(box)
        for i in range(3):
            if lo[i] < mins[i]:
                mins[i] = lo[i]
            if hi[i] > maxs[i]:
                maxs[i] = hi[i]
    if any(v == math.inf for v in mins) or any(v == -math.inf for v in maxs):
        raise RuntimeError("Could not derive scene bounds from any leaf boundingVolume.box.")
    return mins, maxs


def aabb_to_box(mins: list[float], maxs: list[float]) -> list[float]:
    cx, cy, cz = [(mins[i] + maxs[i]) * 0.5 for i in range(3)]
    hx, hy, hz = [(maxs[i] - mins[i]) * 0.5 for i in range(3)]
    return [
        cx, cy, cz,
        hx, 0.0, 0.0,
        0.0, hy, 0.0,
        0.0, 0.0, hz,
    ]


def geometric_error_for_bounds(mins: list[float], maxs: list[float]) -> float:
    diagonal = math.dist(mins, maxs)
    return max(1.0, diagonal / 24.0)


def flatten(tileset: dict) -> dict:
    root = tileset.get("root")
    if not isinstance(root, dict):
        raise RuntimeError("tileset.json missing 'root'.")

    leaves = list(collect_leaves(root))
    if not leaves:
        raise RuntimeError("No leaf tiles with content.uri found; nothing to flatten.")

    mins, maxs = union_bounds_from_leaves(leaves)
    scene_box = aabb_to_box(mins, maxs)
    scene_geometric_error = geometric_error_for_bounds(mins, maxs)

    new_root = {
        "boundingVolume": {"box": scene_box},
        "geometricError": scene_geometric_error,
        "refine": "ADD",
        "children": leaves,
    }
    transform = root.get("transform")
    if transform is not None:
        new_root["transform"] = transform

    return {
        **tileset,
        "geometricError": scene_geometric_error,
        "root": new_root,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tileset", type=Path, help="Path to tileset.json to patch.")
    parser.add_argument("--in-place", action="store_true", help="Overwrite the input file.")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output path. Defaults to <input>.flat.json unless --in-place is given.",
    )
    args = parser.parse_args()

    raw = json.loads(args.tileset.read_text(encoding="utf-8"))
    flattened = flatten(raw)

    if args.in_place:
        target = args.tileset
    else:
        target = args.output or args.tileset.with_suffix(".flat.json")
    target.write_text(json.dumps(flattened, ensure_ascii=True, indent=2), encoding="utf-8")

    leaf_count = len(flattened["root"]["children"])
    geometric_error = flattened["root"]["geometricError"]
    print(
        f"[ok] Wrote {target} (leaves={leaf_count}, root.geometricError={geometric_error:.3f})",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
