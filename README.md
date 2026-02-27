# Django Lens

**Natural language queries for Django models, powered by AI.** Ask questions in plain English and get structured data back—with optional Chart.js-ready output for visualizations.

## Features

- **Natural language → Django ORM**: Converts questions like "Total revenue per customer country in 2024" into validated Django querysets
- **Schema extraction**: Automatically crawls your Django project for models, fields, and relationships
- **AI-powered**: Uses Google Gemini to interpret questions and produce structured query JSON
- **Chart-ready output**: Returns data shaped for bar, line, pie, doughnut, radar, and scatter charts
- **Safe & validated**: Pydantic schemas and field validation prevent SQL injection and unsafe operations

## Requirements

- Python 3.10+
- Django 4.x or 5.x
- Google Gemini API key

## Installation

```bash
pip install django-lens
```

## Configuration

Add the following to your Django project's `settings.py`:

```python
# Required
GEMINI_API_KEY = "your_gemini_api_key_here"

# Optional (defaults to gemini-1.5-flash)
GEMINI_MODEL = "gemini-1.5-flash"  # or gemini-1.5-pro, gemini-2.0-flash, etc.
```

Get your API key at [Google AI Studio](https://aistudio.google.com/apikey).

## How to Use

Use Django Lens from within your Django project (views, management commands, shell). Django must be configured before calling `run_ai_query`.

```python
from django_lens import run_ai_query

result = run_ai_query(
    question="Total revenue per customer country in 2024, as a bar chart",
    app_labels=["myapp", "orders"],  # Your Django app labels
)

print(result["data"])        # List of dicts (rows)
print(result["chart_data"])  # Chart.js-ready labels + datasets
print(result["query_schema"])  # The AI-generated query structure
```

**Output structure:**

```python
{
    "success": True,
    "question": "Total revenue per customer country in 2024, as a bar chart",
    "query_schema": { ... },   # Raw JSON from the AI
    "data": [{"country": "US", "total_revenue": 12500.50}, ...],
    "row_count": 5,
    "chart_type": "bar",
    "chart_data": {
        "labels": ["US", "UK", "DE", ...],
        "datasets": [{"label": "Total Revenue", "data": [12500.5, 8900.0, ...]}],
        "label_field": "country",
        "chart_type": "bar"
    }
}
```

### Example: Django view

```python
# views.py
from django.http import JsonResponse
from django_lens import run_ai_query

def ai_query_view(request):
    question = request.GET.get("q", "Count all users")
    result = run_ai_query(
        question=question,
        app_labels=["myapp", "auth"],
    )
    return JsonResponse(result)
```

### Example: Django management command

```python
# management/commands/query.py
from django.core.management.base import BaseCommand
from django_lens import run_ai_query

class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("question", type=str)
        parser.add_argument("--apps", nargs="+", default=["myapp"])

    def handle(self, *args, **options):
        result = run_ai_query(
            question=options["question"],
            app_labels=options["apps"],
        )
        self.stdout.write(str(result["data"]))
```

### Example: Django shell

```python
# python manage.py shell
from django_lens import run_ai_query

result = run_ai_query(
    question="Count of orders per month in 2024",
    app_labels=["orders", "myapp"],
)
```

## Schema extraction (optional)

Extract and save the schema to a JSON file for debugging or documentation:

```python
# python manage.py shell
from django_lens import extract_and_save

# Saves to .django_lens_schema.json in current directory
result = extract_and_save()
print(result["output_path"])
print(result["app_labels"])
```

## Example questions

The AI understands a wide range of questions, such as:

- *"Total revenue per customer country in 2024, as a bar chart"*
- *"Average order value per product category for orders with at least 2 items"*
- *"Top 10 customers by order count"*
- *"Count of orders per month in 2024"*
- *"Average price by product category"*

## Project structure

```
django-lens/
├── django_lens/
│   ├── __init__.py
│   ├── ai_query.py          # Main entry: run_ai_query()
│   ├── schema_extrator.py   # Schema extraction & loading
│   ├── prompt_builder.py    # LLM prompt construction
│   ├── query_schema.py      # Pydantic schemas for validation
│   └── queryset_builder.py  # Translates schema → Django ORM
├── pyproject.toml
└── README.md
```

## API reference

### `run_ai_query(question, app_labels, max_retries=2)`

Runs the full pipeline: build schema from Django models → ask LLM → validate → build queryset → return data.

| Argument     | Type   | Description |
|--------------|--------|--------------|
| `question`   | `str`  | Natural language query |
| `app_labels` | `list` | Django app labels to query (e.g. `["myapp", "orders"]`) |
| `max_retries`| `int`  | Number of retries if the AI returns invalid JSON or queryset fails |

**Returns:** `dict` with `success`, `question`, `query_schema`, `data`, `row_count`, `chart_type`, `chart_data`

**Raises:** `ValueError` if `app_labels` is empty; `RuntimeError` if `GEMINI_API_KEY` is not set or all retries fail.

### `extract_and_save(output_file=None)`

Extracts model schemas from the currently loaded Django and saves to JSON. Requires Django to be configured.

**Returns:** `{"schema": str, "app_labels": list, "output_path": str}`

### `load_schema(schema_file=None, project_path=None)`

Loads schema and app_labels from a cached JSON file (e.g. produced by `extract_and_save`).

**Returns:** `(schema_str, app_labels)`

## License

See the project repository for license information.
