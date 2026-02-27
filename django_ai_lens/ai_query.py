from __future__ import annotations

import json

from django.conf import settings
from google import genai
from google.genai import types
from pydantic import ValidationError

from django_ai_lens.schema_extrator import get_models_schema
from django_ai_lens.prompt_builder import build_messages
from django_ai_lens.query_schema import AIQuerySchema, ChartType
from django_ai_lens.queryset_builder import build_queryset, queryset_to_list


def _get_client():
    """Lazy-initialize Gemini client from Django settings."""
    api_key = getattr(settings, "GEMINI_API_KEY", None)
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Add it to your Django settings.py. "
            "Example: GEMINI_API_KEY = 'your_api_key_here'"
        )
    return genai.Client(api_key=api_key)


def _get_model_name():
    """Get model name from Django settings."""
    return getattr(settings, "GEMINI_MODEL", "gemini-1.5-flash")


# ── Main entry point ───────────────────────────────────────────────────────

def run_ai_query(
    question: str,
    app_labels: list[str],
    max_retries: int = 2,
) -> dict:
    """
    Full pipeline:
      1. Build schema from Django models (via app_labels)
      2. Ask LLM to produce a structured query JSON
      3. Validate with Pydantic (retry on failure)
      4. Build and execute Django queryset
      5. Shape chart data if applicable

    Requires Django to be configured (django.setup() called) and
    GEMINI_API_KEY, GEMINI_MODEL set in settings.py.

    Args:
        question: Natural language query.
        app_labels: App labels to query (e.g. ["myapp", "orders"]).
        max_retries: Number of retries if the AI returns invalid JSON or queryset fails.

    Returns:
        dict with success, question, query_schema, data, row_count, chart_type, chart_data
    """
    if not app_labels:
        raise ValueError("app_labels is required. Provide the Django app labels to query.")

    schema = get_models_schema(app_labels)
    payload = build_messages(schema, question)

    client = _get_client()
    model_name = _get_model_name()

    last_error: str = ""
    messages = payload["messages"]

    for attempt in range(1, max_retries + 2):  # 1 initial + max_retries
        # ── Convert messages to google.genai Content format ─────────────────
        contents = []
        for msg in messages:
            part = types.Part.from_text(text=msg["content"])
            if msg["role"] == "user":
                contents.append(types.UserContent(parts=[part]))
            else:
                contents.append(types.ModelContent(parts=[part]))

        # ── LLM call ──────────────────────────────────────────────────────
        response = client.models.generate_content(
            model=model_name,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=payload["system"],
                max_output_tokens=2048,
            ),
        )
        raw_text = response.text.strip()

        # Strip accidental markdown fences
        if raw_text.startswith("```"):
            raw_text = raw_text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        # ── Parse JSON ────────────────────────────────────────────────────
        try:
            raw_json = json.loads(raw_text)
        except json.JSONDecodeError as e:
            last_error = f"Invalid JSON on attempt {attempt}: {e}"
            messages = _append_retry_message(messages, raw_text, last_error)
            continue

        # ── Pydantic validation ───────────────────────────────────────────
        try:
            query_schema = AIQuerySchema(**raw_json)
        except ValidationError as e:
            last_error = f"Schema validation failed on attempt {attempt}:\n{e}"
            messages = _append_retry_message(messages, raw_text, last_error)
            continue

        # ── Queryset execution ────────────────────────────────────────────
        try:
            qs = build_queryset(query_schema, app_labels)
            data = queryset_to_list(qs)
        except Exception as e:
            last_error = f"Queryset error on attempt {attempt}: {e}"
            messages = _append_retry_message(messages, raw_text, last_error)
            continue

        # ── Success ───────────────────────────────────────────────────────
        chart_data = None
        if query_schema.chart_type != ChartType.NONE and query_schema.aggregations:
            chart_data = shape_chart_data(data, query_schema)

        django_query = _build_django_query_string(query_schema)

        return {
            "success": True,
            "question": question,
            "query_schema": raw_json,       # Transparent — frontend can show this
            "django_query": django_query,   # Django ORM chain for debugging
            "data": data,
            "row_count": len(data),
            "chart_type": query_schema.chart_type.value,
            "chart_data": chart_data,
        }

    # All retries exhausted
    raise RuntimeError(
        f"AI query failed after {max_retries + 1} attempts. "
        f"Last error: {last_error}"
    )


