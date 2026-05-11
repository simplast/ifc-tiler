from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PrecleanResult:
    cleaned_ifc: Path
    removed_total: int
    removed_no_body_representation: int
    removed_empty_body_items: int
    removed_proxy_body_items: int
    removed_brep_body_items: int
    removed_surface_body_items: int

    def to_dict(self) -> dict[str, int | str]:
        return {
            "cleaned_ifc": str(self.cleaned_ifc),
            "removed_total": self.removed_total,
            "removed_no_body_representation": self.removed_no_body_representation,
            "removed_empty_body_items": self.removed_empty_body_items,
            "removed_proxy_body_items": self.removed_proxy_body_items,
            "removed_brep_body_items": self.removed_brep_body_items,
            "removed_surface_body_items": self.removed_surface_body_items,
        }


def preclean_ifc_for_py3dtiles(
    *,
    input_ifc: Path,
    output_ifc: Path,
    skip_proxy_body_items_ge: int,
    skip_brep_body_items_ge: int,
    skip_surface_body_items_ge: int,
) -> PrecleanResult:
    import ifcopenshell

    model = ifcopenshell.open(str(input_ifc))
    products = list(model.by_type("IfcProduct"))

    removed_no_body_representation = 0
    removed_empty_body_items = 0
    removed_proxy_body_items = 0
    removed_brep_body_items = 0
    removed_surface_body_items = 0

    for product in products:
        if _is_spatial_hierarchy_node(product):
            # Keep spatial/container nodes even when they have no body geometry.
            # py3dtiles IFC tiler relies on this hierarchy to build root/children metadata.
            continue
        body_reps = _get_body_representations(product)
        if not body_reps:
            model.remove(product)
            removed_no_body_representation += 1
            continue

        max_body_items = 0
        has_brep = False
        has_surface_model = False

        is_proxy = _safe_is_a(product) == "IfcBuildingElementProxy"
        if is_proxy:
            # Keep parity with native converter complexity metrics:
            # when proxy Body is mapped, count items on the mapped source representation.
            body_rep = body_reps[0]
            direct_items = list(getattr(body_rep, "Items", None) or [])
            underlying_rep = _unwrap_body_representation(body_rep)
            underlying_items = list(getattr(underlying_rep, "Items", None) or [])
            max_body_items = max(max_body_items, len(underlying_items))
            underlying_type = str(getattr(underlying_rep, "RepresentationType", "") or "")
            has_brep = underlying_type == "Brep"
            has_surface_model = underlying_type == "SurfaceModel"
            if max_body_items == 0 and direct_items:
                # Fallback for malformed mapped entries.
                max_body_items = len(direct_items)
        else:
            for rep in body_reps:
                items = list(getattr(rep, "Items", None) or [])
                max_body_items = max(max_body_items, len(items))
                for item in items:
                    item_type = _safe_is_a(item)
                    if item_type.endswith("Brep"):
                        has_brep = True
                    if item_type in {"IfcFaceBasedSurfaceModel", "IfcShellBasedSurfaceModel"}:
                        has_surface_model = True

        if max_body_items == 0:
            model.remove(product)
            removed_empty_body_items += 1
            continue

        if is_proxy:
            if (
                (skip_proxy_body_items_ge > 0 and max_body_items >= skip_proxy_body_items_ge)
                or (skip_brep_body_items_ge > 0 and has_brep and max_body_items >= skip_brep_body_items_ge)
                or (
                    skip_surface_body_items_ge > 0
                    and has_surface_model
                    and max_body_items >= skip_surface_body_items_ge
                )
            ):
                model.remove(product)
                removed_proxy_body_items += 1
                continue

        if skip_brep_body_items_ge > 0 and has_brep and max_body_items >= skip_brep_body_items_ge:
            model.remove(product)
            removed_brep_body_items += 1
            continue

        if skip_surface_body_items_ge > 0 and has_surface_model and max_body_items >= skip_surface_body_items_ge:
            model.remove(product)
            removed_surface_body_items += 1
            continue

    output_ifc.parent.mkdir(parents=True, exist_ok=True)
    model.write(str(output_ifc))

    removed_total = (
        removed_no_body_representation
        + removed_empty_body_items
        + removed_proxy_body_items
        + removed_brep_body_items
        + removed_surface_body_items
    )
    return PrecleanResult(
        cleaned_ifc=output_ifc,
        removed_total=removed_total,
        removed_no_body_representation=removed_no_body_representation,
        removed_empty_body_items=removed_empty_body_items,
        removed_proxy_body_items=removed_proxy_body_items,
        removed_brep_body_items=removed_brep_body_items,
        removed_surface_body_items=removed_surface_body_items,
    )


def _get_body_representations(product) -> list:
    representation = getattr(product, "Representation", None)
    if representation is None:
        return []
    reps = list(getattr(representation, "Representations", None) or [])
    body_reps = []
    for rep in reps:
        identifier = str(getattr(rep, "RepresentationIdentifier", "") or "").strip().lower()
        if identifier == "body":
            body_reps.append(rep)
    return body_reps


def _safe_is_a(entity) -> str:
    try:
        return str(entity.is_a() or "")
    except Exception:
        return ""


def _unwrap_body_representation(body_representation):
    representation_type = str(getattr(body_representation, "RepresentationType", "") or "")
    if representation_type != "MappedRepresentation":
        return body_representation
    items = list(getattr(body_representation, "Items", None) or [])
    for item in items:
        try:
            is_mapped_item = bool(item.is_a("IfcMappedItem"))
        except Exception:
            is_mapped_item = False
        if not is_mapped_item:
            continue
        mapping_source = getattr(item, "MappingSource", None)
        mapped_representation = getattr(mapping_source, "MappedRepresentation", None) if mapping_source is not None else None
        if mapped_representation is not None:
            return mapped_representation
    return body_representation


def _is_spatial_hierarchy_node(entity) -> bool:
    try:
        return bool(
            entity.is_a("IfcProject")
            or entity.is_a("IfcSpatialElement")
            or entity.is_a("IfcSpatialStructureElement")
        )
    except Exception:
        return False
