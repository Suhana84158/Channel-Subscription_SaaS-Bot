import re
import unicodedata
from difflib import SequenceMatcher
from typing import Iterable

EN_ESCALATION = (
    "❌ I couldn't fully resolve your issue.\n\n"
    "Please contact the Owner Support.\n\n"
    "👤 {support}\n\n"
    "Our support team will help you personally."
)

HI_ESCALATION = (
    "❌ Main aapki problem ko poori tarah solve nahi kar saka.\n\n"
    "Kripya Owner Support se contact karein.\n\n"
    "👤 {support}\n\n"
    "Hamari support team aapki personally help karegi."
)

EN_CLARIFY = (
    "🤖 I need a little more information.\n\n"
    "Please ask one clear question after /ai and include the feature name.\n\n"
    "Examples:\n"
    "/ai How do I add a private group?\n"
    "/ai My invite link is not working.\n"
    "/ai How do I set a payment QR?\n"
    "/ai Broadcast is failing.\n"
    "/ai How do I enable Live Support?"
)

HI_CLARIFY = (
    "🤖 Mujhe thodi aur information chahiye.\n\n"
    "Kripya /ai ke baad ek clear question likhein aur feature ka naam bhi batayein.\n\n"
    "Examples:\n"
    "/ai Private group kaise add kare?\n"
    "/ai Invite link kaam nahi kar raha.\n"
    "/ai Payment QR kaise set kare?\n"
    "/ai Broadcast fail ho raha hai.\n"
    "/ai Live Support kaise enable kare?"
)


def _guide(title_en: str, title_hi: str, aliases: tuple[str, ...], keywords: tuple[str, ...], en: str, hi: str) -> dict:
    return {
        "title_en": title_en,
        "title_hi": title_hi,
        "aliases": aliases,
        "keywords": keywords,
        "en": en,
        "hi": hi,
    }


