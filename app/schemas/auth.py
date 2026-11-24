"""Authentication request schemas."""

from datetime import date

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.config import settings


class RegistrationRequest(BaseModel):
    """Public self-registration payload; public accounts are always customers."""

    full_name: str = Field(..., min_length=2, max_length=255)
    email: EmailStr
    password: str
    dob: date
    phone: str = Field(..., min_length=7, max_length=32)
    address: str = Field(..., min_length=5, max_length=1000)
    state_region: str = Field(..., min_length=2, max_length=128)

    @field_validator("password")
    @classmethod
    def validate_password_length(cls, value: str) -> str:
        if len(value) < settings.PASSWORD_MIN_LENGTH:
            raise ValueError(
                f"Password must be at least {settings.PASSWORD_MIN_LENGTH} characters"
            )
        return value

    @field_validator("dob")
    @classmethod
    def validate_date_of_birth(cls, value: date) -> date:
        if value >= date.today():
            raise ValueError("Date of birth must be in the past")
        return value

    @field_validator("full_name", "phone", "address", "state_region")
    @classmethod
    def strip_text_fields(cls, value: str) -> str:
        return value.strip()
