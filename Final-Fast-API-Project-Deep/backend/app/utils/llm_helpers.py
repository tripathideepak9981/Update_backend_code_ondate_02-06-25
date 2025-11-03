# app/utils/llm_helpers.py
 
import logging
from langchain_google_genai import GoogleGenerativeAI
from app.config import GOOGLE_API_KEY, MODEL_NAME
from app.utils.data_processing import summarize_schema_for_llm
from app.utils.sql_helpers import generate_sql_query, execute_sql_query
import sqlalchemy
import re
from datetime import datetime

import time
from app.utils.sql_helpers import enhance_user_query
from app.state import get_duckdb_connection
 
import pandas as pd
 
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
 
# Initialize global LLM instance
llm = GoogleGenerativeAI(model=MODEL_NAME, api_key=GOOGLE_API_KEY)
import time
 
def call_llm_with_retry(prompt, llm, retries=3, delay=5):
    """
    Calls LLM and retries on known quota/rate errors.
    """
    for attempt in range(retries):
        try:
            response = llm(prompt)
            if not response or response.strip() == "":
                raise ValueError("LLM returned empty response.")
            return response.strip()
        except Exception as e:
            logger.warning(f"LLM attempt {attempt+1} failed: {e}")
            if "429" in str(e) and attempt < retries - 1:
                time.sleep(delay)
                continue
            raise e
 
def icon_for_issue(issue: str) -> str:
    s = issue.lower()
    if "missing value" in s or "missing values" in s:
        return "❗"
    if "duplicate column" in s or "duplicates" in s:
        return "♻️"
    if "completely empty" in s:
        return "🚫"
    if "invalid email" in s or "valid email" in s:
        return "📧"
    if "invalid phone" in s or "valid phone" in s:
        return "📱"
    if "special characters" in s:
        return "🌀"
    if "mixed data types" in s:
        return "🔀"
    if "constant" in s or "same value" in s:
        return "⏹️"
    # Add other mappings as needed
    return "❗"

def issues_with_icons_and_numbers(issues: list) -> str:
    """
    Takes a list of issue sentences, adds icons and numbers for display.
    """
    if not issues:
        return "✅ No issues found. Everything looks good!"

    output = []
    for idx, issue in enumerate(issues, 1):
        icon = icon_for_issue(issue)
        # Clean up double numbering if LLM already puts "1.", "2." etc.
        cleaned = issue
        if cleaned[:2].isdigit() and cleaned[2] in [".", ")", "-", " "]:
            cleaned = cleaned[3:].strip()
        output.append(f"{idx}. {icon} {cleaned}")
    return "\n".join(output)


def generate_data_issue_summary(issues: list, file_name: str, llm_instance) -> str:
    """
    Summarizes detected data issues for business users.
    Returns at most 7 lines, only real detected issues.
    Avoids headings and extra numbering.
    """
    if not issues or all(("no data issues" in i.lower() or "no issues" in i.lower()) for i in issues):
        return f"✅ No issues found in '{file_name}'. Everything looks good!"

    prompt = f"""
You are a data quality assistant for business users.

Here is a list of real, grouped, counted data issues found in the file '{file_name}':
{chr(10).join(issues)}

Instructions:
- For each issue, write a short, clear, business-friendly bullet.
- Do not use technical jargon (never say "null", "NaN", "dtype", "IQR", etc).
- Show numbers (counts of columns, missing values, etc) clearly where present.
- Never invent or summarize issues, never cluster different issues into one.
- Output at most 7 bullets (pick the most important).
- Each line should be clear on what the issue is, with column names and numbers where possible.
- Do not mention outliers.
- Do not include any summary or heading lines, just direct numbered issues.
- Example: "The column 'Email' has 3 values that do not look like valid email addresses."
Just output the rephrased issues, one per line.
"""

    def is_heading_line(line: str):
        HEADINGS = [
            "here's a summary",
            "summary of data quality issues",
            "summary",
            "below is a list",
            "here are the issues",
            "data issues",
            "the following issues",
            "cleaning summary"
        ]
        l = line.lower()
        return any(h in l for h in HEADINGS)

    try:
        llm_response = llm_instance(prompt)
        # Split by real lines, remove headings, deduplicate, limit to 7
        clean_lines = []
        seen = set()
        for l in llm_response.splitlines():
            line = l.strip("•- \t")
            if not line or is_heading_line(line):
                continue
            # Remove leading numbering if present
            if line[:2].isdigit() and line[2] in [".", ")", "-", " "]:
                line = line[3:].strip()
            if line in seen:
                continue
            seen.add(line)
            clean_lines.append(line)
            if len(clean_lines) == 7:
                break
        issues_list = issues_with_icons_and_numbers(clean_lines)
    except Exception:
        # fallback: use the plain issues
        issues_list = issues_with_icons_and_numbers(issues[:7])
    return issues_list

 
