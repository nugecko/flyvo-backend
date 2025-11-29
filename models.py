from datetime import datetime

from sqlalchemy import Column, Integer, String, Boolean, DateTime
from db import Base


class AdminConfig(Base):
    __tablename__ = "admin_config"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(100), unique=True, nullable=False)
    value = Column(String(255), nullable=True)
    description = Column(String(255), nullable=True)


class AppUser(Base):
    __tablename__ = "app_users"

    id = Column(Integer, primary_key=True, index=True)

    # id of the user in Base44
    external_id = Column(String(100), unique=True, index=True, nullable=False)

    # contact details
    email = Column(String(255), index=True, nullable=False)
    first_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)
    country = Column(String(100), nullable=True)

    # consent and source
    marketing_consent = Column(Boolean, default=None)
    source = Column(String(50), nullable=True)  # for example "base44"

    # housekeeping
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
