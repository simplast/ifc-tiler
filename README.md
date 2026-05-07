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

## Engine

This project uses an internal native engine based on `ifcopenshell` + `trimesh`.
No external converter command is required.

## Run

```bash
ifc-tiler convert /abs/path/model.ifc
```

Optional flags:

- `--out-dir` (default: `./outputs`)
- `--job-name` (default: IFC filename stem)
- `--overwrite`
- `--keep-temp`
- `--timeout-min` (default: `120`)

## Exit codes

- `0`: success
- `2`: input validation failed
- `3`: converter engine failed
- `4`: output collision (without `--overwrite`)
- `5`: output validation failed
