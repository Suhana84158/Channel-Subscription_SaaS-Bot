from datetime import datetime, timedelta, timezone
from uuid import uuid4
from pymongo import ReturnDocument
from database.mongo import get_database

SETTINGS="seller_settings"; PLANS="seller_plans"; CHANNELS="seller_channels"; USERS="seller_users"
PAYMENTS="seller_payments"; SUBS="seller_subscriptions"; REFERRALS="seller_referrals"


def c(name): return get_database()[name]


async def initialize_seller_data_indexes():
    await c(SETTINGS).create_index("owner_id", unique=True)
    await c(PLANS).create_index([("owner_id",1),("plan_id",1)], unique=True)
    await c(CHANNELS).create_index([("owner_id",1),("chat_id",1)], unique=True)
    await c(USERS).create_index([("owner_id",1),("user_id",1)], unique=True)
    await c(PAYMENTS).create_index([("owner_id",1),("status",1),("created_at",-1)])
    await c(SUBS).create_index([("owner_id",1),("user_id",1)], unique=True)
    await c(SUBS).create_index([("owner_id",1),("active",1),("expiry_date",1)])
    await c(REFERRALS).create_index([("owner_id",1),("referred_user_id",1)], unique=True)
    await c(REFERRALS).create_index([("owner_id",1),("referrer_user_id",1),("rewarded",1)])


async def ensure_seller_defaults(owner_id:int, bot_name="Subscription Bot"):
    now=datetime.now(timezone.utc)
    defaults={
        "owner_id":owner_id,
        "bot_name":bot_name,
        "welcome_message":f"👋 Welcome to {bot_name}!",
        "support_username":"",
        "currency":"INR",
        "timezone":"Asia/Kolkata",
        "reminder_days":1,
        "upi_id":"",
        "upi_name":"",
        "upi_qr_file_id":"",
        "welcome_media_type":"",
        "welcome_media_file_id":"",
        "welcome_buttons":[],
        "referral_reward_days":7,
        "created_at":now,
        "updated_at":now,
    }
    await c(SETTINGS).update_one(
        {"owner_id":owner_id},
        {"$setOnInsert":defaults},
        upsert=True,
    )
    for key,value in defaults.items():
        if key in {"owner_id","created_at"}:
            continue
        await c(SETTINGS).update_one(
            {"owner_id":owner_id,key:{"$exists":False}},
            {"$set":{key:value,"updated_at":now}},
        )
    return await get_seller_settings(owner_id)


async def get_seller_settings(owner_id:int): return await c(SETTINGS).find_one({"owner_id":owner_id}) or {}
async def set_seller_setting(owner_id:int,key:str,value):
    allowed={"bot_name","welcome_message","support_username","currency","timezone","reminder_days","upi_id","upi_name","upi_qr_file_id","welcome_media_type","welcome_media_file_id","welcome_buttons","referral_reward_days"}
    if key not in allowed: raise ValueError("Unsupported setting")
    now=datetime.now(timezone.utc)
    await c(SETTINGS).update_one({"owner_id":owner_id},{"$set":{key:value,"updated_at":now},"$setOnInsert":{"owner_id":owner_id,"created_at":now}},upsert=True)


async def create_plan(owner_id,name,duration_text,duration_minutes,price):
    now=datetime.now(timezone.utc); doc={"owner_id":owner_id,"plan_id":uuid4().hex[:12],"name":name.strip(),"duration_text":duration_text,"duration_minutes":int(duration_minutes),"price":float(price),"active":True,"created_at":now,"updated_at":now}
    await c(PLANS).insert_one(doc); return doc
async def get_plan(owner_id,plan_id): return await c(PLANS).find_one({"owner_id":owner_id,"plan_id":plan_id})
async def get_plans(owner_id,active_only=False):
    q={"owner_id":owner_id};
    if active_only:q["active"]=True
    return await c(PLANS).find(q).sort("price",1).to_list(length=100)
async def update_plan(owner_id,plan_id,**values):
    values["updated_at"]=datetime.now(timezone.utc); r=await c(PLANS).update_one({"owner_id":owner_id,"plan_id":plan_id},{"$set":values}); return r.matched_count>0
async def delete_plan(owner_id,plan_id): return (await c(PLANS).delete_one({"owner_id":owner_id,"plan_id":plan_id})).deleted_count>0


async def add_channel(owner_id,chat_id,title,chat_type):
    now=datetime.now(timezone.utc)
    await c(CHANNELS).update_one({"owner_id":owner_id,"chat_id":int(chat_id)},{"$set":{"title":title,"chat_type":chat_type,"active":True,"updated_at":now},"$setOnInsert":{"owner_id":owner_id,"chat_id":int(chat_id),"created_at":now}},upsert=True)
    return await c(CHANNELS).find_one({"owner_id":owner_id,"chat_id":int(chat_id)})