GUIDES = [
    _guide(
        "Group Setup", "Group Setup",
        (
            "how to add group", "how to connect group", "add private group", "connect private group",
            "group setup", "group kaise add", "group kese add", "group kaise connect",
            "group kese connect", "group add karna", "group jodna", "private group add",
        ),
        ("group", "add", "connect", "setup", "private", "jod", "kaise", "kese"),
        "➕ Add a Private Group\n\n"
        "1. Add the clone bot to your private group.\n"
        "2. Promote the bot as administrator.\n"
        "3. Enable Invite Users and Ban Users permissions.\n"
        "4. Open Admin Panel → Channels / Groups → Add Channel/Group.\n"
        "5. Forward a message from the group to the bot.\n"
        "6. If forwarding does not detect it, send:\n"
        "-1001234567890 | Group Name\n"
        "7. Open Channel List and confirm that the group is connected.\n\n"
        "Tip: The group ID normally starts with -100.",
        "➕ Private Group Add Karne ka Tarika\n\n"
        "1. Clone bot ko private group me add karein.\n"
        "2. Bot ko administrator banayein.\n"
        "3. Invite Users aur Ban Users permissions ON karein.\n"
        "4. Admin Panel → Channels / Groups → Add Channel/Group kholen.\n"
        "5. Group ka koi message bot ko forward karein.\n"
        "6. Agar detect na ho to ye format bhejein:\n"
        "-1001234567890 | Group Name\n"
        "7. Channel List me check karein ki group connected hai.\n\n"
        "Tip: Group ID aam taur par -100 se start hoti hai."
    ),
    _guide(
        "Invite Link Troubleshooting", "Invite Link Problem",
        (
            "invite link not working", "users cannot join", "user cannot join group", "group invite problem",
            "invite nahi chal raha", "invite link kaam nahi", "users join nahi", "joined nahin ho pa raha",
            "resend invite link", "invite link error",
        ),
        ("invite", "link", "join", "joined", "resend", "cannot", "nahi", "nahin", "working"),
        "🔗 Invite Link Troubleshooting\n\n"
        "1. Open Admin Panel → Channels / Groups.\n"
        "2. Confirm the correct channel or group is connected.\n"
        "3. Make the clone bot an administrator.\n"
        "4. Enable Invite Users and Ban Users permissions.\n"
        "5. Confirm the user's subscription is active and unexpired.\n"
        "6. Open Subscription Guard and run Force Sync.\n"
        "7. Use Resend Invite Links for active subscribers.\n\n"
        "If it still fails, remove and re-add the bot as admin, then reconnect the group.",
        "🔗 Invite Link Problem Solution\n\n"
        "1. Admin Panel → Channels / Groups kholen.\n"
        "2. Sahi channel ya group connected hai ya nahi check karein.\n"
        "3. Clone bot ko administrator banayein.\n"
        "4. Invite Users aur Ban Users permissions ON karein.\n"
        "5. User ka subscription active aur unexpired check karein.\n"
        "6. Subscription Guard me Force Sync chalayein.\n"
        "7. Active subscribers ke liye Resend Invite Links use karein.\n\n"
        "Phir bhi fail ho to bot ko admin se remove karke dobara add karein aur group reconnect karein."
    ),
    _guide(
        "Channel Setup", "Channel Setup",
        ("how to add channel", "connect channel", "channel setup", "channel kaise add", "channel kese add", "private channel add"),
        ("channel", "add", "connect", "setup", "private", "kaise", "kese"),
        "📢 Add a Private Channel\n\n"
        "1. Add the clone bot to the channel.\n"
        "2. Promote it as administrator with Invite Users permission.\n"
        "3. Open Admin Panel → Channels / Groups → Add Channel/Group.\n"
        "4. Forward a channel post to the bot.\n"
        "5. Confirm the channel in Channel List.\n"
        "6. Use a test subscription to verify the invite flow.",
        "📢 Private Channel Add Karne ka Tarika\n\n"
        "1. Clone bot ko channel me add karein.\n"
        "2. Invite Users permission ke saath administrator banayein.\n"
        "3. Admin Panel → Channels / Groups → Add Channel/Group kholen.\n"
        "4. Channel ka post bot ko forward karein.\n"
        "5. Channel List me confirm karein.\n"
        "6. Test subscription se invite flow check karein."
    ),
    _guide(
        "Subscription Plans", "Subscription Plans",
        ("create plan", "add plan", "subscription plan", "plan kaise", "plan kese", "manage plans", "edit plan", "delete plan"),
        ("plan", "subscription", "price", "duration", "create", "add", "edit", "delete"),
        "📦 Subscription Plans\n\n"
        "1. Open Admin Panel → Manage Plans.\n"
        "2. Tap Add Plan.\n"
        "3. Enter the plan name, duration, and price.\n"
        "4. Review and save it.\n"
        "5. Keep it enabled so users can buy it.\n\n"
        "You can also edit, disable, enable, or delete existing plans.",
        "📦 Subscription Plans\n\n"
        "1. Admin Panel → Manage Plans kholen.\n"
        "2. Add Plan par tap karein.\n"
        "3. Plan name, duration aur price enter karein.\n"
        "4. Details check karke save karein.\n"
        "5. Users ko dikhane ke liye plan enabled rakhein.\n\n"
        "Existing plans ko edit, disable, enable ya delete bhi kar sakte hain."
    ),
    _guide(
        "Payment Settings", "Payment Settings",
        ("payment qr", "qr not showing", "upi setup", "payment setup", "set qr", "qr kaise", "upi kaise", "payment setting"),
        ("payment", "qr", "upi", "gateway", "razorpay", "cashfree", "price"),
        "💳 Payment Settings\n\n"
        "1. Open Admin Panel → Payment Settings.\n"
        "2. Set your UPI ID and UPI Name.\n"
        "3. Upload a clear QR image.\n"
        "4. Save the settings.\n"
        "5. Start a test purchase to confirm the payment screen.\n\n"
        "For automatic gateways, enter valid credentials and test the connection before enabling it.",
        "💳 Payment Settings\n\n"
        "1. Admin Panel → Payment Settings kholen.\n"
        "2. UPI ID aur UPI Name set karein.\n"
        "3. Clear QR image upload karein.\n"
        "4. Settings save karein.\n"
        "5. Test purchase karke payment screen check karein.\n\n"
        "Automatic gateway ke liye valid credentials enter karke enable karne se pehle connection test karein."
    ),
    _guide(
        "Broadcast", "Broadcast",
        ("broadcast failed", "broadcast setup", "send message all users", "scheduled broadcast", "brodcast", "broadcast kaise"),
        ("broadcast", "brodcast", "scheduled", "message", "send", "failed", "retry"),
        "📣 Broadcast Guide\n\n"
        "1. Open Admin Panel → Broadcast.\n"
        "2. Send one text or media message.\n"
        "3. Preview and confirm the broadcast.\n"
        "4. For later delivery, open Scheduled and select date/time.\n"
        "5. If deliveries fail, use Retry Failed.\n\n"
        "Large broadcasts may take time because Telegram applies sending limits.",
        "📣 Broadcast Guide\n\n"
        "1. Admin Panel → Broadcast kholen.\n"
        "2. Ek text ya media message bhejein.\n"
        "3. Preview karke broadcast confirm karein.\n"
        "4. Baad me bhejne ke liye Scheduled me date/time choose karein.\n"
        "5. Delivery fail ho to Retry Failed use karein.\n\n"
        "Telegram sending limits ke karan large broadcast me time lag sakta hai."
    ),
    _guide(
        "Live Support", "Live Support",
        ("live support", "support setup", "connectsupport", "reply template", "auto delete template", "support kaise"),
        ("live", "support", "template", "reply", "connectsupport", "auto", "delete"),
        "💬 Live Support Setup\n\n"
        "1. Create a Telegram forum group for support.\n"
        "2. Add the clone bot as an administrator.\n"
        "3. Run /connectsupport inside that group.\n"
        "4. Open Admin Panel → Live Support and enable it.\n"
        "5. Create reply templates for common answers.\n"
        "6. Set Auto Delete Time per template when needed.",
        "💬 Live Support Setup\n\n"
        "1. Support ke liye Telegram forum group banayein.\n"
        "2. Clone bot ko administrator banayein.\n"
        "3. Group ke andar /connectsupport command chalayein.\n"
        "4. Admin Panel → Live Support kholkar enable karein.\n"
        "5. Common answers ke reply templates banayein.\n"
        "6. Zaroorat par har template ka Auto Delete Time set karein."
    ),
    _guide(
        "User Management", "User Management",
        ("give subscription", "extend subscription", "custom duration", "ban user", "user management", "remove subscription", "search user"),
        ("user", "subscription", "extend", "give", "ban", "unban", "search", "duration"),
        "👥 User Management\n\n"
        "1. Open Admin Panel → User Management.\n"
        "2. Search by Telegram User ID or username.\n"
        "3. Open the user's details.\n"
        "4. Use Give / Extend Subscription and select a plan or custom duration.\n"
        "5. You can remove a subscription, ban, or unban the user.\n\n"
        "Custom duration examples: 30m, 12h, 7d, 3mo, 1y.",
        "👥 User Management\n\n"
        "1. Admin Panel → User Management kholen.\n"
        "2. Telegram User ID ya username se search karein.\n"
        "3. User details kholen.\n"
        "4. Give / Extend Subscription me plan ya custom duration choose karein.\n"
        "5. Subscription remove, ban ya unban bhi kar sakte hain.\n\n"
        "Custom duration examples: 30m, 12h, 7d, 3mo, 1y."
    ),
    _guide(
        "Staff Management", "Staff Management",
        ("staff management", "promote admin", "promote moderator", "add staff", "remove staff", "staff kaise"),
        ("staff", "admin", "moderator", "promote", "suspend", "permission"),
        "👮 Staff Management\n\n"
        "1. Open Admin Panel → Staff Management.\n"
        "2. Choose Promote Admin or Promote Moderator.\n"
        "3. Send the person's Telegram User ID.\n"
        "4. The person must start the clone bot once.\n"
        "5. Use Staff List to suspend or remove access.\n\n"
        "Only promote people you trust.",
        "👮 Staff Management\n\n"
        "1. Admin Panel → Staff Management kholen.\n"
        "2. Promote Admin ya Promote Moderator choose karein.\n"
        "3. Person ka Telegram User ID bhejein.\n"
        "4. Us person ko clone bot ek baar start karna hoga.\n"
        "5. Staff List se access suspend ya remove karein.\n\n"
        "Sirf trusted logon ko promote karein."
    ),
    _guide(
        "Subscription Guard", "Subscription Guard",
        ("subscription guard", "auto kick", "removed active user", "unauthorized user", "force sync", "expired user"),
        ("guard", "kick", "unauthorized", "expired", "force", "sync", "removed"),
        "🛡 Subscription Guard\n\n"
        "Subscription Guard removes expired, banned, or unauthorized members.\n\n"
        "1. Open Admin Panel → Subscription Guard.\n"
        "2. Enable the required protection switches.\n"
        "3. Check the user's expiry date in User Management.\n"
        "4. Run Force Sync.\n"
        "5. Review Guard Logs to see why a user was removed.",
        "🛡 Subscription Guard\n\n"
        "Subscription Guard expired, banned ya unauthorized members ko remove karta hai.\n\n"
        "1. Admin Panel → Subscription Guard kholen.\n"
        "2. Zaroori protection switches ON karein.\n"
        "3. User Management me expiry date check karein.\n"
        "4. Force Sync chalayein.\n"
        "5. Guard Logs me removal ka reason dekhein."
    ),
    _guide(
        "Content Protection", "Content Protection",
        ("content protection", "stop forwarding", "screenshot protection", "copy message", "restricted mode"),
        ("content", "protection", "forward", "copy", "screenshot", "restricted"),
        "🔒 Content Protection\n\n"
        "Open Admin Panel → Content Protection and enable it. It protects messages sent by the clone bot from forwarding or copying where Telegram supports protection. It does not retroactively protect old messages or content posted directly in connected chats.",
        "🔒 Content Protection\n\n"
        "Admin Panel → Content Protection kholkar enable karein. Ye clone bot ke messages ko forwarding ya copying se protect karta hai jahan Telegram support karta hai. Purane messages ya connected chats me directly post kiya content automatically protect nahi hota."
    ),
    _guide(
        "Deleting Messages", "Deleting Messages",
        ("deleting messages", "delete links", "delete service message", "auto delete commands", "link deletion"),
        ("deleting", "delete", "links", "command", "service", "message"),
        "🗑 Deleting Messages\n\n"
        "1. Open Admin Panel → Deleting Messages.\n"
        "2. Enable the feature.\n"
        "3. Select which message types should be deleted.\n"
        "4. Make sure the bot has Delete Messages permission in the group.\n"
        "5. Send a test message to verify it.",
        "🗑 Deleting Messages\n\n"
        "1. Admin Panel → Deleting Messages kholen.\n"
        "2. Feature enable karein.\n"
        "3. Kaunse message types delete honge wo select karein.\n"
        "4. Group me bot ke paas Delete Messages permission honi chahiye.\n"
        "5. Test message bhejkar verify karein."
    ),
    _guide(
        "Timezone", "Timezone",
        ("timezone", "time zone", "schedule wrong time", "wrong time", "timezone kaise"),
        ("timezone", "time", "zone", "schedule", "utc", "wrong"),
        "🌍 Timezone Settings\n\n"
        "Open Admin Panel → Bot Settings → Timezone. Select your timezone from the buttons or use manual entry. Scheduled broadcasts and displayed times use this timezone, while records are stored internally in UTC.",
        "🌍 Timezone Settings\n\n"
        "Admin Panel → Bot Settings → Timezone kholen. Button se timezone select karein ya manual entry use karein. Scheduled broadcasts aur displayed time isi timezone ka use karte hain; records internally UTC me save hote hain."
    ),
    _guide(
        "Coupons", "Coupons",
        ("coupon setup", "create coupon", "discount code", "coupon kaise", "coupon not working"),
        ("coupon", "discount", "code", "limit", "create"),
        "🎟 Coupon Setup\n\n"
        "1. Open Admin Panel → Coupons.\n"
        "2. Create a code.\n"
        "3. Set the discount type and value.\n"
        "4. Set usage limits and validity.\n"
        "5. Save and test the coupon before sharing it.",
        "🎟 Coupon Setup\n\n"
        "1. Admin Panel → Coupons kholen.\n"
        "2. Coupon code banayein.\n"
        "3. Discount type aur value set karein.\n"
        "4. Usage limit aur validity set karein.\n"
        "5. Share karne se pehle coupon test karein."
    ),
]


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    replacements = {
        "kese": "kaise", "kyu": "why", "nahin": "nahi", "nhi": "nahi",
        "brodcast": "broadcast", "invte": "invite", "subcription": "subscription",
        "grp": "group", "usr": "user", "msg": "message", "botton": "button",
    }
    text = re.sub(r"[^a-z0-9\u0900-\u097f@]+", " ", text)
    words = [replacements.get(word, word) for word in text.split()]
    return " ".join(words).strip()


