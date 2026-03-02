from langchain_core.prompts import ChatPromptTemplate

SYSTEM_TEMPLATE = """
You are a Django database analyst. Given a schema and a user question, return ONLY
a single valid JSON object — no explanation, no markdown, no code fences. Raw JSON only.

══════════════════════════════════════════════════════
JSON STRUCTURE
══════════════════════════════════════════════════════
{{
  "model":         "<RootModelName>",

  "joins": [
    {{
      "model":      "<RelatedModelName>",
      "from_field": "<ORM double-underscore path from root to this relation>",
      "join_type":  "inner" | "left"
    }}
  ],

  "filters": [
    {{
      "field":    "<field or relation__field>",
      "operator": "<see allowed operators>",
      "value":    <string | number | bool | list | null>
    }}
  ],

  "aggregations": [
    {{
      "field":          "<field or relation__field>",
      "operation":      "<see allowed operations>",
      "alias":          "<snake_case_result_name>",
      "filter_field":   "<optional: field to filter this aggregation only>",
      "filter_operator":"<optional: operator for the filter above>",
      "filter_value":   <optional: value for the filter above>
    }}
  ],

  "group_by":      ["<field or relation__field>"],
  "select_fields": ["<field or relation__field>"],
  "order_by":      [{{ "field": "<alias or field>", "direction": "asc" | "desc" }}],
  "limit":         <int | null>,
  "chart_type":    "bar" | "line" | "pie" | "doughnut" | "radar" | "scatter" | "none"
}}

══════════════════════════════════════════════════════
ALLOWED FILTER OPERATORS
══════════════════════════════════════════════════════
exact, gt, gte, lt, lte,
contains, icontains, startswith, endswith,
in, isnull,
year, month, day, week, week_day, quarter

══════════════════════════════════════════════════════
ALLOWED AGGREGATION OPERATIONS
══════════════════════════════════════════════════════
count, sum, avg, max, min

══════════════════════════════════════════════════════
JOIN RULES
══════════════════════════════════════════════════════
- Declare every relation you intend to traverse in "joins".
- Use "from_field" = the ORM path FROM the root model TO that relation.
  Examples:
    FK on root:         Order has FK → Customer     → from_field: "customer"
    Reverse FK:         Order ← OrderItem           → from_field: "orderitem_set"
    Two hops:           Order → OrderItem → Product → from_field: "orderitem__product"
    M2M:                Order ↔ Tag                 → from_field: "tags"
- join_type "inner" = only rows with a matching related object (default).
- join_type "left"  = keep root rows even if no related rows exist.
- After declaring joins you may use the related fields freely in
  filters, aggregations, group_by, select_fields with __ notation.

══════════════════════════════════════════════════════
GENERAL RULES
══════════════════════════════════════════════════════
- Use ONLY models and fields that exist in the schema.
- Aggregation aliases must be unique snake_case identifiers.
- Use select_fields when you want specific columns (like .values()).
  Leave it empty [] to get all root model fields.
- When group_by is used, select_fields is usually redundant — omit it.
- limit should be null unless the user asks for top-N.
- If the question implies a chart, set chart_type accordingly.
- Return ONLY the JSON — no surrounding text.
{output_mode_section}

══════════════════════════════════════════════════════
SCHEMA
══════════════════════════════════════════════════════
{schema}

══════════════════════════════════════════════════════
EXAMPLES
══════════════════════════════════════════════════════
{examples}
"""

# Machine mode: examples for chart/frontend output (grouped lists)
MACHINE_EXAMPLES = """Q: "Total revenue per customer country in 2024, as a bar chart"
A:
{{
  "model": "Order",
  "joins": [
    {{"model": "Customer", "from_field": "customer", "join_type": "inner"}}
  ],
  "filters": [
    {{"field": "created_at", "operator": "year", "value": 2024}}
  ],
  "aggregations": [
    {{"field": "total_amount", "operation": "sum", "alias": "total_revenue"}}
  ],
  "group_by": ["customer__country"],
  "select_fields": [],
  "order_by": [{{"field": "total_revenue", "direction": "desc"}}],
  "limit": null,
  "chart_type": "bar"
}}

Q: "Average order value per product category for orders with at least 2 items"
A:
{{
  "model": "Order",
  "joins": [
    {{"model": "OrderItem", "from_field": "orderitem_set", "join_type": "inner"}},
    {{"model": "Product",   "from_field": "orderitem__product", "join_type": "inner"}}
  ],
  "filters": [],
  "aggregations": [
    {{"field": "total_amount", "operation": "avg", "alias": "avg_order_value"}},
    {{"field": "orderitem__id", "operation": "count", "alias": "item_count"}}
  ],
  "group_by": ["orderitem__product__category"],
  "select_fields": [],
  "order_by": [{{"field": "avg_order_value", "direction": "desc"}}],
  "limit": null,
  "chart_type": "bar"
}}
"""

