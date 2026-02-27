"""
Django schema extractor: crawl a Django project for models and persist the schema.

Embedded mode only: When Django is already configured (e.g. from Django shell or
a running app), extracts schema from the currently loaded INSTALLED_APPS.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


# Default schema cache file (relative to cwd)
DEFAULT_SCHEMA_FILE = ".django_lens_schema.json"


def _get_installed_app_labels_from_settings() -> list[str]:
    """
    Extract app labels from INSTALLED_APPS using Django's app registry.
    Use when Django is already configured (e.g. from Django shell).
    Returns actual app labels (e.g. 'common', 'bplus') excluding Django built-ins.
    Uses apps.get_app_configs() so 'apps.common' → 'common', not 'apps'.
    """
    from django.apps import apps
    from django.conf import settings

    # Build name -> label map from app registry (handles dotted paths correctly)
    name_to_label = {ac.name: ac.label for ac in apps.get_app_configs()}

    app_labels = []
    seen = set()
    for app in getattr(settings, "INSTALLED_APPS", []):
        if isinstance(app, str):
            if app.startswith("django."):
                continue
            label = name_to_label.get(app)
            if label is not None and label not in seen:
                seen.add(label)
                app_labels.append(label)
        elif hasattr(app, "label"):
            if app.label not in seen:
                seen.add(app.label)
                app_labels.append(app.label)

    return app_labels


def get_models_schema(app_labels: list[str]) -> str:
    """
    Produce a detailed plain-text schema description including:
    - All fields with types
    - ForeignKey / OneToOne / ManyToMany relationships with target model
    - Reverse relation accessor names

    Requires Django to be already configured (django.setup() called).
    """
    from django.apps import apps

    schema_parts = []
    all_models: dict[str, type] = {}

    for app_label in app_labels:
        try:
            app_config = apps.get_app_config(app_label)
        except LookupError:
            continue
        for model in app_config.get_models():
            all_models[model.__name__] = model

    for model_name, model in all_models.items():
        lines = [f"Model: {model_name} (app: {model._meta.app_label})"]

        # ── Direct fields ──────────────────────────────────────────────────
        lines.append("  Fields:")
        for field in model._meta.get_fields():
            if field.is_relation and not field.concrete:
                continue

            field_type = type(field).__name__

            if field.is_relation and field.concrete:
                related_model = field.related_model
                related_name = related_model.__name__ if related_model else "Unknown"
                lines.append(
                    f"    - {field.name}: {field_type} → {related_name}"
                    f"  [ORM path: {field.name}__<{related_name.lower()}_field>]"
                )
            else:
                lines.append(f"    - {field.name}: {field_type}")

        # ── Reverse relations ──────────────────────────────────────────────
        reverse_lines = []
        for field in model._meta.get_fields():
            if field.is_relation and not field.concrete:
                rel_type = type(field).__name__
                related_model = field.related_model
                related_name = related_model.__name__ if related_model else "Unknown"
                accessor = (
                    field.get_accessor_name()
                    if hasattr(field, "get_accessor_name")
                    else getattr(field, "name", str(field))
                )
                reverse_lines.append(
                    f"    - {accessor}: {rel_type} ← {related_name}"
                    f"  [ORM path: {accessor}__<{related_name.lower()}_field>]"
                )

        if reverse_lines:
            lines.append("  Reverse relations (can be used in filters / group_by):")
            lines.extend(reverse_lines)

        # ── M2M ───────────────────────────────────────────────────────────
        m2m_lines = []
        for field in model._meta.many_to_many:
            related_model = field.related_model
            related_name = related_model.__name__ if related_model else "Unknown"
            through = field.remote_field.through
            through_name = (
                through.__name__
                if through and not through._meta.auto_created
                else "auto"
            )
            m2m_lines.append(
                f"    - {field.name}: ManyToManyField → {related_name}"
                f"  [through: {through_name}]"
                f"  [ORM path: {field.name}__<{related_name.lower()}_field>]"
            )

        if m2m_lines:
            lines.append("  ManyToMany:")
            lines.extend(m2m_lines)

        schema_parts.append("\n".join(lines))

    return "\n\n".join(schema_parts)


def extract_from_loaded_django(
    output_file: str | Path | None = None,
) -> dict:
    """
    Extract model schemas from the currently loaded Django (embedded mode).
    Use when Django is already configured, e.g. from Django shell.

    Args:
        output_file: Where to save the schema JSON. Defaults to cwd/.django_lens_schema.json.

    Returns:
        {"schema": str, "app_labels": list[str], "output_path": str}
    """
    app_labels = _get_installed_app_labels_from_settings()
    schema = get_models_schema(app_labels)

    out_path = Path(output_file or Path.cwd() / DEFAULT_SCHEMA_FILE)
    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    settings_module = os.environ.get("DJANGO_SETTINGS_MODULE", "")
    payload = {
        "schema": schema,
        "app_labels": app_labels,
        "settings_module": settings_module,
        "project_path": "",
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    return {
        "schema": schema,
        "app_labels": app_labels,
        "output_path": str(out_path),
    }


def extract_and_save(
    output_file: str | Path | None = None,
) -> dict:
    """
    Extract model schemas from the currently loaded Django and save to a JSON file.
    Requires Django to be already configured (e.g. from Django shell or a running app).

    Args:
        output_file: Where to save the schema. Defaults to cwd/.django_lens_schema.json.

    Returns:
        {"schema": str, "app_labels": list[str], "output_path": str}
    """
    return extract_from_loaded_django(output_file=output_file)


def load_schema(
    schema_file: str | Path | None = None,
    project_path: str | None = None,
) -> tuple[str, list[str]]:
    """
    Load schema and app_labels from a cached JSON file.

    Args:
        schema_file: Path to the schema JSON. If None, uses project_path or cwd.
        project_path: Used to resolve schema file when schema_file is None.

    Returns:
        (schema_str, app_labels)
    """
    if schema_file is not None:
        path = Path(schema_file).resolve()
    elif project_path:
        path = Path(project_path).resolve() / DEFAULT_SCHEMA_FILE
    else:
        path = Path.cwd() / DEFAULT_SCHEMA_FILE

    if not path.exists():
        raise FileNotFoundError(
            f"Schema file not found: {path}. Run extract_and_save() first from Django shell."
        )

    data = json.loads(path.read_text(encoding="utf-8"))
    return data["schema"], data.get("app_labels", [])
