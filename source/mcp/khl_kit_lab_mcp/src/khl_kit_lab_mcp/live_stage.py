"""Controlled live-stage operations implemented through the existing Kit bridge."""

from __future__ import annotations

import json
import re
from typing import Literal


MAX_PRIM_PATH_CHARS = 1024
PRIM_PATH_PATTERN = re.compile(r"^/(?:[A-Za-z_][A-Za-z0-9_]*)(?:/[A-Za-z_][A-Za-z0-9_]*)*$")
PROTECTED_REMOVAL_PATHS = {"/", "/World"}

PrimType = Literal[
    "Xform",
    "Scope",
    "Cube",
    "Sphere",
    "Cylinder",
    "Cone",
    "Capsule",
    "Camera",
    "DistantLight",
    "SphereLight",
    "RectLight",
    "DiskLight",
]


def validate_prim_path(path: str, *, allow_protected: bool = True) -> str:
    if not isinstance(path, str):
        raise ValueError("Prim path must be text")
    if len(path) > MAX_PRIM_PATH_CHARS:
        raise ValueError(f"Prim path exceeds {MAX_PRIM_PATH_CHARS} characters")
    if not PRIM_PATH_PATTERN.fullmatch(path):
        raise ValueError(
            "Prim path must be an absolute slash-separated USD prim path using "
            "ASCII identifiers, for example /World/AgentSceneLab/Cube"
        )
    if not allow_protected and path in PROTECTED_REMOVAL_PATHS:
        raise ValueError(f"Refusing to remove protected prim path {path}")
    return path


def build_create_prim_source(path: str, prim_type: PrimType) -> str:
    safe_path = validate_prim_path(path)
    safe_type = str(prim_type)
    if safe_type not in PrimType.__args__:
        raise ValueError(f"Unsupported prim type: {safe_type}")
    path_literal = json.dumps(safe_path, ensure_ascii=True)
    type_literal = json.dumps(safe_type, ensure_ascii=True)
    return f"""def __khl_lab_create_prim():
    import omni.usd
    from pxr import Sdf, UsdGeom, UsdLux
    stage = omni.usd.get_context().get_stage()
    if stage is None:
        raise RuntimeError("No active USD stage")
    path = {path_literal}
    prim_type = {type_literal}
    existing = stage.GetPrimAtPath(path)
    if existing.IsValid():
        raise RuntimeError(f"Prim already exists: {{path}}")
    parent_path = Sdf.Path(path).GetParentPath()
    if parent_path != Sdf.Path.absoluteRootPath:
        parent = stage.GetPrimAtPath(parent_path)
        if not parent.IsValid():
            raise RuntimeError(f"Parent prim does not exist: {{parent_path}}")
    schemas = {{
        "Xform": UsdGeom.Xform,
        "Scope": UsdGeom.Scope,
        "Cube": UsdGeom.Cube,
        "Sphere": UsdGeom.Sphere,
        "Cylinder": UsdGeom.Cylinder,
        "Cone": UsdGeom.Cone,
        "Capsule": UsdGeom.Capsule,
        "Camera": UsdGeom.Camera,
        "DistantLight": UsdLux.DistantLight,
        "SphereLight": UsdLux.SphereLight,
        "RectLight": UsdLux.RectLight,
        "DiskLight": UsdLux.DiskLight,
    }}
    prim = schemas[prim_type].Define(stage, path).GetPrim()
    if not prim.IsValid():
        raise RuntimeError(f"Failed to create prim: {{path}}")
    return {{"created": True, "path": str(prim.GetPath()), "type_name": prim.GetTypeName()}}
__khl_lab_create_prim()"""


def build_remove_prim_source(path: str) -> str:
    safe_path = validate_prim_path(path, allow_protected=False)
    path_literal = json.dumps(safe_path, ensure_ascii=True)
    return f"""def __khl_lab_remove_prim():
    import omni.usd
    stage = omni.usd.get_context().get_stage()
    if stage is None:
        raise RuntimeError("No active USD stage")
    path = {path_literal}
    prim = stage.GetPrimAtPath(path)
    if not prim.IsValid():
        return {{"removed": False, "path": path, "reason": "not_found"}}
    previous_type = prim.GetTypeName()
    stage.RemovePrim(path)
    remaining = stage.GetPrimAtPath(path)
    if remaining.IsValid():
        raise RuntimeError(f"Prim remains composed after removal attempt: {{path}}")
    return {{"removed": True, "path": path, "previous_type_name": previous_type}}
__khl_lab_remove_prim()"""
