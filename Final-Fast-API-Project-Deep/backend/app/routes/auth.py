# app/routes/auth.py

import logging
import random
import smtplib
import ssl
from datetime import datetime, timedelta
from email.mime.text import MIMEText
import re  # ✅ Added for regex validations
from collections import defaultdict
from time import time


from fastapi import (
    APIRouter, Depends, HTTPException, status, Response, BackgroundTasks
)
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session
from sqlalchemy import create_engine, text

from app.utils.auth_helpers import get_current_user
from app.config import (
    SECRET_KEY, ALGORITHM, ACCESS_TOKEN_EXPIRE_MINUTES,
    MYSQL_USER, MYSQL_PASSWORD, MYSQL_HOST,
    EMAIL_FROM, EMAIL_PASSWORD, SMTP_SERVER, SMTP_PORT
)
from app.database import SessionLocal
from app.models import User
from app.utils.db_helpers import connect_personal_db, list_tables, load_tables_from_personal_db
from app.state import get_user_state, clear_user_state

logger = logging.getLogger("auth")
logger.setLevel(logging.INFO)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")
router = APIRouter()

otp_store = {}  # Temporary in-memory OTP store
reset_otp_store = {}  # email: { otp, timestamp }
reset_otp_tracker = defaultdict(list)  # email: [timestamps]
RESET_MAX_PER_HOUR = 5

# -------- Pydantic Models --------
class UserCreate(BaseModel):
    email: EmailStr
    mobile_number: str = Field(..., pattern=r"^[6-9]\d{9}$", description="10-digit Indian mobile number starting with 6-9")
    password: str = Field(
        ...,
        min_length=8,
        max_length=64,
        description="Password must contain at least 1 uppercase letter, 1 lowercase letter, 1 digit, and 1 special character"
    )

    def validate_password_complexity(self):
        password = self.password
        if (not re.search(r"[A-Z]", password) or
            not re.search(r"[a-z]", password) or
            not re.search(r"\d", password) or
            not re.search(r"[@$!%*#?&]", password)):
            raise ValueError("Password must include uppercase, lowercase, digit, and special character.")

class OTPVerifyRequest(BaseModel):
    otp_code: str = Field(..., min_length=4, max_length=6)

class Token(BaseModel):
    access_token: str
    token_type: str
    tables: list[str] = []
    email: EmailStr


# -------- Utility Functions --------
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
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_user_by_email(db: Session, email: str):
    return db.query(User).filter(User.email == email).first()

def get_user_by_mobile(db: Session, mobile: str):
    return db.query(User).filter(User.mobile_number == mobile).first()

def create_dynamic_database_for_user(user_identifier: str) -> str:
    safe_db_name = (
        user_identifier.strip()
        .lower()
        .replace("@", "_at_")
        .replace(".", "_dot_")
    )
    db_name = f"{safe_db_name}_db"
    engine = create_engine(f"mysql+mysqlconnector://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}")
    with engine.connect() as connection:
        connection.execute(text(f"CREATE DATABASE IF NOT EXISTS `{db_name}`;"))
    logger.info(f"Dynamic database '{db_name}' created for user '{user_identifier}'.")
    return db_name

def error_response(status_code: int, message: str, error_type: str = None):
    raise HTTPException(
        status_code=status_code,
        detail={
            "error_type": error_type,
            "message": message,
            "timestamp": datetime.utcnow().isoformat()
        }
    )

def send_otp_to_email(email: str, otp: str):
    try:
        sender = EMAIL_FROM
        password = EMAIL_PASSWORD
        smtp_server = SMTP_SERVER
        smtp_port = SMTP_PORT

        msg = MIMEText(f"Your OTP code is {otp}")
        msg["Subject"] = "Email Verification OTP"
        msg["From"] = sender
        msg["To"] = email

        context = ssl.create_default_context()
        with smtplib.SMTP(smtp_server, int(smtp_port)) as server:
            server.starttls(context=context)
            server.login(sender, password)
            server.send_message(msg)

        logger.info(f"[OTP] Sent OTP {otp} to email {email}")
    except Exception as e:
        logger.exception("Failed to send OTP via email")
        error_response(status.HTTP_500_INTERNAL_SERVER_ERROR, "Failed to send OTP", "EMAIL_SEND_FAILED")

