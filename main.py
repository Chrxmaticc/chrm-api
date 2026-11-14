import os
import asyncpg
import json
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from contextlib import asynccontextmanager

DATABASE_URL = os.environ.get("DATABASE_URL")
ENGINE_SECRET = os.environ.get("ENGINE_SECRET")

db_pool = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
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
    yield
    await db_pool.close()

app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

class TokenLogin(BaseModel):
    token: str
    discord_id: str

class SettingsUpdate(BaseModel):
    settings: dict
    is_active: Optional[bool] = None

@app.post("/api/auth/token")
async def login_with_token(data: TokenLogin):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (discord_id, token) VALUES ($1, $2)
            ON CONFLICT (discord_id) DO UPDATE SET token = $2, is_active = true
        """, data.discord_id, data.token)
    return {"status": "success"}

# --- DASHBOARD ENDPOINTS ---
@app.get("/api/user/{discord_id}")
async def get_user_settings(discord_id: str):
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT settings, is_active FROM users WHERE discord_id = $1", discord_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return {"settings": json.loads(user["settings"]), "is_active": user["is_active"]}

@app.patch("/api/user/{discord_id}")
async def update_user_settings(discord_id: str, data: SettingsUpdate):
    async with db_pool.acquire() as conn:
        if data.is_active is not None:
            await conn.execute("UPDATE users SET is_active = $1 WHERE discord_id = $2", data.is_active, discord_id)
        
        # Merge new settings with old settings
        user = await conn.fetchrow("SELECT settings FROM users WHERE discord_id = $1", discord_id)
        if user:
            current = json.loads(user["settings"])
            current.update(data.settings)
            await conn.execute("UPDATE users SET settings = $1 WHERE discord_id = $2", json.dumps(current), discord_id)
            
    return {"status": "success"}

# --- ENGINE ENDPOINT ---
@app.get("/api/engine/bots")
async def get_bots(x_engine_secret: str = Header(None)):
    if x_engine_secret != ENGINE_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
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
