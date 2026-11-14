import os
import asyncpg
import json
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from contextlib import asynccontextmanager

# Securely load env vars (must be set in Render dashboard)
DATABASE_URL = os.environ.get("DATABASE_URL")
ENGINE_SECRET = os.environ.get("ENGINE_SECRET")

db_pool = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    # Connect to Neon Postgres
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    
    # Create users table if it doesn't exist
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                discord_id VARCHAR(255) UNIQUE NOT NULL,
                token TEXT NOT NULL,
                settings JSONB DEFAULT '{}'::jsonb,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
    yield
    await db_pool.close()

app = FastAPI(lifespan=lifespan)

# Allow Vercel frontend to talk to this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # You can change this to your Vercel URL later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class TokenLogin(BaseModel):
    token: str
    discord_id: str = "test_user" # In prod, this would come from Discord OAuth

@app.post("/api/auth/token")
async def login_with_token(data: TokenLogin):
    """Receives raw token from frontend and saves to Neon."""
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (discord_id, token) 
                VALUES ($1, $2)
                ON CONFLICT (discord_id) DO UPDATE SET token = $2
            """, data.discord_id, data.token)
        return {"status": "success", "message": "token saved"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/engine/bots")
async def get_bots(x_engine_secret: str = Header(None)):
    """Backend engine calls this to fetch all user tokens securely."""
    if x_engine_secret != ENGINE_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    async with db_pool.acquire() as conn:
        users = await conn.fetch("SELECT discord_id, token, settings FROM users")
        return [
            {
                "discord_id": u["discord_id"],
                "token": u["token"],
                "settings": json.loads(u["settings"])
            } for u in users
        ]

@app.delete("/api/auth/logout/{discord_id}")
async def logout(discord_id: str):
    """Deletes the token from the database."""
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE discord_id = $1", discord_id)
    return {"status": "success", "message": "logged out"}

@app.get("/")
def health():
    return {"status": "chrm api is online"}
