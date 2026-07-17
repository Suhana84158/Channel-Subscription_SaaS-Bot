import re
from typing import Iterable


EN_ESCALATION = (
    "❌ I couldn't fully resolve your issue.\n"
    "Please contact the Owner Support.\n\n"
    "👤 {support}\n\n"
    "Our support team will help you personally."
)

HI_ESCALATION = (
    "❌ Main aapki problem ko poori tarah solve nahi kar saka.\n"
    "Kripya Owner Support se contact karein.\n\n"
    "👤 {support}\n\n"
    "Hamari support team aapki personally help karegi."
)

GUIDES = [
    {
        "keys": ("invite link", "join group", "join channel", "group invite", "link not working", "invite nahi", "group join"),
        "en": (
            "🔗 Invite Link Troubleshooting\n\n"
            "1. Open Admin Panel → Channels / Groups.\n"
            "2. Confirm the correct channel or group is connected.\n"
            "3. Make the clone bot an administrator.\n"
            "4. Enable Invite Users and Ban Users permissions.\n"
            "5. Open Subscription Guard and run Force Sync.\n"
            "6. Use Resend Invite Links for active subscribers.\n\n"
            "Also check that the user's subscription is active and has not expired."
        ),
        "hi": (
            "🔗 Invite Link Problem Solution\n\n"
            "1. Admin Panel → Channels / Groups kholen.\n"
            "2. Sahi channel ya group connected hai ya nahi check karein.\n"
            "3. Clone bot ko administrator banayein.\n"
            "4. Invite Users aur Ban Users permissions ON karein.\n"
            "5. Subscription Guard kholkar Force Sync chalayein.\n"
            "6. Active subscribers ke liye Resend Invite Links use karein.\n\n"
            "User ka subscription active aur unexpired hona bhi zaroori hai."
        ),
    },
    {
        "keys": ("subscription guard", "removed active", "auto kick", "unauthorized", "expired user"),
        "en": (
            "🛡 Subscription Guard\n\n"
            "Subscription Guard removes expired, banned, or unauthorized members and protects connected chats.\n\n"
            "To check it:\n"
            "1. Admin Panel → Subscription Guard.\n"
            "2. Enable the required protection switches.\n"
            "3. Confirm the user's expiry date in User Management.\n"
            "4. Run Force Sync.\n"
            "5. Review Guard Logs to see why the user was removed."
        ),
        "hi": (
            "🛡 Subscription Guard\n\n"
            "Subscription Guard expired, banned ya unauthorized members ko remove karta hai.\n\n"
            "Check karne ke liye:\n"
            "1. Admin Panel → Subscription Guard kholen.\n"
            "2. Zaroori protection switches ON karein.\n"
            "3. User Management me expiry date check karein.\n"
            "4. Force Sync chalayein.\n"
            "5. Guard Logs me removal ka exact reason dekhein."
        ),
    },
    {
        "keys": ("create plan", "add plan", "subscription plan", "plan kaise", "manage plans"),
        "en": (
            "📦 Create a Subscription Plan\n\n"
            "1. Open Admin Panel → Manage Plans.\n"
            "2. Tap Add Plan.\n"
            "3. Enter the plan name, duration, and price.\n"
            "4. Review the details and save.\n"
            "5. Keep the plan enabled so users can see it."
        ),
        "hi": (
            "📦 Subscription Plan Banane ka Tarika\n\n"
            "1. Admin Panel → Manage Plans kholen.\n"
            "2. Add Plan par tap karein.\n"
            "3. Plan name, duration aur price enter karein.\n"
            "4. Details check karke save karein.\n"
            "5. Plan ko enabled rakhein taaki users usse dekh saken."
        ),
    },
    {
        "keys": ("payment qr", "qr not showing", "upi", "payment setting", "payment setup", "razorpay", "cashfree"),
        "en": (
            "💳 Payment Settings\n\n"
            "1. Open Admin Panel → Payment Settings.\n"
            "2. Set your UPI ID and UPI Name.\n"
            "3. Upload a clear QR image.\n"
            "4. For an automatic gateway, enter the required credentials and test the connection.\n"
            "5. Use Preview or start a test purchase to confirm the payment screen."
        ),
        "hi": (
            "💳 Payment Settings\n\n"
            "1. Admin Panel → Payment Settings kholen.\n"
            "2. UPI ID aur UPI Name set karein.\n"
            "3. Clear QR image upload karein.\n"
            "4. Automatic gateway ke liye credentials enter karke connection test karein.\n"
            "5. Test purchase karke payment screen verify karein."
        ),
    },
    {
        "keys": ("broadcast", "scheduled broadcast", "broadcast failed", "message send"),
        "en": (
            "📣 Broadcast Guide\n\n"
            "1. Open Admin Panel → Broadcast.\n"
            "2. Send one message, photo, video, document, audio, voice, GIF, sticker, or forwarded post.\n"
            "3. Confirm the broadcast.\n"
            "4. For later delivery, use Scheduled and choose the date and time.\n"
            "5. Open Retry Failed if some deliveries fail."
        ),
        "hi": (
            "📣 Broadcast Guide\n\n"
            "1. Admin Panel → Broadcast kholen.\n"
            "2. Ek text, photo, video, document, audio, voice, GIF, sticker ya forwarded post bhejein.\n"
            "3. Broadcast confirm karein.\n"
            "4. Baad me bhejne ke liye Scheduled me date aur time choose karein.\n"
            "5. Kuch delivery fail ho to Retry Failed use karein."
        ),
    },
    {
        "keys": ("live support", "reply template", "auto delete", "support setup", "connectsupport"),
        "en": (
            "💬 Live Support Setup\n\n"
            "1. Create a Telegram forum group for support.\n"
            "2. Add the clone bot as an administrator.\n"
            "3. Run /connectsupport inside that group.\n"
            "4. Open Admin Panel → Live Support and enable it.\n"
            "5. Create reply templates for common answers.\n"
            "6. Set Auto Delete Time on a template when required."
        ),
        "hi": (
            "💬 Live Support Setup\n\n"
            "1. Support ke liye Telegram forum group banayein.\n"
            "2. Clone bot ko administrator banayein.\n"
            "3. Group ke andar /connectsupport command chalayein.\n"
            "4. Admin Panel → Live Support kholkar enable karein.\n"
            "5. Common answers ke reply templates banayein.\n"
            "6. Zaroorat par template ka Auto Delete Time set karein."
        ),
    },
    {
        "keys": ("staff", "moderator", "promote admin", "staff management"),
        "en": (
            "👮 Staff Management\n\n"
            "1. Open Admin Panel → Staff Management.\n"
            "2. Choose Promote Admin or Promote Moderator.\n"
            "3. Send the person's Telegram User ID.\n"
            "4. The person must start the clone bot once.\n"
            "5. Use Staff List to suspend or remove access.\n\n"
            "Only promote people you trust."
        ),
        "hi": (
            "👮 Staff Management\n\n"
            "1. Admin Panel → Staff Management kholen.\n"
            "2. Promote Admin ya Promote Moderator choose karein.\n"
            "3. Person ka Telegram User ID bhejein.\n"
            "4. Us person ko clone bot ek baar start karna hoga.\n"
            "5. Staff List se access suspend ya remove karein.\n\n"
            "Sirf trusted logon ko promote karein."
        ),
    },
    {
        "keys": ("give subscription", "extend subscription", "custom duration", "ban user", "user management"),
        "en": (
            "👥 User Management\n\n"
            "1. Open Admin Panel → User Management.\n"
            "2. Search by Telegram User ID or username.\n"
            "3. Open the user's details.\n"
            "4. Use Give / Extend Subscription and select a plan or custom duration.\n"
            "5. You can also remove a subscription, ban, or unban the user."
        ),
        "hi": (
            "👥 User Management\n\n"
            "1. Admin Panel → User Management kholen.\n"
            "2. Telegram User ID ya username se search karein.\n"
            "3. User details kholen.\n"
            "4. Give / Extend Subscription me plan ya custom duration choose karein.\n"
            "5. Yahin se subscription remove, ban ya unban bhi kar sakte hain."
        ),
    },
    {
        "keys": ("content protection", "forward", "screenshot", "copy message"),
        "en": (
            "🔒 Content Protection\n\n"
            "Open Admin Panel → Content Protection and enable it. It protects messages sent by the clone bot from forwarding or copying where Telegram supports protection. It does not automatically protect old messages or content posted directly in connected chats."
        ),
        "hi": (
            "🔒 Content Protection\n\n"
            "Admin Panel → Content Protection kholkar enable karein. Ye clone bot ke bheje messages ko forwarding ya copying se protect karta hai jahan Telegram protection support karta hai. Purane messages ya connected chats me directly post content automatically protect nahi hota."
        ),
    },
    {
        "keys": ("deleting messages", "delete links", "service message", "auto delete messages"),
        "en": (
            "🗑 Deleting Messages\n\n"
            "Open Admin Panel → Deleting Messages. Enable the feature and choose which message types should be removed, such as commands, service messages, links, or other configured categories. Confirm that the bot has Delete Messages permission in the group."
        ),
        "hi": (
            "🗑 Deleting Messages\n\n"
            "Admin Panel → Deleting Messages kholen. Feature enable karke choose karein ki commands, service messages, links ya kaunse configured message types delete honge. Group me bot ke paas Delete Messages permission zaroor honi chahiye."
        ),
    },
    {
        "keys": ("timezone", "time zone", "schedule wrong", "wrong time"),
        "en": (
            "🌍 Timezone Settings\n\n"
            "Open Admin Panel → Bot Settings → Timezone. Select your country/timezone from the buttons or use manual timezone entry. Scheduled broadcasts and displayed times use this timezone, while records are stored internally in UTC."
        ),
        "hi": (
            "🌍 Timezone Settings\n\n"
            "Admin Panel → Bot Settings → Timezone kholen. Country/timezone button se select karein ya manual timezone enter karein. Scheduled broadcast aur displayed time isi timezone ka use karte hain, jabki records internally UTC me save hote hain."
        ),
    },
    {
        "keys": ("coupon", "discount code"),
        "en": (
            "🎟 Coupon Setup\n\n"
            "Open Admin Panel → Coupons. Create a code with discount type, value, and usage limit. Check the coupon details before sharing it with users."
        ),
        "hi": (
            "🎟 Coupon Setup\n\n"
            "Admin Panel → Coupons kholen. Discount type, value aur usage limit ke saath code banayein. Users ko share karne se pehle details check karein."
        ),
    },
]


