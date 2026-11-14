import os
import asyncpg
import json
import logging
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from contextlib import asynccontextmanager

# Setup logging so we can see exactly what happens in Render
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("chrm-api")

DATABASE_URL = os.environ.get("DATABASE_URL")
ENGINE_SECRET = os.environ.get("ENGINE_SECRET", "fallback_secret_123")

db_pool = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    logger.info("⏳ Connecting to Neon Database...")
    try:
        # Add timeout to prevent hanging
        db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
        
        async with db_pool.acquire() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    discord_id VARCHAR(255) UNIQUE NOT NULL,
                    token TEXT NOT NULL,
                    settings JSONB DEFAULT '{}'::jsonb,
                    is_active BOOLEAN DEFAULT true,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
        logger.info("✅ Database connected and table checked!")
    except Exception as e:
        logger.error(f"❌ DATABASE CONNECTION FAILED: {e}")
        # Don't crash the app, just log it. (Render will keep it alive, but routes will fail if DB is down).
        
    yield
    
    if db_pool:
        logger.info("Closing database pool...")
        await db_pool.close()

app = FastAPI(lifespan=lifespan)

# 🚨 BULLETPROOF CORS (Fixes the Safari "Load Failed" bug)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=False, # MUST BE FALSE when using "*"
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    allow_headers=["*"],
)

# Global exception handler to always return JSON, even on crashes
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled error: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal Server Error: {str(exc)}"},
    )

class TokenLogin(BaseModel):
    token: str
    discord_id: str = "test_user"

class SettingsUpdate(BaseModel):
    settings: dict
    is_active: Optional[bool] = None

@app.post("/api/auth/token")
async def login_with_token(data: TokenLogin):
    logger.info(f"Received token for {data.discord_id}")
    if not db_pool:
        raise HTTPException(status_code=500, detail="Database not connected")
        
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (discord_id, token) 
                VALUES ($1, $2)
                ON CONFLICT (discord_id) DO UPDATE SET token = $2, is_active = true
            """, data.discord_id, data.token)
        return {"status": "success", "message": "Token saved successfully"}
    except Exception as e:
        logger.error(f"Failed to save token: {e}")
        raise HTTPException(status_code=500, detail="Failed to save token to database")

@app.get("/api/user/{discord_id}")
async def get_user_settings(discord_id: str):
    if not db_pool:
        raise HTTPException(status_code=500, detail="Database not connected")
        
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT settings, is_active FROM users WHERE discord_id = $1", discord_id)
        if not user:
            # If user doesn't exist yet, return default settings instead of 404
            return {"settings": {}, "is_active": False}
        return {"settings": json.loads(user["settings"]), "is_active": user["is_active"]}

@app.patch("/api/user/{discord_id}")
async def update_user_settings(discord_id: str, data: SettingsUpdate):
    if not db_pool:
        raise HTTPException(status_code=500, detail="Database not connected")
        
    async with db_pool.acquire() as conn:
        # Check if user exists
        user = await conn.fetchrow("SELECT settings FROM users WHERE discord_id = $1", discord_id)
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        if data.is_active is not None:
            await conn.execute("UPDATE users SET is_active = $1 WHERE discord_id = $2", data.is_active, discord_id)
        
        if data.settings:
            current = json.loads(user["settings"])
            current.update(data.settings)
            await conn.execute("UPDATE users SET settings = $1 WHERE discord_id = $2", json.dumps(current), discord_id)
            
    return {"status": "success"}

@app.get("/api/engine/bots")
async def get_bots(x_engine_secret: str = Header(None)):
    if x_engine_secret != ENGINE_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    if not db_pool:
        raise HTTPException(status_code=500, detail="Database not connected")
        
    async with db_pool.acquire() as conn:
        users = await conn.fetch("SELECT discord_id, token, settings, is_active FROM users")
        return [
            {
                "discord_id": u["discord_id"],
                "token": u["token"],
                "settings": json.loads(u["settings"]),
                "is_active": u["is_active"]
            } for u in users
        ]

# 🚨 FIXED UPTIMEROBOT 404 ISSUE (Handles both GET and HEAD)
@app.api_route("/", methods=["GET", "HEAD"])
def health():
    return {"status": "chrm api is online"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
