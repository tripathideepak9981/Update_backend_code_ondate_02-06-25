# app/main.py


from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import logging
from starlette.status import HTTP_500_INTERNAL_SERVER_ERROR
from threading import Thread
import time

from app.state import clear_inactive_states
from app.routes import auth, upload, db, query, join, modify, validate_sql


# ---------------- LOGGING SETUP ---------------- #
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger("main")

# ---------------- APP INIT ---------------- #
app = FastAPI(title="AI Data Analysis Chatbot API")

# ---------------- MIDDLEWARE ---------------- #
app.add_middleware(
    CORSMiddleware,

    allow_origins=["http://localhost:5173"],  # Set correct frontend origin

    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- ROUTES ---------------- #
app.include_router(auth.router, prefix="/api/auth", tags=["Authentication"])
app.include_router(upload.router, prefix="/api")
app.include_router(db.router, prefix="/api")
app.include_router(query.router, prefix="/api")
app.include_router(join.router, prefix="/api")
app.include_router(modify.router, prefix="/api")
app.include_router(validate_sql.router, prefix="/api", tags=["SQL Validation"])



# ---------------- BACKGROUND CLEANER ---------------- #
def background_cleaner():
    while True:
        logger.info("🧹 Running background session cleanup...")
        clear_inactive_states(ttl_minutes=30)
        time.sleep(1800)  # Runs every 30 minutes


# ---------------- STARTUP/SHUTDOWN ---------------- #
@app.on_event("startup")
async def startup_event():
    logger.info("🚀 API started successfully")
    Thread(target=background_cleaner, daemon=True).start()

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("🛑 API shutdown started")
    clear_inactive_states(ttl_minutes=30)
    logger.info("✅ Session cleanup complete")


# ---------------- HEALTH CHECK ---------------- #
@app.get("/health", tags=["Monitoring"])
def health_check():
    return {"status": "ok"}


# ---------------- GLOBAL ERROR HANDLER ---------------- #
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled error on {request.url}: {str(exc)}")
    return JSONResponse(
        status_code=HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "status": "error",
            "message": "An unexpected error occurred. Our team has been notified.",
        },
    )


# ---------------- ROOT ---------------- #
@app.get("/", tags=["Info"])
def root():
    return {"message": "Welcome to the AI Data Analysis Chatbot API"}
