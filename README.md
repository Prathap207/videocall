# main.py
from fastapi import APIRouter, WebSocket, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
import asyncio
import json
from datetime import datetime
from typing import Optional, Dict
import uuid
from starlette.websockets import WebSocketState
# Local modules
from app.config import config
from app.config.database import SessionLocal, engine
from app.schemas.databaseSchemas import Base, User, StreamSession, CallHistory, CallRequestHistory

# Create tables
Base.metadata.create_all(bind=engine)

router = APIRouter()

# -------------------------------
# In-Memory State
# -------------------------------
connected_vendors: Dict[int, WebSocket] = {}      # vendor_id → ws
busy_vendors: set = set()                         # vendor_id
customer_websockets: Dict[int, WebSocket] = {}   # customer_id → ws
call_queue: list = []                            # [{ customer_id, vendor_id, ... }]
pending_requests: Dict[int, Dict] = {}           # vendor_id → request data
active_calls: Dict[str, Dict] = {}               # room_name → call info
pending_request_ids: Dict[int, str] = {}         # customer_id → request_id
MAX_CALL_DURATION = 30  # 2 minutes


# Dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


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
def get_queue_position(customer_id: int) -> Optional[Dict]:
    target_entry = None
    for entry in call_queue:
        if entry["customer_id"] == customer_id:
            target_entry = entry
            break
    if not target_entry:
        return None

    vendor_id = target_entry["vendor_id"]
    ahead_count = 0
    for entry in call_queue:
        if entry["vendor_id"] == vendor_id:
            if entry["customer_id"] == customer_id:
                break
            ahead_count += 1

    is_vendor_busy = vendor_id in busy_vendors
    total_wait_slots = (1 if is_vendor_busy else 0) + ahead_count
    wait_time = total_wait_slots * MAX_CALL_DURATION

    return {
        "position": ahead_count + 1,
        "wait_time_seconds": wait_time,
        "wait_time_human": f"{wait_time // 30} min {wait_time % 30} sec"
    }


# -------------------------------
# APIs
# -------------------------------
@router.get("/api/v1/vendors/available")
def get_available_vendors(db: Session = Depends(get_db)):
    vendors = db.query(User).filter(User.role_id == 0).all()
    available = []

    for v in vendors:
        is_online = v.user_id in connected_vendors
        is_busy = v.user_id in busy_vendors
        queue_size = len([c for c in call_queue if c["vendor_id"] == v.user_id])

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

    pos = get_queue_position(customer_id)
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
def debug_state():
    return {
        "connected_vendors": list(connected_vendors.keys()),
        "busy_vendors": list(busy_vendors),
        "pending_requests": {k: v["customer_id"] for k, v in pending_requests.items()},
        "call_queue": [{"customer": c["customer_id"], "vendor": c["vendor_id"]} for c in call_queue],
        "active_calls": active_calls
    }


# -------------------------------
# Call Management Functions
# -------------------------------
async def assign_next_request_to_vendor(vendor_id: int):
    if vendor_id not in connected_vendors:
        return

    vendor_ws = connected_vendors[vendor_id]

    # Find next customer for this vendor
    next_customer = None
    for req in call_queue:
        if req["vendor_id"] == vendor_id:
            next_customer = req
            break
    if not next_customer:
        return

    customer_id = next_customer["customer_id"]
    db = SessionLocal()
    try:
        cust_user = db.query(User).filter(User.user_id == customer_id).first()
        if not cust_user:
            return

        # ✅ If vendor is FREE → send call_request
        if vendor_id not in busy_vendors and vendor_id not in pending_requests:
            db.query(CallRequestHistory).filter(
                CallRequestHistory.customer_id == customer_id,
                CallRequestHistory.vendor_id == vendor_id,
                CallRequestHistory.status == "pending"
            ).update({"status": "assigned"})
            db.commit()

            pending_requests[vendor_id] = {
                "customer_id": customer_id,
                "vendor_id": vendor_id,
                "product_id": next_customer["product_id"],
                "room_name": next_customer["room_name"],
                "request_id": next_customer["request_id"],
                "timestamp": datetime.utcnow()
            }

            if is_websocket_connected(vendor_ws):
                await vendor_ws.send_text(json.dumps({
                    "event": "call_request",
                    "customer_id": customer_id,
                    "customer_name": cust_user.user_name,
                    "position": get_queue_position(customer_id)["position"]
                }))
                print(f"📞 Sent call_request to vendor {vendor_id} for customer {customer_id}")

        # ✅ If vendor is BUSY → check if backlog is growing
        else:
            queue_size = len([c for c in call_queue if c["vendor_id"] == vendor_id])
            # Only notify if 2 or more are WAITING (excluding current)
            if queue_size >= 2:
                # Dedup logic (optional)
                if is_websocket_connected(vendor_ws):
                    try:
                        await vendor_ws.send_text(json.dumps({
                            "event": "queue_update",
                            "message": f"{queue_size - 1} other(s) waiting after current call.",
                            "queue_size": queue_size - 1
                        }))
                        print(f"📊 Notified vendor {vendor_id} of backlog: {queue_size - 1} waiting")
                    except Exception as e:
                        print(f"❌ Failed to send queue_update: {e}")

    except Exception as e:
        db.rollback()
        print(f"❌ Failed to assign request: {e}")
    finally:
        db.close()

        
