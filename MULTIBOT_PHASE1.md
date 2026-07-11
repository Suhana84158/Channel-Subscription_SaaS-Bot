# Multi-Bot SaaS — Phase 1

This build adds the first working foundation for the planned clone/SaaS system.

## Included

- `/seller` dashboard
- automatic seller registration
- one seller = one connected bot
- BotFather token verification through Telegram `getMe`
- encrypted token storage using `SECRET_KEY`
- duplicate bot registration protection
- view connected bot details without exposing token
- pause/resume seller bot record
- remove/replace bot token
- MongoDB indexes for seller and bot collections

## Required Render environment variable

Set a strong `SECRET_KEY`. Do not change it after sellers add tokens, otherwise old encrypted tokens cannot be decrypted.

Example generation locally:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

## Not included yet

The connected child bot is registered securely but is not yet launched as a separate polling/webhook runtime. Per-bot channels, users, payments, settings and subscriptions will be added in later phases.
