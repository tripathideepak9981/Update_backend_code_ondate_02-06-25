# app/routes/db.py

from fastapi import APIRouter, HTTPException, Body, Depends
from pydantic import BaseModel
from typing import List
from app.utils.db_helpers import connect_personal_db, list_tables, disconnect_database
from app.state import get_user_state
from fastapi.encoders import jsonable_encoder
import logging
import pandas as pd
import math
from app.models import User
from app.utils.auth_helpers import get_current_user
from sqlalchemy import text
from app.config import MYSQL_USER, MYSQL_PASSWORD, MYSQL_HOST
from sqlalchemy.exc import OperationalError, InterfaceError, ProgrammingError, DBAPIError

router = APIRouter()
logger = logging.getLogger("db")
logger.setLevel(logging.DEBUG)


class DBConnectionParams(BaseModel):
    db_type: str
    host: str
    port: int
    user: str
    password: str
    database: str


def clean_nan(obj):
    """Recursively replace NaN with None for JSON serializable output."""
    if isinstance(obj, list):
        return [clean_nan(item) for item in obj]
    elif isinstance(obj, dict):
        return {key: clean_nan(value) for key, value in obj.items()}
    elif isinstance(obj, float):
        if math.isnan(obj):
            return None
        else:
            return obj
    else:
        return obj


@router.post("/connect_db")
def connect_db(params: DBConnectionParams, current_user: User = Depends(get_current_user)):
    """
    Connects to a personal MySQL/Vertica database and returns available tables.
    Provides user-friendly error messages for all major connection issues.
    """
    from sqlalchemy.engine import URL  # ✅ Import added here
    logger.info(f"Attempting DB connection for user: {current_user.username}")

    try:
        # Step 1: Handle Vertica separately (directly here for better TLS control)
        if params.db_type.lower() == "vertica":
            try:
                from sqlalchemy import create_engine
                import sqlalchemy.dialects
                sqlalchemy.dialects.registry.register(
                    "vertica.vertica_python", "vertica_sqlalchemy.dialect", "VerticaDialect"
                )

                # ✅ Build Vertica URL safely (handles special characters like @, :, /, etc.)
                connection_url = URL.create(
                    drivername="vertica+vertica_python",
                    username=params.user,
                    password=params.password,
                    host=params.host,
                    port=params.port or 5433,
                    database=params.database
                )

                # ✅ Try connection with TLS disabled (server doesn't support SSL)
                try:
                    engine = create_engine(connection_url, connect_args={"tlsmode": "disable"})
                    with engine.connect() as conn:
                        version = conn.execute(text("SELECT version();")).scalar()

                    logger.info(f"✅ Vertica connected successfully: {version}")
                    tables = list_tables(engine)

                    user_state = get_user_state(current_user.id)
                    user_state.personal_engine = engine

                    return jsonable_encoder({
                        "status": "connected",
                        "db_type": "vertica",
                        "tables": tables
                    })

                except Exception as e:
                    msg = str(e).lower()
                    if "ssl" in msg or "tls" in msg:
                        raise HTTPException(
                            status_code=400,
                            detail="🔒 SSL/TLS configuration mismatch. Try disabling SSL or check server TLS settings."
                        )
                    elif "authentication failed" in msg or "invalid user" in msg:
                        raise HTTPException(status_code=400, detail="❌ Invalid Vertica username or password.")
                    elif "connection refused" in msg or "timeout" in msg:
                        raise HTTPException(status_code=400, detail="⚠️ Cannot reach Vertica server. Check host/port.")
                    elif "database" in msg and "not found" in msg:
                        raise HTTPException(status_code=400, detail="❌ Specified Vertica database not found.")
                    else:
                        logger.error(f"❌ Vertica DB connection failed: {e}")
                        raise HTTPException(
                            status_code=400,
                            detail="⚠️ Could not establish Vertica connection. Please verify settings."
                        )
            except HTTPException:
                raise
            except Exception as e:
                logger.exception("Unexpected Vertica connection error")
                raise HTTPException(status_code=500, detail=f"Unexpected Vertica error: {str(e)}")

        # Step 2: Handle MySQL or other databases (existing logic)
        try:
            # ✅ Use URL.create() for MySQL too (handles special characters)
            connection_url = URL.create(
                drivername="mysql+pymysql",
                username=params.user,
                password=params.password,
                host=params.host,
                port=params.port or 3306,
                database=params.database
            )

            from sqlalchemy import create_engine
            engine = create_engine(connection_url)

        except Exception as e:
            logger.error(f"Failed to create MySQL engine: {e}")
            raise HTTPException(status_code=500, detail="Failed to initialize database engine.")

        # Step 3: Test connection
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except (OperationalError, InterfaceError, ProgrammingError, DBAPIError) as e:
            msg = str(e).lower()

            if "access denied" in msg or "authentication failed" in msg:
                detail = "❌ Invalid username or password. Please verify your credentials."
            elif "unknown database" in msg:
                detail = "❌ Database not found. Please check the database name."
            elif "can't connect" in msg or "connection refused" in msg:
                detail = "⚠️ Unable to reach database host. Please check host and port."
            elif "timeout" in msg:
                detail = "⏳ Connection timed out. Please ensure the database server is reachable."
            elif "host" in msg and "not known" in msg:
                detail = "⚠️ Invalid host address. Please check the hostname or IP."
            elif "ssl" in msg:
                detail = "🔒 SSL connection error. Please verify SSL configuration."
            elif "too many connections" in msg:
                detail = "🚫 Too many open connections. Try again later."
            elif "could not connect" in msg or "failed to establish" in msg:
                detail = "⚠️ Could not establish connection. Please check settings."
            else:
                detail = f"Unexpected database error: {str(e)}"

            logger.error(f"❌ DB connection failed: {e}")
            raise HTTPException(status_code=400, detail=detail)

        # Step 4: Fetch tables
        try:
            tables = list_tables(engine)
        except Exception as e:
            logger.error(f"[list_tables] Failed to fetch tables: {e}")
            raise HTTPException(
                status_code=500,
                detail="Connected successfully, but failed to retrieve table list. Please verify permissions."
            )

        # Step 5: Store connection
        user_state = get_user_state(current_user.id)
        user_state.personal_engine = engine

        logger.info(f"✅ Connected successfully. Tables: {tables}")
        return jsonable_encoder({"status": "connected", "tables": tables})

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unexpected error during connect_db()")
        raise HTTPException(status_code=500, detail=f"Unexpected server error: {str(e)}")