# -------- Signup OTP Request --------
@router.post("/signup/request_otp")
def request_signup_otp(user: UserCreate, db: Session = Depends(get_db)):
    email_regex = r"(^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$)"
    if not re.match(email_regex, user.email):
        error_response(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid email format.", "INVALID_EMAIL")
    
    if not re.fullmatch(r"^[6-9]\d{9}$", user.mobile_number):
        error_response(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid mobile number. Must be 10 digits starting with 6-9.", "INVALID_MOBILE")

    try:
        user.validate_password_complexity()
    except ValueError as ve:
        error_response(status.HTTP_422_UNPROCESSABLE_ENTITY, str(ve), "WEAK_PASSWORD")

    if get_user_by_email(db, user.email):
        error_response(status.HTTP_409_CONFLICT, "Email already exists.", "EMAIL_EXISTS")
    if get_user_by_mobile(db, user.mobile_number):
        error_response(status.HTTP_409_CONFLICT, "Mobile number already exists.", "MOBILE_EXISTS")

    otp = f"{random.randint(100000, 999999)}"
    otp_store[otp] = {
        "email": user.email,
        "mobile_number": user.mobile_number,
        "password": user.password,
        "timestamp": datetime.utcnow()

    }
    send_otp_to_email(user.email, otp)
    return {"message": "OTP sent to email. Please verify to complete signup."}

# -------- Signup OTP Verification --------
@router.post("/signup/verify_otp", response_model=Token)
def verify_signup_otp(payload: OTPVerifyRequest, db: Session = Depends(get_db)):
    if not payload.otp_code or not payload.otp_code.strip().isdigit():
        error_response(status.HTTP_400_BAD_REQUEST, "OTP must be numeric and not empty.", "INVALID_OTP_FORMAT")

    data = otp_store.get(payload.otp_code)
    if not data:
        error_response(status.HTTP_400_BAD_REQUEST, "Invalid or expired OTP.", "INVALID_OTP")
    
    otp_time = data["timestamp"]
    if datetime.utcnow() - otp_time > timedelta(minutes=5):
        otp_store.pop(payload.otp_code, None)
        error_response(status.HTTP_400_BAD_REQUEST, "OTP expired. Please request a new one.", "OTP_EXPIRED")

    try:
        db_name = create_dynamic_database_for_user(data["email"])
        hashed_password = get_password_hash(data["password"])

        new_user = User(
            email=data["email"],
            mobile_number=data["mobile_number"],
            hashed_password=hashed_password,
            dynamic_db=db_name,
            username=data["email"].split("@")[0]
        )

        db.add(new_user)
        db.commit()
        db.refresh(new_user)

        access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = create_access_token(
            data={"sub": new_user.email, "user_id": new_user.id},
            expires_delta=access_token_expires,
        )

        logger.info(f"User '{new_user.email}' signed up successfully.")
        otp_store.pop(payload.otp_code, None)
        return {
            "access_token": access_token,
            "token_type": "bearer",
            "tables": [],
            "email": new_user.email

        }
    except Exception:
        db.rollback()
        logger.exception("Unexpected signup error")
        error_response(status.HTTP_500_INTERNAL_SERVER_ERROR, "Signup failed", "SIGNUP_ERROR")

# -------- Login with email or mobile --------
@router.post("/login", response_model=Token, status_code=200)
def login(
    background_tasks: BackgroundTasks,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
):
    try:
        if not form_data.username or not form_data.password:
            error_response(status.HTTP_400_BAD_REQUEST, "Email or mobile and password required.", "MISSING_CREDENTIALS")

        email_regex = r"(^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$)"
        if "@" in form_data.username:
            if not re.match(email_regex, form_data.username):
                error_response(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid email format.", "INVALID_EMAIL")
        else:
            if not re.fullmatch(r"^[6-9]\d{9}$", form_data.username):
                error_response(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid mobile number format.", "INVALID_MOBILE")

        user = get_user_by_email(db, form_data.username)
        if not user:
            user = get_user_by_mobile(db, form_data.username)
        if not user:
            error_response(status.HTTP_401_UNAUTHORIZED, "Invalid credentials.", "USER_NOT_FOUND")

        if not verify_password(form_data.password, user.hashed_password):
            error_response(status.HTTP_401_UNAUTHORIZED, "Invalid credentials.", "INVALID_PASSWORD")

        user_state = get_user_state(user.id)
        access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = create_access_token(
            data={"sub": user.email, "user_id": user.id},
            expires_delta=access_token_expires,
        )

        logger.info(f"User '{user.email}' logged in.")
        background_tasks.add_task(initialize_user_context, user, user_state)
        return {
            "access_token": access_token,
            "token_type": "bearer",
            "tables": [],
            "email": user.email  # ✅ Add this line
        }

    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected login error")
        error_response(status.HTTP_500_INTERNAL_SERVER_ERROR, "Login failed", "LOGIN_ERROR")

# -------- Init Context --------
def initialize_user_context(user, user_state):
    try:
        if not user.dynamic_db:
            logger.warning(f"No dynamic DB for user {user.email}")
            return

        engine = connect_personal_db(
            db_type="mysql",
            host=MYSQL_HOST,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            database=user.dynamic_db
        )
        if not engine:
            logger.warning(f"DB connect failed for {user.email}")
            return

        table_names = list_tables(engine)
        loaded, original = load_tables_from_personal_db(engine, table_names)

        user_state.personal_engine = engine
        user_state.table_names = loaded
        user_state.original_table_names = original

        logger.info(f"Preloaded tables for '{user.email}': {[name for name, _ in loaded]}")

    except Exception:
        logger.exception(f"Init failed for user '{user.email}'")

# -------- Logout --------
@router.post("/logout")
def logout(response: Response, current_user: User = Depends(get_current_user)):
    try:
        clear_user_state(current_user.id)
        response.delete_cookie("access_token")
        logger.info(f"User '{current_user.email}' logged out.")
        return {"detail": "Successfully logged out"}
    except Exception:
        logger.exception("Error during logout")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error_type": "LOGOUT_ERROR",
                "message": "Logout failed. Try again.",
                "timestamp": datetime.utcnow().isoformat()
            }
        )



class UserCreate(BaseModel):
    email: EmailStr
    mobile_number: str = Field(..., pattern=r"^[6-9]\d{9}$")
    password: str = Field(..., min_length=8, max_length=64)

    def validate_password_complexity(self):
        if (not re.search(r"[A-Z]", self.password) or
            not re.search(r"[a-z]", self.password) or
            not re.search(r"\d", self.password) or
            not re.search(r"[@$!%*#?&]", self.password)):
            raise ValueError("Password must include uppercase, lowercase, digit, and special character.")

class OTPVerifyRequest(BaseModel):
    otp_code: str = Field(..., min_length=4, max_length=6)

class ResetPasswordRequest(BaseModel):
    email: EmailStr
    otp: str = Field(..., min_length=4, max_length=6)
    new_password: str = Field(..., min_length=8, max_length=64)

    def validate_password_complexity(self):
        if (not re.search(r"[A-Z]", self.new_password) or
            not re.search(r"[a-z]", self.new_password) or
            not re.search(r"\d", self.new_password) or
            not re.search(r"[@$!%*#?&]", self.new_password)):
            raise ValueError("Password must include uppercase, lowercase, digit, and special character.")

    


# -------- Utility Functions --------
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
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_user_by_email(db: Session, email: str):
    return db.query(User).filter(User.email == email).first()

def get_user_by_mobile(db: Session, mobile: str):
    return db.query(User).filter(User.mobile_number == mobile).first()

def create_dynamic_database_for_user(user_identifier: str) -> str:
    safe_db_name = user_identifier.strip().lower().replace("@", "_at_").replace(".", "_dot_")
    db_name = f"{safe_db_name}_db"
    engine = create_engine(f"mysql+mysqlconnector://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}")
    with engine.connect() as connection:
        connection.execute(text(f"CREATE DATABASE IF NOT EXISTS `{db_name}`;"))
    logger.info(f"Dynamic database '{db_name}' created for user '{user_identifier}'.")
    return db_name

def error_response(status_code: int, message: str, error_type: str = None):
    raise HTTPException(
        status_code=status_code,
        detail={"error_type": error_type, "message": message, "timestamp": datetime.utcnow().isoformat()}
    )

def send_otp_to_email(email: str, otp: str):
    try:
        msg = MIMEText(f"Your OTP code is {otp}")
        msg["Subject"] = "OTP Verification"
        msg["From"] = EMAIL_FROM
        msg["To"] = email

        context = ssl.create_default_context()
        with smtplib.SMTP(SMTP_SERVER, int(SMTP_PORT)) as server:
            server.starttls(context=context)
            server.login(EMAIL_FROM, EMAIL_PASSWORD)
            server.send_message(msg)

        logger.info(f"[OTP] Sent OTP {otp} to email {email}")
    except Exception:
        logger.exception("Failed to send OTP via email")
        error_response(status.HTTP_500_INTERNAL_SERVER_ERROR, "Failed to send OTP", "EMAIL_SEND_FAILED")

# -------- Forgot Password Flow --------
@router.post("/forgot/request_otp")
def request_password_reset(email: EmailStr, db: Session = Depends(get_db)):
    user = get_user_by_email(db, email)
    if not user:
        error_response(status.HTTP_404_NOT_FOUND, "Email not found.", "EMAIL_NOT_REGISTERED")

    now = time()
    reset_otp_tracker[email] = [ts for ts in reset_otp_tracker[email] if now - ts < 3600]
    if len(reset_otp_tracker[email]) >= RESET_MAX_PER_HOUR:
        error_response(status.HTTP_429_TOO_MANY_REQUESTS, "Too many OTP requests. Try again later.", "RESET_OTP_LIMIT")

    otp = f"{random.randint(100000, 999999)}"
    reset_otp_store[email] = {"otp": otp, "timestamp": datetime.utcnow()}
    reset_otp_tracker[email].append(now)
    send_otp_to_email(email, otp)
    return {"message": "OTP sent to your email."}

@router.post("/forgot/verify_otp")
def verify_reset_otp(payload: ResetPasswordRequest, db: Session = Depends(get_db)):
    record = reset_otp_store.get(payload.email)
    if not record:
        error_response(status.HTTP_400_BAD_REQUEST, "OTP not requested or expired.", "OTP_MISSING")

    if record["otp"] != payload.otp:
        error_response(status.HTTP_400_BAD_REQUEST, "Invalid OTP.", "INVALID_OTP")

    if datetime.utcnow() - record["timestamp"] > timedelta(minutes=5):
        reset_otp_store.pop(payload.email, None)
        error_response(status.HTTP_400_BAD_REQUEST, "OTP expired.", "OTP_EXPIRED")

    try:
        payload.validate_password_complexity()
    except ValueError as ve:
        error_response(status.HTTP_422_UNPROCESSABLE_ENTITY, str(ve), "WEAK_PASSWORD")

    user = get_user_by_email(db, payload.email)
    if not user:
        error_response(status.HTTP_404_NOT_FOUND, "User not found.", "USER_NOT_FOUND")

    user.hashed_password = get_password_hash(payload.new_password)
    db.commit()
    db.refresh(user)
    reset_otp_store.pop(payload.email, None)

    logger.info(f"Password reset successful for {payload.email}")
    return {"message": "Password reset successfully."}


