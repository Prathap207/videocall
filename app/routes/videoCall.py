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
MAX_CALL_DURATION = 30  # 30 seconds for testing

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
    vendor_id = None
    for entry in call_queue:
        if entry["customer_id"] == customer_id:
            target_entry = entry
            vendor_id = entry["vendor_id"]
            break
    if not target_entry or not vendor_id:
        return None

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
    """
    Notify vendor of the next valid call request in queue (FIFO).
    Only includes currently connected customers.
    """
    if vendor_id not in connected_vendors:
        return

    if vendor_id in busy_vendors or vendor_id in pending_requests:
        return

    # Get all requests for this vendor
    vendor_queue = [req for req in call_queue if req["vendor_id"] == vendor_id]

    # Sort by join time (oldest first)
    vendor_queue.sort(key=lambda x: x["joined_at"])

    # Keep only connected customers
    connected_queue = [
        req for req in vendor_queue
        if req["customer_id"] in customer_websockets 
        and is_websocket_connected(customer_websockets[req["customer_id"]])
    ]

    # Update queue (remove stale)
    call_queue[:] = [c for c in call_queue if c["vendor_id"] != vendor_id] + connected_queue

    if not connected_queue:
        return

    first_req = connected_queue[0]
    customer_id = first_req["customer_id"]

    db = SessionLocal()
    try:
        cust_user = db.query(User).filter(User.user_id == customer_id).first()
        if not cust_user:
            print(f"❌ Customer {customer_id} not found in DB")
            return

        request_id = str(uuid.uuid4())
        pending_request_ids[customer_id] = request_id

        pending_requests[vendor_id] = {
            "customer_id": customer_id,
            "product_id": first_req["product_id"],
            "room_name": first_req["room_name"],
            "timestamp": datetime.now(timezone.utc),
            "request_id": request_id
        }

        db.query(CallRequestHistory).filter(
            CallRequestHistory.customer_id == customer_id,
            CallRequestHistory.vendor_id == vendor_id,
            CallRequestHistory.status == "pending"
        ).update({"status": "assigned"})
        db.commit()

        vendor_ws = connected_vendors.get(vendor_id)
        if vendor_ws and is_websocket_connected(vendor_ws):
            await vendor_ws.send_text(json.dumps({
                "is_incoming_call":True,
                "event": "call_request",
                "customer_name": cust_user.user_name,
                "customer_id": customer_id,
                "product_id": first_req["product_id"],
                "timestamp": datetime.now(timezone.utc).isoformat()
            }))
            print(f"📞 Sent call request to vendor {vendor_id} for customer {customer_id}")

    except Exception as e:
        print(f"❌ Failed to assign request to vendor {vendor_id}: {e}")
        db.rollback()
    finally:
        db.close()


