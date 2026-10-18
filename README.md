# Movie bot

Runtime files (upload to GitHub, top level only; same names replace the old ones):
`app.py config.py ctx.py handlers.py index.py normalize.py parse.py store.py ui.py requirements.txt Dockerfile`.
`tests/` and `scripts/` are for checking the code; Render doesn't need them.

## Updating an existing deploy
Upload the files above over the old ones (Add file > Upload files, same names). Render redeploys by itself.
Then once: `/index` for each channel starting from 1 (`/index CHANNEL 1`) so files already saved get their
encoder, codec and language details filled in. After that `/dedupe`.

## Environment variables
See `.env.example`. New: `REQUESTS_CHANNEL` (optional). `LOG_CHANNEL` still works as an old name for it.

## Everyday commands (admins, private chat)
- `/index` resume every channel where it stopped; `/index CHANNEL` one channel; `/index CHANNEL 12345` start at message 12345
- `/indexstop`, `/stats`, `/settings`, `/dedupe`, `/skipped`, `/requests`, `/misses`
- `/customize` lists the look-and-feel commands: `/setwelcome /setabout /setimage /setlinks /setfooter /setpicks /preview`
- `/forcesub`, `/alias`, `/ban`, `/unban`, `/picks`
Keep UptimeRobot on `/health` before you index: Render puts a service to sleep after 15 minutes
without web traffic, and background indexing doesn't count as traffic.

## Smoke test after a deploy
1. `/start` shows the welcome with Discover / How it works / About.
2. Search a film: numbered versions with encoder, codec and size; tap one; the file arrives with its caption.
3. In a group, someone else taps a version from your search: it opens the bot and delivers to them.
4. Request something that doesn't exist; Report a problem under a file and pick a reason.
5. `/index` shows one message that updates itself with a Stop button.
