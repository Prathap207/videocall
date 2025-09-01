from ast import Dict, List
from datetime import datetime, timezone
import json
from typing import Optional
import uuid
import redis.asyncio as redis
from videocall.app.routes.videoCall import MAX_CALL_DURATION

# Redis Key Patterns
KEY_CONNECTED_VENDORS = "vendors:connected"          # Set: vendor_id
KEY_BUSY_VENDORS = "vendors:busy"                    # Set: vendor_id
KEY_CUSTOMER_WS = "customer:ws:{customer_id}"        # String: websocket_id (not actual ws)
KEY_CALL_QUEUE = "queue:vendor:{vendor_id}"          # List: JSON str of request
KEY_PENDING_REQUESTS = "pending:vendor:{vendor_id}"  # Hash or String: request data
KEY_ACTIVE_CALLS = "active:call:{room_name}"         # Hash: call info
KEY_PENDING_REQUEST_ID = "reqid:cust:{customer_id}"  # String: request_id
CHANNEL_VENDOR_NOTIFY = "notify:vendor:{vendor_id}"   # Pub/Sub channel
CHANNEL_CUSTOMER_NOTIFY = "notify:cust:{customer_id}"

# -------------------------------
# Redis Helpers
# -------------------------------
redis_client: redis.Redis
async def add_connected_vendor(vendor_id: int):
    await redis_client.sadd(KEY_CONNECTED_VENDORS, vendor_id)

async def remove_connected_vendor(vendor_id: int):
    await redis_client.srem(KEY_CONNECTED_VENDORS, vendor_id)

async def is_vendor_connected(vendor_id: int) -> bool:
    return await redis_client.sismember(KEY_CONNECTED_VENDORS, vendor_id)

async def set_vendor_busy(vendor_id: int, busy: bool):
    if busy:
        await redis_client.sadd(KEY_BUSY_VENDORS, vendor_id)
    else:
        await redis_client.srem(KEY_BUSY_VENDORS, vendor_id)

async def is_vendor_busy(vendor_id: int) -> bool:
    return await redis_client.sismember(KEY_BUSY_VENDORS, vendor_id)

async def enqueue_call_request(customer_id: int, vendor_id: int, product_id: int, room_name: str):
    data = json.dumps({
        "customer_id": customer_id,
        "vendor_id": vendor_id,
        "product_id": product_id,
        "room_name": room_name,
        "joined_at": datetime.now(timezone.utc).isoformat(),
        "request_id": str(uuid.uuid4())
    })
    await redis_client.lpush(KEY_CALL_QUEUE.format(vendor_id=vendor_id), data)

async def dequeue_call_request(vendor_id: int) -> Optional[Dict]:
    data = await redis_client.rpop(KEY_CALL_QUEUE.format(vendor_id=vendor_id))
    if not data:
        return None
    return json.loads(data)

async def peek_call_queue(vendor_id: int) -> List[Dict]:
    items = await redis_client.lrange(KEY_CALL_QUEUE.format(vendor_id=vendor_id), 0, -1)
    return [json.loads(item) for item in items]

async def set_pending_request(vendor_id: int, data: Dict):
    await redis_client.set(KEY_PENDING_REQUESTS.format(vendor_id=vendor_id), json.dumps(data))

async def get_pending_request(vendor_id: int) -> Optional[Dict]:
    data = await redis_client.get(KEY_PENDING_REQUESTS.format(vendor_id=vendor_id))
    return json.loads(data) if data else None

async def clear_pending_request(vendor_id: int):
    await redis_client.delete(KEY_PENDING_REQUESTS.format(vendor_id=vendor_id))

async def set_active_call(room_name: str, data: Dict):
    pipe = redis_client.pipeline()
    pipe.hset(KEY_ACTIVE_CALLS.format(room_name=room_name), mapping=data)
    pipe.expire(KEY_ACTIVE_CALLS.format(room_name=room_name), MAX_CALL_DURATION + 60)
    await pipe.execute()

async def get_active_call(room_name: str) -> Optional[Dict]:
    data = await redis_client.hgetall(KEY_ACTIVE_CALLS.format(room_name=room_name))
    return data if data else None

async def remove_active_call(room_name: str):
    await redis_client.delete(KEY_ACTIVE_CALLS.format(room_name=room_name))

async def set_request_id(customer_id: int, request_id: str):
    await redis_client.set(KEY_PENDING_REQUEST_ID.format(customer_id=customer_id), request_id)

async def get_request_id(customer_id: int) -> Optional[str]:
    return await redis_client.get(KEY_PENDING_REQUEST_ID.format(customer_id=customer_id))

async def publish_vendor_update(vendor_id: int, event: str, data: dict):
    channel = CHANNEL_VENDOR_NOTIFY.format(vendor_id=vendor_id)
    await redis_client.publish(channel, json.dumps({"event": event, **data}))

async def publish_customer_update(customer_id: int, event: str, data: dict):
    channel = CHANNEL_CUSTOMER_NOTIFY.format(customer_id=customer_id)
    await redis_client.publish(channel, json.dumps({"event": event, **data}))