async def start_call_manual(customer_id: int, vendor_id: int, product_id: int, room_name: str, db: Session):
    """
    Start call only after vendor accepts.
    """
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

    customer_ws = customer_websockets.get(customer_id)
    vendor_ws = connected_vendors.get(vendor_id)

    if not (customer_ws and is_websocket_connected(customer_ws)):
        print(f"❌ Customer {customer_id} not connected")
        return

    if not (vendor_ws and is_websocket_connected(vendor_ws)):
        print(f"❌ Vendor {vendor_id} not connected")
        return

    busy_vendors.add(vendor_id)
    active_calls[room_name] = {
        "customer_id": customer_id,
        "vendor_id": vendor_id,
        "product_id": product_id,
        "start_time": accepted_at,
        "room_name": room_name
    }

    try:
        # Create LiveKit room
        lk_room = await create_livekit_room(room_name)
        stream = StreamSession(
            user_id=vendor_id,
            room_id=lk_room.sid,
            room_name=room_name,
            stream_is_live=True
        )
        db.add(stream)
        db.commit()

        # Get user info
        cust_user = db.query(User).filter(User.user_id == customer_id).first()
        vend_user = db.query(User).filter(User.user_id == vendor_id).first()

        if not cust_user or not vend_user:
            print("❌ User not found")
            busy_vendors.discard(vendor_id)
            return

        # Generate tokens
        customer_token = create_livekit_token(cust_user.user_name, str(customer_id), room_name, True)
        vendor_token = create_livekit_token(vend_user.user_name, str(vendor_id), room_name, True)

        # Notify both
        await customer_ws.send_text(json.dumps({
            "event": "call_started",
            "room": room_name,
            "token": customer_token
        }))
        print(f"✅ Sent call_started to customer {customer_id}")

        await vendor_ws.send_text(json.dumps({
            "event": "call_started",
            "room": room_name,
            "token": vendor_token
        }))
        print(f"✅ Sent call_started to vendor {vendor_id}")

        # Start duration monitor
        asyncio.create_task(monitor_call_duration(room_name, vendor_id, db))

    except Exception as e:
        print(f"❌ Failed to start call: {e}")
        busy_vendors.discard(vendor_id)


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

    cust_ws = customer_websockets.get(customer_id)
    vend_ws = connected_vendors.get(vendor_id)

    for ws in [cust_ws, vend_ws]:
        if ws and is_websocket_connected(ws):
            try:
                await ws.send_text(json.dumps({"event": "call_ended", "duration": duration}))
            except Exception as e:
                print(f"❌ Failed to notify user: {e}")

    # ✅ Cleanup: Remove customer from queue and tracking
    if customer_id in customer_websockets:
        del customer_websockets[customer_id]

    # ✅ Remove from call queue to prevent auto-re-request
    call_queue[:] = [c for c in call_queue if c["customer_id"] != customer_id]

    busy_vendors.discard(vendor_id)
    await end_livekit_room(room_name)

    # ✅ Check for next customer (only if someone else is waiting)
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

            # Check if any pending calls
            asyncio.create_task(assign_next_request_to_vendor(user.user_id))

            while True:
                try:
                    msg = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
                    cmd = json.loads(msg)

                    if cmd.get("action") == "accept_call":
                        customer_id = cmd.get("customer_id")
                        if not customer_id:
                            await websocket.send_text(json.dumps({"error": "customer_id required"}))
                            continue

                        req = pending_requests.get(vendor_id)
                        if not req:
                            print(f"❌ No pending request for vendor {vendor_id}")
                            continue
                        if req["customer_id"] != customer_id:
                            print(f"❌ Mismatch: expected {req['customer_id']}, got {customer_id}")
                            continue
                        if vendor_id in busy_vendors:
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
                            del pending_requests[vendor_id]
                        except Exception as e:
                            print(f"❌ Error in start_call_manual: {e}")
                        finally:
                            db_task.close()

                    elif cmd.get("action") == "reject_call":
                        customer_id = cmd.get("customer_id")
                        req = pending_requests.get(vendor_id)
                        if not req or req["customer_id"] != customer_id:
                            continue

                        db_task = SessionLocal()
                        try:
                            now = datetime.now(timezone.utc)
                            wait_duration = int((now - req["timestamp"]).total_seconds())
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

                        del pending_requests[vendor_id]

                        cust_ws = customer_websockets.get(customer_id)
                        if cust_ws:
                            await cust_ws.send_text(json.dumps({
                                "event": "call_rejected",
                                "message": "Vendor rejected your request."
                            }))

                        # Notify next in queue
                        asyncio.create_task(assign_next_request_to_vendor(vendor_id))

                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    print(f"⚠️ Vendor loop error: {e}")
                    break

        # --- Customer: Request Call ---
        else:
            print(f"🟢 Customer {user_id} connected and requesting call to vendor {vendor_id}")

            # Deduplicate: remove existing queue entry
            call_queue[:] = [c for c in call_queue if c["customer_id"] != user_id]

            request_id = str(uuid.uuid4())
            pending_request_ids[user_id] = request_id

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
                "joined_at": datetime.now(timezone.utc),
                "request_id": request_id,
                "db_id": call_request_log.id
            }
            call_queue.append(entry)
            customer_websockets[user_id] = websocket

            # ✅ Ensure vendor is notified of new request
            asyncio.create_task(assign_next_request_to_vendor(vendor_id))

            # Get updated position
            queue_pos_info = get_queue_position(user_id)

            # Determine message
            if queue_pos_info:
                position = queue_pos_info["position"]
                wait_time = queue_pos_info["wait_time_seconds"]
                is_vendor_busy = queue_pos_info["is_vendor_busy"]
            else:
                position = None
                wait_time = 0
                is_vendor_busy = vendor_id in busy_vendors

            if not queue_pos_info:
                message = "Request received. Waiting for vendor availability."
                estimated_wait = 0
            elif is_vendor_busy:
                if position == 1:
                    message = "You're next in line. Estimated wait: 30 seconds."
                else:
                    message = f"{position - 1} people ahead. Estimated wait: {wait_time} seconds."
                estimated_wait = wait_time
            else:
                if position == 1:
                    message = "Vendor is available. Waiting for them to accept your request..."
                    estimated_wait = 0
                else:
                    message = f"{position - 1} people ahead. Estimated wait: {wait_time} seconds."
                    estimated_wait = wait_time

            await websocket.send_text(json.dumps({
                "event": "request_sent",
                "position": position,
                "message": message,
                "estimated_wait_seconds": estimated_wait,
                "status": "waiting"
            }))
            print(f"📩 Sent request_sent to customer {user_id}: {message}")

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
                    print(f"⚠️ Customer loop error: {e}")
                    break

    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        if is_vendor and user_id in connected_vendors:
            print(f"🔴 Vendor {user_id} disconnected")
            connected_vendors.pop(user_id, None)
            busy_vendors.discard(user_id)
            if user_id in pending_requests:
                del pending_requests[user_id]
            print(f"🧹 Vendor {user_id} cleaned up")

        elif not is_vendor:
            print(f"🔴 Customer {user_id} disconnected")
            customer_websockets.pop(user_id, None)
            call_queue[:] = [c for c in call_queue if c["customer_id"] != user_id]
            print(f"🧹 Customer {user_id} cleaned up")

        db.close()