def translate_natural_language_to_sql(user_query: str, schema_info: str, llm_instance: GoogleGenerativeAI) -> str:
    prompt = f"""
Translate the user's instruction to a valid SQL query for INSERT/UPDATE/DELETE.
 
User Query: {user_query}
Schema Info:
{schema_info}
 
Respond with: Final SQL Query:
"""
    response = llm_instance(prompt)
    if "Final SQL Query:" in response:
        return response.split("Final SQL Query:")[-1].strip()
    from app.utils.sql_helpers import clean_sql_query
    return clean_sql_query(response)
 
 
def classify_user_query_llm(user_query: str, llm_instance: GoogleGenerativeAI) -> str:
    prompt = f"""
You are a query classifier.
 
User Query: "{user_query}"
 
Classify as one of: SQL, SUMMARY, ANALYSIS, STATISTICAL
Respond with: Final Answer: <type>
"""
    try:
        response = llm_instance(prompt)
        for line in response.splitlines():
            if "Final Answer:" in line:
                return line.split("Final Answer:")[-1].strip().upper()
    except Exception:
        pass
    return "SQL"  # Fallback
 
 
def get_special_prompt(prompt_type: str) -> str:
    prompts = {
        "SUMMARY": """
- Strictly follow user intent and data.
- Show regional/category/time variations.
- Format clearly: Metrics, Trends, Takeaways, Recommendations.
"""
    }
    return prompts.get(prompt_type.upper(), "")
 
 
def explain_sql_failure_simple(user_query: str, sql_query: str, error_message: str, llm_instance: GoogleGenerativeAI) -> str:
    prompt = f"""
The query "{user_query}" failed.
 
Generated SQL: "{sql_query}"
Error: "{error_message}"
 
Explain this simply for a non-technical user. No SQL jargon.
"""
    try:
        return llm_instance(prompt).strip()
    except Exception:
        return "There was a problem understanding your query. Please check table or column names."
 
 
def generate_dynamic_response(user_query: str, column_name: str, value) -> str:
    prompt = f"""
User asked: "{user_query}"
Result for column "{column_name}": {value}
 
Generate a clear and friendly response. Avoid repeating the raw column name.
"""
    return llm(prompt).strip()
 
 
def generate_initial_suggestions_from_state(llm, user_state) -> list[str]:
    schema_info = "\n".join([
        f"Table: {name}, Columns: {', '.join(df.columns)}"
        for name, df in user_state.table_names

    ])
 
    prompt = f"""
A user uploaded data with this schema:
 
{schema_info}
 
Generate 5 simple, short, natural language questions to analyze the data.
Only output 1 question per line. Keep under 20 words. No technical words.
"""
 
    try:
        raw_response = call_llm_with_retry(prompt, llm)
        candidates = [q.strip("-• ").strip() for q in raw_response.splitlines() if q.strip()]
        valid_questions = []
 
        # ✅ Use direct SQL execution, not TestClient
        for q in candidates:
            try:
                enhanced_query = enhance_user_query(q, user_state.table_names)
                sql_query, _ = generate_sql_query(enhanced_query, schema_info, [], llm, user_state.table_names)
                con = user_state.duckdb_conn
                for table_name, df in user_state.table_names:

                    con.register(table_name, df)
                result_df = con.execute(sql_query).df()
                if not result_df.empty:
                    valid_questions.append(q)
            except Exception:
                continue
 
        return valid_questions[:4]
 
    except Exception as e:
        print(f"Suggestion generation failed: {e}")
        return []
 
 
