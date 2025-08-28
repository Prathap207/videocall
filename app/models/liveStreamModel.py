from typing import Optional
from pydantic import BaseModel

class StreamCreateRequest(BaseModel):
    user_id: int
    title: str
    
class StreamIdResponse(BaseModel):
    user_id: int
    room_id: str
    room_name: str
    stream_is_live: bool
    

    class Config:
        from_attributes = True

class CreateRoomIdResponse(BaseModel):
    status: bool
    message: str
    data: StreamIdResponse

    class Config:
        from_attributes = True

class TokenRequest(BaseModel):
    username:str
    role: str
    identity: str
    room: str

class RoomResponse(BaseModel):
    name: str
    sid: str
    creation_time: str
    empty_timeout: Optional[int] = None
    max_participants: Optional[int] = None
    metadata: Optional[str] = None
    num_participants: Optional[int] = None
    num_publishers: Optional[int] = None
    active_recording: Optional[bool] = None

class EndLiveResponse(BaseModel):
    status: bool
    message: str
    
class EndLiveRequest(BaseModel):
    user_id:int
    room_id:str
    room_name: str

class ConnectLiveRequest(BaseModel):
    live_url: str
    token: str

class ConnectLiveResponse(BaseModel):
    status: bool
    message: str

class UpdateParticipantPermissionRequest(BaseModel):
    room_name: str
    user_id: int
    role: str

class GetAllParticipantsRequest(BaseModel):
    room_name: str