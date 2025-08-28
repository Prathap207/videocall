from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session  
from app.config import database
from app.schemas import databaseSchemas
from app.models import userModels
from sqlalchemy.exc import SQLAlchemyError


# Get all users or specific user by user_id (POST due to body)
from sqlalchemy.orm import joinedload
from sqlalchemy import text


router  = APIRouter()

# Dependency for DB session
def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post("/create-users", response_model=userModels.UserResponse)
def createUser(user: userModels.UserCreate, db: Session = Depends(get_db)):
    print(f"User : {user}")
    try:
        print(f"User : {user}")
        
        # Check if user already exists
        existing_user = db.query(databaseSchemas.User).filter(
            databaseSchemas.User.phone_number == user.phone_number
        ).first()

        if existing_user:
            return userModels.UserErrorResponse(
                status=False,
                message="Phone number already registered"
            )

        # Create new user
        new_user = databaseSchemas.User(
            user_name=user.user_name,
            phone_number=user.phone_number,
            role_id=user.role_id
        )
        db.add(new_user)
        db.commit()
        db.refresh(new_user)

        return userModels.UserResponse(
            status=True,
            message="User created successfully!"
        )
    
    except SQLAlchemyError as e:
        db.rollback()
        print(f"Database Error: {str(e)}")
        return userModels.UserErrorResponse(
            status=False,
            message="Database error occurred. Please try again later."
        )
    
    except Exception as e:
        print(f"Unexpected Error: {str(e)}")
        return userModels.UserErrorResponse(
            status=False,
            message="An unexpected error occurred. Please contact support."
        )

# User login
@router.post("/user-login", response_model=userModels.LoginResponse)
def userLogin(user_details: userModels.UserLogin, db: Session = Depends(get_db)):
    print(f"User Login : {user_details}")
    try:
        user = db.query(databaseSchemas.User).filter(
            databaseSchemas.User.phone_number == user_details.phone_number
        ).first()

        if user:
            return userModels.LoginResponse(
                status=True,
                message="Login successful",
                data={
                    "user_id": user.user_id,
                    "user_name": user.user_name,
                    "role_id": user.role_id
                }
            )
        else:
            raise HTTPException(status_code=404, detail="User not found")

    except SQLAlchemyError as e:
        print(f"Database Error: {str(e)}")
        raise HTTPException(status_code=500, detail="Database error occurred. Please try again later.")

    except Exception as e:
        print(f"Unexpected Error: {str(e)}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")


@router.get("/get-users", response_model=userModels.CommonResponseModel)
def getAllUsers(user_id: Optional[int] = Query(default=None), db: Session = Depends(get_db)):
    try:
        if user_id:
            # Fetch single user with stream_sessions eagerly loaded
            user = db.query(databaseSchemas.User).options(
                joinedload(databaseSchemas.User.stream_sessions)
            ).filter(databaseSchemas.User.user_id == user_id).first()

            if not user:
                raise HTTPException(status_code=404, detail="User not found!")

            # Get latest stream session for the user
            latest_stream = (
                db.query(databaseSchemas.StreamSession)
                .filter(databaseSchemas.StreamSession.user_id == user_id)
                .order_by(databaseSchemas.StreamSession.id.desc())
                .first()
            )

            user_dict = user.__dict__.copy()
            user_dict["stream_sessions"] = [latest_stream] if latest_stream else []
            user_dict.pop("_sa_instance_state", None)
            print(f"User Dict Data : {user_dict}")

            return userModels.CommonResponseModel(
                status=True,
                message="User fetched successfully!",
                data=[user_dict]
            )

        else:
            # Get all users
            users = db.query(databaseSchemas.User).all()

            # Raw SQL to get latest stream per user
            sql = text("""
                SELECT DISTINCT ON (s.user_id) s.*
                FROM public.streams AS s
                ORDER BY s.user_id, s.id DESC
            """)
            stream_results = db.execute(sql).mappings().all()

            # Map user_id to latest stream session
            latest_stream_map = {stream["user_id"]: stream for stream in stream_results}
            print(f"Latest Stream : {latest_stream_map}")

            # Build response
            response_data = []
            for user in users:
                user_entry = {
                    "user_id": user.user_id,
                    "user_name": user.user_name,
                    "phone_number": user.phone_number,
                    "role_id": user.role_id,
                    "stream_sessions": [latest_stream_map[user.user_id]] if user.user_id in latest_stream_map else []
                }
                response_data.append(user_entry)
            print(f"User Data : {response_data}")
            return userModels.CommonResponseModel(
                status=True,
                message="Users fetched successfully!",
                data=response_data
            )

    except SQLAlchemyError as e:
        db.rollback()
        print(f"Database Error: {str(e)}")
        raise HTTPException(status_code=500, detail="Database error occurred. Please try again later.")

    except Exception as e:
        print(f"Unexpected Error: {str(e)}")
        raise HTTPException(status_code=500, detail="An unexpected error occurred.")