from datetime import datetime

# Dictionary for known business metrics
USER_DEFINED_METRICS = {
    "conversion rate": "CAST(signups AS FLOAT) / NULLIF(visits, 0)",
    "profit margin": "CAST((revenue - cost) AS FLOAT) / NULLIF(revenue, 0)",
    "bounce rate": "CAST(bounced_sessions AS FLOAT) / NULLIF(total_sessions, 0)"
}

def inject_metric_replacements(user_query: str) -> str:
    """
    Replace known metrics in the query with SQL-safe expressions.
    """
    for term, formula in USER_DEFINED_METRICS.items():
        if term.lower() in user_query.lower():
            user_query = user_query.lower().replace(term.lower(), formula)
    return user_query


def resolve_date_phrase_to_sql(phrase: str, column: str = "date") -> str:
    """
    Translate common date phrases into SQL conditions.
    """
    today = datetime.today()
    year = today.year
    month = today.month
    phrase = phrase.lower()

    if "this year" in phrase:
        return f"YEAR({column}) = {year}"
    if "last year" in phrase:
        return f"YEAR({column}) = {year - 1}"
    if "this month" in phrase:
        return f"YEAR({column}) = {year} AND MONTH({column}) = {month}"
    if "last month" in phrase:
        prev_month = month - 1 if month > 1 else 12
        prev_year = year if month > 1 else year - 1
        return f"YEAR({column}) = {prev_year} AND MONTH({column}) = {prev_month}"
    if "last quarter" in phrase:
        q = (month - 1) // 3 + 1
        start_q = q - 1 if q > 1 else 4
        start_month = (start_q - 1) * 3 + 1
        return f"YEAR({column}) = {year if start_q != 4 else year - 1} AND MONTH({column}) BETWEEN {start_month} AND {start_month + 2}"
    return ""

def extract_sql_from_llm_response(raw_sql: str) -> str:
    """
    Extracts a valid SQL query from LLM output that uses custom markers like [START SQL] ... [END SQL].
    Falls back to original if markers are missing.
    """
    start_token = "[START SQL]"
    end_token = "[END SQL]"
    if start_token in raw_sql and end_token in raw_sql:
        sql_cleaned = raw_sql.split(start_token)[1].split(end_token)[0].strip()
        return sql_cleaned
    return raw_sql.strip()



def parse_analysis_response(response: str) -> dict:
    """
    Dynamically extract the correct block from LLM output.
    Supports:
    - SQL block with EXPLANATION
    - SUMMARY block
    - INSIGHTS block
    """
    parsed = {"sql": "", "explanation": "", "summary": "", "insights": ""}

    if "SUMMARY:" in response:
        parsed["summary"] = response.split("SUMMARY:")[1].strip()
    elif "INSIGHTS:" in response:
        parsed["insights"] = response.split("INSIGHTS:")[1].strip()
    elif "SQL:" in response:
        # Optional START/END SQL block
        parts = response.split("SQL:")[1].strip()
        if "[START SQL]" in parts:
            sql = parts.split("[START SQL]")[1].split("[END SQL]")[0].strip()
            parsed["sql"] = sql
        else:
            parsed["sql"] = parts.split("EXPLANATION:")[0].strip()

        if "EXPLANATION:" in parts:
            parsed["explanation"] = parts.split("EXPLANATION:")[1].strip()

    return parsed