def looks_hindi(text: str) -> bool:
    lower = _normalize(text)
    hindi_words: Iterable[str] = (
        "kaise", "why", "nahi", "mera", "mere", "karna", "karo", "ho raha",
        "samaj", "group me", "bot me", "chal", "dikha", "add kare", "batao",
        "phir", "fir", "wala", "wali", "mujhe", "muje",
    )
    return any(word in lower for word in hindi_words) or bool(re.search(r"[\u0900-\u097F]", text or ""))


def _support_name(value: str) -> str:
    support = (value or "").strip()
    if support and not support.startswith("@") and not support.startswith("http"):
        support = "@" + support
    return support or "Owner Support"


def _token_score(query: str, guide: dict) -> float:
    query_tokens = set(query.split())
    if not query_tokens:
        return 0.0

    best_alias = 0.0
    for alias in guide["aliases"]:
        normalized_alias = _normalize(alias)
        if normalized_alias in query or query in normalized_alias:
            best_alias = max(best_alias, 1.0)
        else:
            best_alias = max(best_alias, SequenceMatcher(None, query, normalized_alias).ratio())

    keyword_hits = sum(1 for keyword in guide["keywords"] if _normalize(keyword) in query_tokens or _normalize(keyword) in query)
    keyword_score = min(1.0, keyword_hits / 3.0)

    # Require a meaningful topic word to avoid random answers.
    topic_hit = any(_normalize(keyword) in query for keyword in guide["keywords"][:3])
    if not topic_hit and best_alias < 0.72:
        return 0.0
    return (best_alias * 0.62) + (keyword_score * 0.38)


