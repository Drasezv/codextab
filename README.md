# codextab

Telegram notifications when a Codex task finishes, with token usage, estimated API cost, and account limits. The message layout follows cctab.

## Install

On macOS with Codex and Python 3.10 or newer:

```sh
mkdir -p ~/.agents/skills
git clone https://github.com/drasezv/codextab.git ~/.agents/skills/codextab
```

Open Codex and ask it to set up Codextab. Confirm bot creation in Telegram. Restart Codex and review the Codextab hooks under `/hooks` in the Codex CLI.

## Settings from the phone

Use `/settings` to choose alerts and set the quiet threshold. The default is 30 minutes. Select **Everything** to receive short task completions too. Use `/spend` for estimated API costs and `/limits` for account limits.

Remote approvals and answer excerpts are off by default. Local history and bot credentials live in `~/.codextab`.

## License

MIT.
