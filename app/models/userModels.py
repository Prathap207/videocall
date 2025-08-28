from pydantic import BaseModel
from typing import Optional, List, Dict
from datetime import datetime


class UserCreate(BaseModel):
    user_name: str
    phone_number: str
    role_id: int


class UserLogin(BaseModel):
    phone_number: str


# class   UserResponseData(BaseModel):
#     user_id: int
#     user_name: str
#     phone_number: str

#     class Config:
#         from_attributes = True  # or orm_mode = True if using Pydantic v1


class UserResponse(BaseModel):
    status: bool
    message: str
    # data: UserResponseData  # ✅ Added this to fix ValidationError

    class Config:
        from_attributes = True


class LoginResponse(BaseModel):
    status: bool
    message: str
    data: Optional[Dict]  # Consider making this a proper model if you want stricter typing


class GetUserRequest(BaseModel):
    user_id: Optional[int] = None

class StreamSessionResponseModel(BaseModel):
    id: Optional[int] = None
    room_id: Optional[str] = None
    room_name: Optional[str] = None
    stream_is_live: Optional[bool] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class UserResponseModel(BaseModel):
    user_id: int
    user_name: str
    phone_number: str
    role_id: int
    stream_sessions: List[StreamSessionResponseModel] = []

    class Config:
        from_attributes = True

class CommonResponseModel(BaseModel):
    status: bool
    message: str
    data: List[UserResponseModel]


# class CommonResponseModel(BaseModel):
#     status: bool
#     message: str
#     data: List[UserResponseData]

class UserErrorResponse(BaseModel):
    status: bool
    message: str

    class Config:
        from_attributes = True  # or orm_mode = True if using Pydantic v1