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
- Django 5.x
- Google Gemini API key

## Installation

```bash
# Clone or add this project to your workspace
cd django-lens

# Create a virtual environment (recommended)
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

## Configuration

1. Copy the example environment file and add your Gemini API key:

```bash
cp .env.example .env
```

2. Edit `.env`:

```
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-1.5-flash   # Optional: gemini-1.5-pro, gemini-2.0-flash, etc.
```

Get your API key at [Google AI Studio](https://aistudio.google.com/apikey).

## How to Use

### Option 1: Standalone (with a Django project path)

Point Django Lens at any Django project. It will extract the schema on first run (or load from cache) and execute queries.

```python
import sys
sys.path.insert(0, "src")  # Add src to path

from ai_query import run_ai_query

result = run_ai_query(
    question="Total revenue per customer country in 2024, as a bar chart",
    project_path="/path/to/your/django/project",
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

### Option 2: Embedded (Django already configured)

When Django is already set up (e.g., inside a Django management command or view), pass `app_labels` directly:

```python
import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myproject.settings")
django.setup()

import sys
sys.path.insert(0, "/path/to/django-lens/src")
from ai_query import run_ai_query

result = run_ai_query(
    question="Count of orders per month in 2024",
    app_labels=["myapp", "orders"],
)
```

### Schema extraction (manual)

Extract and save the schema once, then reuse it:

```python
import sys
sys.path.insert(0, "src")
from schema_extrator import extract_and_save, load_schema

# Extract schema from project and save to .django_lens_schema.json
result = extract_and_save(project_path="/path/to/django/project")
print(result["output_path"])  # Path to saved schema
print(result["app_labels"])   # List of app labels

# Later: load schema without re-extracting
schema, app_labels = load_schema(schema_file="/path/to/django/project/.django_lens_schema.json")
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
├── src/
│   ├── ai_query.py          # Main entry: run_ai_query()
│   ├── schema_extrator.py   # Schema extraction & loading
│   ├── prompt_builder.py    # LLM prompt construction
│   ├── query_schema.py      # Pydantic schemas for validation
│   └── queryset_builder.py  # Translates schema → Django ORM
├── requirements.txt
├── .env.example
└── README.md
```

## API reference

### `run_ai_query(question, project_path=None, schema_file=None, app_labels=None, max_retries=2)`

Runs the full pipeline: load schema → ask LLM → validate → build queryset → return data.

| Argument       | Type   | Description |
|----------------|--------|-------------|
| `question`     | `str`  | Natural language query |
| `project_path` | `str`  | Path to Django project root (where `manage.py` lives) |
| `schema_file`  | `str`  | Override path to schema JSON (default: `project_path/.django_lens_schema.json`) |
| `app_labels`   | `list` | For embedded use: app labels when Django is already configured |
| `max_retries`  | `int`  | Number of retries if the AI returns invalid JSON or queryset fails |

**Returns:** `dict` with `success`, `question`, `query_schema`, `data`, `row_count`, `chart_type`, `chart_data`

**Raises:** `ValueError` if neither `project_path` nor `app_labels` is provided; `RuntimeError` if all retries fail.

### `extract_and_save(project_path, output_file=None)`

Extracts model schemas from a Django project and saves to JSON.

**Returns:** `{"schema": str, "app_labels": list, "output_path": str}`

## Running from the command line

Example script (create `examples/query_example.py` in your project):

```python
#!/usr/bin/env python
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ai_query import run_ai_query

if __name__ == "__main__":
    project_path = sys.argv[1] if len(sys.argv) > 1 else "."
    question = sys.argv[2] if len(sys.argv) > 2 else "Count all users"

    result = run_ai_query(question=question, project_path=project_path)
    print("Data:", result["data"])
    if result.get("chart_data"):
        print("Chart:", result["chart_data"])
```

Run it:

```bash
python examples/query_example.py /path/to/django/project "Total sales by product category"
```

## License

See the project repository for license information.
