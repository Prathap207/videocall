# main.py
from fastapi import APIRouter, WebSocket, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
import asyncio
import json
from datetime import datetime, timezone
from typing import Optional, Dict
import uuid
from starlette.websockets import WebSocketState
import redis.asyncio as redis

# Local modules
from app.config import config
from app.config.database import SessionLocal, engine, get_db
from app.schemas.databaseSchemas import Base, User, StreamSession, CallHistory, CallRequestHistory

# Create tables
Base.metadata.create_all(bind=engine)
redis_client: redis.Redis = None


def set_redis_client(client: redis.Redis):
    global redis_client
    redis_client = client
    print("✅ Redis client successfully injected into router!")


router = APIRouter()

# -------------------------------
# Redis Key Prefixes
# -------------------------------
PREFIX_CONNECTED_VENDORS = "ws:connected_vendors"  # hash: vendor_id → ws_id (we track connection via app-level id)
PREFIX_BUSY_VENDORS = "ws:busy_vendors"            # set: vendor_id
PREFIX_CUSTOMER_WS = "ws:customer_websockets"      # hash: customer_id → ws_id
PREFIX_CALL_QUEUE = "ws:call_queue"                # list of JSON strings
PREFIX_PENDING_REQUESTS = "ws:pending_requests"    # hash: vendor_id → JSON
PREFIX_ACTIVE_CALLS = "ws:active_calls"            # hash: room_name → JSON
PREFIX_PENDING_REQUEST_IDS = "ws:pending_request_ids"  # hash: customer_id → request_id

# For mapping WebSocket IDs (if needed in distributed setup)
PREFIX_WS_REGISTRY = "ws:registry"  # hash: ws_id → {type, user_id}

# Global WebSocket registry (temporary in-memory for current process)
# In production, you'd need a shared message bus (e.g., Redis Pub/Sub + external WS service)
connected_websockets: Dict[str, WebSocket] = {}
WS_ID_COUNTER = 0
ws_id_lock = asyncio.Lock()

# Helper: Generate unique WS ID per connection in this process
async def generate_ws_id() -> str:
    global WS_ID_COUNTER
    async with ws_id_lock:
        WS_ID_COUNTER += 1
        return f"ws_{WS_ID_COUNTER}"


# -------------------------------
# LiveKit Helpers
# -------------------------------
def create_livekit_token(username: str, identity: str, room: str, can_publish: bool) -> str:
    from livekit.api import AccessToken, VideoGrants
    token = AccessToken(
        api_key=config.LIVEKIT_API_KEY,
        api_secret=config.LIVEKIT_API_SECRET
    )
    token.with_identity(identity)
    token.with_name(username)
    token.with_grants(
        VideoGrants(
            room_join=True,
            room=room,
            can_publish=can_publish,
            can_subscribe=True,
            can_publish_data=True
        )
    )
    return token.to_jwt()


async def create_livekit_room(room_name: str):
    from livekit.api import LiveKitAPI, CreateRoomRequest
    async with LiveKitAPI(
        config.LIVEKIT_URL,
        config.LIVEKIT_API_KEY,
        config.LIVEKIT_API_SECRET
    ) as lkapi:
        room = await lkapi.room.create_room(CreateRoomRequest(name=room_name))
        return room


async def end_livekit_room(room_name: str):
    from livekit.api import LiveKitAPI, DeleteRoomRequest
    async with LiveKitAPI(
        config.LIVEKIT_URL,
        config.LIVEKIT_API_KEY,
        config.LIVEKIT_API_SECRET
    ) as lkapi:
        try:
            await lkapi.room.delete_room(DeleteRoomRequest(room=room_name))
        except Exception as e:
            print(f"LiveKit room delete failed: {e}")