def looks_hindi(text: str) -> bool:
    lower = text.lower()
    hindi_words: Iterable[str] = (
        "kaise", "kese", "kyu", "nahi", "nahin", "mera", "mere", "karna",
        "karo", "ho raha", "samaj", "group me", "bot me", "chal", "dikha",
    )
    return any(word in lower for word in hindi_words) or bool(re.search(r"[\u0900-\u097F]", text))


def is_unresolved_message(text: str) -> bool:
    lower = text.lower().strip()
    phrases = (
        "still not working", "same error", "tried everything", "not fixed",
        "doesn't work", "does not work", "bar bar", "baar baar", "fir bhi",
        "phir bhi", "samaj nahi", "samaj nahin", "same problem",
    )
    return any(x in lower for x in phrases)


def answer_question(question: str, support_username: str) -> tuple[str, bool]:
    question = (question or "").strip()
    hindi = looks_hindi(question)
    support = (support_username or "").strip()
    if support and not support.startswith("@") and not support.startswith("http"):
        support = "@" + support
    if not support:
        support = "Owner Support"

    if is_unresolved_message(question):
        template = HI_ESCALATION if hindi else EN_ESCALATION
        return template.format(support=support), True

    lower = question.lower()
    scored = []
    for guide in GUIDES:
        score = sum(1 for key in guide["keys"] if key in lower)
        if score:
            scored.append((score, guide))
    if scored:
        scored.sort(key=lambda item: item[0], reverse=True)
        guide = scored[0][1]
        return guide["hi" if hindi else "en"], False

    template = HI_ESCALATION if hindi else EN_ESCALATION
    return template.format(support=support), True