def build_analysis_prompt(user_query: str, schema_info: str, overview_stats: str = "") -> str:
    """
    Unified and intelligent LLM prompt to support:
    - Summary generation
    - Plain-language insights
    - SQL query construction with robust subquery support
    """
    return f'''
You are an expert AI data analyst assisting a business user in understanding or querying their data.

Here is the user's question:
"{user_query}"

Here is the schema of the available data (tables, columns, types):
{schema_info}

{f"Here are sample stats and summary overview:\n{overview_stats}" if overview_stats else ""}

=======================
🎯 TASK OBJECTIVE
=======================

Your job is to:
1. Identify the user's intent from their question.
2. Depending on the intent, return one of the following:
   - A plain-language **SUMMARY** of the data
   - High-level **INSIGHTS** or trends from the data
   - A valid **SQL** query using SELECT-only logic

=======================
📌 DECISION LOGIC
=======================

1. 🔍 If the user wants a **summary** (e.g. "summarize the data", "describe this dataset"):
   - Describe what the dataset contains
   - Highlight column types (numeric, categorical, dates)
   - Mention row/column count, uniqueness, or missing values
   - Keep it under 6 sentences
   - Start your response with:
     SUMMARY:

2. 📊 If the user asks for **insights** (e.g. "what trends do you see", "what are the top groups", "what stands out"):
   - Provide helpful analysis in plain English
   - Mention dominant patterns, spikes, anomalies, common values
   - DO NOT return SQL
   - Start your response with:
     INSIGHTS:

3. 🧠 If the user is asking for **data retrieval, filtering, aggregations, rankings, or comparisons**, such as:
   - totals, averages, minimums, maximums
   - comparisons between groups (e.g., "more than average", "top 3", "second highest")
   - identifying specific rows that match ranked results

Then:
   - Write a SELECT-only SQL query
   - Use inferred filters, proper WHERE and GROUP BY clauses, and column aliases
   - ✅ If user asks about "common", "typical", or "frequent" patterns across groups (e.g. conditions in high-mortality centers), use **AVG(...)** across filtered groups — not raw rows
   - ❌ Avoid using `LIMIT 1` unless the user clearly asks for a single example or center
   - ✅ For comparisons like "higher than average", use a subquery with `AVG(...)`
   - ❌ Do NOT use SELECT *
   - ✅ Use subqueries where needed, especially for:
       - comparing aggregates (e.g., admissions > average admissions)
       - computing RATES such as "sepsis rate", "oxygen usage rate", or any percentage: use numerator / denominator (e.g., death_sepsis / total_treated), cast as FLOAT and handle division-by-zero safely (use NULLIF)
       - finding top or second-best entries (avoid LIMIT N, OFFSET tricks like LIMIT 1,1)
       - filtering by ranked or aggregated values (e.g., WHERE x = (SELECT MAX(...)))
   - ❌ Do NOT write SQL for mean, median, mode, standard deviation — those are handled separately by the analysis engine

SQL:
[START SQL]
SELECT ...
FROM ...
WHERE ...
GROUP BY ...
[END SQL]

EXPLANATION:
You are a professional data analyst presenting this result to a non-technical business stakeholder (like a product manager, CEO, or client).

Write a **short and clear explanation** of what this SQL result tells us, in business terms.

🧠 Guidelines:
- DO NOT mention SQL syntax or terms like SELECT, GROUP BY, JOIN, etc.
- Focus on **what the result reveals or answers** (e.g., total counts, comparisons, trends).
- Be **confident, natural, and helpful**, like you're in a business meeting.
- Use column names **only if needed**, and always in simple, readable form.
- Keep it **1–2 sentences max**. Avoid technical jargon completely.

🎯 Goal: Help the business user make sense of the result, and decide what to do next.


=======================
🚫 RULES
=======================

- DO NOT write INSERT, UPDATE, DELETE queries
- DO NOT use SELECT *
- DO NOT mention SQL structure in EXPLANATION
- Your response must follow one of the 3 formats:
  → SUMMARY: ...
  → INSIGHTS: ...
  → SQL + EXPLANATION

If unsure of the user's intent, default to providing a SUMMARY.

=======================
📌 EXAMPLE — SUBQUERY REFERENCE
=======================

Q: Which districts have more total admissions than the average total admissions across all districts?

SQL:
[START SQL]
SELECT district, SUM(admission) AS total_admissions
FROM childhealthdatafortesting_1_sheet1
GROUP BY district
HAVING SUM(admission) > (
    SELECT AVG(total)
    FROM (
        SELECT SUM(admission) AS total
        FROM childhealthdatafortesting_1_sheet1
        GROUP BY district
    ) AS district_totals
);
[END SQL]

EXPLANATION:
This shows only the districts where total admissions are above the average of all district totals.
    '''.strip()

