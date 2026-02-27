from fastapi import APIRouter, HTTPException, Depends
from app.database import users_collection, transactions_collection, requests_collection
from app.models import UserRegister, UserLogin, TransferMoney, MoneyRequest
from app.auth import hash_password, verify_password, create_access_token, get_current_user
from bson import ObjectId
from datetime import datetime
import qrcode
import base64
from io import BytesIO

MAX_TRANSACTION_LIMIT = 20000
DAILY_TRANSACTION_LIMIT = 100000

router = APIRouter()


# ---------------- REGISTER ---------------- #

@router.post("/register")
def register(user: UserRegister):

    if users_collection.find_one({"email": user.email}):
        raise HTTPException(status_code=400, detail="User already exists")

    upi_id = user.email.split("@")[0] + "@paywave"

    users_collection.insert_one({
        "name": user.name,
        "email": user.email,
        "password": hash_password(user.password),
        "balance": 1000,
        "upi_id": upi_id,
        "upi_pin": hash_password(user.upi_pin)
    })

    return {
        "message": "User registered successfully",
        "upi_id": upi_id
    }


# ---------------- LOGIN ---------------- #

@router.post("/login")
def login(user: UserLogin):

    db_user = users_collection.find_one({"email": user.email})

    if not db_user or not verify_password(user.password, db_user["password"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token({"user_id": str(db_user["_id"])})

    return {"access_token": token}


# ---------------- PROFILE ---------------- #

@router.get("/me")
def get_profile(current_user: dict = Depends(get_current_user)):

    return {
        "name": current_user["name"],
        "email": current_user["email"],
        "upi_id": current_user["upi_id"],
        "balance": current_user["balance"]
    }


# ---------------- TRANSFER MONEY ---------------- #

@router.post("/transfer")
def transfer_money(
    data: TransferMoney,
    current_user: dict = Depends(get_current_user)
):

    # Prevent self transfer
    if data.receiver_upi == current_user["upi_id"]:
        raise HTTPException(status_code=400, detail="Cannot transfer to yourself")

    receiver = users_collection.find_one({"upi_id": data.receiver_upi})

    if not receiver:
        raise HTTPException(status_code=404, detail="Receiver not found")

    # Verify UPI PIN
    if not verify_password(data.upi_pin, current_user["upi_pin"]):
        raise HTTPException(status_code=401, detail="Invalid UPI PIN")

    # Per transaction limit
    if data.amount > MAX_TRANSACTION_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum per transaction limit is ₹{MAX_TRANSACTION_LIMIT}"
        )

    # Daily limit calculation
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    today_transactions = transactions_collection.find({
        "sender_upi": current_user["upi_id"],
        "type": "debit",
        "timestamp": {"$gte": today_start}
    })

    today_total = sum(tx["amount"] for tx in today_transactions)

    if today_total + data.amount > DAILY_TRANSACTION_LIMIT:
        raise HTTPException(
            status_code=400,
            detail="Daily transaction limit exceeded (₹100000)"
        )

    # Balance check
    if current_user["balance"] < data.amount:
        raise HTTPException(status_code=400, detail="Insufficient balance")

    # Deduct sender
    users_collection.update_one(
        {"_id": current_user["_id"]},
        {"$inc": {"balance": -data.amount}}
    )

    # Credit receiver
    users_collection.update_one(
        {"_id": receiver["_id"]},
        {"$inc": {"balance": data.amount}}
    )

    # Store sender debit entry
    transactions_collection.insert_one({
        "sender_upi": current_user["upi_id"],
        "receiver_upi": receiver["upi_id"],
        "amount": data.amount,
        "type": "debit",
        "status": "success",
        "timestamp": datetime.utcnow()
    })

    # Store receiver credit entry
    transactions_collection.insert_one({
        "sender_upi": current_user["upi_id"],
        "receiver_upi": receiver["upi_id"],
        "amount": data.amount,
        "type": "credit",
        "status": "success",
        "timestamp": datetime.utcnow()
    })

    return {"message": "Transaction successful"}


# ---------------- TRANSACTIONS ---------------- #

@router.get("/transactions")
def get_transactions(current_user: dict = Depends(get_current_user)):

    transactions = list(transactions_collection.find({
        "$or": [
            {"sender_upi": current_user["upi_id"]},
            {"receiver_upi": current_user["upi_id"]}
        ]
    }).sort("timestamp", -1))

    for tx in transactions:
        tx["_id"] = str(tx["_id"])

    return transactions


# ---------------- REQUEST MONEY ---------------- #

@router.post("/request-money")
def request_money(
    data: MoneyRequest,
    current_user: dict = Depends(get_current_user)
):

    receiver = users_collection.find_one({"upi_id": data.receiver_upi})

    if not receiver:
        raise HTTPException(status_code=404, detail="Receiver not found")

    requests_collection.insert_one({
        "sender_upi": current_user["upi_id"],
        "receiver_upi": receiver["upi_id"],
        "amount": data.amount,
        "status": "pending",
        "timestamp": datetime.utcnow()
    })

    return {"message": "Money request sent"}


@router.get("/requests")
def get_requests(current_user: dict = Depends(get_current_user)):

    requests = list(requests_collection.find({
        "receiver_upi": current_user["upi_id"],
        "status": "pending"
    }))

    for req in requests:
        req["_id"] = str(req["_id"])

    return requests


@router.post("/requests/{request_id}/accept")
def accept_request(
    request_id: str,
    current_user: dict = Depends(get_current_user)
):

    request = requests_collection.find_one({"_id": ObjectId(request_id)})

    if not request:
        raise HTTPException(status_code=404, detail="Request not found")

    if request["status"] != "pending":
        raise HTTPException(status_code=400, detail="Request already processed")

    if current_user["balance"] < request["amount"]:
        raise HTTPException(status_code=400, detail="Insufficient balance")

    # Deduct current user (who is paying)
    users_collection.update_one(
        {"upi_id": current_user["upi_id"]},
        {"$inc": {"balance": -request["amount"]}}
    )

    # Credit requester
    users_collection.update_one(
        {"upi_id": request["sender_upi"]},
        {"$inc": {"balance": request["amount"]}}
    )

    requests_collection.update_one(
        {"_id": ObjectId(request_id)},
        {"$set": {"status": "accepted"}}
    )

    return {"message": "Request accepted and money transferred"}


# ---------------- QR GENERATION ---------------- #

@router.get("/generate-qr")
def generate_qr(current_user: dict = Depends(get_current_user)):

    upi_string = f"upi://pay?pa={current_user['upi_id']}&pn={current_user['name']}&cu=INR"

    qr = qrcode.make(upi_string)

    buffer = BytesIO()
    qr.save(buffer, format="PNG")

    img_str = base64.b64encode(buffer.getvalue()).decode()

    return {
        "upi_id": current_user["upi_id"],
        "qr_code_base64": img_str
    }