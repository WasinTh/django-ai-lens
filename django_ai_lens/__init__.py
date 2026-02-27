"""
Django AI Lens: Natural language queries for Django models, powered by AI.
"""

from django_ai_lens.ai_query import generate_schema, run_ai_query, shape_chart_data
from django_ai_lens.schema_extrator import (
    DEFAULT_SCHEMA_FILE,
    extract_and_save,
    extract_from_loaded_django,
    get_models_schema,
    load_schema,
)

__all__ = [
    "generate_schema",
    "run_ai_query",
    "shape_chart_data",
    "DEFAULT_SCHEMA_FILE",
    "extract_and_save",
    "extract_from_loaded_django",
    "get_models_schema",
    "load_schema",
]
