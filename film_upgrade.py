"""FilmBot v5 feature integration, kept separate from legacy handlers."""
from dataclasses import replace
from html import escape
import asyncio
import time
import re
from index import film_id
from normalize import normalize
from parse import parse_best, is_junk, SUB_EXTS
from film_rules import clean_rules, stored_rules
from ui import Btn


async def media_callback(ctx, client, cb, uid):
    from handlers import kb, HTML, missing_channels
    from pyrogram.enums import ChatType
    if cb.message.chat.type != ChatType.PRIVATE:
        await cb.answer('Open the file in your private chat with the bot.', show_alert=True)
        return
    rec = await ctx.store.get_file(uid)
    if not rec or (rec.ext or '').lower() in SUB_EXTS:
        await cb.answer('No indexed video is available for inspection.', show_alert=True)
        return
    if await missing_channels(ctx, client, cb.from_user.id):
        await cb.answer('Please join the required channels first.', show_alert=True)
        return
    from media_info import MediaInspector
    inspector = getattr(ctx, 'media_inspector', None)
    if inspector is None:
        inspector = ctx.media_inspector = MediaInspector()
    if inspector.busy:
        await cb.answer('Another inspection is running. Please try again shortly.', show_alert=True)
        return
    await cb.answer('Inspecting media information… up to 120 seconds.')
    _, report, _ = await inspector.inspect(client, cb.from_user.id, rec.ch, rec.mid, rec.name, rec.size)
    await client.send_message(cb.message.chat.id, report, parse_mode=HTML,
                              reply_markup=kb([[Btn('Close', f'miclose:{cb.from_user.id}')]]))


async def load_title_locks(ctx):
    locks, routes = {}, {}
    async for d in ctx.store.iter_title_locks():
        fid = d['fid']
        key = film_id(normalize(d['title']), d['year'])
        locks[fid] = (d['title'], d['year'])
        if key in routes and routes[key] != fid:
            raise ValueError('Conflicting manual film identities; resolve before indexing')
        routes[key] = fid
    ctx.title_locks, ctx.title_routes = locks, routes


def rules_screen(settings):
    r = clean_rules(settings.get('parser_rules'))
    return ('<b>FilmBot Parser Rules</b>\n'
            f'Custom release markers: {escape(", ".join(r["markers"][:12]) or "none")}\n'
            f'Disabled optional markers: {escape(", ".join(r["disabled"][:12]) or "none")}\n'
            f'Exact title exceptions: {len(r["exceptions"])}\n\n'
            '/parserrules add TOKEN · delete TOKEN\n'
            '/parserrules disable TOKEN · enable TOKEN\n'
            '/parserrules exception FILENAME | TITLE | YEAR (or -)\n'
            '/parserrules unexception FILENAME\n'
            '/parserrules exceptions\n'
            '/addpromo · /delpromo · /promos\n'
            '/testparse FILENAME\n'
            '/reparse dry — read-only impact preview\n'
            'Series exclusion remains controlled by the existing index_series setting.',
            [[Btn('Back to Settings', 'st:refresh')]])


