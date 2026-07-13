# Payment Gateway Setup

This patch adds automatic payments for both levels:

- **Owner gateways:** sellers buy/upgrade SaaS plans.
- **Seller gateways:** child-bot users buy subscriptions; money goes to that seller's configured merchant account.

Supported gateways:

- Razorpay
- Cashfree Payments
- PhonePe Payment Gateway
- Paytm Payment Gateway
- Manual UPI screenshot fallback

## Required Render environment variables

```env
PUBLIC_BASE_URL=https://YOUR-RENDER-SERVICE.onrender.com
SECRET_KEY=USE-A-LONG-RANDOM-SECRET-AND-NEVER-CHANGE-IT
```

`SECRET_KEY` encrypts stored merchant secrets. Changing it later makes previously saved gateway secrets unreadable.

## Owner setup

Open:

`Owner Dashboard -> Subscription Management -> Payment Setting -> Automatic Payment Gateways`

## Seller setup

Open:

`Seller Dashboard -> Child Bot Payment Gateways`

The same gateway settings are also available inside the child bot:

`/admin -> Payment Settings -> Automatic Gateways`

## Credential formats

```text
Razorpay:
KEY_ID | KEY_SECRET | WEBHOOK_SECRET

Cashfree:
CLIENT_ID | CLIENT_SECRET

PhonePe:
CLIENT_ID | CLIENT_VERSION | CLIENT_SECRET | WEBHOOK_USERNAME | WEBHOOK_PASSWORD

Paytm:
MID | MERCHANT_KEY | WEBSITE_NAME
```

## Webhook URLs

Replace the domain with `PUBLIC_BASE_URL`.

Owner merchant account:

```text
https://YOUR-DOMAIN/webhooks/razorpay/owner/0
https://YOUR-DOMAIN/webhooks/cashfree/owner/0
https://YOUR-DOMAIN/webhooks/phonepe/owner/0
https://YOUR-DOMAIN/webhooks/paytm/owner/0
```

Seller merchant account, where `SELLER_TELEGRAM_ID` is the seller's numeric Telegram ID:

```text
https://YOUR-DOMAIN/webhooks/razorpay/seller/SELLER_TELEGRAM_ID
https://YOUR-DOMAIN/webhooks/cashfree/seller/SELLER_TELEGRAM_ID
https://YOUR-DOMAIN/webhooks/phonepe/seller/SELLER_TELEGRAM_ID
https://YOUR-DOMAIN/webhooks/paytm/seller/SELLER_TELEGRAM_ID
```

Cashfree's notify URL and Paytm callback URL are also included automatically while creating each transaction. Razorpay and PhonePe webhooks must be configured in their merchant dashboards.

## Test first

1. Set each gateway to **Test Mode**.
2. Enable only one gateway.
3. Make one seller-plan test payment.
4. Make one child-bot subscription test payment.
5. Confirm transaction status changes to `fulfilled` in Gateway History.
6. Confirm plan/subscription activates only after a verified webhook.
7. Switch to Live Mode only after merchant KYC and production credentials are approved.
