# Codextab implementation notes

## Codex event mapping

| Codex hook | Codextab work |
| --- | --- |
| `UserPromptSubmit` | Record the start time by session and turn id. Prompt text is discarded. |
| `PostModelRequest` | Aggregate token usage and the last model request status for the turn. Retries may contribute token counts. |
| `Stop` | Finalize the turn, classify failures from the last model request, estimate duration and notify if configured. |
| `Interrupt` | Mark the turn interrupted and notify if failures are enabled. |
| `PermissionRequest` | Notify and defer to normal Codex approval by default. When remote approvals are enabled, wait for a one time Telegram callback and return only the matching allow or deny decision. |

Codex hooks expose a `Stop` event but no equivalent to Claude's `Notification` hook. Question alerts are therefore a conservative text heuristic. Codex transcript JSONL is used only for historical `/spend`; its format can change.

## Usage numbers

`PostModelRequest` provides token usage and status when available. The estimate applies public API token rates and cache discounts to those tokens, so it is a comparison figure, not an invoice. Unknown model pricing is shown as unavailable. The message labels it as API equivalent.

The error alert reports the last model request status and interruptions. Codex does not expose a general task failure event through these hooks, so a failed shell command followed by a normal assistant response cannot always be classified as a failed task.

Rate limits come from `account/rateLimits/read` through a short lived local `codex app-server` process and are cached for five minutes. This relies on Codex's own authentication and does not read OAuth files. The Telegram bot also has `/limits` for a forced refresh.

## Telegram message layout

Proactive Codextab messages follow the cctab card layout: an emoji and compact uppercase heading, an italic project or context label, a short quoted detail or usage block, then model and duration. Turn messages show token and API-equivalent estimates plus available limit bars; failures show redacted error text, and answer excerpts remain optional. Permission, interruption, exhausted-limit, spend, help, and connection messages use the same visual hierarchy. Keep Codex-specific wording and metrics where its hooks differ from Claude Code.

## Telegram receiver and local state

The Codextab bot uses long polling for `/settings`, `/spend`, `/limits`, and approval buttons. It must be its own bot because `cctab` already consumes updates from its bot. A file lock prevents two Codextab receiver processes from polling simultaneously. Telegram update offsets, per turn usage, preferences, and expiring approvals are stored in SQLite under `~/.codextab` with owner only permissions.

The setup script writes a macOS LaunchAgent and atomically merges hook entries into the existing Codex hooks file. Existing non Codextab hook entries are preserved. Uninstall removes only entries that point to this Codextab script and leaves the database and bot token for explicit cleanup.
