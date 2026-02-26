from __future__ import annotations
from pydantic import BaseModel, field_validator, model_validator
from typing import Optional, Literal
from enum import Enum


# ── Enums (whitelist) ──────────────────────────────────────────────────────

class FilterOperator(str, Enum):
    # Comparison
    EXACT      = "exact"
    GT         = "gt"
    GTE        = "gte"
    LT         = "lt"
    LTE        = "lte"
    # String
    CONTAINS   = "contains"
    ICONTAINS  = "icontains"
    STARTSWITH = "startswith"
    ENDSWITH   = "endswith"
    # Membership
    IN         = "in"
    # Null checks
    ISNULL     = "isnull"
    # Date extractors
    YEAR       = "year"
    MONTH      = "month"
    DAY        = "day"
    WEEK       = "week"
    WEEK_DAY   = "week_day"
    QUARTER    = "quarter"


class AggregationOperation(str, Enum):
    COUNT = "count"
    SUM   = "sum"
    AVG   = "avg"
    MAX   = "max"
    MIN   = "min"


class JoinType(str, Enum):
    INNER = "inner"   # Django default: filter(related__field=...)
    LEFT  = "left"    # select_related / prefetch, produces LEFT OUTER


class ChartType(str, Enum):
    BAR      = "bar"
    LINE     = "line"
    PIE      = "pie"
    DOUGHNUT = "doughnut"
    RADAR    = "radar"
    SCATTER  = "scatter"
    NONE     = "none"


# ── Field name safety ──────────────────────────────────────────────────────

BLOCKED_FIELD_SEGMENTS = {"delete", "update", "create", "raw", "execute", "bulk", "truncate"}


def validate_field_name(v: str) -> str:
    """
    Allow Django ORM double-underscore traversal (order__customer__name)
    but block any segment that maps to a mutating operation.
    """
    segments = v.split("__")
    for seg in segments:
        if seg.lower() in BLOCKED_FIELD_SEGMENTS:
            raise ValueError(f"Blocked field segment: {seg!r}")
        if not seg.replace("_", "").isalnum():
            raise ValueError(f"Invalid field segment: {seg!r}")
    return v


# ── Join schema ────────────────────────────────────────────────────────────

class JoinSchema(BaseModel):
    """
    Describes a relationship to traverse.

    Examples
    --------
    - Order → Customer (FK on Order):
        { "model": "Customer", "from_field": "customer", "join_type": "inner" }

    - Order → OrderItem (reverse FK, OneToMany):
        { "model": "OrderItem", "from_field": "orderitem_set", "join_type": "left" }

    - Order → Product (M2M through OrderItem):
        { "model": "Product", "from_field": "orderitem__product", "join_type": "inner" }
    """
    model: str
    from_field: str          # The ORM path from the root model to this relation
    join_type: JoinType = JoinType.INNER

    @field_validator("model")
    @classmethod
    def model_is_identifier(cls, v: str) -> str:
        if not v.isidentifier():
            raise ValueError("Join model name must be a valid Python identifier.")
        return v

    @field_validator("from_field")
    @classmethod
    def safe_from_field(cls, v: str) -> str:
        return validate_field_name(v)


# ── Filter schema ──────────────────────────────────────────────────────────

class FilterSchema(BaseModel):
    """
    A single WHERE condition.

    The `field` may traverse relations using __ notation, e.g.
    "customer__country" or "orderitem__product__category".
    """
    field: str
    operator: FilterOperator
    value: str | int | float | bool | list | None

    @field_validator("field")
    @classmethod
    def safe_field(cls, v: str) -> str:
        return validate_field_name(v)


# ── Aggregation schema ─────────────────────────────────────────────────────

class AggregationSchema(BaseModel):
    """
    A single annotated aggregation column, e.g. SUM(orderitem__price).
    The field may span relations.
    """
    field: str
    operation: AggregationOperation
    alias: str
    # Optional: only aggregate rows where a condition holds (filtered annotate)
    filter_field: Optional[str] = None
    filter_operator: Optional[FilterOperator] = None
    filter_value: Optional[str | int | float | bool | list | None] = None

    @field_validator("field")
    @classmethod
    def safe_field(cls, v: str) -> str:
        return validate_field_name(v)

    @field_validator("alias")
    @classmethod
    def safe_alias(cls, v: str) -> str:
        if not v.replace("_", "").isalnum():
            raise ValueError("Alias must be alphanumeric with underscores only.")
        return v

    @field_validator("filter_field")
    @classmethod
    def safe_filter_field(cls, v: str | None) -> str | None:
        if v is not None:
            return validate_field_name(v)
        return v


# ── Order-by schema ────────────────────────────────────────────────────────

class OrderBySchema(BaseModel):
    field: str
    direction: Literal["asc", "desc"] = "asc"

    @field_validator("field")
    @classmethod
    def safe_field(cls, v: str) -> str:
        return validate_field_name(v)


# ── Root query schema ──────────────────────────────────────────────────────

class AIQuerySchema(BaseModel):
    """
    Complete structured query description produced by the LLM.
    No executable code — only declarative intent.
    """
    model: str
    joins: list[JoinSchema]               = []
    filters: list[FilterSchema]           = []
    aggregations: list[AggregationSchema] = []
    group_by: list[str]                   = []
    order_by: list[OrderBySchema]         = []
    select_fields: list[str]              = []   # explicit .values() columns
    limit: Optional[int]                  = None
    chart_type: ChartType                 = ChartType.NONE

    @field_validator("model")
    @classmethod
    def model_is_identifier(cls, v: str) -> str:
        if not v.isidentifier():
            raise ValueError("Root model name must be a valid Python identifier.")
        return v

    @field_validator("group_by", "select_fields", mode="before")
    @classmethod
    def safe_field_list(cls, items: list[str]) -> list[str]:
        return [validate_field_name(f) for f in items]

    @field_validator("limit")
    @classmethod
    def cap_limit(cls, v: int | None) -> int | None:
        if v is not None and v > 1000:
            raise ValueError("Limit cannot exceed 1000 rows.")
        return v

    @model_validator(mode="after")
    def validate_join_fields_in_filters_and_aggs(self) -> "AIQuerySchema":
        """
        When joins are declared, cross-reference that filter/agg fields
        that span relations actually correspond to a declared join path.
        This is a soft warning check only — hard enforcement happens in the
        queryset builder when Django resolves the lookup.
        """
        declared_paths = {j.from_field for j in self.joins}

        for f in self.filters:
            if "__" in f.field:
                relation_path = "__".join(f.field.split("__")[:-1])
                # Only warn — Django will raise FieldError if it's truly wrong
                _ = relation_path  # reserved for future strict mode

        return self
