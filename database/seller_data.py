from datetime import datetime, timedelta, timezone
from uuid import uuid4
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
    allowed={"bot_name","welcome_message","support_username","currency","timezone","reminder_days","upi_id","upi_name","upi_qr_file_id","welcome_media_type","welcome_media_file_id","welcome_buttons"}
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
async def remove_channel(owner_id,chat_id): return (await c(CHANNELS).update_one({"owner_id":owner_id,"chat_id":int(chat_id)},{"$set":{"active":False,"updated_at":datetime.now(timezone.utc)}})).matched_count>0


async def upsert_user(owner_id,user):
    now=datetime.now(timezone.utc)
    await c(USERS).update_one({"owner_id":owner_id,"user_id":user.id},{"$set":{"first_name":user.first_name,"username":user.username,"updated_at":now},"$setOnInsert":{"owner_id":owner_id,"user_id":user.id,"joined_at":now,"banned":False}},upsert=True)
async def get_user(owner_id,user_id): return await c(USERS).find_one({"owner_id":owner_id,"user_id":user_id})
async def count_users(owner_id): return await c(USERS).count_documents({"owner_id":owner_id})


async def create_payment(owner_id,user_id,plan,screenshot_file_id):
    now=datetime.now(timezone.utc); doc={"owner_id":owner_id,"payment_id":uuid4().hex[:16],"user_id":user_id,"plan_id":plan["plan_id"],"plan":plan["name"],"amount":plan["price"],"duration_text":plan["duration_text"],"duration_minutes":plan["duration_minutes"],"screenshot_file_id":screenshot_file_id,"status":"pending","created_at":now,"updated_at":now}
    await c(PAYMENTS).insert_one(doc); return doc
async def get_payment(owner_id,payment_id): return await c(PAYMENTS).find_one({"owner_id":owner_id,"payment_id":payment_id})
async def pending_payments(owner_id): return await c(PAYMENTS).find({"owner_id":owner_id,"status":"pending"}).sort("created_at",-1).to_list(length=50)
async def payment_history(owner_id): return await c(PAYMENTS).find({"owner_id":owner_id,"status":{"$in":["approved","rejected"]}}).sort("updated_at",-1).to_list(length=50)
async def set_payment_status(owner_id,payment_id,status,admin_id):
    r=await c(PAYMENTS).update_one({"owner_id":owner_id,"payment_id":payment_id,"status":"pending"},{"$set":{"status":status,"admin_id":admin_id,"updated_at":datetime.now(timezone.utc)}}); return r.modified_count>0


async def get_subscription(owner_id,user_id): return await c(SUBS).find_one({"owner_id":owner_id,"user_id":user_id})
async def activate_subscription(owner_id,user_id,plan_name,duration_minutes):
    now=datetime.now(timezone.utc); current=await get_subscription(owner_id,user_id)
    base=current.get("expiry_date") if current and current.get("active") and current.get("expiry_date") and current["expiry_date"]>now else now
    expiry=base+timedelta(minutes=int(duration_minutes))
    await c(SUBS).update_one({"owner_id":owner_id,"user_id":user_id},{"$set":{"plan":plan_name,"active":True,"expiry_date":expiry,"updated_at":now},"$setOnInsert":{"owner_id":owner_id,"user_id":user_id,"created_at":now}},upsert=True)
    return expiry
async def expired_subscriptions(owner_id):
    now=datetime.now(timezone.utc); return await c(SUBS).find({"owner_id":owner_id,"active":True,"expiry_date":{"$lte":now}}).to_list(length=500)
async def mark_expired(owner_id,user_id): await c(SUBS).update_one({"owner_id":owner_id,"user_id":user_id},{"$set":{"active":False,"updated_at":datetime.now(timezone.utc)}})


async def stats(owner_id):
    revenue=await c(PAYMENTS).aggregate([{"$match":{"owner_id":owner_id,"status":"approved"}},{"$group":{"_id":None,"total":{"$sum":"$amount"}}}]).to_list(length=1)
    return {"users":await count_users(owner_id),"plans":await c(PLANS).count_documents({"owner_id":owner_id}),"channels":await c(CHANNELS).count_documents({"owner_id":owner_id,"active":True}),"pending":await c(PAYMENTS).count_documents({"owner_id":owner_id,"status":"pending"}),"revenue":revenue[0]["total"] if revenue else 0}
