# app/utils/db_helpers.py 
import os
import pandas as pd
import sqlalchemy
from sqlalchemy import text
from app.config import MYSQL_USER, MYSQL_PASSWORD, MYSQL_HOST, MYSQL_DATABASE
from app.state import get_user_state
from fastapi import Depends, HTTPException
from typing import List, Union
from sqlalchemy.engine import Engine
import logging

logger = logging.getLogger("db_helpers")
logger.setLevel(logging.INFO)


# ============================================================
# 🔁 Refresh Table Data (For both MySQL / Vertica)
# ============================================================
def refresh_tables(connection, table_names, original_table_names) -> None:
    if connection is None:
        print("Cannot refresh tables: connection is None.")
        return

    if hasattr(connection, "cursor"):
        # MySQL raw connection
        engine = sqlalchemy.create_engine(
            f"mysql+mysqlconnector://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}/{MYSQL_DATABASE}"
        )
        cursor = connection.cursor(buffered=True)
        try:
            cursor.execute("SHOW TABLES;")
            db_tables = cursor.fetchall()
        finally:
            cursor.close()
        with engine.connect() as conn:
            for tbl in db_tables:
                tbl_name = tbl[0]
                try:
                    df = pd.read_sql_query(f"SELECT * FROM `{tbl_name}`", conn)
                except Exception as e:
                    print(f"Error loading table '{tbl_name}': {e}")
                    continue
                if tbl_name not in [tn for tn, _ in table_names]:
                    table_names.append((tbl_name, df))
                else:
                    idx = next(i for i, (name, _) in enumerate(table_names) if name == tbl_name)
                    table_names[idx] = (tbl_name, df)
    else:
        # SQLAlchemy connections
        dialect_name = getattr(connection.engine.dialect, "name", "")
        if dialect_name == "mysql":
            query = text("SHOW TABLES;")
            result = connection.execute(query)
            db_tables = result.fetchall()
        else:
            query = text(
                "SELECT table_name FROM v_catalog.tables "
                "WHERE is_system_table = false "
                "AND table_schema NOT IN ('v_catalog','v_monitor','v_internal')"
            )
            result = connection.execute(query)
            db_tables = [(row[0],) for row in result.fetchall()]

        for tbl in db_tables:
            tbl_name = tbl[0]
            try:
                df = pd.read_sql_query(f"SELECT * FROM `{tbl_name}`", connection)
            except Exception as e:
                print(f"Error loading table '{tbl_name}': {e}")
                continue
            if tbl_name not in [tn for tn, _ in table_names]:
                table_names.append((tbl_name, df))
            else:
                idx = next(i for i, (name, _) in enumerate(table_names) if name == tbl_name)
                table_names[idx] = (tbl_name, df)


# ============================================================
# 📋 List Tables (MySQL / Vertica)
# ============================================================
def list_tables(connection: Union[Engine, any]) -> List[str]:
    """
    Returns a list of tables for MySQL or Vertica connections.
    """
    try:
        # ✅ Case 1: MySQL raw connector
        if hasattr(connection, "cursor") and "mysql" in str(type(connection)).lower():
            cursor = connection.cursor()
            db_name = getattr(connection, "database", MYSQL_DATABASE)
            cursor.execute(
                """
                SELECT TABLE_NAME 
                FROM INFORMATION_SCHEMA.TABLES 
                WHERE TABLE_SCHEMA = %s;
                """,
                (db_name,),
            )
            tables = cursor.fetchall()
            cursor.close()
            return [table[0] for table in tables]

        # ✅ Case 2: SQLAlchemy engine
        elif hasattr(connection, "connect"):
            with connection.connect() as conn:
                dialect = conn.engine.dialect.name.lower()
                logger.info(f"[SQLAlchemy] Detected dialect: {dialect}")

                if dialect == "mysql":
                    db_name = connection.url.database
                    query = text(
                        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = :schema"
                    )
                    result = conn.execute(query, {"schema": db_name})
                    tables = [row[0] for row in result.fetchall()]
                    logger.info(f"[MySQL] Tables found: {tables}")
                    return tables

                elif dialect == "vertica":
                    logger.info("[Vertica] Fetching non-system schemas...")
                    try:
                        schema_query = text("""
                            SELECT schema_name 
                            FROM v_catalog.schemata 
                            WHERE is_system_schema = false
                        """)
                        schemas = [row[0] for row in conn.execute(schema_query).fetchall()]
                    except Exception as e:
                        logger.error(f"[Vertica] Failed to fetch schemas: {e}")
                        return []

                    tables = []
                    for schema in schemas:
                        try:
                            result = conn.execute(text("""
                                SELECT table_name 
                                FROM v_catalog.tables 
                                WHERE table_schema = :schema AND is_system_table = false
                            """), {"schema": schema})
                            rows = result.fetchall()
                            tables.extend([f"{schema}.{row[0]}" for row in rows])
                        except Exception as e:
                            logger.warning(f"[Vertica] Failed to query schema '{schema}': {e}")
                    logger.info(f"[Vertica] Tables found: {tables}")
                    return tables

                else:
                    logger.warning(f"Unsupported DB dialect: {dialect}")
                    return []

        else:
            logger.warning("Unsupported connection type for listing tables.")
            return []

    except Exception as e:
        logger.error(f"[list_tables] Error listing tables: {e}", exc_info=True)
        return []


