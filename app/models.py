from pydantic import BaseModel, EmailStr

class UserRegister(BaseModel):
    name: str
    email: EmailStr
    password: str
    upi_pin: str

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class TransferMoney(BaseModel):
    receiver_upi: str
    amount: float
    upi_pin: str

class MoneyRequest(BaseModel):
    receiver_upi: str
    amount: float