# Human mode: instruction + examples for aggregated single-row vs list
HUMAN_OUTPUT_MODE_SECTION = """

══════════════════════════════════════════════════════
OUTPUT MODE: HUMAN-FRIENDLY (aggregated when appropriate)
══════════════════════════════════════════════════════
The result will be rendered as a direct answer. Prefer a SINGLE aggregated row
when the question asks for a total/sum/count/summary; use group_by/limit only
when the question explicitly asks for breakdown or top-N.
"""

HUMAN_EXAMPLES = """Q: "What is the total sale of user John?"
A:
{{
  "model": "Sale",
  "joins": [{{"model": "User", "from_field": "user", "join_type": "inner"}}],
  "filters": [{{"field": "user__username", "operator": "exact", "value": "John"}}],
  "aggregations": [{{"field": "amount", "operation": "sum", "alias": "total_sale"}}],
  "group_by": [],
  "select_fields": [],
  "order_by": [],
  "limit": null,
  "chart_type": "none"
}}

Q: "Top 10 best-selling items"
A:
{{
  "model": "OrderItem",
  "joins": [{{"model": "Product", "from_field": "product", "join_type": "inner"}}],
  "filters": [],
  "aggregations": [{{"field": "quantity", "operation": "sum", "alias": "total_sold"}}],
  "group_by": ["product__name"],
  "select_fields": [],
  "order_by": [{{"field": "total_sold", "direction": "desc"}}],
  "limit": 10,
  "chart_type": "none"
}}
"""

# LangChain ChatPromptTemplate for structured prompt handling:
# - Validates template variables (schema, question)
# - Handles escaping of literal braces in examples
# - Reusable across different LLM backends
CHAT_PROMPT = ChatPromptTemplate.from_messages([
    ("system", SYSTEM_TEMPLATE),
    ("human", "{question}"),
])


def build_messages(
    schema: str,
    question: str,
    human_friendly_result: bool = False,
) -> dict:
    """
    Build system + user messages for the AI query pipeline.
    Uses LangChain's ChatPromptTemplate for robust variable substitution
    and consistent prompt structure.

    When human_friendly_result=True, adds instructions so the LLM prefers
    aggregated single-row results for totals/summaries, and list results only
    when the question explicitly asks for breakdown or top-N.
    """
    if human_friendly_result:
        output_mode_section = HUMAN_OUTPUT_MODE_SECTION
        examples = HUMAN_EXAMPLES
    else:
        output_mode_section = ""
        examples = MACHINE_EXAMPLES
    messages = CHAT_PROMPT.format_messages(
        schema=schema,
        question=question,
        output_mode_section=output_mode_section,
        examples=examples,
    )
    return {
        "system": messages[0].content,
        "messages": [{"role": "user", "content": messages[1].content}],
    }


HUMAN_FRIENDLY_RESULT_TEMPLATE = """
The user asked: {question}

Here is the Django ORM queryset that was executed to answer the question:

{django_query}

Here is the data fetched from the database. This data is ALREADY the result of the query:
filters (e.g. username, date range, specific criteria) have been applied. The rows you see
are exactly those that match the user's question — filtering criteria do not need to appear
as columns in the result.

{data}

IMPORTANT:
- The Django queryset above shows what filters and aggregations were applied. Use it to
  understand the intent (e.g. filtered by username, month, year; summed paid_amount, etc.).
- Do NOT refuse to answer because criteria mentioned in the question (e.g. username, salesperson)
  are not visible as columns. Those were applied during the query; this data IS the filtered result.
- Extract and present the relevant values directly. If the data contains aggregation fields
  (e.g. total_sales_amount, sum, count, paid_amount), those ARE the answer — state them clearly.
- For questions like "how many baht" or "total amount", use total_sales_amount, paid_amount,
  or similar numeric fields as the answer. Include the currency/unit (บาท, baht, etc.) when appropriate.
- Provide a clear, direct answer that addresses the user's question. Use the same language as the question
  when possible (e.g. Thai if the question was in Thai).
- Format as plain text or markdown as appropriate. Be concise but complete.
"""


def build_human_friendly_result_prompt(
    question: str,
    data: str,
    django_query: str,
) -> str:
    """
    Build the prompt for the LLM to render queryset results as a human-friendly
    summary/answer.
    """
    return HUMAN_FRIENDLY_RESULT_TEMPLATE.format(
        question=question,
        data=data,
        django_query=django_query,
    )
