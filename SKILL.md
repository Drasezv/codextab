---
name: codextab
description: Use when setting up or troubleshooting Codextab, Telegram notifications, limits, usage estimates, or remote permission approvals for Codex.
---

# Codextab

Codextab brings the useful Claude Code Tab workflows to Codex. Its local runtime supports long turn and failure notifications, token and API price estimates, account rate limits, a Telegram settings menu, `/spend`, `/limits`, question hints, and optional yes or no permission approvals.

## Setup

- Codextab uses the existing `cctab-manager` to create a managed Telegram bot. Do not send the user to BotFather or ask them to copy a token unless the manager is unreachable.
- When the user asks to install or connect Codextab, first run `python3 ~/.agents/skills/codextab/scripts/codextab.py status`.
- If status prints `NO_BOT`, ask one question and wait: would they like a clickable link or a QR code? Then run `python3 ~/.agents/skills/codextab/scripts/codextab.py link` or add `--qr` for QR. Send the printed `LINK` as a bare URL on its own line. For QR, say the code is open on their screen. Explain that Telegram opens a confirmation window with a generated username and the Codextab name already filled in, and they only need to confirm it.
- After sending the link, in the same turn run `python3 ~/.agents/skills/codextab/scripts/codextab.py wait` and wait up to 180 seconds. The manager returns the token directly to the local setup without exposing it. Setup automatically applies the Codextab display name, descriptions, commands, and avatar, greets the owner, installs Codex hooks, and starts the macOS LaunchAgent.
- Do not stop after sending the link without running `wait`. If it prints `EXPIRED`, say the link timed out and a fresh one can be generated. If it prints `UNREACHABLE`, show the fallback steps printed by the setup tool. Never put a bot token in chat, command arguments, or logs.
- The manager reserves usernames in its existing `cctab_<random>_bot` format. Codextab changes the visible display name and avatar automatically after creation.
- Restart Codex once after setup so it loads the hooks. After that, notifications and Telegram controls work in the background without manual commands.
- To begin setup, use the `link` then `wait` commands. Do not tell the user to run them manually.
- The user must open the printed private bot link and press Start to pair the chat. The receiver accepts only that private chat and its pairing owner.
- Check status with `python3 ~/.agents/skills/codextab/scripts/codextab.py status`. Preview the merged hook config with `python3 ~/.agents/skills/codextab/scripts/codextab.py hooks-preview`.
- Uninstall hooks and the LaunchAgent with `python3 ~/.agents/skills/codextab/scripts/codextab.py uninstall`. This leaves `~/.codextab` data intact. Delete that directory only if the user asks to remove the token and history too.

## Behavior and privacy

- Telegram settings use the same tabbed interaction as cctab: Time, Messages, and Spend, with inline toggles, threshold presets, custom minutes, refresh, and language choices. Keep the layout and interaction consistent when extending the bot.
- Default done threshold is 30 minutes. Failures, interruptions, likely direct questions, and exhausted limits can notify sooner.
- Telegram messages contain the project folder, model, duration, token counts, an API price comparison, and redacted error text. Full answer excerpts are off by default. A detected short question may be sent so the alert is useful. Telegram chats are not end to end encrypted.
- `/settings` has inline controls and text commands. `/spend` reads local Codex session records and estimates the equivalent public API price for models with known rates. Unknown models show no price instead of a guessed amount. It is not ChatGPT plan billing. Historical usage reads Codex JSONL metadata only; Codex does not promise that transcript format as a stable interface.
- `/limits` and limit updates use the local Codex app server. They depend on ChatGPT account authentication. API key only setups may not expose plan limits.
- Codex does not provide the same dedicated question notification event as Claude. Question alerts are a text heuristic, and error alerts report the last model request status rather than proving the entire task failed. Disable question alerts if they misclassify ordinary replies.
- Remote approvals are off by default. Turning them on lets a Telegram button allow or deny the exact permission request shown, with a one time nonce and a 60 second timeout. Telegram remains an approval surface, so warn the user before enabling it.
- Do not run setup, install hooks, pair a bot, enable approvals, or send Telegram messages unless the user explicitly asks for that action. Never copy the Claude token or alter Claude's files.