# app/routes/db.py

@router.post("/load_tables")
def load_tables(
    table_names: List[str] = Body(...),
    current_user: User = Depends(get_current_user)
):
    user_state = get_user_state(current_user.id)

    if not getattr(user_state, "personal_engine", None):
        raise HTTPException(status_code=400, detail="No personal database connected.")

    engine = user_state.personal_engine
    
    # --- START OF FIX ---
    # Get the dialect (e.g., 'mysql' or 'vertica')
    try:
        dialect = engine.dialect.name.lower()
    except Exception:
        dialect = "mysql" # Default, though it should always be set
    
    logger.info(f"Loading tables for dialect: {dialect}")
    # --- END OF FIX ---

    previews = {}
    loaded_tables = []

    for table in table_names:
        try:
            # --- START OF FIX ---
            # Build the query based on the database dialect
            query = ""
            if dialect == "mysql":
                # MySQL uses backticks
                query = f"SELECT * FROM `{table}`;"
            elif dialect == "vertica":
                # Vertica uses schema.table format (which list_tables already provides)
                # It does NOT use backticks.
                query = f"SELECT * FROM {table};"
            else:
                # A sensible default for other SQL databases (like PostgreSQL)
                query = f"SELECT * FROM {table};"
            # --- END OF FIX ---
            
            logger.info(f"Executing preview query: {query}")
            
            df = pd.read_sql_query(query, engine)
            loaded_tables.append((table, df))
            logger.info(f"Fetched table '{table}' with shape: {df.shape}")

            if df.empty:
                previews[table] = "No data available (table is empty)."
            else:
                preview_data = df.head(10).to_dict(orient="records")
                preview_data = clean_nan(preview_data)
                previews[table] = preview_data if preview_data else "No preview data available."
        except Exception as e:
            logger.error(f"Error fetching data for table '{table}': {e}")
            previews[table] = f"Error fetching data: {e}"

    user_state.table_names = loaded_tables

    response = {
        "status": "tables loaded",
        "tables": table_names,
        "previews": previews,
        "debug": "direct fetch preview"
    }
    logger.info(f"Final Response: {response}")
    return jsonable_encoder(response)

