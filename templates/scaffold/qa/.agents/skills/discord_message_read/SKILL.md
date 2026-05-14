---
name: discord_message_read
description: Use inside the QA role when validation requires reading a specific Discord channel message or nearby message context through the Discord API, especially when a new or changed function ultimately outputs a Discord message.
---

# Discord Message Read Skill

Use this skill when QA needs direct evidence for a Discord message final output and has, or can recover, a `channel_id` and `message_id`.

## Read First

- `Current request.result`
- recent `Current request.events`
- `Current request.reply_route` when present
- `logs/discord/*.jsonl` for inbound/outbound transcript evidence
- designer, architect, and developer reports that describe expected Discord message behavior

## Workflow

1. Identify the message evidence target.
   Prefer `channel_id` and `message_id` from the request record, reply route, runtime events, Discord transcript logs, or the developer/architect report.
   For market-monitoring runs, inspect runner state for `last_discord_channel_id` plus `last_discord_message_ids` or `discord_message_ids` after delivery completes.
2. Use local transcript evidence first when it already contains the exact sent message.
   Transcript evidence is enough only when the message ID, channel ID, content, attachments, embeds/components metadata, and timestamp match the validation target.
3. Use the helper for live Discord evidence when direct channel/message verification is needed:

```bash
python ./.agents/skills/discord_message_read/scripts/read_discord_message.py --workspace-root . --channel-id <channel_id> --message-id <message_id>
```

4. Include nearby context only when the surrounding conversation affects QA judgment:

```bash
python ./.agents/skills/discord_message_read/scripts/read_discord_message.py --workspace-root . --channel-id <channel_id> --message-id <message_id> --around 5
```

5. Record what was actually observed in `proposals.qa_validation.evidence_matrix`.
   Cite whether evidence came from `discord_api`, `logs/discord`, or a local artifact.

## Guardrails

- This skill is read-only. Do not send, edit, delete, pin, react to, or otherwise mutate Discord messages.
- Do not print or expose Discord bot tokens. The helper reports the token environment variable name only.
- If the helper cannot read the message, mark the criterion `not_checked` and include the HTTP/status/error evidence in residual risks.
- If Discord returns an empty `content`, `embeds`, `attachments`, or `components` payload unexpectedly, treat message-content intent or permissions as missing evidence until confirmed.
- Use UX reopen for readability/order/design drift, and developer verification reopen for wrong data, escaping, truncation, field mapping, or send/render behavior.

## Token And Permission Notes

The helper resolves the QA bot token from `discord_agents_config.yaml` using the `qa` role's `token_env`, then falls back to `DISCORD_TOKEN`. The bot must have access to the target channel and Discord permissions required to view the channel and read message history.
