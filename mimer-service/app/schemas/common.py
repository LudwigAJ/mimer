"""Shared schema building blocks.

Decimal values are serialised to JSON as *strings* to avoid float precision
issues on the client. List endpoints use a `{ "data": [...], "meta": {...} }`
envelope; single-resource and summary endpoints return the object directly.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, PlainSerializer

T = TypeVar("T")


def _decimal_to_str(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


# A Decimal that always serialises to a plain decimal string in JSON output.
DecimalStr = Annotated[
    Decimal,
    PlainSerializer(_decimal_to_str, return_type=str, when_used="json"),
]


class ORMModel(BaseModel):
    """Base for read schemas populated directly from ORM instances."""

    model_config = ConfigDict(from_attributes=True)


class Meta(BaseModel):
    count: int


class ListResponse(BaseModel, Generic[T]):
    """Consistent envelope for collection endpoints."""

    data: list[T]
    meta: Meta

    @classmethod
    def of(cls, items: list[T]) -> ListResponse[T]:
        return cls(data=items, meta=Meta(count=len(items)))
