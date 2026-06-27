"""Investable hierarchy schema.

A bounded tree the GUI can render as Portfolio -> positions -> top holdings.
Node ids are namespaced strings (``workspace:1``, ``position:3``,
``position:3:holding:7``) so they are globally unique and never cycle.
"""

from __future__ import annotations

from pydantic import BaseModel

from app.schemas.common import DecimalStr


class HierarchyNode(BaseModel):
    id: str
    # portfolio | position | holding
    kind: str
    label: str
    value: DecimalStr | None = None
    currency: str | None = None
    weight: DecimalStr | None = None
    status: str
    source: str
    children: list[HierarchyNode] = []


class HierarchyResponse(BaseModel):
    root: HierarchyNode


HierarchyNode.model_rebuild()