def generate_non_sql_response(user_query: str, overview_stats: str, sample_df: pd.DataFrame, llm_instance) -> dict:
    try:
        preview_md = sample_df.iloc[:5, :5].to_markdown(index=False) if not sample_df.empty else ""
    except Exception:
        preview_md = ""

    prompt = f"""
You are an enterprise-level AI data analyst helping a business user make sense of their data.

The user asked:
"{user_query}"

Here is a business-level overview of the data:
{overview_stats}

{f"Here is a small preview of the data:\n{preview_md}" if preview_md else "[No data preview available]"}

============================
🎯 OBJECTIVE
============================

Your goal is to provide a **clear, useful, and user-friendly response** to the user's question using the actual data.

Adapt your answer based on the user's intent. Consider the following rules:

1. 🔍 If the user asks for a **summary** (e.g. "summarize the data", "generate a summary about the dataset", "give me a data overview"):

- Respond in a structured format that’s friendly for business and non-technical users.
- Keep it concise (max 6–8 lines).
- Use natural, plain English.
- Organize the response with visual headings and bullet points.

Use this format:

📊 Dataset Overview  
<Brief 1–2 sentence summary of what the dataset is about — include context like department, timeframe, and type of metrics tracked.>

📌 Key Highlights:
- <Highlight 1: State a meaningful metric or insight (e.g., "Most admissions were recorded in District X.")>
- <Highlight 2: State another trend, average, or outlier in plain language>
- <Highlight 3: Continue with 2–3 more bullet points showing trends, patterns, or anomalies>
- <Avoid technical language like standard deviation, null values, or schema terms>

✅ End the summary with a final takeaway if possible:


2. 🔹 If the user asked for **insights, patterns, trends, or what stands out**:
   - Focus on differences, spikes, trends, or anomalies
   - Highlight surprising or actionable patterns
   - Avoid just listing stats — interpret the meaning
   - Suggest what could be explored or done next

3. 🔹 If the user asked something **open-ended** ("tell me about this", "what's interesting here", "how are we doing"):
   - Blend both summary and insight
   - Focus on **what matters most** (biggest metric, risk, opportunity)
   - Do not over-explain or use technical terms

4. 🔹 If the user asked a **statistical query** (mean, median, std, correlation):
   - Use real numbers from the sample data
   - Show calculations or simplified explanation
   - Be accurate, clear, and helpful

============================
📌 GUIDELINES
============================

- Use bullet points or short paragraphs
- Be specific, use **real numbers or column values**
- Avoid phrases like “this dataset contains” or “the table has columns”
- Do NOT repeat or explain the prompt back
- NEVER say “as an AI model”
- Stay within 5–7 sentences total

Respond now.
"""

    try:
        response = call_llm_with_retry(prompt, llm_instance)
        return {
            "status": "success",
            "response_type": "non_sql",
            "response": response,
            "result": []
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "message": "Failed to generate AI-based response. Try rephrasing your query."
        }