async def get_channels(owner_id): return await c(CHANNELS).find({"owner_id":owner_id,"active":True}).to_list(length=100)


async def save_owner_access_invite_link(owner_id:int, chat_id:int, invite_link:str):
    """Store the reusable, no-expiry owner access link for one connected chat."""
    now=datetime.now(timezone.utc)
    result=await c(CHANNELS).update_one(
        {"owner_id":int(owner_id),"chat_id":int(chat_id),"active":True},
        {"$set":{
            "owner_access_invite_link":str(invite_link),
            "owner_access_link_updated_at":now,
            "updated_at":now,
        }},
    )
    return result.matched_count>0
async def remove_channel(owner_id,chat_id): return (await c(CHANNELS).update_one({"owner_id":owner_id,"chat_id":int(chat_id)},{"$set":{"active":False,"updated_at":datetime.now(timezone.utc)}})).matched_count>0


async def upsert_user(owner_id,user):
    now=datetime.now(timezone.utc)
    username=user.username or ""
    await c(USERS).update_one(
        {"owner_id":owner_id,"user_id":user.id},
        {
            "$set":{
                "first_name":user.first_name,
                "last_name":user.last_name,
                "username":username,
                "username_normalized":username.lower(),
                "language_code":user.language_code,
                "updated_at":now,
            },
            "$setOnInsert":{
                "owner_id":owner_id,
                "user_id":user.id,
                "joined_at":now,
                "banned":False,
                "ban_reason":"",
            },
        },
        upsert=True,
    )
async def get_user(owner_id,user_id): return await c(USERS).find_one({"owner_id":owner_id,"user_id":user_id})
async def count_users(owner_id): return await c(USERS).count_documents({"owner_id":owner_id})


async def get_user_by_username(owner_id:int, username:str):
    normalized=username.strip().lstrip("@").lower()
    if not normalized:
        return None
    return await c(USERS).find_one(
        {"owner_id":owner_id,"username_normalized":normalized}
    )


async def set_user_ban(owner_id:int, user_id:int, banned:bool, reason:str=""):
    now=datetime.now(timezone.utc)
    result=await c(USERS).update_one(
        {"owner_id":owner_id,"user_id":int(user_id)},
        {"$set":{
            "banned":bool(banned),
            "ban_reason":reason.strip() if banned else "",
            "updated_at":now,
        }},
    )
    return result.matched_count>0


async def remove_subscription(owner_id:int, user_id:int):
    now=datetime.now(timezone.utc)
    result=await c(SUBS).update_one(
        {"owner_id":owner_id,"user_id":int(user_id)},
        {"$set":{
            "active":False,
            "removed_by_admin":True,
            "updated_at":now,
        }},
    )
    return result.matched_count>0


async def create_payment(owner_id,user_id,plan,screenshot_file_id):
    now=datetime.now(timezone.utc); doc={"owner_id":owner_id,"payment_id":uuid4().hex[:16],"user_id":user_id,"plan_id":plan["plan_id"],"plan":plan["name"],"amount":plan["price"],"duration_text":plan["duration_text"],"duration_minutes":plan["duration_minutes"],"screenshot_file_id":screenshot_file_id,"status":"pending","created_at":now,"updated_at":now}
    await c(PAYMENTS).insert_one(doc); return doc

async def create_automatic_payment(owner_id,user_id,plan,gateway,transaction_id,gateway_payment_id=""):
    now=datetime.now(timezone.utc)
    doc={
        "owner_id":int(owner_id),"payment_id":str(transaction_id),"user_id":int(user_id),
        "plan_id":plan["plan_id"],"plan":plan["name"],"amount":float(plan["price"]),
        "duration_text":plan["duration_text"],"duration_minutes":int(plan["duration_minutes"]),
        "payment_method":gateway,"gateway_payment_id":str(gateway_payment_id or ""),
        "status":"approved","admin_id":0,"processed_at":now,"created_at":now,"updated_at":now,
    }
    result=await c(PAYMENTS).update_one(
        {"owner_id":int(owner_id),"payment_id":str(transaction_id)},
        {"$setOnInsert":doc},upsert=True,
    )
    payment=await c(PAYMENTS).find_one({"owner_id":int(owner_id),"payment_id":str(transaction_id)})
    if payment is not None:
        payment["_created_now"] = result.upserted_id is not None
    return payment
