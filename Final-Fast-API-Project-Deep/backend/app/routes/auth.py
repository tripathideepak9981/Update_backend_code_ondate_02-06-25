#app/routes/auth.py

import logging
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, status, Response
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from app.utils.auth_helpers import get_current_user
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session
from sqlalchemy import create_engine, text
from app.config import SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES, MYSQL_USER, MYSQL_PASSWORD, MYSQL_HOST, DATABASE_URI
from app.database import SessionLocal
from app.models import User
from app.utils.db_helpers import connect_personal_db, load_tables_from_personal_db, list_tables
from app.config import GOOGLE_API_KEY, MODEL_NAME
from app.utils.llm_factory import get_llm
llm = get_llm()

router = APIRouter()
logger = logging.getLogger("auth")
logger.setLevel(logging.INFO)
 
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/auth/login")
 
# Pydantic models.
class UserCreate(BaseModel):
    email: EmailStr
    username: str
    password: str
 
class Token(BaseModel):
    access_token: str
    token_type: str
    tables: list[str] = []
 
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
 
def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)
 
def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)
 
def create_access_token(data: dict, expires_delta: timedelta = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt
 
def get_user_by_username(db: Session, username: str):
    return db.query(User).filter(User.username == username).first()
 
def get_user_by_email(db: Session, email: str):
    return db.query(User).filter(User.email == email).first()
 
def create_dynamic_database_for_user(user: User) -> str:
    """
    Create a dynamic database based on the username.
    For example, if username is "deepak", the database name will be "deepak_db".
    """
    db_name = f"{user.username.strip().lower()}_db"
    engine = create_engine(f"mysql+mysqlconnector://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}")
    with engine.connect() as connection:
        connection.execute(text(f"CREATE DATABASE IF NOT EXISTS {db_name};"))
    logger.info(f"Dynamic database '{db_name}' created for user '{user.username}'.")
    return db_name
 
@router.post("/signup", response_model=Token)
def signup(user: UserCreate, db: Session = Depends(get_db)):
    # Check if user already exists
    if get_user_by_email(db, user.email):
        raise HTTPException(status_code=400, detail="Email already registered")
    if get_user_by_username(db, user.username):
        raise HTTPException(status_code=400, detail="Username already registered")
   
    # Hash password
    hashed_password = get_password_hash(user.password)
 
    # ✅ Create dynamic database for user at signup time
    db_name = create_dynamic_database_for_user(user)
 
    # ✅ Save the dynamic_db name immediately in user record
    new_user = User(
        email=user.email,
        username=user.username,
        hashed_password=hashed_password,
        dynamic_db=db_name  # ✅ Stored in DB immediately
    )
 
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
 
    # Generate JWT token
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": new_user.username, "user_id": new_user.id},
        expires_delta=access_token_expires,
    )
 
    logger.info(f"User '{new_user.username}' signed up and dynamic DB '{db_name}' created.")
    return {"access_token": access_token, "token_type": "bearer"}
 
 
 
from fastapi import BackgroundTasks
 
@router.post("/login", response_model=Token)
def login(
    background_tasks: BackgroundTasks,  # ✅ Now this is first
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    from app.state import get_user_state
    from app.config import ACCESS_TOKEN_EXPIRE_MINUTES
 
    user = get_user_by_email(db, form_data.username)
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Incorrect email or password")
 
    user_state = get_user_state(user.id)
 
    # JWT token generation
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.username, "user_id": user.id},
        expires_delta=access_token_expires,
    )
 
    logger.info(f"User '{user.username}' authenticated successfully.")
 
    # Background task for loading DB + LLM suggestions
    background_tasks.add_task(initialize_user_context, user, user_state)
 
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "tables": []  # frontend should call /available_tables after a few seconds
    }
 
def initialize_user_context(user, user_state):
    from app.utils.db_helpers import connect_personal_db, list_tables, load_tables_from_personal_db
    from app.utils.llm_helpers import GoogleGenerativeAI, generate_initial_suggestions_from_state
    from app.config import GOOGLE_API_KEY, MODEL_NAME, MYSQL_USER, MYSQL_PASSWORD, MYSQL_HOST
 
    try:
        if not user.dynamic_db:
            logger.warning(f"No dynamic DB found for user {user.username}")
            return
 
        engine = connect_personal_db(
            db_type="mysql",
            host=MYSQL_HOST,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            database=user.dynamic_db
        )
 
        if not engine:
            logger.warning(f"Could not connect to DB for user {user.username}")
            return
 
        table_names = list_tables(engine)
        loaded, original = load_tables_from_personal_db(engine, table_names)
 
        user_state.personal_engine = engine
        user_state.table_names = loaded
        user_state.original_table_names = original
 
        logger.info(f"Preloaded tables for user '{user.username}': {[name for name, _ in loaded]}")
 
        # Generate LLM suggestions
        try:
            llm = GoogleGenerativeAI(model=MODEL_NAME, api_key=GOOGLE_API_KEY)
            suggestions = generate_initial_suggestions_from_state(llm, user_state)
            user_state.initial_suggestions = suggestions
            logger.info(f"Initial suggestions generated for user '{user.username}'")
        except Exception as e:
            logger.warning(f"Failed to generate LLM suggestions for user '{user.username}': {e}")
            user_state.initial_suggestions = []
 
    except Exception as e:
        logger.error(f"Background init failed for user '{user.username}': {e}")
 
 
from app.state import clear_user_state

 
@router.post("/logout")
def logout(response: Response, current_user: User = Depends(get_current_user)):

    clear_user_state(current_user.id)

    """
    In a stateless JWT approach, logout is handled on the client side by removing the token.
    If using cookies, clear the cookie here.
    """
    response.delete_cookie("access_token")
    return {"detail": "Successfully logged out"}