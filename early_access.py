from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from db import SessionLocal
from models import EarlyAccessSubscriber

import os
import smtplib
from email.mime.text import MIMEText

router = APIRouter()


class EarlyAccessInput(BaseModel):
    email: EmailStr

def send_early_access_welcome_email(to_email: str) -> None:
    print(f"[early_access] Preparing welcome email for {to_email}")

    host = os.getenv("EMAIL_HOST", "mail-eu.smtp2go.com")
    port_raw = os.getenv("EMAIL_PORT", "587")
    user = os.getenv("EMAIL_USER")
    password = os.getenv("EMAIL_PASSWORD")
    from_email = os.getenv("EMAIL_FROM", user) or user

    print(f"[early_access] SMTP config host={host} port={port_raw} user={user}")

    try:
        port = int(port_raw)
    except Exception as e:
        print(f"[early_access] Invalid EMAIL_PORT value '{port_raw}': {e}")
        return

    if not user or not password:
        print("[early_access] EMAIL_USER or EMAIL_PASSWORD not set, skipping welcome email")
        return

    subject = "Welcome to Flyyv early access"
    body = (
        "Hi,\n\n"
        "Thank you for joining the Flyyv early access list.\n"
        "You will be among the first to try our smart premium flight alerts.\n\n"
        "Talk soon,\n"
        "The Flyyv team"
    )

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_email

    try:
        print("[early_access] Before SMTP connect")
        with smtplib.SMTP(host, port, timeout=10) as server:
            print("[early_access] Connected to SMTP server")
            server.starttls()
            print("[early_access] STARTTLS done")
            server.login(user, password)
            print("[early_access] Logged in to SMTP")
            server.send_message(msg)
            print(f"[early_access] Welcome email sent to {to_email}")
    except Exception as e:
        # Do not break signup if email fails
        print(f"[early_access] Failed to send welcome email to {to_email}: {e}")

@router.post("/early-access")
def early_access_signup(payload: EarlyAccessInput):
    print(f"[early_access] Signup request received for {payload.email}")
    db: Session = SessionLocal()

    try:
        # Check if email already exists
        existing = (
            db.query(EarlyAccessSubscriber)
            .filter(EarlyAccessSubscriber.email == payload.email)
            .first()
        )
        if existing:
            print(f"[early_access] {payload.email} already subscribed")
            return {"message": "Already subscribed"}

        # Create new subscriber
        subscriber = EarlyAccessSubscriber(email=payload.email)
        db.add(subscriber)
        db.commit()
        print(f"[early_access] New subscriber saved: {payload.email}")

        # Send welcome email, do not block on failure
        send_early_access_welcome_email(payload.email)

        return {"message": "Success"}
    except Exception as e:
        db.rollback()
        print(f"[early_access] Error during signup for {payload.email}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        db.close()
