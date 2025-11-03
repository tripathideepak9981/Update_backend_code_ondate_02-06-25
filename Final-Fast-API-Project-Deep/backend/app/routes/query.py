# app/routes/query.py

import re
import sqlalchemy
import pandas as pd
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from fastapi.responses import JSONResponse
import json
from fastapi.responses import Response


from app.utils.auth_helpers import get_current_user
from app.models import User
from app.state import get_user_state, get_duckdb_connection
from app.database import get_db
from app.config import MODEL_NAME, GOOGLE_API_KEY

from app.utils.llm_helpers import (
    build_analysis_prompt,
    build_followup_prompt,
    parse_analysis_response,
    extract_sql_from_llm_response,
    generate_statistical_response,
    generate_non_sql_response
)

from app.utils.sql_helpers import (
    clean_sql_query,
    execute_sql_query
)

from app.utils.data_processing import (
    summarize_schema_for_llm,
    generate_structured_business_overview
)

from app.utils.llm_factory import get_llm
llm = get_llm()

router = APIRouter()

class UserQuery(BaseModel):
    query: str


import re

def patch_mysql_limit_in_subquery(sql_query: str) -> str:
    """
    Dynamically wrap subqueries using LIMIT inside a SELECT alias block.
    Fixes MySQL limitation on LIMIT in IN()/NOT IN().
    """
    pattern = re.compile(
        r"""
        (NOT\s+IN\s*|IN\s*)           # Match IN or NOT IN
        \(\s*                         # Opening parenthesis
        (SELECT\s+.*?LIMIT\s+\d+\s*)  # SELECT ... LIMIT N
        \)                            # Closing parenthesis
        """, re.IGNORECASE | re.DOTALL | re.VERBOSE
    )

    def replacer(match):
        keyword = match.group(1)
        inner_query = match.group(2).strip()
        return f"""{keyword} (
            SELECT * FROM (
                {inner_query}
            ) AS subquery_fix
        )"""

    return pattern.sub(replacer, sql_query)


# ✅ Add validator helper
def validate_generated_sql(sql: str, allowed_tables: list[str]) -> tuple[bool, str]:
    if not sql.lower().startswith("select"):
        return False, "Only SELECT queries are allowed."
    # Check for table usage
    for table in allowed_tables:
        if table.lower() in sql.lower():
            return True, ""
    return False, "Query uses unknown or missing table(s)."

@router.post("/execute_query")
def execute_user_query(
    user_query: UserQuery,
    current_user: User = Depends(get_current_user),
    db: sqlalchemy.orm.Session = Depends(get_db),
):
    user_state = get_user_state(current_user.id)
    query_text = user_query.query.strip()

    if not user_state.table_names:
        raise HTTPException(status_code=400, detail="No tables loaded.")

    schema_info = summarize_schema_for_llm(user_state.table_names)
    overview_stats = generate_structured_business_overview(user_state.table_names)
    preview_df = user_state.table_names[0][1] if user_state.table_names else pd.DataFrame()

    # Step 1: Detect if query is contextual (smart, dynamic)
    last_entry = user_state.get_last_chat_entry()
    resolved_query = query_text

    if last_entry:
        try:
            from app.utils.llm_helpers import is_query_contextual, build_context_aware_prompt
            last_q = last_entry["user_query"]
            last_a = str(last_entry["result_preview"])[:300]
            if is_query_contextual(query_text, last_q, last_a, llm):
                rephrase_prompt = build_context_aware_prompt(query_text, last_q, last_a)
                resolved_query = llm(rephrase_prompt).strip()
        except Exception:
            resolved_query = query_text  # fallback

    # Step 2: Check for simple statistics request
    stat_keywords = ["mean", "median", "mode", "standard deviation", "std deviation"]

    # Only trigger statistical logic if it's clearly not SQL-driven
    if any(kw in query_text.lower() for kw in stat_keywords) and "of" in query_text.lower():
        result = generate_statistical_response(query_text, preview_df, llm)
        if result and result.get("status") == "success":
           return result


    # Step 3: Use LLM to generate SQL
    try:
        full_prompt = build_analysis_prompt(resolved_query, schema_info, overview_stats)
        llm_response = llm(full_prompt)
        parsed = parse_analysis_response(llm_response)
        sql_query = extract_sql_from_llm_response(parsed.get("sql", ""))
        explanation = parsed.get("explanation", "").strip()

        if not sql_query:
            return generate_non_sql_response(resolved_query, overview_stats, preview_df, llm)

        sql_query = clean_sql_query(sql_query)

        # ✅ Validate SQL
        allowed_tables = [name for name, _ in user_state.table_names]
        is_valid, error_msg = validate_generated_sql(sql_query, allowed_tables)
        if not is_valid:
            return {
                "status": "error",
                "message": f"Generated SQL is invalid: {error_msg}",
                "suggestion": "Try rephrasing your question or checking table names."
            }
        sql_query = patch_mysql_limit_in_subquery(sql_query)


    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "message": "Failed to generate SQL from your query."
        }

    # Step 4: Execute SQL
    try:
        if user_state.personal_engine:
            result_df = execute_sql_query(sql_query, resolved_query, user_state.personal_engine)
        else:
            con = user_state.duckdb_conn
            for tname, df in user_state.table_names:
                con.register(tname, df)
            result_df = con.execute(sql_query).df()

        user_state.add_chat_entry(query_text, resolved_query, sql_query, result_df)
        
        clean_df = result_df.where(pd.notnull(result_df), None)
        for col in clean_df.select_dtypes(include=["datetime64[ns]", "datetime64[ns, UTC]"]).columns:
            clean_df[col] = clean_df[col].astype(str)

        clean_result = clean_df.to_dict(orient="records")

        response_data = {
            "status": "success",
            "query": query_text,
            "resolved_query": resolved_query,
            "sql_query": sql_query,
            "response": explanation or "Query executed successfully.",
            "result": clean_result
        }

        # ✅ Safe JSON serialization
        json_str = json.dumps(response_data, allow_nan=False)
        return Response(content=json_str, media_type="application/json")
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "message": "SQL was generated but failed to execute. Check the query or your data."
        }


@router.post("/reset_context")
def reset_chat_context(current_user: User = Depends(get_current_user)):
    user_state = get_user_state(current_user.id)
    user_state.reset()
    return {"status": "chat context reset"}

@router.get("/initial_suggestions")
def get_initial_suggestions(current_user: User = Depends(get_current_user)):
    user_state = get_user_state(current_user.id)
    return {"suggested_questions": user_state.initial_suggestions}