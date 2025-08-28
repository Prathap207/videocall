from livekit import api
from app.config import config
from livekit.api import LiveKitAPI, CreateRoomRequest, DeleteRoomRequest, UpdateParticipantRequest, ParticipantPermission, ListParticipantsRequest

# Will read LIVEKIT_URL, LIVEKIT_API_KEY, and LIVEKIT_API_SECRET from environment variables

  # ... use your client with `lkapi.room` ...

async def create_room(user_id:int, title:str):
    print(f"Live url : {config.LIVEKIT_URL}")
    print(f"Live api : {config.LIVEKIT_API_KEY}")
    async with LiveKitAPI(config.LIVEKIT_URL,config.LIVEKIT_API_KEY,
        config.LIVEKIT_API_SECRET) as lkapi:

        room = await lkapi.room.create_room(CreateRoomRequest(
            name=title,
            # empty_timeout=10 * 60,    
            # max_participants=20,
        ))
        return room

async def end_stream_in_livekit(room_id:str, room_name: str):
    async with LiveKitAPI(config.LIVEKIT_URL,config.LIVEKIT_API_KEY,
        config.LIVEKIT_API_SECRET) as lkapi:
        end_live = await lkapi.room.delete_room(DeleteRoomRequest(room=room_id))

async def list_of_particitions(room_name: str):
    async with LiveKitAPI(config.LIVEKIT_URL,config.LIVEKIT_API_KEY,
        config.LIVEKIT_API_SECRET) as lkapi:
        list_participants = await lkapi.room.list_participants(ListParticipantsRequest(
  room=room_name
))
        print(f"List Of Participants : {list_participants.participants}")
        return list_participants.participants

def create_access_token(username:str,role: str,identity: str, room: str) -> str:
    if role == "viewer":
        token = api.AccessToken(config.LIVEKIT_API_KEY,
            config.LIVEKIT_API_SECRET,) \
        .with_identity(identity) \
        .with_name(username) \
        .with_grants(api.VideoGrants(
            can_publish=False,
            room_join=True,
            room=room,
        ))
        return token.to_jwt()
    else:
        token = api.AccessToken(config.LIVEKIT_API_KEY,
            config.LIVEKIT_API_SECRET,) \
        .with_identity(identity) \
        .with_name(username) \
        .with_grants(api.VideoGrants(
            room_join=True,
            room=room,
        ))
        return token.to_jwt()

async def update_participants_permission(room_name: str, role:str):
    async with LiveKitAPI(config.LIVEKIT_URL,config.LIVEKIT_API_KEY,
        config.LIVEKIT_API_SECRET) as lkapi:
        await lkapi.room.update_participant(UpdateParticipantRequest(
        room=room_name,
        identity=role,
        permission=ParticipantPermission(
            can_subscribe=True,
            can_publish=False,
            can_publish_data=True,
        )))
