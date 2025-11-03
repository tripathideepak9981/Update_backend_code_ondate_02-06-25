# app/state.py

from collections import defaultdict
import threading
from datetime import datetime, timedelta
import duckdb
from fastapi import Depends
from app.models import User
from app.utils.auth_helpers import get_current_user


class UserState:
    def __init__(self):
        self.lock = threading.Lock()
        self.table_names = []
        self.original_table_names = []
        self.personal_engine = None
        self.mysql_connection = None
        self.chat_history = []
        self.initial_suggestions = []
        self.source = "file"
        self.duckdb_conn = duckdb.connect(database=':memory:')
        self.last_active = datetime.utcnow()  # ✅ Tracks last activity time

    def reset(self):
        with self.lock:
            self.table_names.clear()
            self.original_table_names.clear()
            self.personal_engine = None
            self.mysql_connection = None
            self.chat_history.clear()
            self.initial_suggestions.clear()
            self.source = "file"
            self.duckdb_conn = duckdb.connect(database=':memory:')
            self.last_active = datetime.utcnow()  # ✅ Refresh activity timestamp

    def add_chat_entry(self, user_query, resolved_query, sql_query, result_df, max_history=5):
        with self.lock:
            self.chat_history.append({
              "user_query": user_query,
              "resolved_query": resolved_query,
              "sql_query": sql_query,
              "result_preview": result_df.head(5).to_dict(orient="records"),
              "timestamp": datetime.utcnow().isoformat()
            })
        if len(self.chat_history) > max_history:
            self.chat_history = self.chat_history[-max_history:]
        self.last_active = datetime.utcnow()


    def get_last_chat_entry(self):
        with self.lock:
            return self.chat_history[-1] if self.chat_history else None


# ✅ Global in-memory session store
user_states = defaultdict(UserState)

# ✅ Session accessor — also updates activity time
def get_user_state(user_id: int) -> UserState:
    state = user_states[user_id]
    state.last_active = datetime.utcnow()
    return state

# ✅ DuckDB per-user connection dependency
def get_duckdb_connection(user_state: UserState = Depends(get_user_state)):
    return user_state.duckdb_conn

# ✅ Manual session cleanup (one user)
def clear_user_state(user_id: int):
    if user_id in user_states:
        print(f"🧹 Clearing user state for user_id: {user_id}")
        user_states[user_id].reset()
        del user_states[user_id]

# ✅ Auto cleanup: delete inactive sessions
def clear_inactive_states(ttl_minutes: int = 30):
    now = datetime.utcnow()
    expired_users = [
        user_id for user_id, state in user_states.items()
        if now - state.last_active > timedelta(minutes=ttl_minutes)
    ]
    for user_id in expired_users:
        print(f"🧹 Clearing inactive user: {user_id}")
        user_states[user_id].reset()
        del user_states[user_id]