async def get_payment(owner_id,payment_id): return await c(PAYMENTS).find_one({"owner_id":owner_id,"payment_id":payment_id})
async def pending_payments(owner_id): return await c(PAYMENTS).find({"owner_id":owner_id,"status":"pending"}).sort("created_at",-1).to_list(length=50)
async def payment_history(owner_id): return await c(PAYMENTS).find({"owner_id":owner_id,"status":{"$in":["approved","rejected"]}}).sort("updated_at",-1).to_list(length=50)
async def set_payment_status(owner_id,payment_id,status,admin_id):
    now=datetime.now(timezone.utc)
    r=await c(PAYMENTS).update_one(
        {
            "owner_id":owner_id,
            "payment_id":payment_id,
            "status":{"$in":["pending","processing"]},
        },
        {
            "$set":{
                "status":status,
                "admin_id":admin_id,
                "processed_at":now,
                "updated_at":now,
            }
        },
    )
    return r.modified_count>0


async def claim_payment_for_processing(owner_id,payment_id,admin_id):
    now=datetime.now(timezone.utc)
    r=await c(PAYMENTS).update_one(
        {
            "owner_id":owner_id,
            "payment_id":payment_id,
            "status":"pending",
        },
        {
            "$set":{
                "status":"processing",
                "processing_admin_id":admin_id,
                "processing_started_at":now,
                "updated_at":now,
            }
        },
    )
    return r.modified_count>0


async def finalize_processed_payment(owner_id,payment_id,status,admin_id):
    now=datetime.now(timezone.utc)
    r=await c(PAYMENTS).update_one(
        {
            "owner_id":owner_id,
            "payment_id":payment_id,
            "status":"processing",
        },
        {
            "$set":{
                "status":status,
                "admin_id":admin_id,
                "processed_at":now,
                "updated_at":now,
            },
            "$unset":{
                "processing_admin_id":"",
                "processing_started_at":"",
                "processing_error":"",
            },
        },
    )
    return r.modified_count>0


async def release_processing_payment(owner_id,payment_id,error_message=""):
    now=datetime.now(timezone.utc)
    r=await c(PAYMENTS).update_one(
        {
            "owner_id":owner_id,
            "payment_id":payment_id,
            "status":"processing",
        },
        {
            "$set":{
                "status":"pending",
                "processing_error":str(error_message)[:500],
                "updated_at":now,
            },
            "$unset":{
                "processing_admin_id":"",
                "processing_started_at":"",
            },
        },
    )
    return r.modified_count>0


async def get_subscription(owner_id,user_id): return await c(SUBS).find_one({"owner_id":owner_id,"user_id":user_id})
async def activate_subscription(
    owner_id,
    user_id,
    plan_name,
    duration_minutes,
    amount=None,
    duration_text=None,
):
    now=datetime.now(timezone.utc)
    current=await get_subscription(owner_id,user_id)

    current_expiry=(current or {}).get("expiry_date")
    if current_expiry and current_expiry.tzinfo is None:
        current_expiry=current_expiry.replace(tzinfo=timezone.utc)
    elif current_expiry:
        current_expiry=current_expiry.astimezone(timezone.utc)

    # Renewal always starts from the remaining expiry when it is still active.
    # This prevents any already-paid remaining validity from being lost.
    if current and current.get("active") and current_expiry and current_expiry>now:
        base=current_expiry
    else:
        base=now

    added_minutes=int(duration_minutes)
    expiry=base+timedelta(minutes=added_minutes)

    previous_total_minutes=int((current or {}).get("total_duration_minutes") or 0)
    previous_total_paid=float((current or {}).get("total_paid") or 0)
    payment_amount=float(amount or 0)

    values={
        "plan":plan_name,
        "active":True,
        "expiry_date":expiry,
        "last_renewed_at":now,
        "last_added_minutes":added_minutes,
        "total_duration_minutes":previous_total_minutes+added_minutes,
        "total_paid":previous_total_paid+payment_amount,
        "removed_by_admin":False,
        "updated_at":now,
    }

    if amount is not None:
        values["amount"]=amount
        values["last_payment_amount"]=amount
    if duration_text is not None:
        values["duration_text"]=duration_text
        values["last_duration_text"]=duration_text

    if not current or not current.get("active") or not current_expiry or current_expiry<=now:
        values["start_date"]=now

    await c(SUBS).update_one(
        {"owner_id":owner_id,"user_id":user_id},
        {
            "$set":values,
            "$setOnInsert":{
                "owner_id":owner_id,
                "user_id":user_id,
                "created_at":now,
            },
        },
        upsert=True,
    )
    return expiry