def _is_generic_help(query: str) -> bool:
    generic = {
        "help", "ai help", "please help", "madad", "help me", "problem", "error",
        "kya help", "mujhe help", "muje help", "guide", "commands",
    }
    return query in generic or len(query.split()) <= 1


def _is_failure_followup(query: str) -> bool:
    phrases = (
        "still not working", "same error", "tried everything", "not fixed",
        "does not work", "doesnt work", "same problem", "baar baar", "bar bar",
        "fir bhi", "phir bhi", "abhi bhi", "solve nahi", "fix nahi",
    )
    return any(phrase in query for phrase in phrases)


def answer_question(question: str, support_username: str) -> tuple[str, bool]:
    raw_question = (question or "").strip()
    query = _normalize(raw_question)
    hindi = looks_hindi(raw_question)
    support = _support_name(support_username)

    if not query or _is_generic_help(query):
        return (HI_CLARIFY if hindi else EN_CLARIFY), False

    scored = sorted(
        ((_token_score(query, guide), guide) for guide in GUIDES),
        key=lambda item: item[0],
        reverse=True,
    )
    best_score, best_guide = scored[0]

    # A recognized topic always receives a useful guide, even when the seller says
    # "still not working". Owner Support is reserved for questions we cannot match.
    if best_score >= 0.43:
        return best_guide["hi" if hindi else "en"], False

    if _is_failure_followup(query):
        template = HI_ESCALATION if hindi else EN_ESCALATION
        return template.format(support=support), True

    # Unclear questions receive a clarification instead of immediately escalating.
    return (HI_CLARIFY if hindi else EN_CLARIFY), False