# -------------------------------
# Helper: Queue Position
# -------------------------------
async def get_queue_position(customer_id: int) -> Optional[Dict]:
    queue = await redis_client.lrange(PREFIX_CALL_QUEUE, 0, -1)
    entries = [json.loads(item) for item in queue]

    target_entry = None
    vendor_id = None
    for entry in entries:
        if entry["customer_id"] == customer_id:
            target_entry = entry
            vendor_id = entry["vendor_id"]
            break
    if not target_entry or not vendor_id:
        return None

    ahead_count = 0
    for entry in entries:
        if entry["vendor_id"] == vendor_id:
            if entry["customer_id"] == customer_id:
                break
            ahead_count += 1

    is_vendor_busy = await redis_client.sismember(PREFIX_BUSY_VENDORS, str(vendor_id))
    total_wait_slots = (1 if is_vendor_busy else 0) + ahead_count
    wait_time = total_wait_slots * MAX_CALL_DURATION

    return {
        "position": ahead_count + 1,
        "ahead_count": ahead_count,
        "wait_time_seconds": wait_time,
        "wait_time_human": f"{wait_time // 60} min {wait_time % 60} sec",
        "is_vendor_busy": is_vendor_busy
    }


# -------------------------------
# APIs
# -------------------------------
@router.get("/api/v1/vendors/available")
def get_available_vendors(db: Session = Depends(get_db)):
    vendors = db.query(User).filter(User.role_id == 0).all()
    available = []

    for v in vendors:
        is_online = asyncio.run(redis_client.hexists(PREFIX_CONNECTED_VENDORS, str(v.user_id)))
        is_busy = asyncio.run(redis_client.sismember(PREFIX_BUSY_VENDORS, str(v.user_id)))
        queue_size = len([c for c in asyncio.run(redis_client.lrange(PREFIX_CALL_QUEUE, 0, -1))
                          if json.loads(c).get("vendor_id") == v.user_id])

        if is_online and not is_busy:
            available.append({
                "user_id": v.user_id,
                "user_name": v.user_name,
                "phone_number": v.phone_number,
                "queue_size": queue_size
            })

    return {"success": True, "total": len(available), "vendors": available}


