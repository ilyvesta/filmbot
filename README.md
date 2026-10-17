# Movie bot

Runtime files (upload these to GitHub, top level only): `app.py config.py ctx.py handlers.py index.py
normalize.py parse.py store.py ui.py requirements.txt Dockerfile`.
`tests/` and `scripts/` are for checking the code; you don't need them on Render.

## Deploy (phone-friendly)
1. GitHub: new **private** repository, then Add file > Upload files, choose the runtime files above.
2. Render: New > Web Service > pick the repo. Runtime **Docker**, instance type **Free**.
   Health Check Path: `/health`. Add the environment variables from `.env.example`.
3. UptimeRobot: HTTP monitor on `https://YOUR-SERVICE.onrender.com/health`, every 5 minutes.
4. Telegram: add the bot as an **admin** in both file channels, and in your force-join channel.
5. First run: send `/start`, then `/stats`. Then `/index CHANNEL_ID LAST_MESSAGE_ID` for each channel
   (the last id is the number at the end of the newest post's link).

## Admin commands (private chat with the bot, admins only)
`/stats` `/settings` `/forcesub` `/index` `/indexstop` `/requests` `/misses` `/alias` `/ban` `/unban`

## First-deploy smoke test
- `/stats` answers and shows Files/Films.
- Search a film you know exists; tap a version; the file arrives.
- Search it in a group; tapping a version opens the bot and delivers it.
- Search something nonsense; Request button reaches you.
- `/forcesub CHANNEL_ID` then search from a second account that hasn't joined.
