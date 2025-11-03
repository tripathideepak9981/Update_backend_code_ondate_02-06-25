# app/routes/join.py

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from typing import List, Optional
from app.state import get_user_state
import pandas as pd
import uuid
from app.models import User
from app.utils.auth_helpers import get_current_user

import logging
logger = logging.getLogger("join")
logger.setLevel(logging.INFO)

router = APIRouter()

class JoinRequest(BaseModel):
    table1: str
    table2: str
    join_column1: str
    join_column2: str
    join_type: str  # "INNER", "LEFT", "RIGHT", "FULL OUTER"
    new_table_name: Optional[str] = None
    select_columns: Optional[List[str]] = Field(
        default=None,
        description="Optional: Columns to include in result (use exact joined column names)"
    )
    limit: Optional[int] = Field(default=10, description="Number of rows in preview (default 10)")

@router.post("/join_tables")
def join_tables(
    request: JoinRequest,
    current_user: User = Depends(get_current_user)
):
    user_state = get_user_state(current_user.id)

    # ✅ Access table list correctly
    tables = dict(user_state.table_names)

    if request.table1 not in tables or request.table2 not in tables:
        raise HTTPException(status_code=400, detail="Selected tables not available.")

    df1, df2 = tables[request.table1], tables[request.table2]

    # ✅ Validate join columns
    if request.join_column1 not in df1.columns or request.join_column2 not in df2.columns:
        raise HTTPException(
            status_code=400,
            detail=f"Join columns not found: {request.join_column1} in {request.table1} or {request.join_column2} in {request.table2}"
        )

    # ✅ Map join type
    join_type_map = {
        "INNER": "inner",
        "LEFT": "left",
        "RIGHT": "right",
        "FULL OUTER": "outer"
    }

    jt = request.join_type.strip().upper()
    if jt not in join_type_map:
        raise HTTPException(status_code=400, detail=f"Invalid join type. Allowed: {', '.join(join_type_map.keys())}")

    try:
        joined_df = pd.merge(
            df1, df2,
            left_on=request.join_column1,
            right_on=request.join_column2,
            how=join_type_map[jt],
            suffixes=(f"_{request.table1}", f"_{request.table2}")
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error joining tables: {e}")

    # ✅ Select columns if specified
    if request.select_columns:
        valid_cols = [col for col in request.select_columns if col in joined_df.columns]
        if not valid_cols:
            raise HTTPException(status_code=400, detail="No valid select_columns found in joined table.")
        result_df = joined_df[valid_cols]
    else:
        result_df = joined_df

    preview_df = result_df.head(request.limit or 10)
    preview = preview_df.to_dict(orient="records")
    columns = list(preview_df.columns)

    joined_name = request.new_table_name or f"joined_{request.table1}_{request.table2}_{str(uuid.uuid4())[:8]}"

    # ✅ Replace any old entry
    user_state.table_names = [(n, df) for n, df in user_state.table_names if n != joined_name]
    user_state.table_names.append((joined_name, joined_df))

    logger.info(f"User joined {request.table1} with {request.table2} into '{joined_name}' using '{jt}' join.")

    # ✅ Save to DB if engine exists
    engine = user_state.personal_engine
    
    logger.info(f"Engine for user {current_user.id}: {engine}")

    if engine:
        try:
            logger.info(f"Saving joined table `{joined_name}` to DB...")
            joined_df.to_sql(joined_name, engine, index=False, if_exists="replace")
            logger.info(f"Successfully saved `{joined_name}` to DB...")
        except Exception as e:
            logger.error(f"Failed to save joined table to DB: {e}")
            

    return {
        "joined_table_name": joined_name,
        "columns": columns,
        "preview": preview,
        "row_count": len(joined_df),
        "message": f"Join successful! You can now query this joined table as '{joined_name}'."
    }

@router.get("/available_tables")
def available_tables(current_user: User = Depends(get_current_user)):
    user_state = get_user_state(current_user.id)
    return [
        {
            "table_name": t,
            "columns": list(df.columns),
            "row_count": len(df)
        } for t, df in user_state.table_names
    ]