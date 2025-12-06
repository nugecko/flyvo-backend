from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from db import SessionLocal
from models import EarlyAccessSubscriber
from alerts_email import send_early_access_welcome_email

router = APIRouter()


class EarlyAccessInput(BaseModel):
    email: EmailStr


@router.post("/early-access")
def early_access_signup(payload: EarlyAccessInput):
    db: Session = SessionLocal()

    try:
        # Check if email already exists
        existing = (
            db.query(EarlyAccessSubscriber)
            .filter(EarlyAccessSubscriber.email == payload.email)
            .first()
        )
        if existing:
            return {"message": "Already subscribed"}

        # Create new subscriber
        subscriber = EarlyAccessSubscriber(email=payload.email)
        db.add(subscriber)
        db.commit()

        # We will add the email sending logic here in the next step
        # for example: send_early_access_welcome_email(payload.email)

        return {"message": "Success"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        db.close()