def generate_statistical_response(user_query: str, df: pd.DataFrame, llm_instance) -> dict:
    query_lower = user_query.lower()
    if df.empty:
        return {"status": "error", "message": "No data available."}

    # Step 1: Detect metric type
    metrics = {
        "mean": "mean",
        "average": "mean",
        "median": "median",
        "mode": "mode",
        "standard deviation": "std",
        "std": "std",
        "min": "min",
        "max": "max"
    }

    matched_metrics = [v for k, v in metrics.items() if k in query_lower]
    if not matched_metrics:
        return None  # This is not a statistical query

    # Step 2: Detect relevant columns
    from difflib import get_close_matches

    def normalize(s): return re.sub(r"[^a-z0-9]", "", s.lower())
    col_map = {normalize(c): c for c in df.columns}
    all_norm_cols = list(col_map.keys())

    words = user_query.split()
    phrases = [" ".join(words[i:j]) for i in range(len(words)) for j in range(i+1, min(i+4, len(words)+1))]
    candidate_phrases = [normalize(p) for p in phrases if len(p) > 3]

    used_cols = set()
    matched_cols = []
    for p in candidate_phrases:
        match = get_close_matches(p, all_norm_cols, n=1, cutoff=0.85)
        if match:
            col = col_map[match[0]]
            if col not in used_cols:
                matched_cols.append(col)
                used_cols.add(col)

    if not matched_cols:
        return {
            "status": "error",
            "message": "Couldn't identify a valid numeric column in the question."
        }

    # Step 3: Compute values
    result_parts = []
    for col in matched_cols:
        series = df[col].dropna()
        if not pd.api.types.is_numeric_dtype(series):
            continue

        for metric in matched_metrics:
            val = None
            try:
                if metric == "mean":
                    val = series.mean()
                elif metric == "median":
                    val = series.median()
                elif metric == "mode":
                    val = series.mode().iloc[0]
                elif metric == "std":
                    val = series.std()
                elif metric == "min":
                    val = series.min()
                elif metric == "max":
                    val = series.max()
            except Exception:
                continue

            if val is not None:
                result_parts.append(f"The **{metric}** of **{col}** is **{val:.2f}**.")

    if not result_parts:
        return {
            "status": "error",
            "message": "Could not compute statistical values. Check if the columns are numeric."
        }

    prompt = f"""
You are a professional data analyst assistant helping a business stakeholder understand key metrics.

The user asked:
"{user_query}"

Here is what we calculated from the actual data:
{chr(10).join(result_parts)}

Now, write a brief, clear, and business-appropriate response that includes:
- A simple summary using actual values (e.g., mean, median, etc.)
- Bullet points if there are multiple results
- Interpret the difference (e.g., median vs mean) if relevant
- Use short and precise sentences
- Avoid technical terms like 'standard deviation' unless explained

DO NOT mention that you're an AI or repeat the question.
Output must be ready to present to a manager.
"""

    try:
        response = call_llm_with_retry(prompt, llm_instance)
        return {
            "status": "success",
            "response_type": "statistical",
            "response": response,
            "result": []
        }
    except Exception:
        return {
            "status": "success",
            "response_type": "statistical",
            "response": " ".join(result_parts),
            "result": []
        }


def build_followup_prompt(current_followup, previous_sql, previous_result_df):
    schema_columns = ", ".join(previous_result_df.columns)
    preview = previous_result_df.head(5).to_markdown(index=False) if not previous_result_df.empty else ""

    return f"""
You are a smart SQL assistant.

Here are the available columns: {schema_columns}

The previous SQL query was:
{previous_sql}

Preview of result:
{preview}

User follow-up: "{current_followup}"

Modify the query as needed, but only use valid columns.

Respond in this format:
[START SQL]
<updated SQL>
[END SQL]
Optionally, include one line explanation like:
[START EXPLANATION]
<explanation>
[END EXPLANATION]
"""


def is_follow_up_query(current_query: str, last_query: str, model) -> bool:
    prompt = f"""
You are a smart assistant. Tell me if the second question depends on the first.

Previous Question: "{last_query}"
Current Question: "{current_query}"

Reply only with 'yes' or 'no'.
"""
    try:
        response = model(prompt)
        return "yes" in response.lower()
    except Exception:
        return False

def is_query_contextual(new_query: str, last_query: str, last_answer: str, model) -> bool:
    prompt = f"""
Decide if the following user query depends on the previous one.

Previous question: "{last_query}"
Answer: "{last_answer}"
Current question: "{new_query}"

Is the current query a follow-up that refers to the previous context?

Answer only: yes or no.
"""
    try:
        response = model(prompt)
        return "yes" in response.lower()
    except Exception:
        return False


def build_context_aware_prompt(current_query, last_query, last_answer):
    return f"""
You are an assistant rewriting a user query to make it standalone.

Previous query: "{last_query}"
Answer: "{last_answer}"

Now user asks: "{current_query}"

Rewrite this into a full standalone question so it makes sense on its own.
Only return the rewritten query.
"""

