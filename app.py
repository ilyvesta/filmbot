"""Entry point: python app.py"""
import asyncio
import logging

from aiohttp import web

from config import ConfigError, load
from ctx import Ctx
from index import FilmIndex
from store import MongoStore

log = logging.getLogger("moviebot")


async def load_index(ctx: Ctx) -> None:
    """Pull every film from Mongo into the search index, then open for business."""
    try:
        from film_upgrade import load_title_locks
        await load_title_locks(ctx)
        batch, total = [], 0
        async for d in ctx.store.iter_films():
            if not d["n"]:
                continue
            batch.append((d["title"], d["year"], d["n"], d["hits"], tuple(d["aliases"]), d["fid"]))
            if len(batch) >= 5000:
                total += await asyncio.to_thread(ctx.index.add_films, batch)
                batch = []
        if batch:
            total += await asyncio.to_thread(ctx.index.add_films, batch)
        ctx.banned = await ctx.store.banned_ids()
        # Snapshot the old catalogue before releasing queued live posts.
        settings = await ctx.store.settings()
        for ch in ctx.cfg.file_channels:
            ctx.catchup_initial[ch] = int(settings.get(f"catchup_{ch}") or await ctx.store.latest_indexed_id(ch))
        ctx.ready = True
        log.info("index ready: %s films", total)
    except Exception as e:
        ctx.load_error = str(e)
        log.exception("index load failed")


def web_app(ctx: Ctx, client) -> web.Application:
    cache = {"t": 0.0, "ok": True}

    async def health(_):
        now = asyncio.get_event_loop().time()
        if now - cache["t"] > 15:
            cache["ok"], cache["t"] = await ctx.store.ping(), now
        body = {"ready": ctx.ready, "telegram": client.is_connected, "mongo": cache["ok"],
                "films": len(ctx.index) if ctx.ready else 0, "error": ctx.load_error}
        # 200 while still loading so Render doesn't restart us; 503 only once something is truly down
        bad = ctx.load_error or (ctx.ready and not (client.is_connected and cache["ok"]))
        return web.json_response(body, status=503 if bad else 200)

    app = web.Application()
    app.add_routes([web.get("/", lambda _: web.Response(text="ok")), web.get("/health", health),
                    web.head("/", lambda _: web.Response())])
    return app


async def heartbeat(ctx, client):
    from handlers import rss_mb
    loop = asyncio.get_running_loop()
    while True:
        start = loop.time()
        await asyncio.sleep(60)
        log.info("heartbeat: mem=%sMB loop_lag=%.2fs ready=%s telegram=%s queue=%s indexing=%s maintenance=%s",
                 rss_mb(), max(0, loop.time()-start-60), ctx.ready, client.is_connected, ctx.auto_queue.qsize() if ctx.auto_queue is not None else 0, bool(ctx.index_job), ctx.maintenance)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = load()
    except ConfigError as e:
        raise SystemExit(str(e))
    from pyrogram import Client, idle
    import handlers

    store = MongoStore(cfg.mongo_uri, cfg.db_name)
    await store.setup()
    ctx = Ctx(cfg, store, FilmIndex())
    client = Client("moviebot", api_id=cfg.api_id, api_hash=cfg.api_hash, bot_token=cfg.bot_token,
                    in_memory=True)
    ctx.auto_queue = asyncio.Queue(maxsize=200)
    handlers.register(client, ctx)

    runner = web.AppRunner(web_app(ctx, client))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", cfg.port).start()      # open the port first: Render waits for it

    await client.start()
    bot = await client.get_me()
    ctx.bot_username = bot.username
    ctx.bot_id = bot.id
    log.info("started as @%s", ctx.bot_username)
    try:
        from pyrogram.types import BotCommand
        await client.set_bot_commands([
            BotCommand("start", "Home"),
            BotCommand("discover", "Browse lists"),
            BotCommand("mylist", "Your watchlist"),
            BotCommand("donate", "Support the bot"),
            BotCommand("supporters", "Supporters Wall"),
            BotCommand("request", "Supporter custom requests"),
            BotCommand("donationclaim", "My donation claim"),
            BotCommand("survey", "Community survey"),
            BotCommand("help", "How it works"),
            BotCommand("about", "About this bot"),
        ])
    except Exception:
        log.warning("couldn't set the command menu", exc_info=True)

    async def boot() -> None:
        await load_index(ctx)
        if ctx.ready:
            await handlers.resume_index(ctx, client)
    from film_upgrade import auto_index_worker, catchup_worker
    tasks = [asyncio.create_task(boot()), asyncio.create_task(handlers.requests_job(ctx, client)),
             asyncio.create_task(handlers.sweeper(ctx, client)), asyncio.create_task(handlers.stats_job(ctx, client)),
             asyncio.create_task(heartbeat(ctx, client)), asyncio.create_task(auto_index_worker(ctx)),
             asyncio.create_task(catchup_worker(ctx, client))]
    await idle()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    try:
        await ctx.flush_stats()                       # don't lose the last minute of counters
    except Exception:
        pass
    await client.stop()
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
