"""
Django schema extractor: crawl a Django project for models and persist the schema.

Given a project path, bootstraps Django, reads INSTALLED_APPS from settings.py,
extracts model schemas, and stores them in a file for use by prompt_builder.

Uses static parsing of settings.py (no import/execution) to avoid pulling in
the target project's dependencies (environ, etc.).
"""

from __future__ import annotations

import ast
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


def _resolve_settings_path(project_path: Path, settings_module: str) -> Path:
    """Resolve settings module name to the actual settings file path."""
    # e.g. "sdr_backend.settings" -> project/sdr_backend/settings.py
    # e.g. "myproject.settings.base" -> project/myproject/settings/base.py
    parts = settings_module.split(".")
    candidates = [
        project_path.joinpath(*parts[:-1], f"{parts[-1]}.py"),
        project_path.joinpath(*parts, "__init__.py"),
    ]
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]  # raise later with clearer error


def _extract_strings_from_ast_node(node: ast.AST) -> list[str]:
    """Recursively extract string literals from an AST node (list, tuple, or BinOp)."""
    result: list[str] = []
    if isinstance(node, (ast.List, ast.Tuple)):
        for elt in node.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                result.append(elt.value)
            elif isinstance(elt, ast.Str):  # Python < 3.8
                result.append(elt.s)
            elif isinstance(elt, (ast.List, ast.Tuple, ast.BinOp)):
                result.extend(_extract_strings_from_ast_node(elt))
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        result.extend(_extract_strings_from_ast_node(node.left))
        result.extend(_extract_strings_from_ast_node(node.right))
    return result


def _parse_installed_apps_from_tree(tree: ast.AST) -> list[str] | None:
    """Extract INSTALLED_APPS from an AST tree. Returns None if not found."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "INSTALLED_APPS":
                    return _extract_strings_from_ast_node(node.value)
    return None


def _parse_installed_apps_static(settings_path: Path) -> list[str]:
    """
    Parse INSTALLED_APPS from settings.py without executing it.
    For split settings (e.g. settings/base.py), also checks sibling .py files.
    Returns the raw app identifiers (strings) as defined in the file.
    """
    # Try the main settings file first
    content = settings_path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(content)
    except SyntaxError as e:
        raise ValueError(
            f"Cannot parse settings file {settings_path}: {e}. "
            "Ensure it is valid Python."
        ) from e

    result = _parse_installed_apps_from_tree(tree)
    if result is not None:
        return result

    # Fallback: split settings (e.g. from .base import *) — search sibling files
    for sibling in settings_path.parent.glob("*.py"):
        if sibling == settings_path:
            continue
        try:
            tree = ast.parse(sibling.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        result = _parse_installed_apps_from_tree(tree)
        if result is not None:
            return result

    raise ValueError(
        f"INSTALLED_APPS not found in {settings_path} or sibling files. "
        "Ensure the settings define INSTALLED_APPS as a list/tuple."
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
    Parse INSTALLED_APPS from settings.py (without executing it) and bootstrap
    Django via settings.configure(). Returns app labels (e.g. 'myapp', 'auth')
    excluding Django built-ins.
    """
    project = Path(project_path).resolve()
    if str(project) not in sys.path:
        sys.path.insert(0, str(project))

    # Parse INSTALLED_APPS statically — never import/execute the target's settings
    settings_path = _resolve_settings_path(project, settings_module)
    if not settings_path.exists():
        raise FileNotFoundError(
            f"Settings file not found: {settings_path}. "
            f"Expected from module {settings_module!r}."
        )
    installed_apps = _parse_installed_apps_static(settings_path)

    # Bootstrap Django without loading the target's settings module
    import django
    from django.conf import settings

    if not settings.configured:
        settings.configure(
            DEBUG=True,
            SECRET_KEY="django-lens-schema-extraction",
            INSTALLED_APPS=installed_apps,
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.sqlite",
                    "NAME": ":memory:",
                }
            },
            USE_TZ=True,
        )
    django.setup()

    app_labels = []
    for app in installed_apps:
        if isinstance(app, str):
            if app.startswith("django."):
                continue
            label = app.split(".")[0]
            app_labels.append(label)

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
