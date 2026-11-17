from pydantic import BaseModel, EmailStr, Field


class SignInRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1)


class SignInResponse(BaseModel):
    shopper_id: str
    token: str


class ShopperResponse(BaseModel):
    shopper_id: str
    email: str
