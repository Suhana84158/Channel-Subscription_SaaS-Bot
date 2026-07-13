from datetime import datetime, timezone
from database.seller_subscriptions import expiring_assignments, mark_reminder_sent, reminder_was_sent, usage_warning
from config import ADMIN_IDS

async def run_seller_subscription_reminders(bot):
    now=datetime.now(timezone.utc)
    for a in await expiring_assignments(8):
        expiry=a.get("expiry_date")
        if not expiry: continue
        if expiry.tzinfo is None: expiry=expiry.replace(tzinfo=timezone.utc)
        seconds=(expiry-now).total_seconds()
        days=max(0, int((seconds + 86399)//86400))
        if seconds <= 0: label="expired"
        elif days in {7,3,1}: label=f"{days}d"
        else: continue
        key=f"{expiry.isoformat()}:{label}"
        if await reminder_was_sent(a["owner_id"],key): continue
        if label=="expired":
            text="⛔ Your seller plan has expired. Existing subscribers keep access, but new payments, users, plans and channels are restricted until renewal."
        else:
            text=f"⏰ Seller Plan Expiry Reminder\n\nYour plan expires in {days} day{'s' if days!=1 else ''}. Renew before expiry to avoid restrictions."
        try: await bot.send_message(a["owner_id"],text)
        except Exception: pass
        for admin_id in ADMIN_IDS:
            try: await bot.send_message(admin_id,f"Seller {a['owner_id']}: {text}")
            except Exception: pass
        await mark_reminder_sent(a["owner_id"],key)
        warning=await usage_warning(a["owner_id"],0.8)
        if warning:
            try: await bot.send_message(a["owner_id"],warning)
            except Exception: pass