# ============================================================
# 🔌 Main Connection Function (MySQL / Vertica)
# ============================================================
import logging
from fastapi import HTTPException

logger = logging.getLogger(__name__)


from sqlalchemy.engine import URL

def connect_personal_db(db_type, host, user, password, database, port=3306):
    """
    Create and validate a connection to either MySQL or Vertica.
    Uses sqlalchemy.engine.URL.create to avoid URL-encoding issues with special chars.
    """
    from sqlalchemy import create_engine, text
    import traceback
    import logging
    from fastapi import HTTPException

    logger = logging.getLogger("connect_personal_db")
    logger.setLevel(logging.INFO)

    try:
        db_type_l = str(db_type).lower()

        # -----------------------------
        # MySQL
        # -----------------------------
        if db_type_l == "mysql":
            # Build a proper URL object (avoids manual percent-encoding)
            url = URL.create(
                drivername="mysql+mysqlconnector",
                username=str(user),
                password=str(password),
                host=str(host),
                port=int(port) if port else 3306,
                database=str(database),
            )
            engine = create_engine(url, pool_pre_ping=True)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1;"))
            logger.info(f"✅ MySQL connection OK for DB: {database}")
            return engine

        # -----------------------------
        # Vertica
        # -----------------------------
        elif db_type_l == "vertica":
            # Registering vertica dialect might be needed in some environments;
            # if you already have vertica_sqlalchemy installed this is optional.
            try:
                import sqlalchemy.dialects
                sqlalchemy.dialects.registry.register(
                    "vertica.vertica_python", "vertica_sqlalchemy.dialect", "VerticaDialect"
                )
            except Exception:
                # Not fatal — registration may already exist
                logger.debug("Vertica dialect registration skipped/failed (might already exist).")

            url = URL.create(
                drivername="vertica+vertica_python",
                username=str(user),
                password=str(password),
                host=str(host),
                port=int(port) if port else 5433,
                database=str(database),
            )

            # use connect_args to control tlsmode if your Vertica server needs it
            engine = create_engine(url, connect_args={"tlsmode": "disable"})
            try:
                with engine.connect() as conn:
                    conn.execute(text("SELECT version();"))
                logger.info(f"✅ Vertica connection OK for DB: {database}")
                return engine
            except Exception as e:
                logger.error(f"❌ Vertica DB connection failed: {e}\n{traceback.format_exc()}")
                raise HTTPException(
                    status_code=400,
                    detail="⚠️ Could not establish Vertica connection. Please verify host, port, username, password, and database name."
                )

        # -----------------------------
        # Unsupported
        # -----------------------------
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported DB type: {db_type}")

    except Exception as e:
        logger.error(f"❌ General DB connection error: {e}\n{traceback.format_exc()}")
        raise HTTPException(
            status_code=400,
            detail=f"⚠️ Could not establish {db_type.upper()} connection. Please verify host, port, username, password, and database name."
        )


# ============================================================
# 📦 Load Tables from DB
# ============================================================
def load_tables_from_personal_db(engine, table_list: list) -> tuple:
    loaded_tables = []
    original_tables = []
    dialect = engine.url.get_dialect().name.lower() if engine.url.get_dialect() else ""
    for tbl in table_list:
        try:
            query = f"SELECT * FROM {tbl}" if dialect == "vertica" else f"SELECT * FROM `{tbl}`"
            df = pd.read_sql_query(query, con=engine)
            df.columns = [col.strip().replace(" ", "_").lower() for col in df.columns]
            original_df = df.copy()
            from app.utils.cleaning import clean_data
            df = clean_data(df)
            loaded_tables.append((tbl, df))
            original_tables.append((tbl, original_df))
        except Exception as e:
            print(f"Error loading table '{tbl}': {e}")
    return loaded_tables, original_tables


# ============================================================
# ❌ Disconnect Database
# ============================================================
def disconnect_database(user_state):
    if getattr(user_state, "personal_engine", None):
        try:
            user_state.personal_engine.dispose()
            print("Personal database disconnected.")
        except Exception as e:
            print(f"Error disconnecting personal database: {e}")
        user_state.personal_engine = None

    if getattr(user_state, "mysql_connection", None):
        try:
            user_state.mysql_connection.close()
            print("MySQL connection disconnected.")
        except Exception as e:
            print(f"Error disconnecting MySQL connection: {e}")
        user_state.mysql_connection = None

    user_state.table_names = []
    user_state.original_table_names = []
