import aiortc
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import database
from app.routes import videoCall, mainRouter
from app.schemas import databaseSchemas
from app.config.websocket import websocket_endpoint
import redis.asyncio as redis
from app.config import config

app = FastAPI(version="1.0.0")

# -------------------------------
# CORS Middleware
# -------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Replace in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------------
# Redis Client - MUST be initialized to None
# -------------------------------
redis_client: redis.Redis = None  # ✅ Fixed: now exists in global scope

async def get_redis():
    global redis_client
    if redis_client is None:
        redis_client = redis.from_url(config.REDIS_URL, decode_responses=True)
    return redis_client

# -------------------------------
# Startup Event
# -------------------------------
@app.on_event("startup")
async def startup_event():
    global redis_client
    if redis_client is None:
        redis_client = redis.from_url(config.REDIS_URL, decode_responses=True)
        print("✅ Redis client connected")

    # 🔁 Patch the router's redis_client
    if hasattr(videoCall, 'set_redis_client'):
        videoCall.set_redis_client(redis_client)
        print("🔁 Redis client injected into router")
    else:
        print("⚠️ Router does not support set_redis_client")
# -------------------------------
# Shutdown Event
# -------------------------------
@app.on_event("shutdown")
async def shutdown_event():
    global redis_client
    if redis_client:
        await redis_client.close()
        print("🛑 Redis client disconnected")
        redis_client = None  # Avoid reuse

# -------------------------------
# Root Route
# -------------------------------
@app.get("/")
def root():
    return {"message": "LiveKit FastAPI backend is running."}

# -------------------------------
# Include Routes
# -------------------------------
app.include_router(videoCall.router, tags=["video-call"], prefix="/videoCall")

# -------------------------------
# Create Database Tables
# -------------------------------
database.Base.metadata.create_all(bind=database.engine)

# Optional: WebRTC (commented)
# pc_config = {"iceServers": [{"urls": ["stun:stun.l.google.com:19302"]}]}
# aiortc.RTCPeerConnection(configuration=pc_config)

# Optional: WebSocket route
# app.add_api_websocket_route("/ws/{room_id}", websocket_endpoint)