@router.post("/disconnect")
def disconnect(current_user: User = Depends(get_current_user)):
    user_state = get_user_state(current_user.id)
    disconnect_database(user_state)
    return jsonable_encoder({"status": "disconnected"})


logger = logging.getLogger(__name__)


@router.delete("/delete_table/{table_name}")
def delete_table(table_name: str, current_user: User = Depends(get_current_user)):
    """Deletes a specific table from the user's personal database."""
    engine = connect_personal_db(
        db_type="mysql",
        host=MYSQL_HOST,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=current_user.dynamic_db
    )

    if not engine:
        raise HTTPException(status_code=500, detail="Failed to connect to database.")

    try:
        with engine.connect() as conn:
            result = conn.execute(text(f"SHOW TABLES LIKE '{table_name}'"))
            if result.fetchone() is None:
                raise HTTPException(status_code=404, detail="Table not found in database.")
    except Exception as e:
        logger.error(f"Error checking table existence: {e}")
        raise HTTPException(status_code=500, detail="Error checking table existence.")

    try:
        with engine.connect() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS `{table_name}`"))
            logger.info(f"Table '{table_name}' deleted successfully for user {current_user.username}.")
        user_state = get_user_state(current_user.id)
        user_state.table_names = [t for t in user_state.table_names if t[0] != table_name]
        return {"status": "success", "message": f"Table '{table_name}' deleted from database."}
    except Exception as e:
        logger.error(f"Failed to delete table '{table_name}': {e}")
        raise HTTPException(status_code=500, detail="Failed to delete table.")


@router.get("/load_user_tables_with_preview")
def load_user_tables_with_preview(current_user: User = Depends(get_current_user)):
    from app.utils.db_helpers import connect_personal_db, list_tables
    import pandas as pd
    from app.state import get_user_state
    from fastapi.encoders import jsonable_encoder
    import math
    import time
    from app.utils.data_processing import get_data_preview

    def clean_nan_fast(df: pd.DataFrame) -> list:
        return df.replace({math.nan: None}).to_dict(orient="records")

    start_time = time.time()
    user_state = get_user_state(current_user.id)
    engine = connect_personal_db(
        db_type="mysql",
        host=MYSQL_HOST,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=current_user.dynamic_db
    )

    if not engine:
        raise HTTPException(status_code=500, detail="Database connection failed.")

    table_names = list_tables(engine)
    loaded_tables, original_tables, previews = [], [], []

    for table_name in table_names:
        try:
            df = pd.read_sql_query(f"SELECT * FROM `{table_name}` LIMIT 20", con=engine)
            loaded_tables.append((table_name, df))
            original_tables.append((table_name, df.copy()))
            preview = clean_nan_fast(df)
            previews.append({"table_name": table_name, "preview": preview})
        except Exception as e:
            logger.warning(f"Failed to load table '{table_name}': {e}")

    user_state.table_names = loaded_tables
    user_state.original_table_names = original_tables
    user_state.personal_engine = engine

    duration = round(time.time() - start_time, 2)
    logger.info(f"[Preview Load] Completed in {duration}s for user {current_user.username}")
    return {"status": "success", "tables": previews, "load_time_sec": duration}