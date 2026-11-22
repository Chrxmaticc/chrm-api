import os
import asyncpg
import json
import logging
from fastapi import FastAPI, HTTPException, Header, Request, File, UploadFile, Form, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from contextlib import asynccontextmanager
import aiohttp

from dashboard_auth import (
    issue as issue_dashboard_key,
    verify as verify_dashboard_key,
    matches_engine_secret,
)

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("chrm-api")

DATABASE_URL = os.environ.get("DATABASE_URL")
ENGINE_SECRET = os.environ.get("ENGINE_SECRET", "fallback_secret_123")
IMAGE_WEBHOOK_URL = os.environ.get("IMAGE_WEBHOOK_URL")

db_pool = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    logger.info("⏳ Connecting to Neon Database...")
    try:
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
        
    yield
    
    if db_pool:
        logger.info("Closing database pool...")
        await db_pool.close()

app = FastAPI(lifespan=lifespan)

# 🚨 BULLETPROOF CORS (Fixes iOS Safari "Load failed")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=False, 
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    allow_headers=["*"],
)

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled error: {exc}")
    return JSONResponse(status_code=500, content={"detail": f"Internal Server Error: {str(exc)}"})

class TokenLogin(BaseModel):
    token: str
    discord_id: str

class SettingsUpdate(BaseModel):
    settings: dict
    is_active: Optional[bool] = None


async def verify_discord_token(token: str) -> Optional[dict]:
    """Ask Discord who this token belongs to. The answer is the only id we trust."""
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15)
        ) as session:
            async with session.get(
                "https://discord.com/api/v9/users/@me",
                headers={"Authorization": token},
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
    except Exception as e:  # noqa: BLE001 - a dead Discord must not 500 the login
        logger.error(f"Discord token check failed: {e}")
        return None


async def require_access(
    discord_id: str,
    x_dashboard_key: str = Header(None),
    x_engine_secret: str = Header(None),
) -> None:
    """Caller must be either the engine (shared secret) or this user's dashboard."""
    if matches_engine_secret(x_engine_secret):
        return
    if verify_dashboard_key(x_dashboard_key or "", discord_id):
        return
    raise HTTPException(status_code=401, detail="Invalid or expired dashboard key")

@app.post("/api/auth/token")
async def login_with_token(data: TokenLogin):
    if not db_pool:
        raise HTTPException(status_code=500, detail="Database not connected")

    # The token decides the identity. Without this, anyone could post a token for
    # somebody else's discord_id and overwrite (or break) their engine row.
    who = await verify_discord_token(data.token)
    if not who or not who.get("id"):
        raise HTTPException(status_code=401, detail="Discord rejected that token")

    real_id = str(who["id"])
    if data.discord_id and str(data.discord_id) != real_id:
        raise HTTPException(status_code=400, detail="Token does not belong to that discord_id")

    try:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (discord_id, token) 
                VALUES ($1, $2)
                ON CONFLICT (discord_id) DO UPDATE SET token = $2, is_active = true
            """, real_id, data.token)
        return {
            "status": "success",
            "discord_id": real_id,
            "username": who.get("username"),
            "dashboard_key": issue_dashboard_key(real_id),
        }
    except Exception as e:
        logger.error(f"Failed to save token: {e}")
        raise HTTPException(status_code=500, detail="Failed to save token")

@app.get("/api/user/{discord_id}")
async def get_user_settings(discord_id: str, _: None = Depends(require_access)):
    if not db_pool:
        raise HTTPException(status_code=500, detail="Database not connected")
        
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT settings, is_active FROM users WHERE discord_id = $1", discord_id)
        if not user:
            # 404 rather than an empty 200: a stale browser session should go back
            # to the login form instead of showing a dashboard that can't save.
            raise HTTPException(status_code=404, detail="User not found")
        return {"settings": json.loads(user["settings"]), "is_active": user["is_active"]}

@app.patch("/api/user/{discord_id}")
async def update_user_settings(discord_id: str, data: SettingsUpdate, _: None = Depends(require_access)):
    if not db_pool:
        raise HTTPException(status_code=500, detail="Database not connected")
        
    async with db_pool.acquire() as conn:
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

@app.post("/api/rpc/{discord_id}")
async def update_rpc(
    discord_id: str, 
    rpc_type: str = Form(...),
    name: str = Form(...),
    details: str = Form(""),
    state: str = Form(""),
    image: UploadFile = File(None),
    _: None = Depends(require_access),
):
    if not db_pool:
        raise HTTPException(status_code=500, detail="Database not connected")

    image_url = None
    
    # 1. Upload image to Discord Webhook to get a permanent CDN link
    if image and IMAGE_WEBHOOK_URL:
        try:
            async with aiohttp.ClientSession() as session:
                form = aiohttp.FormData()
                file_bytes = await image.read()
                form.add_field('file', file_bytes, filename=image.filename)
                
                async with session.post(IMAGE_WEBHOOK_URL, data=form) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        image_url = data["attachments"][0]["url"]
                    else:
                        logger.error(f"Webhook upload failed: {resp.status}")
        except Exception as e:
            logger.error(f"Image upload error: {e}")

    # 2. Save RPC settings to Neon
    rpc_data = {
        "type": rpc_type,
        "name": name,
        "details": details,
        "state": state,
        "image_url": image_url
    }

    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT settings FROM users WHERE discord_id = $1", discord_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
            
        current_settings = json.loads(user["settings"])
        current_settings["rpc"] = rpc_data
        
        await conn.execute(
            "UPDATE users SET settings = $1 WHERE discord_id = $2", 
            json.dumps(current_settings), discord_id
        )
        
    return {"status": "success", "image_url": image_url}

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

@app.delete("/api/auth/logout/{discord_id}")
async def logout(discord_id: str, _: None = Depends(require_access)):
    if not db_pool:
        raise HTTPException(status_code=500, detail="Database not connected")
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM users WHERE discord_id = $1", discord_id)
    return {"status": "success", "message": "logged out"}

# 🚨 UPTIMEROBOT FIX
@app.api_route("/", methods=["GET", "HEAD"])
def health():
    return {"status": "chrm api is online"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