async def start_call(customer_id: int, vendor_id: int, product_id: int, room_name: str, db: Session):
    print(f"📞 Starting call: {customer_id} → {vendor_id}")

    req = pending_requests.get(vendor_id)
    if not req or req["customer_id"] != customer_id:
        print(f"⏭️ Call request for {customer_id} is outdated or not found")
        return

    current_request_id = pending_request_ids.get(customer_id)
    if not current_request_id or current_request_id != req["request_id"]:
        print(f"⏭️ Request ID mismatch for {customer_id}. Expected: {req['request_id']}, Got: {current_request_id}")
        return

    request_time = req["timestamp"]
    accepted_at = datetime.utcnow()
    wait_duration = int((accepted_at - request_time).total_seconds())

    # ✅✅ CRITICAL: Remove from pending AND queue
    del pending_requests[vendor_id]
    # ✅ Remove from call queue
    call_queue[:] = [c for c in call_queue if c["customer_id"] != customer_id]

    # Get WebSockets
    customer_ws = customer_websockets.get(customer_id)
    vendor_ws = connected_vendors.get(vendor_id)

    # ✅ Validate customer connection
    if not is_websocket_connected(customer_ws):
        print(f"❌ Customer {customer_id} WebSocket is not connected")
        # Log as missed
        db_task = SessionLocal()
        try:
            db_task.query(CallRequestHistory).filter(
                CallRequestHistory.customer_id == customer_id,
                CallRequestHistory.vendor_id == vendor_id,
                CallRequestHistory.status == "assigned"
            ).update({
                "status": "missed",
                "wait_duration_seconds": wait_duration
            })
            db_task.commit()
            print(f"✅ Logged missed call for customer {customer_id}")
        except Exception as e:
            db_task.rollback()
            print(f"❌ Failed to log missed call: {e}")
        finally:
            db_task.close()

        # ✅ Also remove from customer_websockets if present
        if customer_id in customer_websockets:
            del customer_websockets[customer_id]

        # Try next call
        asyncio.create_task(start_next_call_if_any())
        return
    
async def monitor_call_duration(room_name: str, vendor_id: int, db: Session):
    await asyncio.sleep(MAX_CALL_DURATION)
    if room_name in active_calls:
        await end_call(room_name, vendor_id, db)