async def admin_command(ctx, client, m, cmd, args, say):
    if cmd not in {'parserrules','testparse','settitle','setyear','locktitle','unlocktitle','reparsefile','reparsefilm','dbsize','catalogueaudit','duplicatepreview','filmids'}:
        return False
    if not ctx.is_admin(m.from_user.id):
        return True
    from handlers import _rebuild_search_index
    raw = ' '.join(args).strip()
    # Original message preserves spaces in exact filename exceptions.
    text = str(getattr(m, 'text', '') or '')
    if text and ' ' in text:
        raw = text.split(' ', 1)[1].strip()
    if cmd in ('parserrules', 'testparse'):
        async with ctx.catalogue_lock:
            cfg = await ctx.store.settings()
            if cmd == 'testparse':
                if not raw:
                    await say('Usage: /testparse FILENAME')
                else:
                    p = parse_best(raw, promos=cfg.get('custom_promos', []), rules=cfg.get('parser_rules'))
                    await say(f'<b>{escape(p.title or "Unknown")}</b> ({p.year or "unknown year"})\nKind: {p.kind}\nResolution: {p.resolution or "Unknown"}\nSource: {p.source or "Unknown"}\nGroup: {escape(p.group or "Unknown")}')
                return True
            r = clean_rules(cfg.get('parser_rules'))
            action, _, value = raw.partition(' ')
            action = action.lower()
            value = value.strip()
            if not action:
                await say(*rules_screen(cfg))
                return True
            if action == 'exceptions':
                lines = [f'{escape(k[:80])} → {escape(v[0][:80])} ({v[1] or "-"})' for k,v in list(r['exceptions'].items())[:10]]
                out = []
                for line in lines:
                    if sum(len(x)+1 for x in out) + len(line) > 3500:
                        break
                    out.append(line)
                await say('\n'.join(out) or 'No exceptions.')
                return True
            if action in ('add','delete','disable','enable') and re.fullmatch(r'[A-Za-z0-9]{1,40}', value):
                from parse import _OTHER_TECH
                value = value.lower()
                if action in ('disable','enable') and value not in _OTHER_TECH and value not in r['markers']:
                    await say('Only optional release markers may be disabled.'); return True
                key = 'markers' if action in ('add','delete') else 'disabled'
                if action in ('add','disable') and value not in r[key]:
                    r[key].append(value)
                if action in ('delete','enable') and value in r[key]:
                    r[key].remove(value)
                if action == 'delete' and value in r['disabled']:
                    r['disabled'].remove(value)
            elif action == 'exception':
                parts = [p.strip() for p in value.split('|')]
                if len(parts) != 3 or not 0 < len(parts[0]) <= 512 or not 0 < len(parts[1]) <= 160 or not normalize(parts[1]) or (parts[2] != '-' and not re.fullmatch(r'(?:18[89]\d|19\d\d|20\d\d|2100)', parts[2])):
                    await say('Usage: /parserrules exception FILENAME | TITLE | YEAR (or -)'); return True
                r['exceptions'][parts[0].casefold()] = [parts[1], None if parts[2] == '-' else int(parts[2])]
            elif action == 'unexception' and value:
                r['exceptions'].pop(value.casefold(), None)
            else:
                await say(*rules_screen(cfg)); return True
            if any(len(r[k]) > 100 for k in r):
                await say('Rule limit reached (100 per category).'); return True
            await ctx.set_setting('parser_rules', stored_rules(r))
            await say('Saved. New parsing uses these rules; existing records were not changed.')
        return True
    if cmd == 'filmids':
        if not raw:
            await say('Usage: /filmids FILM_ID or film title/year'); return True
        f = ctx.index.get(raw)
        if not f:
            hits = (await asyncio.to_thread(ctx.index.search,raw)).hits
            if len(hits) != 1:
                await say('Select a film ID:\n'+'\n'.join(f'<code>{h.film.id}</code> {escape(h.film.label)}' for h in hits[:10])); return True
            f = hits[0].film
        recs = await ctx.store.film_files(f.id)
        await say(f'<b>{escape(f.label)}</b> · <code>{f.id}</code> · {len(recs)} files\n'+'\n'.join(f'<code>{escape(r.uid)}</code> {escape(r.name[:70])}' for r in recs[:12]))
        return True
    if cmd in ('dbsize','catalogueaudit','duplicatepreview'):
        if cmd == 'dbsize':
            stats = await ctx.store.storage_report()
            await say(f'<b>Application database storage</b>\n{escape(stats)}\nAtlas quota is cluster-wide, not per database.')
        else:
            if ctx.maintenance or ctx.index_job:
                await say('Please stop other maintenance jobs first.'); return True
            ctx.maintenance = 'audit'
            try:
                counts = await ctx.store.counts()
                skips = await ctx.store.skip_counts()
                rows = await ctx.store.lookalikes(21)
                lines = [f'<b>Read-only {cmd}</b>', f'Files: {counts["files"]:,} · Films: {counts["films"]:,}', f'Skips: {escape(str(skips))}', f'Look-alikes: {len(rows) if len(rows)<21 else "at least 21"} (bounded sample)']
                if cmd == 'catalogueaudit':
                    lines += await identity_audit(ctx)
                lines += [f'{escape(str(r["uid"]))} → {escape(str(r["keep"]))}' for r in rows[:12]]
                await say('\n'.join(lines))
            finally:
                ctx.maintenance = None
        return True
    if not ctx.ready or ctx.index_job or ctx.maintenance:
        await say('Wait until startup or maintenance finishes.'); return True
    async with ctx.catalogue_lock:
        if ctx.index_job or ctx.maintenance:
            await say('Another job is running.'); return True
        if cmd in ('settitle','setyear','locktitle','unlocktitle'):
            fid, _, value = raw.partition(' ')
            d = await ctx.store.get_film(fid)
            if not d:
                await say(f'Usage: /{cmd} FILM_ID' + (' NEW_VALUE' if cmd in ('settitle','setyear') else '')); return True
            title, year = d['title'], d['year']
            if cmd == 'settitle':
                if not normalize(value) or len(value) > 160:
                    await say('Provide a valid title (1–160 characters).'); return True
                title = value
            if cmd == 'setyear':
                if not re.fullmatch(r'(?:18[89]\d|19\d\d|20\d\d|2100|-)', value):
                    await say('Provide a valid year or - to clear it.'); return True
                year = None if value == '-' else int(value)
            key = film_id(normalize(title), year)
            if cmd != 'unlocktitle' and key != fid and (await ctx.store.get_film(key) or ctx.title_routes.get(key, fid) != fid):
                await say('That film identity already exists. No automatic merge was performed.'); return True
            if cmd == 'unlocktitle' and key != fid:
                await say('This film has a manual identity. Restore its original title/year before unlocking to avoid splitting the catalogue.'); return True
            await ctx.store.set_title_lock(fid, title, year, cmd != 'unlocktitle')
            await load_title_locks(ctx)
            await asyncio.to_thread(ctx.index.refresh_film,fid,title,year,d["n"],d["hits"],d["aliases"])
            ctx.variant_cache.invalidate(fid)
            await say(f'Saved: {escape(title)} ({year or "-"}) · ID {fid} · {"unlocked" if cmd == "unlocktitle" else "locked"}')
            return True
        ident, _, mode = raw.partition(' ')
        dry = mode.strip().lower() != 'apply'
        if cmd == 'reparsefile':
            old = await ctx.store.get_file(ident)
            records = [old] if old else []
        else:
            records = await ctx.store.film_files(ident)
        if not records:
            await say(f'Usage: /{cmd} ID [dry|apply] (default dry)'); return True
        if len(records) > 500:
            await say('More than 500 records; use individual file reparsing.'); return True
        cfg = await ctx.store.settings()
        changes = []
        duplicate_conflicts = 0
        locked_count = 0
        for old in records:
            if old.film in ctx.title_locks:
                locked_count += 1
                continue
            # Prefer full Telegram filename for targeted operations, never download video.
            try:
                msg = await client.get_messages(old.ch, old.mid)
                media = getattr(msg,'document',None) or getattr(msg,'video',None)
                name = getattr(media,'file_name',None) or old.name
            except Exception:
                name = old.name
            p = parse_best(name, promos=cfg.get('custom_promos',[]), rules=cfg.get('parser_rules'))
            if is_junk(p.title) or not normalize(p.title) or (p.kind == 'series' and not cfg.get('index_series')):
                continue
            key = film_id(normalize(p.title),p.year)
            fid = ctx.title_routes.get(key,key)
            title, year = ctx.title_locks.get(fid,(p.title,p.year))
            rec = replace(old,film=fid,name=name[:255],res=p.resolution,src=p.source,langs=p.languages,codec=p.codec,grp=p.group,tags=p.tags,ext=p.ext or old.ext)
            if rec != old:
                if rec.film != old.film and await ctx.store.existing_lookalike(rec):
                    duplicate_conflicts += 1
                    continue
                changes.append((rec,title,year))
        if not dry:
            applied = []
            touched = set()
            for rec,title,year in changes:
                prior = await ctx.store.get_file(rec.uid)
                if prior and prior.film != rec.film and await ctx.store.existing_lookalike(rec):
                    duplicate_conflicts += 1
                    continue
                previous = await ctx.store.reparse_file(rec,title,year)
                touched.update([rec.film,previous])
                applied.append((rec,title,year))
            changes = applied
            for fid in touched-{None}:
                d = await ctx.store.get_film(fid)
                ctx.variant_cache.invalidate(fid)
                if d:
                    await asyncio.to_thread(ctx.index.refresh_film,fid,d['title'],d['year'],d['n'],d['hits'],d['aliases'])
                else:
                    await asyncio.to_thread(ctx.index.remove_film,fid)
        lines = [f'{"Dry run: no changes" if dry else "Applied"} · {len(changes)} of {len(records)} records',f'Locked: {locked_count} · Possible duplicates left untouched: {duplicate_conflicts}']
        lines += [f'{escape(r.uid)} → {escape(t)} ({y or "-"})' for r,t,y in changes[:15]]
        await say('\n'.join(lines))
    return True