# ── Django query string (debug) ───────────────────────────────────────────

def _build_django_query_string(schema: AIQuerySchema) -> str:
    """
    Build a Python-style Django ORM chain string for debugging.
    Example: Author.objects.filter(age__gte=30).select_related('publisher').values('name')[:10]
    """
    model = schema.model
    parts = [f"{model}.objects.all()"]

    # select_related / prefetch_related
    if schema.joins:
        forward = [j.from_field for j in schema.joins if "_set" not in j.from_field]
        reverse = [j.from_field for j in schema.joins if "_set" in j.from_field]
        if forward:
            args = ", ".join(repr(p) for p in forward)
            parts.append(f".select_related({args})")
        if reverse:
            args = ", ".join(repr(p) for p in reverse)
            parts.append(f".prefetch_related({args})")

    # filter
    for f in schema.filters:
        lookup = f"{f.field}__{f.operator.value}"
        parts.append(f".filter({lookup}={f.value!r})")

    # values (group_by or select_fields)
    if schema.group_by:
        args = ", ".join(repr(f) for f in schema.group_by)
        parts.append(f".values({args})")
    elif schema.select_fields:
        args = ", ".join(repr(f) for f in schema.select_fields)
        parts.append(f".values({args})")

    # annotate
    if schema.aggregations:
        agg_map = {"count": "Count", "sum": "Sum", "avg": "Avg", "max": "Max", "min": "Min"}
        ann_args = []
        for agg in schema.aggregations:
            cls = agg_map.get(agg.operation.value, "Count")
            ann_args.append(f"{agg.alias}={cls}('{agg.field}')")
        parts.append(f".annotate({', '.join(ann_args)})")

    # order_by
    if schema.order_by:
        fields = [
            f"-{o.field}" if o.direction == "desc" else o.field
            for o in schema.order_by
        ]
        args = ", ".join(repr(f) for f in fields)
        parts.append(f".order_by({args})")

    # limit
    if schema.limit:
        parts.append(f"[:{schema.limit}]")

    return "".join(parts)


# ── Retry helper ───────────────────────────────────────────────────────────

def _append_retry_message(
    messages: list[dict],
    bad_output: str,
    error: str,
) -> list[dict]:
    """
    Extend the conversation so the LLM can self-correct on the next attempt.
    """
    return messages + [
        {"role": "assistant", "content": bad_output},
        {
            "role": "user",
            "content": (
                f"Your previous response caused this error:\n{error}\n\n"
                "Please return a corrected JSON object only, with no explanation."
            ),
        },
    ]


# ── Chart data shaper ──────────────────────────────────────────────────────

def shape_chart_data(data: list[dict], schema: AIQuerySchema) -> dict:
    """
    Convert flat result rows into a Chart.js-compatible payload.

    Supports:
    - Single aggregation   → simple labels + one dataset
    - Multi aggregation    → labels + multiple datasets (e.g. grouped bar)
    - Multi group_by       → first group_by as labels, rest as series
    """
    if not data:
        return {"labels": [], "datasets": []}

    # Determine label field (first group_by, or first non-aggregation key)
    agg_aliases = {agg.alias for agg in schema.aggregations}
    label_field = None

    if schema.group_by:
        label_field = schema.group_by[0]
    else:
        # Fallback: first key that isn't an aggregation alias
        for key in data[0].keys():
            if key not in agg_aliases:
                label_field = key
                break

    labels = [str(row.get(label_field, "")) for row in data] if label_field else []

    # Build one dataset per aggregation
    datasets = [
        {
            "label": agg.alias.replace("_", " ").title(),
            "data": [row.get(agg.alias) for row in data],
        }
        for agg in schema.aggregations
    ]

    return {
        "labels": labels,
        "datasets": datasets,
        # Extra metadata the frontend can use to auto-configure Chart.js
        "label_field": label_field,
        "chart_type": schema.chart_type.value,
    }
