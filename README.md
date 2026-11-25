# Exivum FilmBot v5.2

Runtime files (upload to GitHub, top level only; same names replace the old ones):
`app.py config.py ctx.py handlers.py index.py normalize.py parse.py payload.py store.py ui.py film_rules.py film_upgrade.py media_info.py requirements.txt Dockerfile`.
The Dockerfile includes FFmpeg/FFprobe. Use the existing bot environment and database. No new environment variables or collections are required.
The top-level `test_*.py` files, `stub_pyrogram.py`, and `scripts/` are local regression tooling.

## Environment variables (unchanged)
Use the existing deployment environment: API_ID, API_HASH, BOT_TOKEN, ADMINS, FILE_CHANNELS, DATABASE_URI, and optional DATABASE_NAME, REQUESTS_CHANNEL and PORT. `REQUESTS_CHANNEL` is optional (empty = requests and reports DM your admins).
Force-join channels are NOT environment variables: set them inside the bot with `/forcesub`.

## Admin commands (private chat with the bot)
- `/index` resume every channel; `/index CHANNEL` one channel; `/index CHANNEL 12345` start at message 12345; `/indexstop`
- `/skipped` menu: Unreadable titles, Series, Look-alikes (paged, each with a link to the original post, plus a .txt export)
- `/dedupe` removes extra copies of the same film with exactly the same size, in rounds of 50,000
- `/forcesub add CHANNEL [invite link]`, `/forcesub remove CHANNEL`, `/forcesub list`, `/forcesub off` (up to 4 channels)
- `/purgeseries` counts series episodes saved as films and shows 10 names; deletes only after you confirm, in small batches
- `/setseries @username` sets the series bot; `/setsummary 08:00` (Lagos time) or `/setsummary off` for the daily summary
- `/stats /settings /requests /misses /alias /ban /unban /picks /customize /preview`
- `/reparse` and `/reparse dry` reprocess the indexed catalogue using 400-record batches and MongoDB bulk writes; film counts are reconciled once at the end. `/reparse skipped` rechecks unreadable channel posts (this mode necessarily fetches Telegram messages).
- Underscore-containing leading `@prefixes` are not automatically stripped. Register confirmed promo prefixes with `/addpromo` first; `/underscoreaudit [limit] [offset]` exports indexed filenames containing underscores for manual review (up to 50,000 per export). Use `/reparse dry` to inspect the proposed changes before a full `/reparse`.

Keep UptimeRobot on `/health` before you index: Render puts a service to sleep after 15 minutes without web
traffic, and background indexing doesn't count as traffic.

## Recent fixes: Stars payments, donation claims, and group searches

The payment/spam-fixes build updates `handlers.py`, `store.py`, and `ui.py`.

- **Stars payments:** records Telegram's charge ID so a redelivered payment is not processed twice; records Stars totals and payment counts for `/stats` and the daily summary; sends admins a private notification after a payment is processed.
- **Donation claims:** adds a 24-hour cooldown after a claim is declined. Admins can recover pending claims with `/claims`, decline with `/claim decline CLAIM_ID`, or approve with `/claim approve CLAIM_ID DAYS [bypass]`. The `bypass` argument explicitly requests force-join bypass.
- **Group search:** a group miss is silent and excluded from search/miss counters; the cleaned-query retry is skipped for misses in groups to reduce unnecessary indexing work. Successful group lookups remain available.

After deploying, restart the bot. These changes do not retroactively reconstruct Stars statistics for payments processed before charge-ID tracking was added.


## v5: deployment and new controls

Deploy the complete project with the Dockerfile (Python 3.12, FFmpeg/FFprobe), using the same environment variables and MongoDB database. If using Render's native Python runtime instead, FFprobe is not guaranteed available; the built-in MP4/MKV parser still works, but the Docker runtime is preferred. Do not start a second live worker or automatically reparse the catalogue. No production data was changed by this build.