async def active_subscriptions(owner_id, limit=5000):
    now=datetime.now(timezone.utc)
    return await c(SUBS).find({
        "owner_id":owner_id,
        "active":True,
        "expiry_date":{"$gt":now},
    }).to_list(length=limit)


async def expired_subscriptions(owner_id):
    now=datetime.now(timezone.utc); return await c(SUBS).find({"owner_id":owner_id,"active":True,"expiry_date":{"$lte":now}}).to_list(length=500)
async def mark_expired(owner_id,user_id): await c(SUBS).update_one({"owner_id":owner_id,"user_id":user_id},{"$set":{"active":False,"updated_at":datetime.now(timezone.utc)}})


async def register_referral(owner_id:int, referrer_user_id:int, referred_user_id:int):
    if not referrer_user_id or not referred_user_id or referrer_user_id == referred_user_id:
        return {"created":False,"reason":"invalid"}

    existing = await c(REFERRALS).find_one(
        {"owner_id":owner_id,"referred_user_id":referred_user_id}
    )
    if existing:
        return {"created":False,"reason":"already_registered","record":existing}

    now = datetime.now(timezone.utc)
    doc = {
        "owner_id":owner_id,
        "referrer_user_id":int(referrer_user_id),
        "referred_user_id":int(referred_user_id),
        "rewarded":False,
        "created_at":now,
        "updated_at":now,
    }
    await c(REFERRALS).insert_one(doc)
    return {"created":True,"record":doc}


async def count_successful_referrals(owner_id:int, referrer_user_id:int):
    return await c(REFERRALS).count_documents(
        {
            "owner_id":owner_id,
            "referrer_user_id":int(referrer_user_id),
            "rewarded":True,
        }
    )


async def count_all_referrals(owner_id:int, referrer_user_id:int):
    return await c(REFERRALS).count_documents(
        {
            "owner_id":owner_id,
            "referrer_user_id":int(referrer_user_id),
        }
    )


async def mark_referral_rewarded(
    owner_id:int,
    referred_user_id:int,
    payment_id:str|None=None,
):
    """Atomically claim a referral reward without marking it completed yet."""
    now = datetime.now(timezone.utc)
    return await c(REFERRALS).find_one_and_update(
        {
            "owner_id":owner_id,
            "referred_user_id":int(referred_user_id),
            "rewarded":False,
            "reward_status":{"$nin":["processing","rewarded"]},
        },
        {
            "$set":{
                "reward_status":"processing",
                "reward_payment_id":str(payment_id) if payment_id else None,
                "reward_claimed_at":now,
                "updated_at":now,
            },
            "$inc":{"reward_attempts":1},
        },
        return_document=ReturnDocument.AFTER,
    )


async def finalize_referral_reward(
    owner_id:int,
    referred_user_id:int,
    payment_id:str|None=None,
):
    now = datetime.now(timezone.utc)
    query = {
        "owner_id":owner_id,
        "referred_user_id":int(referred_user_id),
        "rewarded":False,
        "reward_status":"processing",
    }
    if payment_id:
        query["reward_payment_id"] = str(payment_id)

    result = await c(REFERRALS).update_one(
        query,
        {
            "$set":{
                "rewarded":True,
                "reward_status":"rewarded",
                "rewarded_at":now,
                "updated_at":now,
            },
            "$unset":{"reward_error":""},
        },
    )
    return result.modified_count == 1


async def release_referral_reward(
    owner_id:int,
    referred_user_id:int,
    error:str,
    payment_id:str|None=None,
):
    now = datetime.now(timezone.utc)
    query = {
        "owner_id":owner_id,
        "referred_user_id":int(referred_user_id),
        "rewarded":False,
        "reward_status":"processing",
    }
    if payment_id:
        query["reward_payment_id"] = str(payment_id)

    result = await c(REFERRALS).update_one(
        query,
        {
            "$set":{
                "reward_status":"failed",
                "reward_error":str(error)[:500],
                "reward_failed_at":now,
                "updated_at":now,
            }
        },
    )
    return result.modified_count == 1


async def stats(owner_id):
    revenue=await c(PAYMENTS).aggregate([{"$match":{"owner_id":owner_id,"status":"approved"}},{"$group":{"_id":None,"total":{"$sum":"$amount"}}}]).to_list(length=1)
    return {"users":await count_users(owner_id),"plans":await c(PLANS).count_documents({"owner_id":owner_id}),"channels":await c(CHANNELS).count_documents({"owner_id":owner_id,"active":True}),"pending":await c(PAYMENTS).count_documents({"owner_id":owner_id,"status":"pending"}),"revenue":revenue[0]["total"] if revenue else 0}