async def auto_index_worker(ctx):
    from handlers import index_messages, log
    while not ctx.ready:
        await asyncio.sleep(1)
    while True:
        first = await ctx.auto_queue.get()
        batch = [first]
        # Small window coalesces bursts without retaining hundreds of video objects.
        await asyncio.sleep(.2)
        while len(batch) < 25 and not ctx.auto_queue.empty():
            batch.append(ctx.auto_queue.get_nowait())
        try:
            while True:
                try:
                    result = await index_messages(ctx, batch)
                    log.info('auto-indexed films batch=%d added=%d known=%d duplicates=%d queue=%d', len(batch),result.added,result.known,result.lookalike,ctx.auto_queue.qsize())
                    break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception('automatic indexing failed; retaining batch for retry')
                    await asyncio.sleep(10)
        finally:
            for _ in batch:
                ctx.auto_queue.task_done()


async def catchup_channel(ctx, client, ch, max_ids=1000):
    """Read a bounded overlapping range of IDs. Never scan unconfigured channels."""
    from handlers import index_messages
    if ch not in ctx.cfg.file_channels:
        return 0
    cfg = await ctx.store.settings()
    cursor = int(cfg.get(f'catchup_{ch}') or 0)
    if not cursor:
        cursor = ctx.catchup_initial.get(ch,0) or await ctx.store.latest_indexed_id(ch)
    if not cursor:  # don't start a huge history scan on a brand new catalogue
        return 0
    start = max(1,cursor-99)
    highest = cursor
    upper = ctx.catchup_high.get(ch,0)
    recovered = 0
    empty = 0
    for pos in range(start,start+max_ids,200):
        if ctx.maintenance or ctx.index_job:
            break
        ids = list(range(pos,min(pos+200,start+max_ids)))
        messages = await asyncio.wait_for(client.get_messages(ch, ids), timeout=25)
        live = [m for m in messages if m and not getattr(m,'empty',False)]
        if live:
            empty = 0
            from handlers import _media_of
            candidates = []
            for m in live:
                media,_ = _media_of(m)
                if media:
                    candidates.append((m,media.file_unique_id))
            known = await ctx.store.seen_file_ids([u for _,u in candidates])
            unseen = [m for m,u in candidates if u not in known]
            if unseen:
                result = await index_messages(ctx,unseen)
                recovered += result.added
            highest = max(highest,max(m.id for m in live))
            await ctx.store.set_setting(f'catchup_{ch}',highest)
        else:
            empty += 1
            if upper >= ids[-1]:
                highest = max(highest,ids[-1])
                await ctx.store.set_setting(f"catchup_{ch}",highest)
            elif empty >= 2:
                break
        await asyncio.sleep(.2)
    return recovered