@router.get("/api/v1/queue/position/{customer_id}")
def get_position(customer_id: int, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.user_id == customer_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    pos = asyncio.run(get_queue_position(customer_id))
    if not pos:
        return {"customer_id": customer_id, "in_queue": False}

    return {
        "customer_id": customer_id,
        "in_queue": True,
        "position": pos["position"],
        "wait_time": pos["wait_time_human"]
    }


@router.get("/api/v1/call-history/vendor/{vendor_id}")
def get_vendor_history(vendor_id: int, db: Session = Depends(get_db)):
    calls = db.query(CallHistory)\
              .filter(CallHistory.vendor_id == vendor_id)\
              .order_by(CallHistory.started_at.desc())\
              .all()

    return {
        "vendor_id": vendor_id,
        "calls": [
            {
                "customer_name": c.customer.user_name,
                "duration": c.duration_seconds,
                "started_at": c.started_at
            }
            for c in calls
        ]
    }


@router.get("/api/v1/call-requests/customer/{customer_id}")
def get_customer_request_history(customer_id: int, db: Session = Depends(get_db)):
    requests = db.query(CallRequestHistory)\
                 .filter(CallRequestHistory.customer_id == customer_id)\
                 .order_by(CallRequestHistory.requested_at.desc())\
                 .all()

    return {
        "customer_id": customer_id,
        "requests": [
            {
                "vendor_name": r.vendor.user_name,
                "product_id": r.product_id,
                "requested_at": r.requested_at,
                "status": r.status,
                "wait_duration_seconds": r.wait_duration_seconds
            }
            for r in requests
        ]
    }


@router.get("/api/v1/call-requests/vendor/{vendor_id}")
def get_vendor_request_history(vendor_id: int, db: Session = Depends(get_db)):
    requests = db.query(CallRequestHistory)\
                 .filter(CallRequestHistory.vendor_id == vendor_id)\
                 .order_by(CallRequestHistory.requested_at.desc())\
                 .all()

    return {
        "vendor_id": vendor_id,
        "requests": [
            {
                "customer_name": r.customer.user_name,
                "product_id": r.product_id,
                "requested_at": r.requested_at,
                "status": r.status,
                "wait_duration_seconds": r.wait_duration_seconds
            }
            for r in requests
        ]
    }


@router.get("/debug/state")
async def debug_state():
    return {
        "connected_vendors": [int(k) for k in await redis_client.hkeys(PREFIX_CONNECTED_VENDORS)],
        "busy_vendors": [int(v) async for v in redis_client.sscan_iter(PREFIX_BUSY_VENDORS)],
        "pending_requests": {
            int(k): json.loads(v) async for k, v in redis_client.hgetall(PREFIX_PENDING_REQUESTS).items()
        },
        "call_queue": [
            json.loads(item) async for item in redis_client.lrange(PREFIX_CALL_QUEUE, 0, -1)
        ],
        "active_calls": {
            k: json.loads(v) async for k, v in redis_client.hgetall(PREFIX_ACTIVE_CALLS).items()
        }
    }


# -------------------------------
# Call Management Functions
# -------------------------------
MAX_CALL_DURATION = 800  # 30 seconds for testing

async def cleanup_vendor_connection(vendor_id: int, ws_id: str):
    """Safely remove vendor from all state systems"""
    print(f"🧹 Cleaning up stale connection for vendor {vendor_id}")

    await redis_client.hdel(PREFIX_CONNECTED_VENDORS, str(vendor_id))
    await redis_client.srem(PREFIX_BUSY_VENDORS, str(vendor_id))
    await redis_client.hdel(PREFIX_PENDING_REQUESTS, str(vendor_id))
    
    if ws_id in connected_websockets:
        del connected_websockets[ws_id]

    print(f"✅ Vendor {vendor_id} fully cleaned up due to stale connection")

async def assign_next_request_to_vendor(vendor_id: int):
    """
    Notify vendor of the next valid call request in queue (FIFO).
    Only includes currently connected customers.
    """
    is_connected = await redis_client.hexists(PREFIX_CONNECTED_VENDORS, str(vendor_id))
    if not is_connected:
        return

    is_busy = await redis_client.sismember(PREFIX_BUSY_VENDORS, str(vendor_id))
    has_pending = await redis_client.hexists(PREFIX_PENDING_REQUESTS, str(vendor_id))
    if is_busy or has_pending:
        return

    queue = await redis_client.lrange(PREFIX_CALL_QUEUE, 0, -1)
    entries = [json.loads(item) for item in queue if json.loads(item).get("vendor_id") == vendor_id]

    # Sort by join time
    entries.sort(key=lambda x: x["joined_at"])

    # Filter only connected customers
    valid_entries = []
    for req in entries:
        cust_id = req["customer_id"]
        is_cust_connected = await redis_client.hexists(PREFIX_CUSTOMER_WS, str(cust_id))
        if is_cust_connected:
            valid_entries.append(req)

    # Rebuild queue for this vendor
    other_vendors = [item for item in queue if json.loads(item).get("vendor_id") != vendor_id]
    new_queue = other_vendors + [json.dumps(req) for req in valid_entries]
    await redis_client.delete(PREFIX_CALL_QUEUE)
    if new_queue:
        await redis_client.rpush(PREFIX_CALL_QUEUE, *new_queue)

    if not valid_entries:
        return

    first_req = valid_entries[0]
    customer_id = first_req["customer_id"]

    db = SessionLocal()
    try:
        cust_user = db.query(User).filter(User.user_id == customer_id).first()
        if not cust_user:
            print(f"❌ Customer {customer_id} not found in DB")
            return

        request_id = str(uuid.uuid4())
        await redis_client.hset(PREFIX_PENDING_REQUEST_IDS, str(customer_id), request_id)

        pending_data = {
            "customer_id": customer_id,
            "product_id": first_req["product_id"],
            "room_name": first_req["room_name"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "request_id": request_id
        }
        await redis_client.hset(PREFIX_PENDING_REQUESTS, str(vendor_id), json.dumps(pending_data))

        # Update request history
        db.query(CallRequestHistory).filter(
            CallRequestHistory.customer_id == customer_id,
            CallRequestHistory.vendor_id == vendor_id,
            CallRequestHistory.status == "pending"
        ).update({"status": "assigned"})
        db.commit()

        # Send to vendor
        ws_id = await redis_client.hget(PREFIX_CONNECTED_VENDORS, str(vendor_id))
        ws = connected_websockets.get(ws_id)
        if ws and is_websocket_connected(ws):
            try:
                await ws.send_text(json.dumps({
                    "is_incoming_call": True,
                    "event": "call_request",
                    "customer_name": cust_user.user_name,
                    "customer_id": customer_id,
                    "product_id": first_req["product_id"],
                    "timestamp": datetime.now(timezone.utc).isoformat()
                }))
                print(f"📞 Sent call request to vendor {vendor_id} for customer {customer_id}")
            except Exception as e:
                print(f"❌ Failed to send message to vendor {vendor_id}: {e}")
                # 👇 Clean up stale connection
                await cleanup_vendor_connection(vendor_id, ws_id)
        else:
            print(f"⚠️ Cannot send to vendor {vendor_id}: WebSocket not connected")
            await cleanup_vendor_connection(vendor_id, ws_id)

    except Exception as e:
        print(f"❌ Failed to assign request to vendor {vendor_id}: {e}")
        db.rollback()
    finally:
        db.close()


async def start_call_manual(customer_id: int, vendor_id: int, product_id: int, room_name: str, db: Session):
    print(f"✅ Starting call after vendor accept: {customer_id} → {vendor_id}")
    accepted_at = datetime.now(timezone.utc)

    # Update request history
    db_task = SessionLocal()
    try:
        req_hist = db_task.query(CallRequestHistory).filter(
            CallRequestHistory.customer_id == customer_id,
            CallRequestHistory.vendor_id == vendor_id,
            CallRequestHistory.status == "assigned"
        ).first()

        if not req_hist:
            print(f"❌ Request history not found for {customer_id}")
            return

        requested_at = req_hist.requested_at.replace(tzinfo=timezone.utc) if req_hist.requested_at.tzinfo is None else req_hist.requested_at
        wait_duration = int((accepted_at - requested_at).total_seconds())

        db_task.query(CallRequestHistory).filter(CallRequestHistory.id == req_hist.id).update({
            "accepted_at": accepted_at,
            "status": "accepted",
            "wait_duration_seconds": wait_duration
        })
        db_task.commit()
    except Exception as e:
        db_task.rollback()
        print(f"❌ Failed to update request history: {e}")
        return
    finally:
        db_task.close()

    # Get WebSockets
    cust_ws_id = await redis_client.hget(PREFIX_CUSTOMER_WS, str(customer_id))
    vend_ws_id = await redis_client.hget(PREFIX_CONNECTED_VENDORS, str(vendor_id))

    cust_ws = connected_websockets.get(cust_ws_id)
    vend_ws = connected_websockets.get(vend_ws_id)

    if not (cust_ws and is_websocket_connected(cust_ws)):
        print(f"❌ Customer {customer_id} not connected")
        return

    if not (vend_ws and is_websocket_connected(vend_ws)):
        print(f"❌ Vendor {vendor_id} not connected")
        return

    await redis_client.sadd(PREFIX_BUSY_VENDORS, str(vendor_id))

    call_info = {
        "customer_id": customer_id,
        "vendor_id": vendor_id,
        "product_id": product_id,
        "start_time": accepted_at.isoformat(),
        "room_name": room_name
    }
    await redis_client.hset(PREFIX_ACTIVE_CALLS, room_name, json.dumps(call_info))

    try:
        lk_room = await create_livekit_room(room_name)
        stream = StreamSession(
            user_id=vendor_id,
            room_id=lk_room.sid,
            room_name=room_name,
            stream_is_live=True
        )
        db.add(stream)
        db.commit()

        cust_user = db.query(User).filter(User.user_id == customer_id).first()
        vend_user = db.query(User).filter(User.user_id == vendor_id).first()

        if not cust_user or not vend_user:
            print("❌ User not found")
            await redis_client.srem(PREFIX_BUSY_VENDORS, str(vendor_id))
            return

        customer_token = create_livekit_token(cust_user.user_name, str(customer_id), room_name, True)
        vendor_token = create_livekit_token(vend_user.user_name, str(vendor_id), room_name, True)

        await cust_ws.send_text(json.dumps({
            "event": "call_started",
            "room": room_name,
            "token": customer_token
        }))
        print(f"✅ Sent call_started to customer {customer_id}")

        await vend_ws.send_text(json.dumps({
            "event": "call_started",
            "room": room_name,
            "token": vendor_token
        }))
        print(f"✅ Sent call_started to vendor {vendor_id}")

        asyncio.create_task(monitor_call_duration(room_name, vendor_id, db))

    except Exception as e:
        print(f"❌ Failed to start call: {e}")
        await redis_client.srem(PREFIX_BUSY_VENDORS, str(vendor_id))


async def monitor_call_duration(room_name: str, vendor_id: int, db: Session):
    await asyncio.sleep(MAX_CALL_DURATION)
    if await redis_client.hexists(PREFIX_ACTIVE_CALLS, room_name):
        await end_call(room_name, vendor_id, db)


async def end_call(room_name: str, vendor_id: int, db: Session):
    call_data = await redis_client.hget(PREFIX_ACTIVE_CALLS, room_name)
    if not call_data:
        return
    call = json.loads(call_data)
    await redis_client.hdel(PREFIX_ACTIVE_CALLS, room_name)

    customer_id = call["customer_id"]
    start_time = datetime.fromisoformat(call["start_time"]).replace(tzinfo=timezone.utc)

    now = datetime.now(timezone.utc)
    duration = int((now - start_time).total_seconds())

    history = CallHistory(
        vendor_id=vendor_id,
        customer_id=customer_id,
        product_id=call.get("product_id", 0),
        room_name=room_name,
        started_at=start_time,
        ended_at=now,
        duration_seconds=duration
    )
    try:
        db.add(history)
        db.commit()
        print(f"✅ Call logged in history: {room_name}, duration={duration}s")
    except Exception as e:
        db.rollback()
        print(f"❌ Failed to log call history: {e}")

    # Notify users
    for user_id in [customer_id, vendor_id]:
        ws_id = await redis_client.hget(PREFIX_CUSTOMER_WS if user_id == customer_id else PREFIX_CONNECTED_VENDORS, str(user_id))
        ws = connected_websockets.get(ws_id)
        if ws and is_websocket_connected(ws):
            try:
                await ws.send_text(json.dumps({
                    "event": "call_ended",
                    "duration": duration,
                    "customer_id": customer_id  # 👈 Add customer ID
                }))
            except Exception as e:
                print(f"❌ Failed to notify user {user_id}: {e}")

    # Cleanup
    await redis_client.hdel(PREFIX_CUSTOMER_WS, str(customer_id))
    await redis_client.lrem(PREFIX_CALL_QUEUE, 0, json.dumps({k: v for k, v in call.items() if k != "start_time"}))
    await redis_client.srem(PREFIX_BUSY_VENDORS, str(vendor_id))
    await end_livekit_room(room_name)

    # Next request
    await asyncio.sleep(0.1)
    asyncio.create_task(assign_next_request_to_vendor(vendor_id))

    print(f"🔚 Call ended. Vendor {vendor_id} is free. Checking next request.")


def is_websocket_connected(ws: WebSocket) -> bool:
    return ws is not None and ws.client_state == WebSocketState.CONNECTED


# -------------------------------
# WebSocket Endpoint
# -------------------------------
@router.websocket("/ws/room/join")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    db = next(get_db())
    user_id = None
    vendor_id = None
    product_id = None
    is_vendor = False
    ws_id = None

    try:
        raw = await websocket.receive_text()
        data = json.loads(raw)
        user_id = data.get("user_id")
        vendor_id = data.get("vendor_id")
        product_id = data.get("product_id")

        if not user_id or not vendor_id:
            await websocket.send_text(json.dumps({"error": "user_id and vendor_id required"}))
            return

        user = db.query(User).filter(User.user_id == user_id).first()
        if not user:
            await websocket.send_text(json.dumps({"error": "User not found"}))
            return

        target_vendor = db.query(User).filter(User.user_id == vendor_id, User.role_id == 0).first()
        if not target_vendor:
            await websocket.send_text(json.dumps({"error": "Vendor not found"}))
            return

        is_vendor = (user.role_id == 0)
        ws_id = await generate_ws_id()
        connected_websockets[ws_id] = websocket
        expected_disconnect = False 
        # Register in Redis
        if is_vendor:
            exists = await redis_client.hexists(PREFIX_CONNECTED_VENDORS, str(user_id))
            if exists:
                await websocket.send_text(json.dumps({"error": "Already connected"}))
                return

            await redis_client.hset(PREFIX_CONNECTED_VENDORS, str(user_id), ws_id)
            print(f"✅ Vendor {user_id} connected")

            await websocket.send_text(json.dumps({
                "event": "connected",
                "message": "Waiting for call requests..."
            }))

            asyncio.create_task(assign_next_request_to_vendor(user_id))
            expected_disconnect = False 
            while True:
                try:
                    msg = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                    cmd = json.loads(msg)

                    if cmd.get("action") == "accept_call":
                        customer_id = cmd.get("customer_id")
                        if not customer_id:
                            await websocket.send_text(json.dumps({"error": "customer_id required"}))
                            continue

                        req_data = await redis_client.hget(PREFIX_PENDING_REQUESTS, str(vendor_id))
                        if not req_data:
                            print(f"❌ No pending request for vendor {vendor_id}")
                            continue
                        req = json.loads(req_data)
                        if req["customer_id"] != customer_id:
                            print(f"❌ Mismatch: expected {req['customer_id']}, got {customer_id}")
                            continue

                        if await redis_client.sismember(PREFIX_BUSY_VENDORS, str(vendor_id)):
                            print(f"❌ Vendor {vendor_id} is busy")
                            continue

                        print(f"✅ Vendor {vendor_id} accepted call from {customer_id}")
                        db_task = SessionLocal()
                        try:
                            await start_call_manual(
                                customer_id=customer_id,
                                vendor_id=vendor_id,
                                product_id=req["product_id"],
                                room_name=req["room_name"],
                                db=db_task
                            )
                            await redis_client.hdel(PREFIX_PENDING_REQUESTS, str(vendor_id))
                        except Exception as e:
                            print(f"❌ Error in start_call_manual: {e}")
                        finally:
                            db_task.close()

                    elif cmd.get("action") == "reject_call":
                        customer_id = cmd.get("customer_id")
                        req_data = await redis_client.hget(PREFIX_PENDING_REQUESTS, str(vendor_id))
                        if not req_data:
                            continue
                        req = json.loads(req_data)
                        if req["customer_id"] != customer_id:
                            continue

                        db_task = SessionLocal()
                        try:
                            now = datetime.now(timezone.utc)
                            wait_duration = int((now - datetime.fromisoformat(req["timestamp"])).total_seconds())
                            db_task.query(CallRequestHistory).filter(
                                CallRequestHistory.customer_id == customer_id,
                                CallRequestHistory.vendor_id == vendor_id,
                                CallRequestHistory.status == "assigned"
                            ).update({
                                "rejected_at": now,
                                "status": "rejected",
                                "wait_duration_seconds": wait_duration
                            })
                            db_task.commit()
                        finally:
                            db_task.close()

                        await redis_client.hdel(PREFIX_PENDING_REQUESTS, str(vendor_id))

                        cust_ws_id = await redis_client.hget(PREFIX_CUSTOMER_WS, str(customer_id))
                        cust_ws = connected_websockets.get(cust_ws_id)
                        if cust_ws:
                            try:
                                await cust_ws.send_text(json.dumps({
                                    "event": "call_rejected",
                                    "message": "Your call request was rejected by the vendor."
                                }))
                                await cust_ws.close()  # 👈 Explicitly close the customer's connection
                                print(f"📞 Closed WebSocket for customer {customer_id} after rejection")
                            except Exception as e:
                                print(f"Error closing customer WebSocket: {e}")
                            finally:
                                # Remove from in-memory
                                if cust_ws_id in connected_websockets:
                                    del connected_websockets[cust_ws_id]
                                # Remove from Redis
                                await redis_client.hdel(PREFIX_CUSTOMER_WS, str(customer_id))
                                # Remove from queue
                                queue = await redis_client.lrange(PREFIX_CALL_QUEUE, 0, -1)
                                new_queue = [item for item in queue if json.loads(item).get("customer_id") != customer_id]
                                await redis_client.delete(PREFIX_CALL_QUEUE)
                                if new_queue:
                                    await redis_client.rpush(PREFIX_CALL_QUEUE, *new_queue)
                                # Optional: remove from pending request IDs
                                await redis_client.hdel(PREFIX_PENDING_REQUEST_IDS, str(customer_id))

                        asyncio.create_task(assign_next_request_to_vendor(vendor_id))

                    elif cmd.get("action") == "end_call":
                        room_name = cmd.get("room_name")
                        if not room_name:
                            await websocket.send_text(json.dumps({"error": "room_name required to end call"}))
                            continue

                        print(f"🛑 {('Vendor' if is_vendor else 'Customer')} {user_id} manually ended call in room {room_name}")

                        # Reuse the existing end_call logic
                        db_task = SessionLocal()
                        try:
                            await end_call(room_name, vendor_id, db_task)
                        except Exception as e:
                            print(f"❌ Error during manual end_call: {e}")
                        finally:
                            db_task.close()
                        expected_disconnect = True 
                        await websocket.send_text(json.dumps({
                            "event": "call_ended",
                            "message": "Call ended. You are still online and can receive new requests."
                        }))

                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    expected_disconnect = False
                    print(f"⚠️ Vendor loop error: {e}")
                    break

        else:
            print(f"🟢 Customer {user_id} connected and requesting call to vendor {vendor_id}")

            # Remove duplicate
            queue = await redis_client.lrange(PREFIX_CALL_QUEUE, 0, -1)
            new_queue = [item for item in queue if json.loads(item).get("customer_id") != user_id]
            await redis_client.delete(PREFIX_CALL_QUEUE)
            if new_queue:
                await redis_client.rpush(PREFIX_CALL_QUEUE, *new_queue)

            request_id = str(uuid.uuid4())
            await redis_client.hset(PREFIX_PENDING_REQUEST_IDS, str(user_id), request_id)

            room_name = f"room_{vendor_id}_{product_id}_{int(datetime.now(timezone.utc).timestamp())}"

            try:
                call_request_log = CallRequestHistory(
                    vendor_id=vendor_id,
                    customer_id=user_id,
                    product_id=product_id,
                    requested_at=datetime.now(timezone.utc),
                    status="pending",
                    wait_duration_seconds=0
                )
                db.add(call_request_log)
                db.commit()
                db.refresh(call_request_log)
                print(f"✅ Request logged in DB for customer {user_id}, ID: {call_request_log.id}")
            except Exception as e:
                db.rollback()
                print(f"❌ DB error while logging request: {e}")
                await websocket.send_text(json.dumps({"error": "Failed to log request"}))
                return

            entry = {
                "customer_id": user_id,
                "vendor_id": vendor_id,
                "product_id": product_id,
                "room_name": room_name,
                "joined_at": datetime.now(timezone.utc).isoformat(),
                "request_id": request_id,
                "db_id": call_request_log.id
            }
            await redis_client.rpush(PREFIX_CALL_QUEUE, json.dumps(entry))
            await redis_client.hset(PREFIX_CUSTOMER_WS, str(user_id), ws_id)

            asyncio.create_task(assign_next_request_to_vendor(vendor_id))

            # Check vendor connection and busy status
            is_vendor_connected = await redis_client.hexists(PREFIX_CONNECTED_VENDORS, str(vendor_id))
            is_vendor_busy = await redis_client.sismember(PREFIX_BUSY_VENDORS, str(vendor_id))

            queue_pos_info = await get_queue_position(user_id)

            if queue_pos_info:
                position = queue_pos_info["position"]
                wait_time = queue_pos_info["wait_time_seconds"]
            else:
                position = None
                wait_time = 0

            # Determine message based on vendor's real-time status
            if not is_vendor_connected:
                message = "The vendor is currently offline. Your request will be delivered when they come online."
                estimated_wait = None  # Unknown
            elif is_vendor_busy:
                if position == 1:
                    message = "You're next in line. Estimated wait: 800 seconds."
                else:
                    message = f"{position - 1} people ahead. Estimated wait: {wait_time} seconds."
                estimated_wait = wait_time
            else:
                # Vendor is online and not busy
                if position == 1:
                    message = "Vendor is available. Waiting for them to accept your request..."
                else:
                    message = f"{position - 1} people ahead. Estimated wait: {wait_time} seconds."
                estimated_wait = wait_time if position > 1 else 0

            await websocket.send_text(json.dumps({
                "event": "request_sent",
                "position": position,
                "message": message,
                "estimated_wait_seconds": estimated_wait,
                "status": "waiting",
                "vendor_online": is_vendor_connected,
                "vendor_busy": is_vendor_busy
            }))
            print(f"📩 Sent request_sent to customer {user_id}: {message}")

            while True:
                try:
                    msg = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                    data = json.loads(msg)
                    if data.get("action") == "cancel_request":
                        print(f"🛑 Customer {user_id} canceled request")
                        break
                    elif data.get("action") == "end_call":
                        room_name = data.get("room_name")
                        if not room_name:
                            await websocket.send_text(json.dumps({"error": "room_name required"}))
                            continue

                        print(f"🛑 Customer {user_id} manually ended call: {room_name}")

                        db_task = SessionLocal()
                        try:
                            await end_call(room_name, vendor_id, db_task)
                        except Exception as e:
                            print(f"❌ Failed to end call: {e}")
                            await websocket.send_text(json.dumps({"error": "Failed to end call"}))
                        finally:
                            db_task.close()

                        break  # Exit loop after ending call

                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    print(f"⚠️ Customer loop error: {e}")
                    break

    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        if is_vendor and user_id:
            # Only clean up if it was a real disconnection
            if not expected_disconnect:
                print(f"🔴 Vendor {user_id} disconnected (network or error)")
                await redis_client.hdel(PREFIX_CONNECTED_VENDORS, str(user_id))
                await redis_client.srem(PREFIX_BUSY_VENDORS, str(user_id))
                await redis_client.hdel(PREFIX_PENDING_REQUESTS, str(user_id))
                if ws_id in connected_websockets:
                    del connected_websockets[ws_id]
                print(f"🧹 Vendor {user_id} cleaned up")
            else:
                print(f"ℹ️ Vendor {user_id} ended call but remains connected and available")
        elif not is_vendor and user_id:
            print(f"🔴 Customer {user_id} disconnected")
            await redis_client.hdel(PREFIX_CUSTOMER_WS, str(user_id))
            queue = await redis_client.lrange(PREFIX_CALL_QUEUE, 0, -1)
            new_queue = [item for item in queue if json.loads(item).get("customer_id") != user_id]
            await redis_client.delete(PREFIX_CALL_QUEUE)
            if new_queue:
                await redis_client.rpush(PREFIX_CALL_QUEUE, *new_queue)
            if ws_id in connected_websockets:
                del connected_websockets[ws_id]
            print(f"🧹 Customer {user_id} cleaned up")

        db.close()