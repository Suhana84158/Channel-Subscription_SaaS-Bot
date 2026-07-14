PATCH-03: Owner Razorpay Completion

REPLACE:
- handlers/payment_gateways.py
- services/payment_gateways.py
- keep_alive.py

FEATURES:
- Owner Razorpay credential connection test
- Seller plan checkout through owner's Razorpay account
- Secure Razorpay webhook signature verification
- Automatic seller plan activation
- Seller plan invoice generation
- Expiry and invoice shown in Telegram confirmation
- Duplicate webhook/payment processing protection remains enabled

Required webhook URL:
https://YOUR-SERVICE-NAME.onrender.com/webhooks/razorpay/owner/0

Recommended Razorpay events:
- payment.captured
- payment_link.paid