- Delivered indexed videos have a Media Information button. Inspection uses bounded partial retrieval, initially 128 KiB where raw Telegram ranges are supported, with a 24 MiB total application request budget, a 90-second deadline and one active inspection. No full-file fallback or persistent video storage. Live Telegram transport remains unverified.
- `/parserrules` (also in Settings): add/delete a single release token, disable/enable an optional marker, and save exact filename title/year exceptions. `/testparse FILENAME` is read-only. `/reparse dry` previews the existing catalogue without changing it. Register confirmed promo prefixes with `/addpromo IM | MSPmoviesOffl`; unregistered underscore-containing prefixes are deliberately retained.
- `/filmids TITLE` or `/filmids FILM_ID`: find stable IDs and sample file IDs. `/settitle FILM_ID NEW_TITLE`, `/setyear FILM_ID YEAR` (or `-`) and `/locktitle FILM_ID` preserve the existing identity and lock it. `/unlocktitle FILM_ID` is refused while the corrected title/year differs from the original hashed ID: restore that original identity first to prevent a catalogue split. No automatic merging of films.
- `/reparsefile FILE_UID [dry|apply]` and `/reparsefilm FILM_ID [dry|apply]` default to read-only, preserve locks, and leave known look-alike conflicts untouched. Targeted film reparse is limited to 500 records. Existing full `/reparse` remains an explicit write operation; use dry first and avoid it for isolated fixes.
- `/index CHANNEL [FROM [TO]]` supports manually indexing an accessible channel outside FILE_CHANNELS after membership/history checks. Only configured channels are automatically monitored. Catch-up scans bounded overlapping ranges (up to 1,000 IDs per channel per cycle); large sparse gaps may still require manual indexing.
- `/dbsize` shows application database data and index totals, plus files/films/skips breakdown. Atlas quota applies to the cluster. `/catalogueaudit` and `/duplicatepreview` are read-only, bounded samples, not an exhaustive migration audit.
- Heartbeat logs include memory, event-loop lag, queue size and connection status. Automatic indexing batches metadata and serialises the duplicate check within this bot process. Keep only one worker active; cross-process look-alike uniqueness is not guaranteed by MongoDB.

See `CHANGELOG-v5.md` for exact test results, known baseline failures, validation limitations and changed files.

## v5.1 media update

- Raw Telegram ranges start at 128 KiB and grow to aligned requests up to 1 MiB; failed larger requests shrink and switch same-DC endpoint.
- MP4 top-level atom offsets and MKV element sizes / validated SeekHead targets avoid sequential media payload retrieval.
- 120-second overall deadline; 24 MiB application request budget including retries; one active inspection; no full-file fallback.
- Verified complete MKV tracks can be returned if duration retrieval fails. Duration is explicitly unavailable when not verified.
- Legacy stream fallback remains unchanged apart from deadline. No new dependencies, environment variables or database changes.
- Tests use simulated Telegram sessions; latency regression estimates 2.5 seconds per request rather than waiting on real network. Live Telegram and Docker not verified. No incremental progress UI added; acknowledgement reflects new deadline.
- Targeted suite: 10 passed. Full suite: 154 passed, 17 existing failures (same count as v5).


## Supporter and Media Information update

- Home and Donate always offer Supporters Wall, even when empty. Active supporters may opt in/out; the wall uses their Telegram display name, not payment details. Existing Mongo query cap of 100 names remains; pages show 10 names. Group search replies identify active supporters with a star and their display name; this does not modify Telegram user tags. Wall consent and group recognition are separate.
- `/supporters` opens the wall; `/request` opens a supporter-only custom request prompt; `/donationclaim` opens the user's pending claim. Reply specifically to the bot-authored prompt. Ordinary private text remains search text, even with a pending claim. Ignore the prompt to stop the workflow; old prompts may still be replied to explicitly. Donor/admin correspondence is only relayed when explicitly addressed to an authorized prompt (or an existing admin claim card). No automatic approval.
- `/request`: one title per line, max 20 per batch, 80 characters per title; additional batches allowed without a daily quota for active supporters. Existing request storage, duplicate detection and admin review actions are used. Series users can use Season N Episode N or S01E02. Regular users retain the existing request path. Admin title text is copyable monospace.
- `/donor claims` lists up to 20 pending claims; `/donor claim CLAIM_ID` opens a specific claim for review. Verify the payment independently; historical submitted flags may not mean a receipt was delivered. A donor can send proof again as an explicit follow-up using `/donationclaim`. No existing records automatically resolved, migrated or deleted.
- Media report uses existing retrieved data only. MKV omitted language is marked English (container default), explicit undefined remains Unknown unless an unambiguous track name identifies a language; MP4 unspecified remains Unknown. Duration seconds, average bitrate, audio channels/sample rate, codec/track names, optional flags, aspect/bit depth and container technical details are shown only when available. Existing range retrieval, fallback probe behavior, budget (24 MiB), 120s timeout and single inspection are unchanged. No extra probing or downloads. No new packages, collections or environment variables.
- Deploy from repository root using existing Dockerfile, configuration and database(s). No catalogue reparse needed. Check wall opt-in/out, normal search with a pending claim, explicit donor follow-up, custom requests, supporter group reply and media info after deployment. Live Telegram, payment settlement, Atlas and Docker integration were not verified locally.
