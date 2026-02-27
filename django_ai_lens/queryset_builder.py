from __future__ import annotations

from django.apps import apps
from django.db.models import (
    Count, Sum, Avg, Max, Min,
    QuerySet, Q,
    Prefetch,
)
from django.db.models.fields.related import ManyToManyField
from django.db.models.fields.reverse_related import ManyToOneRel, ManyToManyRel, OneToOneRel

from django_ai_lens.query_schema import (
    AIQuerySchema,
    AggregationOperation,
    AggregationSchema,
    JoinSchema,
    JoinType,
)


# ── Aggregation operation map ──────────────────────────────────────────────

AGG_MAP = {
    AggregationOperation.COUNT: Count,
    AggregationOperation.SUM:   Sum,
    AggregationOperation.AVG:   Avg,
    AggregationOperation.MAX:   Max,
    AggregationOperation.MIN:   Min,
}


# ── Model resolution ───────────────────────────────────────────────────────

def resolve_model(model_name: str, app_labels: list[str]):
    """Find a model class by name within the allowed apps only."""
    for app_label in app_labels:
        try:
            return apps.get_model(app_label, model_name)
        except LookupError:
            continue
    raise ValueError(
        f"Model '{model_name}' not found in apps: {app_labels}. "
        "The AI may have hallucinated a model name."
    )


# ── Select-related vs prefetch-related (relation type detection) ──────────────

# Forward FK / OneToOne → select_related (single SQL JOIN)
# Reverse FK, reverse M2M, forward M2M → prefetch_related (separate query)
PREFETCH_FIELD_TYPES = (ManyToOneRel, OneToOneRel, ManyToManyRel, ManyToManyField)


def _is_prefetch_relation(model, path: str) -> bool:
    """
    Determine if a relation path requires prefetch_related (True) or
    select_related (False).

    - Forward ForeignKey / OneToOne (all segments) → select_related
    - Any segment is reverse FK, M2M, or reverse M2M → prefetch_related

    Example: ticket__plantation_sources__plantation — plantation_sources is
    reverse (related_name), so the whole path must use prefetch_related.
    """
    segments = path.split("__")
    current_model = model
    for segment in segments:
        try:
            field = current_model._meta.get_field(segment)
        except Exception:
            return True  # Can't resolve → prefetch to be safe
        if isinstance(field, PREFETCH_FIELD_TYPES) or not field.concrete:
            return True  # Reverse or M2M anywhere in path → prefetch
        # Move to related model for next segment
        if hasattr(field, "related_model") and field.related_model:
            current_model = field.related_model
        else:
            break
    return False


# ── Annotated aggregation builder ─────────────────────────────────────────

def _build_annotation(agg: AggregationSchema):
    """
    Build a single Django aggregation expression, optionally with a
    conditional filter (filtered annotate).
    """
    agg_class = AGG_MAP[agg.operation]

    if agg.filter_field and agg.filter_operator and agg.filter_value is not None:
        lookup = f"{agg.filter_field}__{agg.filter_operator.value}"
        condition = Q(**{lookup: agg.filter_value})
        return agg_class(agg.field, filter=condition)

    return agg_class(agg.field)


# ── Main queryset builder ──────────────────────────────────────────────────

def build_queryset(schema: AIQuerySchema, app_labels: list[str]) -> QuerySet:
    """
    Translate a validated AIQuerySchema into a Django ORM queryset.

    Pipeline:
      1. Resolve root model
      2. Apply select_related / prefetch_related for joins
      3. Apply filters  (WHERE)
      4. Apply group_by (.values())
      5. Apply aggregations (.annotate())
      6. Apply select_fields if no group_by and no aggregations
      7. Apply order_by
      8. Apply limit
    """

    # 1. Root model
    model = resolve_model(schema.model, app_labels)
    qs: QuerySet = model.objects.all()

    # 2. Joins
    #    Forward FK / O2O  → select_related  (single SQL JOIN, efficient)
    #    Reverse FK / M2M  → prefetch_related (separate query, avoids row multiplication)
    #
    #    When aggregations are present we MUST use select_related for forward joins
    #    to avoid the cartesian product issue that prefetch_related would cause.
    #    For reverse relations WITH aggregations, Django handles it correctly via
    #    the __ traversal in annotate() — no explicit prefetch needed.

    if schema.joins:
        has_aggregations = bool(schema.aggregations)

        forward_paths: list[str] = []
        prefetch_paths: list[str] = []

        for join in schema.joins:
            path = join.from_field
            needs_prefetch = _is_prefetch_relation(model, path)

            if needs_prefetch:
                if not has_aggregations:
                    # Only prefetch when we actually need to iterate the reverse set
                    prefetch_paths.append(path)
                # If aggregations exist, Django resolves the reverse via __ in annotate()
            else:
                forward_paths.append(path)

        if forward_paths:
            qs = qs.select_related(*forward_paths)
        if prefetch_paths:
            qs = qs.prefetch_related(*prefetch_paths)

    # 3. Filters
    for f in schema.filters:
        lookup = f"{f.field}__{f.operator.value}"
        qs = qs.filter(**{lookup: f.value})

    # 4. Group by  (.values() before .annotate() tells Django to GROUP BY)
    if schema.group_by:
        qs = qs.values(*schema.group_by)
    elif schema.select_fields:
        # Explicit column selection without grouping
        qs = qs.values(*schema.select_fields)

    # 5. Aggregations
    if schema.aggregations:
        annotations = {
            agg.alias: _build_annotation(agg)
            for agg in schema.aggregations
        }
        qs = qs.annotate(**annotations)

    # 6. Order by
    if schema.order_by:
        order_fields = [
            f"-{o.field}" if o.direction == "desc" else o.field
            for o in schema.order_by
        ]
        qs = qs.order_by(*order_fields)

    # 7. Limit
    if schema.limit:
        qs = qs[: schema.limit]

    return qs


# ── Result serialization ───────────────────────────────────────────────────

def queryset_to_list(qs: QuerySet) -> list[dict]:
    """
    Materialize the queryset into a JSON-serializable list of dicts.

    - If .values() / .annotate() was used → already dicts, just serialize.
    - If full model instances → convert via __dict__, stripping private keys.
    """
    results = list(qs)

    if not results:
        return []

    if isinstance(results[0], dict):
        return [_serialize_dict(row) for row in results]

    # Model instances
    return [
        _serialize_dict(
            {k: v for k, v in obj.__dict__.items() if not k.startswith("_")}
        )
        for obj in results
    ]


def _serialize_dict(row: dict) -> dict:
    """Convert non-JSON-safe types (Decimal, datetime, etc.) to primitives."""
    from decimal import Decimal
    from datetime import datetime, date

    out = {}
    for k, v in row.items():
        if isinstance(v, Decimal):
            out[k] = float(v)
        elif isinstance(v, (datetime, date)):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out
