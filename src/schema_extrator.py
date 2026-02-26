"""
Django schema extractor: crawl a Django project for models and persist the schema.

Given a project path, bootstraps Django, reads INSTALLED_APPS from settings.py,
extracts model schemas, and stores them in a file for use by prompt_builder.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


# Default schema cache file (relative to project or cwd)
DEFAULT_SCHEMA_FILE = ".django_lens_schema.json"


def _find_settings_module(project_path: str) -> str:
    """
    Discover the Django settings module by inspecting manage.py or searching
    for settings.py in the project.
    """
    project = Path(project_path).resolve()
    if not project.is_dir():
        raise ValueError(f"Project path is not a directory: {project_path}")

    # Try manage.py first (most reliable)
    manage_py = project / "manage.py"
    if manage_py.exists():
        content = manage_py.read_text()
        match = re.search(
            r"DJANGO_SETTINGS_MODULE\s*[=,]\s*['\"]([^'\"]+)['\"]",
            content,
        )
        if match:
            return match.group(1)

    # Fallback: search for settings.py
    for path in project.rglob("settings.py"):
        rel = path.relative_to(project)
        parts = list(rel.parts[:-1]) + ["settings"]
        return ".".join(parts) if parts else "settings"

    raise ValueError(
        f"Cannot find Django settings. Ensure {project_path} contains manage.py "
        "or settings.py."
    )


def bootstrap_django(project_path: str) -> str:
    """
    Configure Django for the given project. Call this before using schema/queryset
    when loading from file, so that django.apps can resolve models.
    Returns the settings module name.
    """
    settings_module = _find_settings_module(project_path)
    _get_installed_app_labels(project_path, settings_module)
    return settings_module


def _get_installed_app_labels(project_path: str, settings_module: str) -> list[str]:
    """
    Load the Django settings module and extract INSTALLED_APPS.
    Returns app labels (e.g. 'myapp', 'auth') excluding Django built-ins.
    """
    project = Path(project_path).resolve()
    if str(project) not in sys.path:
        sys.path.insert(0, str(project))

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", settings_module)

    import django
    django.setup()

    from django.conf import settings

    app_labels = []
    for app in getattr(settings, "INSTALLED_APPS", []):
        if isinstance(app, str):
            # Skip Django contrib and third-party by default; keep project apps
            if app.startswith("django."):
                continue
            # App config like "myapp.apps.MyAppConfig" -> "myapp"
            label = app.split(".")[0]
            app_labels.append(label)
        # Skip AppConfig instances for simplicity; string entries cover most cases

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


def extract_and_save(
    project_path: str,
    output_file: str | Path | None = None,
) -> dict:
    """
    Crawl the Django project at `project_path`, extract model schemas from
    INSTALLED_APPS (via settings.py), and save to a JSON file.

    Returns:
        {"schema": str, "app_labels": list[str], "output_path": str}
    """
    project = Path(project_path).resolve()
    settings_module = _find_settings_module(str(project))
    app_labels = _get_installed_app_labels(str(project), settings_module)

    schema = get_models_schema(app_labels)

    out_path = Path(output_file or project / DEFAULT_SCHEMA_FILE)
    out_path = out_path.resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "schema": schema,
        "app_labels": app_labels,
        "settings_module": settings_module,
        "project_path": str(project),
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    return {
        "schema": schema,
        "app_labels": app_labels,
        "output_path": str(out_path),
    }


def load_schema(
    schema_file: str | Path | None = None,
    project_path: str | None = None,
) -> tuple[str, list[str]]:
    """
    Load schema and app_labels from the cached JSON file.

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
            f"Schema file not found: {path}. Run extract_and_save(project_path) first."
        )

    data = json.loads(path.read_text(encoding="utf-8"))
    return data["schema"], data.get("app_labels", [])
