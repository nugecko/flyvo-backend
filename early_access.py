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
    logger.info(f"[early_access] Preparing welcome email for {to_email}")

    host = os.getenv("EMAIL_HOST", "mail-eu.smtp2go.com")
    port = int(os.getenv("EMAIL_PORT", "587"))
    user = os.getenv("EMAIL_USER")
    password = os.getenv("EMAIL_PASSWORD")
    from_email = os.getenv("EMAIL_FROM", user) or user

    if not user or not password:
        logger.error("[early_access] EMAIL_USER or EMAIL_PASSWORD not set, skipping welcome email")
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
        with smtplib.SMTP(host, port) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(msg)
        logger.info(f"[early_access] Welcome email sent to {to_email}")
    except Exception as e:
        # Do not break signup if email fails
        logger.error(f"[early_access] Failed to send welcome email to {to_email}: {e}")

@router.post("/early-access")
def early_access_signup(payload: EarlyAccessInput):
    logger.info(f"[early_access] Signup request received for {payload.email}")
    db: Session = SessionLocal()

    try:
        # Check if email already exists
        existing = (
            db.query(EarlyAccessSubscriber)
            .filter(EarlyAccessSubscriber.email == payload.email)
            .first()
        )
        if existing:
            logger.info(f"[early_access] {payload.email} already subscribed")
            # Optional: still send email if you want to remind them
            return {"message": "Already subscribed"}

        # Create new subscriber
        subscriber = EarlyAccessSubscriber(email=payload.email)
        db.add(subscriber)
        db.commit()
        logger.info(f"[early_access] New subscriber saved: {payload.email}")

        # Send welcome email, do not block on failure
        send_early_access_welcome_email(payload.email)

        return {"message": "Success"}
    except Exception:
        db.rollback()
        logger.exception("[early_access] Error during signup")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        db.close()
