# from av import VideoFrame
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from app.config import database
from app.config.livekit import create_access_token, create_room, end_stream_in_livekit, list_of_particitions, update_participants_permission
from app.schemas import databaseSchemas
from app.models import liveStreamModel


router = APIRouter()


@router.post("/live-streaming/create_room",response_model=liveStreamModel.CreateRoomIdResponse)
async def create_room_api(request: liveStreamModel.StreamCreateRequest, db: Session = Depends(database.get_db)):
    """Creates a new unique room ID and returns it."""
    # try:
        # Check if user already exists
    existing_user = db.query(databaseSchemas.StreamSession).filter(
        databaseSchemas.StreamSession.user_id == request.user_id
    ).all()
    # print(f"Existing User : {existing_user.stream_is_live}")
    for existing_live in existing_user:
        if existing_live is not None and existing_live.stream_is_live:
            raise HTTPException(status_code=400, detail="User is already in live!")
    # Call the LiveKit or custom room creation service
    session = await create_room(request.user_id, request.title)
    print(f"Room created with ID: {session} for user {request.user_id}")

    if session is None:
        print(f"Failed to create room for user {request.user_id}")
        raise HTTPException(
            status_code=400,
            detail="Failed to create room. User not found or other error."
        )

    # Create DB entry for the stream session
    session_data = databaseSchemas.StreamSession(
        user_id=request.user_id,
        room_id=session.sid,
        room_name=session.name,
        
        stream_is_live=True
    )
    print(f"Session created: {session_data}")

    db.add(session_data)
    db.commit()
    db.refresh(session_data)

    return liveStreamModel.CreateRoomIdResponse(
        status=True,
        message="Room created successfully",
        data=liveStreamModel.StreamIdResponse(
            user_id=request.user_id,
            room_id=session.sid,
            room_name=session.name,
            stream_is_live=True
        )
    )

    # except SQLAlchemyError as db_error:
    #     db.rollback()
    #     print(f"Database Error: {str(db_error)}")
    #     raise HTTPException(
    #         status_code=500,
    #         detail="Database error occurred while creating room session."
    #     )

    # except Exception as e:
    #     print(f"Unexpected Error: {str(e)}")
    #     raise HTTPException(
    #         status_code=500,
    #         detail="An unexpected error occurred while creating the room."
    #     )
    
@router.post("/get-token")
def get_token(request: liveStreamModel.TokenRequest,db: Session = Depends(database.get_db)):
    try:
        # Basic validation (optional if Pydantic model already enforces this)
        if not request.username or not request.role or not request.room:
            raise HTTPException(status_code=400, detail="Missing required fields")

        # Create token using your token utility
        token = create_access_token(request.username, request.role,request.identity, request.room)
        print(f"Token : {token}")

        return {"token": token}

    except SQLAlchemyError as db_error:
        print(f"Database Error during token generation: {str(db_error)}")
        raise HTTPException(status_code=500, detail="Database error during token generation")

    except Exception as e:
        print(f"Unexpected Error in get_token: {str(e)}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred while generating the token")
    

    
@router.post("/end-live")
async def end_live_stream(request:liveStreamModel.EndLiveRequest,db: Session = Depends(database.get_db)):
    """End Live and Disconnect all the participants"""
    end_live = end_stream_in_livekit(request.room_id,request.room_name)
    print(f"End Live : {end_live}")
    if end_live:
        stream = db.query(databaseSchemas.StreamSession).filter_by(room_id=request.room_id, user_id=request.user_id).first()
        if stream:
            stream.stream_is_live = False
            db.commit()
        db.close()
        return liveStreamModel.EndLiveResponse(
        status=True,
        message="Live ended successfully!"

        )
    else:
        return HTTPException(status_code=400, detail="Something went wrong!")



# Participants Sections
@router.post("/participants/permission-update-participants")
async def permission_update(request: liveStreamModel.UpdateParticipantPermissionRequest):
    permission = await update_participants_permission(request.room_name ,request.role)
    if permission:
        return {
            "status" : True,
            "message" : "Participant's Permission Updated Successfully!"
        }
    else:
        return {
            "status" : False,
            "message" : "Participant's Permission Updated Faild!"
        }
@router.get("/participants/all-participants")
async def get_all_participants(room_name: str,db: Session = Depends(database.get_db)):
    all_participants = await list_of_particitions(room_name)
    print(f"All Particitions : {all_participants}")
    return all_participants
