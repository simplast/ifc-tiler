# ifc-tiler

Minimal backend CLI project for converting an IFC file into a 3D Tiles output directory.

## Quick start

```bash
cd /Users/doer/company/bim/ifc-tiler
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
pip install -e .
```

> This project uses a `pyproject.toml` editable install flow (PEP 660).  
> Recommended: `pip >= 23`.

## Quick start (uv)

```bash
cd /Users/doer/company/bim/ifc-tiler
uv sync
uv run ifc-tiler --help
```

Install `py3dtiles` into project dependencies:

```bash
uv add py3dtiles
```

Or run once without modifying dependencies:

```bash
uv run --with py3dtiles ifc-tiler convert-py3dtiles /abs/path/model.ifc --out-dir ./outputs --job-name py3dtiles-test --overwrite
```

## Engine

This project uses an internal native engine based on `ifcopenshell` + `trimesh`.
No external converter command is required.

### py3dtiles vs native `convert` (up-axis)

`convert-py3dtiles` is **not** “rotation-free”: py3dtiles’ IFC worker applies the same orthogonal
`(x, y, z) → (x, z, -y)` transform as the constant `Z_UP_MATRIX_4X4` when building each **b3dm**
(`B3dm.from_meshes(..., transform=Z_UP_MATRIX_4X4)`).

The native `convert` path applies the **identical** transform, but **bakes it into vertex positions**
before writing **GLB** tiles. Because `3d-tiles-renderer`’s `TilesRenderer` also applies a default
up-axis adjustment on raw GLB when `asset.gltfUpAxis` is treated as `"Y"`, native exports set
`asset.gltfUpAxis` to `"Z"` for `mesh` / `tile` modes so geometry is not rotated twice relative to
`boundingVolume`. Prefer default **`mesh`** when viewing in `ifc-render` / `3d-tiles-renderer`.

## Run

```bash
ifc-tiler convert /abs/path/model.ifc
```

Run the isolated py3dtiles command:

```bash
ifc-tiler convert-py3dtiles /abs/path/model.ifc --out-dir ./outputs --job-name py3dtiles-test --overwrite
```

Run preclean only (write a cleaned IFC without conversion):

```bash
ifc-tiler preclean /abs/path/model.ifc /abs/path/model.cleaned.ifc
```

Optional preclean flags for `convert-py3dtiles`:

- `--skip-proxy-body-items-ge` (default: `500`)
- `--skip-brep-body-items-ge` (default: `50`)
- `--skip-surface-body-items-ge` (default: `50`)

These filters run before py3dtiles conversion and remove problematic heavy/empty IFC Body representations.

The same threshold flags are available on `preclean`, plus:

- `--overwrite` (replace existing cleaned IFC output path)

Optional flags:

- `--out-dir` (default: `./outputs`)
- `--job-name` (default: IFC filename stem)
- `--overwrite`
- `--keep-temp`
- `--timeout-min` (default: `120`)
- `--lod-mode` (`flat` default, `lod` to generate hierarchical coarse parent tiles)
- `--lod-target-ratio` (default: `0.1`, coarse LOD face ratio)
- `--lod-min-faces` (default: `2000`, skip simplification below this face count)
- `--meshopt` (run `gltfpack` compression if installed; can increase z-fighting on nearly coplanar BIM surfaces because of tighter vertex quantization — try without it if you see shimmer through walls or paving)
- `--skip-proxy-body-items-ge` (default: `500`, skip `IfcBuildingElementProxy` before meshing)
- `--skip-brep-body-items-ge` (default: `50`, skip brep-heavy body representations before meshing)
- `--skip-surface-body-items-ge` (default: `50`, skip surface-model-heavy body representations before meshing)
- `--no-cache` (disable persistent native geometry cache)
- `--rebuild-cache` (force rebuild cache for current IFC + extraction options)
- `--cache-dir` (custom cache base dir, default: `<out-dir>/.cache/native-shapes`)

Native backend cache behavior:

- By default, extracted shape meshes are persisted and reused across `convert` runs.
- Cache key includes IFC content hash plus geometry-affecting extraction options.
- Tile grouping options (`--tile-target-mb`, `--tile-max-mb`, `--max-tile-triangles`, `--lod-*`) do not invalidate cached extracted meshes.

## Compression + deployment notes

- Install `gltfpack` to enable `--meshopt` (must be on your `PATH`; Homebrew does not ship this formula):
  - macOS: download `gltfpack-macos.zip` (Apple Silicon) or `gltfpack-macos-intel.zip` from [meshoptimizer releases](https://github.com/zeux/meshoptimizer/releases/latest), unzip, `chmod +x gltfpack`, put the binary somewhere on `PATH` (or call it by full path)
  - Linux / Windows: use the matching `gltfpack-*.zip` from the same releases page
- Recommended HTTP caching:
  - `*.glb`: `Cache-Control: public, max-age=31536000, immutable`
  - `tileset.json`: `Cache-Control: no-cache`
- Let gzip/brotli compress JSON responses, but do not recompress GLB payloads.

## Exit codes

- `0`: success
- `2`: input validation failed
- `3`: converter engine failed
- `4`: output collision (without `--overwrite`)
- `5`: output validation failed
