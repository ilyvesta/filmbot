"""Environment settings, validated up front so a typo fails with a clear message
instead of a crash ten minutes into a deploy."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    admins: frozenset[int]
    file_channels: frozenset[int]
    mongo_uri: str
    db_name: str
    inbox_channel: int | None      # where requests and file reports are posted (None = DM the admins)
    port: int


def _ints(raw: str) -> list[int]:
    return [int(x) for x in re.split(r"[,\s]+", raw.strip()) if x]


def load(env=os.environ) -> Config:
    errors: list[str] = []

    def need(name: str) -> str:
        v = (env.get(name) or "").strip()
        if not v:
            errors.append(f"{name} is missing")
        return v

    def ints(name: str, negative: bool | None = None) -> list[int]:
        raw = need(name)
        try:
            vals = _ints(raw)
        except ValueError:
            errors.append(f"{name} must be numbers separated by commas")
            return []
        if raw and not vals:
            errors.append(f"{name} is empty")
        if negative is True and any(v > 0 for v in vals):
            errors.append(f"{name}: channel IDs start with -100 (e.g. -1001234567890)")
        return vals

    api_id, api_hash, token = need("API_ID"), need("API_HASH"), need("BOT_TOKEN")
    if api_id and not api_id.isdigit():
        errors.append("API_ID must be a number")
    if token and not re.fullmatch(r"\d{6,}:[\w-]{30,}", token):
        errors.append("BOT_TOKEN doesn't look right (expected 123456:ABC...)")
    admins = ints("ADMINS")
    channels = ints("FILE_CHANNELS", negative=True)
    uri = need("DATABASE_URI")
    if uri and not uri.startswith(("mongodb://", "mongodb+srv://")):
        errors.append("DATABASE_URI must start with mongodb+srv://")
    if uri and "<password>" in uri:
        errors.append("DATABASE_URI still contains <password>; put the real password in")
    log = (env.get("REQUESTS_CHANNEL") or env.get("LOG_CHANNEL") or "").strip()
    if log and not re.fullmatch(r"-?\d+", log):
        errors.append("REQUESTS_CHANNEL must be a number")
    if errors:
        raise ConfigError("Fix these environment variables:\n - " + "\n - ".join(errors))
    return Config(int(api_id), api_hash, token, frozenset(admins), frozenset(channels), uri,
                  (env.get("DATABASE_NAME") or "moviebot").strip(), int(log) if log else None,
                  int(env.get("PORT") or 10000))