async def end_call(room_name: str, vendor_id: int, db: Session):
    call = active_calls.pop(room_name, None)
    if not call:
        return

    customer_id = call["customer_id"]
    start_time = call["start_time"]
    duration = int((datetime.utcnow() - start_time).total_seconds())
    # After call ends
    if customer_id in customer_websockets:
        del customer_websockets[customer_id]
    # Log call history
    history = CallHistory(
        vendor_id=vendor_id,
        customer_id=customer_id,
        product_id=call.get("product_id", 0),
        room_name=room_name,
        started_at=start_time,
        ended_at=datetime.utcnow(),
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
    cust_ws = customer_websockets.get(customer_id)
    vend_ws = connected_vendors.get(vendor_id)

    for ws in [cust_ws, vend_ws]:
        if ws and is_websocket_connected(ws):
            try:
                await ws.send_text(json.dumps({"event": "call_ended", "duration": duration}))
            except Exception as e:
                print(f"❌ Failed to notify user {ws} about call end: {e}")

    # --- ✅ Critical: Cleanup state BEFORE async tasks ---
    busy_vendors.discard(vendor_id)  # Mark vendor as free
    await end_livekit_room(room_name)  # Best effort

    # --- ✅ Trigger next call assignment ---
    # This will check all idle vendors and assign next queued customer
    asyncio.create_task(start_next_call_if_any())

    print(f"🔚 Call ended and cleanup done for room {room_name}. Vendor {vendor_id} is now free.")

def is_websocket_connected(ws: WebSocket) -> bool:
    return ws is not None and ws.client_state == WebSocketState.CONNECTED

async def start_next_call_if_any():
    """
    After a call ends or is rejected, assign the next customer to each idle vendor.
    """
    for vendor_id in connected_vendors:
        if vendor_id not in busy_vendors:
            await assign_next_request_to_vendor(vendor_id)


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
    in_queue = False

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

        is_vendor = user.role_id == 0

        # --- Vendor: Go Online ---
        if is_vendor:
            if user.user_id in connected_vendors:
                await websocket.send_text(json.dumps({"error": "Already connected"}))
                return

            connected_vendors[user.user_id] = websocket
            print(f"✅ Vendor {user_id} connected")

            await websocket.send_text(json.dumps({
                "event": "connected",
                "message": "Waiting for call requests..."
            }))

            # Try to assign immediately
            asyncio.create_task(assign_next_request_to_vendor(user.user_id))

            while True:
                try:
                    msg = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                    cmd = json.loads(msg)

                    if cmd.get("action") == "accept_call":
                        print("pending_requests:", pending_requests)
                        req = pending_requests.get(vendor_id)
                        if not req or req["customer_id"] != cmd["customer_id"]:
                            continue
                        if vendor_id in busy_vendors:
                            continue

                        db_task = SessionLocal()
                        try:
                            await start_call(
                                customer_id=req["customer_id"],
                                vendor_id=vendor_id,
                                product_id=req["product_id"],
                                room_name=req["room_name"],
                                db=db_task
                            )
                        finally:
                            db_task.close()

                    elif cmd.get("action") == "reject_call":
                        req = pending_requests.get(vendor_id)
                        if not req or req["customer_id"] != cmd["customer_id"]:
                            continue

                        # Update DB
                        db_task = SessionLocal()
                        try:
                            now = datetime.utcnow()
                            db_task.query(CallRequestHistory).filter(
                                CallRequestHistory.customer_id == req["customer_id"],
                                CallRequestHistory.vendor_id == vendor_id,
                                CallRequestHistory.status == "assigned"
                            ).update({
                                "rejected_at": now,
                                "status": "rejected",
                                "wait_duration_seconds": int((now - req["timestamp"]).total_seconds())
                            })
                            db_task.commit()
                        finally:
                            db_task.close()

                        del pending_requests[vendor_id]

                        cust_ws = customer_websockets.get(req["customer_id"])
                        if cust_ws:
                            await cust_ws.send_text(json.dumps({
                                "event": "call_rejected",
                                "message": "Vendor rejected your request."
                            }))

                        asyncio.create_task(start_next_call_if_any())

                except asyncio.TimeoutError:
                    continue

        # --- Customer: Request Call ---
        else:
            print(f"🟢 Customer {user_id} connected and requesting call to vendor {vendor_id}")

            request_id = str(uuid.uuid4())
            pending_request_ids[user_id] = request_id

            room_name = f"room_{vendor_id}_{product_id}_{int(datetime.utcnow().timestamp())}"

            # Log request immediately
            try:
                call_request_log = CallRequestHistory(
                    vendor_id=vendor_id,
                    customer_id=user_id,
                    product_id=product_id,
                    requested_at=datetime.utcnow(),
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
                "joined_at": datetime.utcnow(),
                "request_id": request_id,
                "db_id": call_request_log.id
            }
            call_queue.append(entry)
            customer_websockets[user_id] = websocket
            in_queue = True
            asyncio.create_task(assign_next_request_to_vendor(vendor_id))

            # --- After appending to call_queue and customer_websockets ---
            # Estimate wait time and status message
            vendor_is_busy = vendor_id in busy_vendors
            queue_pos_info = get_queue_position(user_id)

            # Base wait time from queue
            estimated_wait = queue_pos_info["wait_time_seconds"] if queue_pos_info else 0

            # If vendor is busy, add duration of current call (up to MAX_CALL_DURATION)
            if vendor_is_busy and not any(c["customer_id"] == user_id for c in call_queue if c["vendor_id"] == vendor_id):
                # This customer is not already in queue; add current call duration
                estimated_wait += MAX_CALL_DURATION

            # Format message
            if vendor_is_busy and estimated_wait > 0:
                if estimated_wait >= 30:
                    wait_msg = f"Vendor is busy. Please wait approximately {estimated_wait} seconds for your turn."
                else:
                    wait_msg = f"Vendor is busy. Please wait {estimated_wait} seconds."
            else:
                wait_msg = "Waiting for vendor to accept your call..."

            await websocket.send_text(json.dumps({
                "event": "request_sent",
                "position": queue_pos_info["position"] if queue_pos_info else 1,
                "message": wait_msg,
                "estimated_wait_seconds": estimated_wait
            }))
            print(f"📩 Sent request_sent to customer {user_id}, position: {get_queue_position(user_id)['position']}")


            # Wait for cancel
            while True:
                try:
                    msg = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                    data = json.loads(msg)
                    if data.get("action") == "cancel_request":
                        print(f"🛑 Customer {user_id} canceled request")
                        break
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    print(f"⚠️  Error in customer loop: {e}")
                    break  # WebSocket likely closed

    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        if is_vendor and user_id in connected_vendors:
            print(f"🔴 Vendor {user_id} disconnected")
            connected_vendors.pop(user_id, None)
            busy_vendors.discard(user_id)
            if user_id in pending_requests:
                del pending_requests[user_id]
            asyncio.create_task(start_next_call_if_any())

        elif not is_vendor and user_id in customer_websockets:
            print(f"🔴 Customer {user_id} disconnected")

            # Cancel any pending request
            if user_id in pending_request_ids:
                req_id = pending_request_ids[user_id]
                for vid, req in list(pending_requests.items()):
                    if req["request_id"] == req_id:
                        db_task = SessionLocal()
                        try:
                            now = datetime.utcnow()
                            db_task.query(CallRequestHistory).filter(
                                CallRequestHistory.customer_id == user_id,
                                CallRequestHistory.vendor_id == vid,
                                CallRequestHistory.status.in_(["pending", "assigned"])
                            ).update({
                                "canceled_at": now,
                                "status": "canceled",
                                "wait_duration_seconds": int((now - req["timestamp"]).total_seconds())
                            })
                            db_task.commit()
                            print(f"✅ Canceled request for customer {user_id} (disconnected)")
                        except Exception as e:
                            print(f"❌ DB error on cancel: {e}")
                        finally:
                            db_task.close()
                        del pending_requests[vid]
                        break

            # Remove from queue and map
            call_queue[:] = [c for c in call_queue if c["customer_id"] != user_id]
            customer_websockets.pop(user_id, None)
            print(f"🧹 Cleaned up customer {user_id}'s session")

            asyncio.create_task(start_next_call_if_any())

        db.close()

async def notify_vendor_of_queue_update(vendor_id: int):
    """
    Notify vendor of queue size — ONLY if they are NOT in a call.
    Prevents distraction during active calls.
    """
    if vendor_id in busy_vendors:
        # Vendor is in a call → don't disturb
        print(f"🔇 Not notifying vendor {vendor_id}: currently in a call.")
        return

    vendor_ws = connected_vendors.get(vendor_id)
    if not vendor_ws or not is_websocket_connected(vendor_ws):
        return

    queue_size = len([c for c in call_queue if c["vendor_id"] == vendor_id])

    # Only notify if there's someone waiting
    if queue_size > 0:
        try:
            await vendor_ws.send_text(json.dumps({
                "event": "queue_update",
                "message": f"{queue_size} customer(s) waiting for you.",
                "queue_size": queue_size
            }))
            print(f"📤 Sent queue update to vendor {vendor_id}: {queue_size} waiting")
        except Exception as e:
            print(f"❌ Failed to send queue update to vendor {vendor_id}: {e}")