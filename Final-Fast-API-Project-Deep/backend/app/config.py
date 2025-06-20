# app/config.py
import os
from dotenv import load_dotenv

load_dotenv()  # Load variables from .env

# Google API and model config
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
MODEL_NAME = "gemini-2.0-flash"

# MySQL config for production dashboard (shared main database)
MYSQL_USER = os.environ.get("MYSQL_USER")
MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD")
MYSQL_HOST = os.environ.get("MYSQL_HOST")
MYSQL_DATABASE = os.environ.get("MYSQL_DATABASE")  # This is our main database (e.g., Exceldata)

# Main DATABASE_URI for user authentication and global tables
DATABASE_URI = os.environ.get("DATABASE_URI")

# JWT and authentication config
SECRET_KEY = os.environ.get("SECRET_KEY", "your_default_secret_key")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))
