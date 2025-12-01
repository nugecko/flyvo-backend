from datetime import datetime
from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    Date,
    Numeric,
    ForeignKey,
    Text
)
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

    external_id = Column(String(100), unique=True, index=True, nullable=False)

    email = Column(String(255), index=True, nullable=False)
    first_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)
    country = Column(String(100), nullable=True)

    marketing_consent = Column(Boolean, default=None)
    source = Column(String(50), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(String, primary_key=True, index=True)
    user_email = Column(String, index=True, nullable=False)

    origin = Column(String, nullable=False)
    destination = Column(String, nullable=False)
    cabin = Column(String, nullable=False)

    departure_start = Column(Date, nullable=False)
    departure_end = Column(Date, nullable=False)
    return_start = Column(Date, nullable=True)
    return_end = Column(Date, nullable=True)

    alert_type = Column(String, nullable=False)
    max_price = Column(Integer, nullable=True)

    last_price = Column(Integer, nullable=True)
    last_run_at = Column(DateTime, nullable=True)
    times_sent = Column(Integer, nullable=False, default=0)

    is_active = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class AlertRun(Base):
    __tablename__ = "alert_runs"

    id = Column(String, primary_key=True, index=True)
    alert_id = Column(String, ForeignKey("alerts.id"), nullable=False, index=True)

    run_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    price_found = Column(Integer, nullable=True)
    sent = Column(Boolean, nullable=False, default=False)
    reason = Column(Text, nullable=True)
