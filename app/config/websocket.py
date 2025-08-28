from fastapi import WebSocket, WebSocketDisconnect

clients = {}

async def websocket_endpoint(websocket: WebSocket, room_id: str):
    await websocket.accept()
    if room_id not in clients:
        clients[room_id] = []
    clients[room_id].append(websocket)

    try:
        while True:
            data = await websocket.receive_text()
            for client in clients[room_id]:
                if client != websocket:
                    await client.send_text(data)
    except WebSocketDisconnect:
        clients[room_id].remove(websocket)