async def catchup_worker(ctx,client):
    from handlers import log
    while not ctx.ready:
        await asyncio.sleep(5)
    await asyncio.sleep(120)
    while True:
        if not ctx.maintenance and not ctx.index_job and ctx.auto_queue.empty():
            for ch in sorted(ctx.cfg.file_channels):
                try:
                    n = await catchup_channel(ctx,client,ch)
                    log.info('film catch-up channel=%s recovered=%d',ch,n)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception('film catch-up failed channel=%s',ch)
        await asyncio.sleep(300)


async def identity_audit(ctx):
    """Bounded identity and orphan sampling; not a claimed exhaustive reconciliation."""
    aliases = {}
    conflicts = invalid = n = 0
    async for d in ctx.store.iter_films():
        n += 1
        if not normalize(d['title']):
            invalid += 1
        for alias in [d['title'], *d['aliases']]:
            key = (normalize(alias), d['year'])
            if key[0] and key in aliases and aliases[key] != d['fid']:
                conflicts += 1
            elif key[0]:
                aliases[key] = d['fid']
        if n >= 5000:
            break
    sampled = orphan = 0
    async for batch in ctx.store.iter_file_batches(500):
        fids = {r.film for r in batch}
        found = await ctx.store.get_films_by_ids(list(fids))
        sampled = len(batch)
        orphan = sum(r.film not in found for r in batch)
        break
    return [f'Identity sample ({n:,} films): {conflicts} overlapping title/alias entries; {invalid} empty titles.',
            f'File sample ({sampled} named records): {orphan} orphaned records.',
            f'Manual title locks loaded: {len(ctx.title_locks)}. No records changed.']
