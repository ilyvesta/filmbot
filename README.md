# Movie bot (v4)

Runtime files (upload to GitHub, top level only; same names replace the old ones):
`app.py config.py ctx.py handlers.py index.py normalize.py parse.py payload.py store.py ui.py requirements.txt Dockerfile`.
`payload.py` is new in v4.
`tests/` and `scripts/` are for checking the code; Render doesn't need them.

## Environment variables (unchanged)
See `.env.example`. `REQUESTS_CHANNEL` is optional (empty = requests and reports DM your admins).
Force-join channels are NOT environment variables: set them inside the bot with `/forcesub`.

## Admin commands (private chat with the bot)
- `/index` resume every channel; `/index CHANNEL` one channel; `/index CHANNEL 12345` start at message 12345; `/indexstop`
- `/skipped` menu: Unreadable titles, Series, Look-alikes (paged, each with a link to the original post, plus a .txt export)
- `/dedupe` removes extra copies of the same film with exactly the same size, in rounds of 50,000
- `/forcesub add CHANNEL [invite link]`, `/forcesub remove CHANNEL`, `/forcesub list`, `/forcesub off` (up to 4 channels)
- `/purgeseries` counts series episodes saved as films and shows 10 names; deletes only after you confirm, in small batches
- `/setseries @username` sets the series bot; `/setsummary 08:00` (Lagos time) or `/setsummary off` for the daily summary
- `/stats /settings /requests /misses /alias /ban /unban /picks /customize /preview`

Keep UptimeRobot on `/health` before you index: Render puts a service to sleep after 15 minutes without web
traffic, and background indexing doesn't count as traffic.
