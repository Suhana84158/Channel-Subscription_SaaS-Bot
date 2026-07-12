# SaaS Child Bot Feature Matrix (RC1)

## Integrated in child bots
- Seller registration and encrypted token storage
- Child-bot runtime start/stop/restore
- Seller-isolated settings, plans, channels, users, payments, subscriptions and referrals
- Plans add/edit/delete/enable-disable
- Channel/group add/list/remove, including manual private-group ID fallback
- UPI ID, UPI name and QR settings
- Bot name, welcome, support, currency, timezone and reminder settings
- User plan display, buy and renew selection
- Payment screenshot submission
- Pending payments, approve/reject and payment history
- Subscription activation/extension
- One-use invite links to connected chats
- Expiry worker and removal from connected chats
- Profile, referral and support-message flow
- Seller broadcast and statistics

## Preserved from the original main bot project
All original source files remain in the project. The SaaS control bot and child-bot runtime are added without deleting the original handlers/database/services.

## Requires live Telegram testing
- Bot administrator rights in every connected channel/group
- Private-group forwarding behavior (Telegram privacy settings can hide origin)
- Invite-link creation permissions
- Ban/unban permissions for expiry removal
- Payment approval and renewal edge cases
- Multiple child bots running concurrently on the selected Render plan

## Not claimed as fully production-complete in this RC
- Automatic payment-provider verification
- Full coupon UI parity in child bots
- Full multi-admin UI parity in child bots
- Web dashboard parity
- High-scale load testing
