# from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, DateTime, func
# from sqlalchemy.orm import relationship
# from app.config.database import Base  # shared Base

# class StreamSession(Base):
#     __tablename__ = "streams"

#     id = Column(Integer, primary_key=True, index=True)
#     user_id = Column(Integer, ForeignKey("users.user_id"))
#     room_id = Column(String, unique=True, index=True)
#     room_name = Column(String, unique=True, nullable=False)
#     stream_is_live = Column(Boolean, default=False, nullable=False)
#     created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

#     users = relationship("User", back_populates="stream_sessions")

# class User(Base):
#     __tablename__ = "users"

#     user_id = Column(Integer, primary_key=True, index=True)
#     user_name = Column(String, nullable=False)
#     phone_number = Column(String, unique=True, nullable=False)
#     role_id = Column(Integer, nullable=False)
#     created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

#     stream_sessions = relationship("StreamSession", back_populates="users")


# models.py
# models.py
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, func, Boolean
from sqlalchemy.orm import relationship
from app.config.database import Base

class User(Base):
    __tablename__ = "users"

    user_id = Column(Integer, primary_key=True, index=True)
    user_name = Column(String, nullable=False)
    phone_number = Column(String, unique=True, nullable=False)
    role_id = Column(Integer, nullable=False)  # 0=vendor, 1=customer
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    stream_sessions = relationship("StreamSession", back_populates="user")
    call_histories_as_vendor = relationship("CallHistory", foreign_keys="[CallHistory.vendor_id]", back_populates="vendor")
    call_histories_as_customer = relationship("CallHistory", foreign_keys="[CallHistory.customer_id]", back_populates="customer")
    request_histories_as_customer = relationship("CallRequestHistory", foreign_keys="[CallRequestHistory.customer_id]", back_populates="customer")
    request_histories_as_vendor = relationship("CallRequestHistory", foreign_keys="[CallRequestHistory.vendor_id]", back_populates="vendor")


class StreamSession(Base):
    __tablename__ = "stream_sessions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.user_id"))
    room_id = Column(String, unique=True, index=True)
    room_name = Column(String, unique=True, nullable=False)
    stream_is_live = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User", back_populates="stream_sessions")


class CallHistory(Base):
    __tablename__ = "call_history"

    id = Column(Integer, primary_key=True, index=True)
    vendor_id = Column(Integer, ForeignKey("users.user_id"), nullable=False)
    customer_id = Column(Integer, ForeignKey("users.user_id"), nullable=False)
    product_id = Column(Integer, nullable=False)
    room_name = Column(String, nullable=False)

    started_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True))
    duration_seconds = Column(Integer, default=0)
    status = Column(String, default="completed")  # completed, canceled
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    vendor = relationship("User", foreign_keys=[vendor_id], back_populates="call_histories_as_vendor")
    customer = relationship("User", foreign_keys=[customer_id], back_populates="call_histories_as_customer")


class CallRequestHistory(Base):
    __tablename__ = "call_request_history"

    id = Column(Integer, primary_key=True, index=True)
    vendor_id = Column(Integer, ForeignKey("users.user_id"), nullable=False)
    customer_id = Column(Integer, ForeignKey("users.user_id"), nullable=False)
    product_id = Column(Integer, nullable=False)

    requested_at = Column(DateTime(timezone=True), nullable=False)
    accepted_at = Column(DateTime(timezone=True))  # NULL if not accepted
    rejected_at = Column(DateTime(timezone=True))  # NULL if accepted
    canceled_at = Column(DateTime(timezone=True))  # NULL if not canceled

    # Calculated fields
    wait_duration_seconds = Column(Integer, default=0)  # time from request to accept/reject
    status = Column(String, nullable=False)  # 'accepted', 'rejected', 'canceled', 'missed'

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    vendor = relationship("User", foreign_keys=[vendor_id], back_populates="request_histories_as_vendor")
    customer = relationship("User", foreign_keys=[customer_id], back_populates="request_histories_as_customer")