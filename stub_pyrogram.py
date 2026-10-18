"""A tiny stand-in for Kurigram/Pyrogram so the handlers can be exercised
without Telegram. It only checks OUR logic, not the real library's behaviour."""
import enum, sys, types


def install():
    if "pyrogram" in sys.modules and getattr(sys.modules["pyrogram"], "_stub", False):
        return
    root = types.ModuleType("pyrogram"); root._stub = True

    class F:
        def __and__(s, o): return F()
        __or__ = __and__
        def __invert__(s): return F()
    root.filters = types.SimpleNamespace(
        private=F(), group=F(), text=F(), bot=F(), channel=F(), document=F(), video=F(), audio=F(),
        command=lambda *a, **k: F(), regex=lambda *a, **k: F(), user=lambda *a, **k: F())

    enums = types.ModuleType("pyrogram.enums")
    enums.ChatMemberStatus = enum.Enum("ChatMemberStatus", "OWNER ADMINISTRATOR MEMBER RESTRICTED LEFT BANNED")
    enums.ParseMode = types.SimpleNamespace(HTML="html")
    enums.ChatType = enum.Enum("ChatType", "PRIVATE GROUP SUPERGROUP CHANNEL")

    errors = types.ModuleType("pyrogram.errors")
    class RPCError(Exception): ...
    class FloodWait(RPCError):
        def __init__(self, value=1): self.value = value
    class UserNotParticipant(RPCError): ...
    class ChannelInvalid(RPCError): ...
    class ChannelPrivate(RPCError): ...
    errors.RPCError, errors.FloodWait = RPCError, FloodWait
    errors.UserNotParticipant, errors.ChannelInvalid, errors.ChannelPrivate = UserNotParticipant, ChannelInvalid, ChannelPrivate

    handlers = types.ModuleType("pyrogram.handlers")
    class _H:
        def __init__(s, cb, flt=None): s.callback, s.filters = cb, flt
    handlers.MessageHandler = handlers.CallbackQueryHandler = _H

    types_ = types.ModuleType("pyrogram.types")
    class InlineKeyboardButton:
        def __init__(s, text, callback_data=None, url=None): s.text, s.callback_data, s.url = text, callback_data, url
    class InlineKeyboardMarkup:
        def __init__(s, rows): s.inline_keyboard = rows
    types_.InlineKeyboardButton, types_.InlineKeyboardMarkup = InlineKeyboardButton, InlineKeyboardMarkup

    for name, mod in {"pyrogram": root, "pyrogram.enums": enums, "pyrogram.errors": errors,
                      "pyrogram.handlers": handlers, "pyrogram.types": types_}.items():
        sys.modules[name] = mod
