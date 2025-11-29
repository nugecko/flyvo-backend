from sqlalchemy import Column, String, Integer, Text, DateTime
from sqlalchemy.dialects.postgresql import UUID
import uuid
from datetime import datetime

from .db import Base

class AdminConfig(Base):
    __tablename__ = "admin_config"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String, unique=True, index=True, nullable=False)
    value = Column(String, nullable=False)
    description = Column(Text)
    updated_at = Column(DateTime, default=datetime.utcnow)

class AppUser(Base):
    __tablename__ = "app_users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String, unique=True, index=True, nullable=False)
    status = Column(String, default="active")
    role = Column(String, default="free")
    plan = Column(String)
    credits = Column(Integer, default=0)
    date_created = Column(DateTime, default=datetime.utcnow)
    last_login_at = Column(DateTime)
    notes = Column(Text)
