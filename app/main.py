import aiortc
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import database
from app.routes import mainRouter
from app.schemas import databaseSchemas
from app.config.websocket import websocket_endpoint
# import redis.asyncio as redis
from app.config import config

app = FastAPI(version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Replace with your frontend's actual origin(s)
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# redis_client: redis.Redis

# async def get_redis():
#     global redis_client
#     if not redis_client:
#         redis_client = redis.from_url(config.REDIS_URL, decode_responses=True)
#     return redis_client
    

@app.get("/")
def root():
    return {"message": "LiveKit FastAPI backend is running."}
mainRouter.mainRouter(app)

# Create tables
database.Base.metadata.create_all(bind=database.engine)

# pc_config = {"iceServers": [{"urls": ["stun:stun.l.google.com:19302"]}]}
# aiortc.RTCPeerConnection(configuration=pc_config)

# # WebSocket for signaling or chat
# app.add_api_websocket_route("/ws/{room_id}", websocket_endpoint)
