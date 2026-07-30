# Telegram Delivery Configuration

## Minimal Working Config

In `~/.hermes/config.yaml` or `~/.hermes/profiles/<profile>/config.yaml`:

```yaml
telegram:
  enabled: true
  reactions: false
  allowed_chats: '<chat-id>'
  extra:
    rich_messages: true
```

In `~/.hermes/profiles/<profile>/.env`:
```
TELEGRAM_BOT_TOKEN=<bot-token>
```

## Check Current State

```bash
hermes config get telegram.enabled
hermes config get telegram.allowed_chats
```

If `enabled` is missing or `false`, or `allowed_chats` is empty → notifications silently dropped.

## Fix Checklist

1. `hermes config set telegram.enabled true`
2. `hermes config set telegram.allowed_chats <chat-id>`
3. `systemctl --user restart hermes-gateway` (DO NOT use `hermes gateway restart` — it times out)

## Cron Job Delivery

Cron jobs created with `deliver: 'local'` save to a log file only — the user never sees the output. Always use:
```python
cronjob(..., deliver='telegram')
```
Or `deliver='all'` to fan out to every connected channel.

## Diagnostics

If a cron job's `last_delivery_error` field shows messages about thread_id being not found, the delivery still falls through to send without a thread — the message is delivered to the main chat. The error is cosmetic.

If `last_delivery_error` contains other errors, check:
- Gateway is running: `systemctl --user is-active hermes-gateway`
- Config has `telegram.enabled: true`
- `allowed_chats` matches the actual chat ID (not an @username)
- Bot token is valid in the .env file