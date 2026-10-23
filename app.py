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
        batch, total = [], 0
        async for d in ctx.store.iter_films():
            batch.append((d["title"], d["year"], d["n"], d["hits"], tuple(d["aliases"])))
            if len(batch) >= 5000:
                total += await asyncio.to_thread(ctx.index.add_films, batch)
                batch = []
        if batch:
            total += await asyncio.to_thread(ctx.index.add_films, batch)
        ctx.banned = await ctx.store.banned_ids()
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
    handlers.register(client, ctx)

    runner = web.AppRunner(web_app(ctx, client))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", cfg.port).start()      # open the port first: Render waits for it

    await client.start()
    ctx.bot_username = (await client.get_me()).username
    log.info("started as @%s", ctx.bot_username)
    try:
        from pyrogram.types import BotCommand
        await client.set_bot_commands([
            BotCommand("start", "Home"),
            BotCommand("discover", "Browse lists"),
            BotCommand("mylist", "Your watchlist"),
            BotCommand("donate", "Support the bot"),
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
    tasks = [asyncio.create_task(boot()), asyncio.create_task(handlers.requests_job(ctx, client)),
             asyncio.create_task(handlers.sweeper(ctx, client)), asyncio.create_task(handlers.stats_job(ctx, client))]
    await idle()
    for t in tasks:
        t.cancel()
    try:
        await ctx.flush_stats()                       # don't lose the last minute of counters
    except Exception:
        pass
    await client.stop()
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
