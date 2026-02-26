from __future__ import annotations

import json
import os

from google import genai
from google.genai import types
from dotenv import load_dotenv
from pydantic import ValidationError

from schema_extrator import (
    DEFAULT_SCHEMA_FILE,
    bootstrap_django,
    extract_and_save,
    load_schema,
    get_models_schema,
)
from prompt_builder import build_messages
from query_schema import AIQuerySchema, ChartType
from queryset_builder import build_queryset, queryset_to_list

load_dotenv()

_api_key = os.getenv("GEMINI_API_KEY")
if not _api_key:
    raise RuntimeError(
        "GEMINI_API_KEY is not set. Add it to your .env file or environment. "
        "See .env.example for a sample configuration."
    )
_client = genai.Client(api_key=_api_key)
_model_name = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")


# ── Main entry point ───────────────────────────────────────────────────────

def run_ai_query(
    question: str,
    project_path: str | None = None,
    schema_file: str | None = None,
    app_labels: list[str] | None = None,
    max_retries: int = 2,
) -> dict:
    """
    Full pipeline:
      1. Load schema (from project_path or schema_file) or extract if needed
      2. Ask LLM to produce a structured query JSON
      3. Validate with Pydantic (retry on failure)
      4. Build and execute Django queryset
      5. Shape chart data if applicable

    Args:
        question: Natural language query.
        project_path: Path to Django project root (where manage.py lives).
          If provided, schema is loaded from .django_lens_schema.json or
          extracted and saved on first run.
        schema_file: Override path to schema JSON. If omitted with project_path,
          uses project_path/.django_lens_schema.json.
        app_labels: Optional. For embedded use when Django is already configured;
          schema is built via get_models_schema(app_labels). If project_path is
          used, app_labels come from the cached schema.
    """
    if project_path:
        from pathlib import Path
        schema_path = Path(schema_file) if schema_file else Path(project_path) / DEFAULT_SCHEMA_FILE
        if schema_path.exists():
            bootstrap_django(project_path)
            schema, resolved_app_labels = load_schema(schema_file=str(schema_path))
        else:
            result = extract_and_save(project_path, output_file=str(schema_path))
            schema = result["schema"]
            resolved_app_labels = result["app_labels"]
        app_labels = resolved_app_labels
    elif app_labels:
        schema = get_models_schema(app_labels)
    else:
        raise ValueError("Provide either project_path or app_labels (with Django configured).")

    if not app_labels:
        raise ValueError("No app_labels available. Ensure INSTALLED_APPS contains project apps.")
    payload = build_messages(schema, question)

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
        response = _client.models.generate_content(
            model=_model_name,
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

        return {
            "success": True,
            "question": question,
            "query_schema": raw_json,       # Transparent — frontend can show this
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
