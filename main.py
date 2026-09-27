import asyncio
import html
import json
import logging
import os
import secrets
import string
from datetime import datetime, timezone
from io import BytesIO

import aiosqlite
from PIL import Image, ImageDraw, ImageFont
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)

try:
    from telegram import CopyTextButton
except ImportError:  # older python-telegram-bot without Bot API 8.0 support
    CopyTextButton = None
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import (
    BadRequest,
    Forbidden,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ============================================================================
# CONFIGURATION — everything below is read from the environment. Nothing is
# hardcoded; set these as env vars (or export them before running the bot).
# ============================================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "8654804052:AAE3ne1SgEGLOd11LwkUtntAM50LcNpFE4M")

# Leave BOT_USERNAME empty to have it auto-detected at startup via getMe().
BOT_USERNAME = os.getenv("BOT_USERNAME", "KshatriyaVotingBot").lstrip("@")

ADMIN_IDS = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "7513729138").split(",")
    if x.strip().lstrip("-").isdigit()
]

# Shown as the "Buy Paid Votes" contact and used anywhere the bot needs to
# point a user at a human for support / paid votes.
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "KSHATRIYA_OP").lstrip("@")

DB_FILE = os.getenv("DB_FILE", "bot_database.db")
USERS_JSON = os.getenv("USERS_JSON", "users.json")

BOT_NAME = os.getenv("BOT_DISPLAY_NAME", "KSHATRIYA GIVEAWAYS BOT")

# "Hosted By" credit shown on every participant channel post.
HOSTED_BY_NAME = os.getenv("HOSTED_BY_NAME", "TEAM SN")
HOSTED_BY_USERNAME = os.getenv("HOSTED_BY_USERNAME", "KSHATRIYA_OP").lstrip("@")

# ----------------------------------------------------------------------------
# Paid Votes (BDT). There's no external payment-gateway API key involved here
# (bKash/Nagad merchant APIs need credentials only you can obtain) — instead
# this is a manual verify flow: buyer sends money to PAYMENT_NUMBER, submits
# their Transaction ID in the bot, and an admin taps Approve/Reject. Leave
# PAYMENT_NUMBER empty to keep the old "contact admin" behaviour instead.
# ----------------------------------------------------------------------------
CURRENCY_SYMBOL = os.getenv("CURRENCY_SYMBOL", "৳")
PAYMENT_METHOD_NAME = os.getenv("PAYMENT_METHOD_NAME", "UPI")
PAYMENT_NUMBER = os.getenv("PAYMENT_NUMBER", "singhthakurharsh369@fam")


def _parse_vote_packages(raw: str):
    packages = []
    for part in raw.split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        votes_str, price_str = part.split(":", 1)
        votes_str, price_str = votes_str.strip(), price_str.strip()
        if votes_str.isdigit() and price_str.replace(".", "", 1).isdigit():
            packages.append((int(votes_str), float(price_str)))
    return packages


# Format: "<votes>:<price>,<votes>:<price>,..."
VOTE_PACKAGES = _parse_vote_packages(
    os.getenv("VOTE_PACKAGES", "10:50,25:110,50:200,100:350,250:800")
) or [(10, 50.0), (25, 110.0), (50, 200.0), (100, 350.0), (250, 800.0)]

# ============================================================================
# LOGGING
# ============================================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("avi_vote_bot")

# ============================================================================
# CONVERSATION STATES
# ============================================================================

(
    CG_NAME,
    CG_GIVEAWAY_CHANNEL,
    CG_VOTE_CHANNEL,
) = range(3)

(BC_CONTENT, BC_CONFIRM) = range(10, 12)
(AV_LIST, AV_AMOUNT) = range(20, 22)
(RV_LIST, RV_AMOUNT) = range(30, 32)
(BAN_USERID,) = range(40, 41)
(UNBAN_USERID,) = range(41, 42)
(FP_USERID,) = range(50, 51)
(BV_TRXID,) = range(60, 61)

# in-memory scratch pad used inside conversations, keyed by user_data

# ============================================================================
# JSON USERS FILE LOCK
# ============================================================================

USERS_JSON_LOCK = asyncio.Lock()

# ============================================================================
# PERFORMANCE INFRASTRUCTURE
# ============================================================================
# A single persistent aiosqlite connection is reused for the whole process
# instead of opening/closing a new connection on every query (this was the
# single biggest source of slowness). WAL mode lets reads and writes happen
# concurrently; a dedicated asyncio.Lock serializes writes so we never hit
# "database is locked" errors under load.

_db_conn: aiosqlite.Connection | None = None
_db_write_lock = asyncio.Lock()

# In-memory caches to avoid re-hitting SQLite for hot lookups. Invalidated
# explicitly whenever the underlying data changes.
_banned_cache: set[int] = set()
_banned_cache_ready = False
_giveaway_by_id_cache: dict[int, aiosqlite.Row] = {}
_giveaway_by_token_cache: dict[str, aiosqlite.Row] = {}

# Per-key async locks prevent duplicate concurrent processing (e.g. a user
# double-tapping Verify/Vote firing two overlapping requests), which was a
# source of duplicated channel posts / double vote races.
_action_locks: dict[str, asyncio.Lock] = {}


def _get_action_lock(key: str) -> asyncio.Lock:
    lock = _action_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _action_locks[key] = lock
    return lock


# ============================================================================
# UTILITIES
# ============================================================================


def esc(value) -> str:
    return html.escape(str(value), quote=False)


def fmt_price(price: float) -> str:
    return str(int(price)) if float(price).is_integer() else f"{price:.2f}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def generate_token(length: int = 8) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def admin_filter():
    return filters.User(user_id=ADMIN_IDS)


ADMIN_MENU = ReplyKeyboardMarkup(
    [
        ["🎁 Create Giveaway", "🏁 End Giveaway"],
        ["📢 Broadcast", "📊 Statistics"],
        ["👥 All Users", "🚫 Ban User"],
        ["✅ Unban User"],
        ["➕ Add Votes", "➖ Remove Votes"],
        ["📈 Giveaway Votes", "🔎 Find Participant"],
    ],
    resize_keyboard=True,
)

CANCEL_KEYBOARD = ReplyKeyboardMarkup([["/cancel"]], resize_keyboard=True)


# ============================================================================
# DATABASE LAYER (async, aiosqlite)
# ============================================================================


async def get_db() -> aiosqlite.Connection:
    """Return the shared, persistent aiosqlite connection, opening it on first use."""
    global _db_conn
    if _db_conn is None:
        _db_conn = await aiosqlite.connect(DB_FILE)
        _db_conn.row_factory = aiosqlite.Row
        # WAL = concurrent readers while a write is in flight; NORMAL sync is a
        # safe, fast trade-off for a bot workload (durability still guaranteed
        # by WAL checkpointing).
        await _db_conn.execute("PRAGMA journal_mode=WAL")
        await _db_conn.execute("PRAGMA synchronous=NORMAL")
        await _db_conn.execute("PRAGMA foreign_keys=ON")
        await _db_conn.execute("PRAGMA busy_timeout=5000")
    return _db_conn


async def close_db() -> None:
    global _db_conn
    if _db_conn is not None:
        await _db_conn.close()
        _db_conn = None


async def init_db() -> None:
    db = await get_db()
    await db.execute(
        """CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            is_banned INTEGER DEFAULT 0,
            joined_at TEXT
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS giveaways (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            giveaway_channel_id INTEGER NOT NULL,
            giveaway_channel_username TEXT,
            giveaway_channel_title TEXT,
            vote_channel_id INTEGER NOT NULL,
            vote_channel_username TEXT,
            vote_channel_title TEXT,
            status TEXT DEFAULT 'running',
            created_at TEXT
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS participants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            giveaway_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            name TEXT,
            votes INTEGER DEFAULT 0,
            message_id INTEGER,
            created_at TEXT,
            UNIQUE(giveaway_id, user_id)
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            participant_id INTEGER NOT NULL,
            voter_id INTEGER NOT NULL,
            created_at TEXT,
            UNIQUE(participant_id, voter_id)
        )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS vote_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            participant_id INTEGER NOT NULL,
            buyer_id INTEGER NOT NULL,
            votes INTEGER NOT NULL,
            price REAL NOT NULL,
            trx_id TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            decided_at TEXT
        )"""
    )
    # Indexes for the lookups that happen on every vote/participation.
    await db.execute("CREATE INDEX IF NOT EXISTS idx_participants_giveaway ON participants(giveaway_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_participants_user ON participants(user_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_votes_participant ON votes(participant_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_votes_voter ON votes(voter_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_giveaways_status ON giveaways(status)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_giveaways_token ON giveaways(token)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_vote_orders_status ON vote_orders(status)")
    await db.commit()
    logger.info("Database ready: %s", DB_FILE)


async def ensure_users_json() -> None:
    if not os.path.exists(USERS_JSON):
        async with USERS_JSON_LOCK:
            with open(USERS_JSON, "w", encoding="utf-8") as f:
                json.dump({}, f)
        logger.info("Created %s", USERS_JSON)


async def update_users_json(user_id: int, username, first_name, joined_at: str) -> None:
    async with USERS_JSON_LOCK:
        try:
            with open(USERS_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        data[str(user_id)] = {
            "username": username,
            "first_name": first_name,
            "joined_at": joined_at,
        }
        with open(USERS_JSON, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


async def db_add_or_update_user(user_id: int, username, first_name) -> None:
    ts = now_iso()
    db = await get_db()
    async with _db_write_lock:
        await db.execute(
            """INSERT INTO users(user_id, username, first_name, is_banned, joined_at)
               VALUES(?,?,?,0,?)
               ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name""",
            (user_id, username, first_name, ts),
        )
        await db.commit()
    await update_users_json(user_id, username, first_name, ts)


async def _refresh_banned_cache() -> None:
    global _banned_cache, _banned_cache_ready
    db = await get_db()
    cur = await db.execute("SELECT user_id FROM users WHERE is_banned=1")
    rows = await cur.fetchall()
    _banned_cache = {r[0] for r in rows}
    _banned_cache_ready = True


async def db_is_banned(user_id: int) -> bool:
    if not _banned_cache_ready:
        await _refresh_banned_cache()
    return user_id in _banned_cache


async def db_set_banned(user_id: int, banned: bool) -> bool:
    db = await get_db()
    async with _db_write_lock:
        cur = await db.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        if not row:
            return False
        await db.execute("UPDATE users SET is_banned=? WHERE user_id=?", (1 if banned else 0, user_id))
        await db.commit()
    if not _banned_cache_ready:
        await _refresh_banned_cache()
    if banned:
        _banned_cache.add(user_id)
    else:
        _banned_cache.discard(user_id)
    return True


async def db_create_giveaway(token, name, gc, vc) -> int:
    db = await get_db()
    async with _db_write_lock:
        cur = await db.execute(
            """INSERT INTO giveaways(token, name, giveaway_channel_id, giveaway_channel_username,
               giveaway_channel_title, vote_channel_id, vote_channel_username, vote_channel_title,
               status, created_at) VALUES(?,?,?,?,?,?,?,?, 'running', ?)""",
            (
                token,
                name,
                gc.id,
                gc.username,
                gc.title,
                vc.id,
                vc.username,
                vc.title,
                now_iso(),
            ),
        )
        await db.commit()
        return cur.lastrowid


async def db_get_giveaway_by_token(token: str, use_cache: bool = True):
    if use_cache and token in _giveaway_by_token_cache:
        return _giveaway_by_token_cache[token]
    db = await get_db()
    cur = await db.execute("SELECT * FROM giveaways WHERE token=?", (token,))
    row = await cur.fetchone()
    if row:
        _giveaway_by_token_cache[token] = row
        _giveaway_by_id_cache[row["id"]] = row
    return row


async def db_get_giveaway(giveaway_id: int, use_cache: bool = True):
    if use_cache and giveaway_id in _giveaway_by_id_cache:
        return _giveaway_by_id_cache[giveaway_id]
    db = await get_db()
    cur = await db.execute("SELECT * FROM giveaways WHERE id=?", (giveaway_id,))
    row = await cur.fetchone()
    if row:
        _giveaway_by_id_cache[giveaway_id] = row
        _giveaway_by_token_cache[row["token"]] = row
    return row


def _invalidate_giveaway_cache(giveaway_id: int | None = None, token: str | None = None) -> None:
    if giveaway_id is not None:
        _giveaway_by_id_cache.pop(giveaway_id, None)
    if token is not None:
        _giveaway_by_token_cache.pop(token, None)


async def db_get_running_giveaways():
    db = await get_db()
    cur = await db.execute("SELECT * FROM giveaways WHERE status='running' ORDER BY id DESC")
    return await cur.fetchall()


async def db_end_giveaway(giveaway_id: int) -> None:
    db = await get_db()
    cached = _giveaway_by_id_cache.get(giveaway_id)
    async with _db_write_lock:
        await db.execute("UPDATE giveaways SET status='ended' WHERE id=?", (giveaway_id,))
        await db.commit()
    _invalidate_giveaway_cache(giveaway_id=giveaway_id, token=cached["token"] if cached else None)


async def db_get_participant_by_user(giveaway_id: int, user_id: int):
    db = await get_db()
    cur = await db.execute(
        "SELECT * FROM participants WHERE giveaway_id=? AND user_id=?",
        (giveaway_id, user_id),
    )
    return await cur.fetchone()


async def db_get_participant(participant_id: int):
    db = await get_db()
    cur = await db.execute("SELECT * FROM participants WHERE id=?", (participant_id,))
    return await cur.fetchone()


async def db_running_participants_page(page: int, page_size: int = 8):
    """Participants of currently-running giveaways, for the admin vote-picker UI."""
    offset = page * page_size
    db = await get_db()
    cur = await db.execute(
        """SELECT p.*, g.name AS giveaway_name FROM participants p
           JOIN giveaways g ON g.id = p.giveaway_id
           WHERE g.status='running'
           ORDER BY g.id DESC, p.votes DESC
           LIMIT ? OFFSET ?""",
        (page_size, offset),
    )
    rows = await cur.fetchall()
    cur = await db.execute(
        """SELECT COUNT(*) FROM participants p
           JOIN giveaways g ON g.id = p.giveaway_id
           WHERE g.status='running'"""
    )
    total = (await cur.fetchone())[0]
    return rows, total


async def db_create_participant(giveaway_id: int, user_id: int, name: str) -> int:
    db = await get_db()
    async with _db_write_lock:
        try:
            cur = await db.execute(
                """INSERT INTO participants(giveaway_id, user_id, name, votes, created_at)
                   VALUES(?,?,?,0,?)""",
                (giveaway_id, user_id, name, now_iso()),
            )
            await db.commit()
            return cur.lastrowid
        except aiosqlite.IntegrityError:
            cur = await db.execute(
                "SELECT id FROM participants WHERE giveaway_id=? AND user_id=?",
                (giveaway_id, user_id),
            )
            row = await cur.fetchone()
            return row[0]


async def db_set_participant_message(participant_id: int, message_id: int) -> None:
    db = await get_db()
    async with _db_write_lock:
        await db.execute(
            "UPDATE participants SET message_id=? WHERE id=?", (message_id, participant_id)
        )
        await db.commit()


async def db_leaderboard(giveaway_id: int, limit: int = 15):
    db = await get_db()
    cur = await db.execute(
        "SELECT * FROM participants WHERE giveaway_id=? ORDER BY votes DESC, id ASC LIMIT ?",
        (giveaway_id, limit),
    )
    return await cur.fetchall()


async def db_top3(giveaway_id: int):
    db = await get_db()
    cur = await db.execute(
        "SELECT * FROM participants WHERE giveaway_id=? ORDER BY votes DESC, id ASC LIMIT 3",
        (giveaway_id,),
    )
    return await cur.fetchall()


async def db_has_voted(participant_id: int, voter_id: int) -> bool:
    db = await get_db()
    cur = await db.execute(
        "SELECT 1 FROM votes WHERE participant_id=? AND voter_id=? LIMIT 1",
        (participant_id, voter_id),
    )
    row = await cur.fetchone()
    return row is not None


async def db_voter_vote_in_giveaway(giveaway_id: int, voter_id: int):
    """A voter may cast only one vote per giveaway (for exactly one participant).
    Returns the participant row they already voted for in this giveaway, or None."""
    db = await get_db()
    cur = await db.execute(
        """SELECT p.* FROM votes v
           JOIN participants p ON p.id = v.participant_id
           WHERE p.giveaway_id=? AND v.voter_id=?
           LIMIT 1""",
        (giveaway_id, voter_id),
    )
    return await cur.fetchone()


async def db_cast_vote(participant_id: int, voter_id: int):
    """Returns (success: bool, new_votes: int, already_voted: bool)"""
    db = await get_db()
    async with _db_write_lock:
        try:
            await db.execute(
                "INSERT INTO votes(participant_id, voter_id, created_at) VALUES(?,?,?)",
                (participant_id, voter_id, now_iso()),
            )
            await db.execute(
                "UPDATE participants SET votes = votes + 1 WHERE id=?", (participant_id,)
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            await db.rollback()
            cur = await db.execute(
                "SELECT votes FROM participants WHERE id=?", (participant_id,)
            )
            row = await cur.fetchone()
            return False, (row[0] if row else 0), True

        cur = await db.execute("SELECT votes FROM participants WHERE id=?", (participant_id,))
        row = await cur.fetchone()
        return True, (row[0] if row else 0), False


async def db_add_votes(participant_id: int, amount: int) -> int:
    db = await get_db()
    async with _db_write_lock:
        await db.execute(
            "UPDATE participants SET votes = MAX(0, votes + ?) WHERE id=?",
            (amount, participant_id),
        )
        await db.commit()
        cur = await db.execute("SELECT votes FROM participants WHERE id=?", (participant_id,))
        row = await cur.fetchone()
        return row[0] if row else 0


async def db_create_vote_order(participant_id: int, buyer_id: int, votes: int, price: float) -> int:
    db = await get_db()
    async with _db_write_lock:
        cur = await db.execute(
            """INSERT INTO vote_orders(participant_id, buyer_id, votes, price, status, created_at)
               VALUES(?,?,?,?, 'pending', ?)""",
            (participant_id, buyer_id, votes, price, now_iso()),
        )
        await db.commit()
        return cur.lastrowid


async def db_set_order_trx(order_id: int, trx_id: str) -> None:
    db = await get_db()
    async with _db_write_lock:
        await db.execute("UPDATE vote_orders SET trx_id=? WHERE id=?", (trx_id, order_id))
        await db.commit()


async def db_get_vote_order(order_id: int):
    db = await get_db()
    cur = await db.execute("SELECT * FROM vote_orders WHERE id=?", (order_id,))
    return await cur.fetchone()


async def db_resolve_vote_order(order_id: int, status: str) -> bool:
    """Atomically move a pending order to approved/rejected. Returns False if
    the order was already decided (prevents a double-approve race)."""
    db = await get_db()
    async with _db_write_lock:
        cur = await db.execute(
            "UPDATE vote_orders SET status=?, decided_at=? WHERE id=? AND status='pending'",
            (status, now_iso(), order_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def db_stats():
    db = await get_db()
    cur = await db.execute(
        """SELECT
             (SELECT COUNT(*) FROM users) AS total_users,
             (SELECT COUNT(*) FROM participants) AS total_participants,
             (SELECT COALESCE(SUM(votes),0) FROM participants) AS total_votes,
             (SELECT COUNT(*) FROM giveaways WHERE status='running') AS running,
             (SELECT COUNT(*) FROM giveaways WHERE status='ended') AS ended"""
    )
    row = await cur.fetchone()
    return {key: row[key] for key in row.keys()}


async def db_giveaway_vote_totals():
    """Per-giveaway breakdown (running first, then ended) — total votes and
    participant count for each, for the admin 'Giveaway Votes' panel."""
    db = await get_db()
    cur = await db.execute(
        """SELECT g.id, g.name, g.status,
                  COUNT(p.id) AS participant_count,
                  COALESCE(SUM(p.votes), 0) AS total_votes
           FROM giveaways g
           LEFT JOIN participants p ON p.giveaway_id = g.id
           GROUP BY g.id
           ORDER BY (g.status = 'running') DESC, g.id DESC"""
    )
    return await cur.fetchall()


async def db_get_participants_by_user(user_id: int):
    """All giveaways a given User ID has joined, with their vote count in each —
    used by the admin 'Find Participant' lookup."""
    db = await get_db()
    cur = await db.execute(
        """SELECT p.*, g.name AS giveaway_name, g.status AS giveaway_status FROM participants p
           JOIN giveaways g ON g.id = p.giveaway_id WHERE p.user_id=?
           ORDER BY g.id DESC""",
        (user_id,),
    )
    return await cur.fetchall()


async def db_all_users_page(page: int, page_size: int = 10):
    offset = page * page_size
    db = await get_db()
    cur = await db.execute(
        "SELECT user_id, username, first_name FROM users ORDER BY joined_at DESC LIMIT ? OFFSET ?",
        (page_size, offset),
    )
    rows = await cur.fetchall()
    cur = await db.execute("SELECT COUNT(*) FROM users")
    total = (await cur.fetchone())[0]
    return rows, total


async def db_all_user_ids():
    db = await get_db()
    cur = await db.execute("SELECT user_id FROM users WHERE is_banned=0")
    rows = await cur.fetchall()
    return [r[0] for r in rows]


async def db_giveaways_containing_channel(chat_id: int):
    db = await get_db()
    cur = await db.execute(
        """SELECT * FROM giveaways WHERE status='running'
           AND (giveaway_channel_id=? OR vote_channel_id=?)""",
        (chat_id, chat_id),
    )
    return await cur.fetchall()


async def db_votes_by_voter_in_giveaway(giveaway_id: int, voter_id: int):
    db = await get_db()
    cur = await db.execute(
        """SELECT v.id AS vote_id, p.* FROM votes v
           JOIN participants p ON p.id = v.participant_id
           WHERE p.giveaway_id=? AND v.voter_id=?""",
        (giveaway_id, voter_id),
    )
    return await cur.fetchall()


async def db_remove_vote(vote_id: int, participant_id: int) -> int:
    db = await get_db()
    async with _db_write_lock:
        await db.execute("DELETE FROM votes WHERE id=?", (vote_id,))
        await db.execute(
            "UPDATE participants SET votes = MAX(0, votes - 1) WHERE id=?", (participant_id,)
        )
        await db.commit()
        cur = await db.execute("SELECT votes FROM participants WHERE id=?", (participant_id,))
        row = await cur.fetchone()
        return row[0] if row else 0


# ============================================================================
# TELEGRAM HELPERS
# ============================================================================


async def check_membership(bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
    except TelegramError:
        return False


async def check_membership_both(bot, chat_id_a: int, chat_id_b: int, user_id: int) -> tuple[bool, bool]:
    """Check two channels concurrently instead of sequentially so voting stays fast."""
    result_a, result_b = await asyncio.gather(
        check_membership(bot, chat_id_a, user_id),
        check_membership(bot, chat_id_b, user_id),
    )
    return result_a, result_b


async def safe_call(coro_factory, *, retries: int = 2, default=None):
    """Run a Telegram API coroutine with resilient handling of transient errors.

    coro_factory is a zero-arg callable returning a fresh coroutine, since a
    coroutine object can only be awaited once and RetryAfter/TimedOut need a
    fresh call on retry.
    """
    attempt = 0
    while True:
        try:
            return await coro_factory()
        except RetryAfter as e:
            attempt += 1
            if attempt > retries:
                logger.warning("RetryAfter exhausted retries: %s", e)
                return default
            await asyncio.sleep(min(e.retry_after, 5) + 0.1)
        except (TimedOut, NetworkError) as e:
            attempt += 1
            if attempt > retries:
                logger.warning("Network error exhausted retries: %s", e)
                return default
            await asyncio.sleep(0.5 * attempt)
        except Forbidden:
            return default
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return default
            logger.warning("BadRequest: %s", e)
            return default
        except TelegramError as e:
            logger.warning("TelegramError: %s", e)
            return default


async def safe_answer(query, text: str = None, show_alert: bool = False) -> None:
    """Answer a callback query, never letting the UI spinner hang forever."""
    await safe_call(lambda: query.answer(text=text, show_alert=show_alert))


async def safe_send(bot, chat_id, text, **kwargs):
    return await safe_call(lambda: bot.send_message(chat_id, text, **kwargs))


async def safe_edit(message, text, **kwargs):
    return await safe_call(lambda: message.edit_text(text, **kwargs))


async def get_join_url(bot, chat_id: int, username) -> str:
    if username:
        return f"https://t.me/{username}"
    try:
        link = await bot.export_chat_invite_link(chat_id)
        return link
    except TelegramError:
        return "https://t.me/"


async def verify_channel_and_admin(bot, chat_ref: str):
    """Returns (chat_object, error_message)"""
    chat_ref = chat_ref.strip()
    try:
        if chat_ref.lstrip("-").isdigit():
            chat = await bot.get_chat(int(chat_ref))
        else:
            uname = chat_ref if chat_ref.startswith("@") else "@" + chat_ref
            chat = await bot.get_chat(uname)
    except TelegramError as e:
        return None, f"❌ Channel not found or inaccessible.\n<i>{esc(e)}</i>"

    try:
        member = await bot.get_chat_member(chat.id, bot.id)
    except TelegramError as e:
        return None, f"❌ Could not verify bot membership.\n<i>{esc(e)}</i>"

    if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        return None, "❌ Bot is not an admin in that channel. Please add the bot as admin and try again."

    return chat, None


def user_display_name(user) -> str:
    name = user.first_name or ""
    if user.last_name:
        name += f" {user.last_name}"
    return name.strip() or (user.username or str(user.id))


# ============================================================================
# ============================================================================
# BANNER ANIMATION (generated once, then reused via Telegram file_id)
# ============================================================================

_banner_file_id: str | None = None
_banner_lock = asyncio.Lock()

_BANNER_FONT_BOLD = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
)
_BANNER_FONT_REGULAR = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
)


def _load_banner_font(candidates, size):
    for path in candidates:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def generate_banner_animation() -> bytes:
    """Premium black + gold animated banner (a gold shimmer sweeps across the
    title on loop) — generated as a GIF so Telegram plays it as an animation."""
    width, height = 1000, 560
    title = "AVI GIVEAWAY"
    subtitle = "V O T E   •   W I N   •   C E L E B R A T E"

    title_font = _load_banner_font(_BANNER_FONT_BOLD, 92)
    sub_font = _load_banner_font(_BANNER_FONT_REGULAR, 28)

    gold = (212, 175, 55)
    border_col = (140, 110, 40)
    subtitle_col = (190, 190, 200)

    # Base frame: dark vertical gradient + border + static text, built once
    # and reused every frame (only the shimmer overlay changes per frame).
    top, bottom = (6, 6, 10), (20, 15, 32)
    base = Image.new("RGB", (width, height))
    px = base.load()
    for y in range(height):
        t = y / height
        row = (
            int(top[0] + (bottom[0] - top[0]) * t),
            int(top[1] + (bottom[1] - top[1]) * t),
            int(top[2] + (bottom[2] - top[2]) * t),
        )
        for x in range(width):
            px[x, y] = row

    draw0 = ImageDraw.Draw(base)
    draw0.rectangle([18, 18, width - 19, height - 19], outline=border_col, width=2)

    tb = draw0.textbbox((0, 0), title, font=title_font)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    tx, ty = (width - tw) / 2 - tb[0], height / 2 - th - 10

    sb = draw0.textbbox((0, 0), subtitle, font=sub_font)
    sw, sh = sb[2] - sb[0], sb[3] - sb[1]
    sx, sy = (width - sw) / 2 - sb[0], ty + th + 30

    draw0.text((tx, ty), title, font=title_font, fill=gold)
    draw0.text((sx, sy), subtitle, font=sub_font, fill=subtitle_col)

    # Mask of the title glyphs only, so the shimmer band is confined to the text.
    text_mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(text_mask).text((tx, ty), title, font=title_font, fill=255)

    frame_count = 20
    band_w = 90
    frames = []
    for i in range(frame_count):
        frame = base.convert("RGBA")
        overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        center = -band_w + (i / frame_count) * (width + 2 * band_w)
        for dx in range(-band_w, band_w):
            alpha = max(0, int(150 * (1 - abs(dx) / band_w)))
            odraw.line([(center + dx, ty - 10), (center + dx, ty + th + 10)], fill=(255, 255, 255, alpha))
        overlay.putalpha(Image.composite(overlay.split()[3], Image.new("L", (width, height), 0), text_mask))
        frame = Image.alpha_composite(frame, overlay).convert("RGB")
        frames.append(frame.convert("P", palette=Image.ADAPTIVE, colors=200))

    buf = BytesIO()
    frames[0].save(
        buf,
        format="GIF",
        save_all=True,
        append_images=frames[1:],
        duration=70,
        loop=0,
        disposal=2,
    )
    return buf.getvalue()


async def send_banner_photo(bot, chat_id: int, caption: str, reply_markup=None):
    """Send ONE message that combines the animated AVI GIVEAWAY banner together
    with the given caption and buttons — never as two separate messages.
    Generated once per process, then re-sent everywhere after via its Telegram
    file_id (no re-encoding, no re-upload). Returns the sent Message, or None
    only if even the text-only fallback failed."""
    global _banner_file_id

    if _banner_file_id:
        sent = await safe_call(
            lambda: bot.send_animation(
                chat_id,
                animation=_banner_file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
        )
        if sent is not None:
            return sent
        _banner_file_id = None  # cached id went stale — fall through and regenerate

    async with _banner_lock:
        if _banner_file_id:
            return await safe_call(
                lambda: bot.send_animation(
                    chat_id,
                    animation=_banner_file_id,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup,
                )
            )
        animation_bytes = await asyncio.to_thread(generate_banner_animation)
        sent = await safe_call(
            lambda: bot.send_animation(
                chat_id,
                animation=BytesIO(animation_bytes),
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
        )
        if sent is not None and sent.animation:
            _banner_file_id = sent.animation.file_id
            return sent

    # Banner totally failed (e.g. Telegram rejected the upload) — still
    # deliver the message as text so the flow never silently breaks.
    return await safe_send(bot, chat_id, caption, parse_mode=ParseMode.HTML, reply_markup=reply_markup)


# ============================================================================
# CORE FLOWS: PARTICIPATION & VOTING
# ============================================================================


def build_message_link(chat_id: int, username, message_id: int) -> str:
    if username:
        return f"https://t.me/{username}/{message_id}"
    raw = str(chat_id)
    raw = raw[4:] if raw.startswith("-100") else raw.lstrip("-")
    return f"https://t.me/c/{raw}/{message_id}"


async def send_join_participation_prompt(chat_id, context: ContextTypes.DEFAULT_TYPE, giveaway, edit_message=None):
    join_url = await get_join_url(context.bot, giveaway["giveaway_channel_id"], giveaway["giveaway_channel_username"])
    text = (
        "📢 <b>Join Giveaway Channel</b>\n\n"
        f"To participate in <b>{esc(giveaway['name'])}</b>, please join the giveaway channel first, "
        "then tap Verify."
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 Join Channel", url=join_url)],
            [InlineKeyboardButton("✅ Verify", callback_data=f"verify_participate:{giveaway['token']}")],
        ]
    )
    if edit_message:
        await safe_edit(edit_message, text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    else:
        await safe_send(context.bot, chat_id, text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


def build_participant_keyboard(vote_join_url: str, participant_id: int, votes: int) -> InlineKeyboardMarkup:
    """Shared keyboard builder so the join/vote buttons are built in exactly one place."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 Join", url=vote_join_url)],
            [InlineKeyboardButton(f"🗳 Vote ({votes})", callback_data=f"vote:{participant_id}")],
        ]
    )


def build_participant_post_text(name: str, user_id: int, giveaway_name: str, bot_username: str) -> str:
    """Static participant post text. The vote count is never embedded here — it lives
    only on the Vote button — so a new vote never requires an expensive text edit."""
    hosted_by = f'<a href="https://t.me/{HOSTED_BY_USERNAME}">{esc(HOSTED_BY_NAME)}</a>'
    made_by = f"@{esc(bot_username)}" if bot_username else "—"
    return (
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🎉 <b>NEW PARTICIPANT</b>\n\n"
        f"👤 <b>Name:</b>\n{esc(name)}\n\n"
        f"🆔 <b>User ID:</b> <code>{user_id}</code>\n\n"
        f"🏆 <b>Giveaway:</b> {esc(giveaway_name)}\n\n"
        f"🤖 <b>Make By:</b>\n{made_by}\n\n"
        f"👑 <b>Hosted By:</b> {hosted_by}\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "❤️ INCREASE YOUR VOTE"
    )


async def do_participate(user, giveaway, context: ContextTypes.DEFAULT_TYPE):
    """Registers the participant, posts to channel, sends confirmation to user.
    Sends the confirmation message (with the AVI GIVEAWAY banner attached)
    directly and returns the sent Message."""
    name = user_display_name(user)
    participant_id = await db_create_participant(giveaway["id"], user.id, name)
    participant = await db_get_participant(participant_id)
    bot_username = context.bot_data.get("bot_username", BOT_USERNAME)

    # If this is a brand-new post (no message yet), post to giveaway channel —
    # the banner image and the post text/buttons go out as ONE message.
    if not participant["message_id"]:
        vote_join_url = await get_join_url(
            context.bot, giveaway["vote_channel_id"], giveaway["vote_channel_username"]
        )
        post_text = build_participant_post_text(name, user.id, giveaway["name"], bot_username)
        keyboard = build_participant_keyboard(vote_join_url, participant_id, participant["votes"])
        sent = await send_banner_photo(
            context.bot, giveaway["giveaway_channel_id"], post_text, reply_markup=keyboard
        )
        if sent is not None:
            await db_set_participant_message(participant_id, sent.message_id)
            participant = await db_get_participant(participant_id)
        else:
            logger.warning("Failed to post participant %s to channel", participant_id)

    vote_link = build_message_link(
        giveaway["giveaway_channel_id"], giveaway["giveaway_channel_username"], participant["message_id"]
    ) if participant["message_id"] else None

    confirm_lines = [
        "🎊 <b>Participation Confirmed!</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        "📢 <b>Target Channel:</b>",
        esc(giveaway["giveaway_channel_title"]),
        "",
    ]
    if vote_link:
        confirm_lines += ["🗳 <b>Your Vote Post:</b>", vote_link, ""]
    confirm_lines += [
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        "✨ <b>Tip:</b>",
        "Click the button below to copy your vote link and share with friends.",
    ]
    confirm_text = "\n".join(confirm_lines)

    keyboard_rows = []
    if vote_link:
        if CopyTextButton is not None:
            keyboard_rows.append(
                [InlineKeyboardButton("📋 Copy Vote Link", copy_text=CopyTextButton(text=vote_link))]
            )
        else:
            keyboard_rows.append([InlineKeyboardButton("📋 Vote Link", url=vote_link)])
    keyboard_rows.append([InlineKeyboardButton("🏆 Leaderboard", callback_data=f"leaderboard:{giveaway['id']}")])
    keyboard_rows.append(
        [InlineKeyboardButton("💎 Buy Paid Votes", callback_data=f"buyvotes:{participant_id}")]
    )
    keyboard = InlineKeyboardMarkup(keyboard_rows)

    # Banner image + confirmation text + buttons, sent together as one message.
    return await send_banner_photo(context.bot, user.id, confirm_text, reply_markup=keyboard)


async def update_channel_post_votes(context: ContextTypes.DEFAULT_TYPE, participant, giveaway):
    """Refresh only the Vote button's count. Never touches the message text/body —
    that keeps this call cheap (no re-render of text) and avoids Telegram's
    'message is not modified' churn on every single vote."""
    if not participant["message_id"]:
        return
    vote_join_url = await get_join_url(
        context.bot, giveaway["vote_channel_id"], giveaway["vote_channel_username"]
    )
    keyboard = build_participant_keyboard(vote_join_url, participant["id"], participant["votes"])
    await safe_call(
        lambda: context.bot.edit_message_reply_markup(
            chat_id=giveaway["giveaway_channel_id"],
            message_id=participant["message_id"],
            reply_markup=keyboard,
        )
    )


async def leaderboard_text(giveaway) -> str:
    rows = await db_leaderboard(giveaway["id"])
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"🏆 <b>Leaderboard — {esc(giveaway['name'])}</b>\n"]
    if not rows:
        lines.append("No participants yet.")
    else:
        for i, r in enumerate(rows):
            prefix = medals[i] if i < 3 else f"{i + 1}."
            lines.append(f"{prefix} {esc(r['name'])} — <b>{r['votes']}</b> votes")
    return "\n".join(lines)


# ============================================================================
# /start & GENERAL HANDLERS
# ============================================================================


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await db_add_or_update_user(user.id, user.username, user.first_name)

    if await db_is_banned(user.id):
        await update.message.reply_text("🚫 You are banned from using this bot.")
        return

    args = context.args
    if args:
        token = args[0].strip()
        giveaway = await db_get_giveaway_by_token(token)
        if not giveaway or giveaway["status"] != "running":
            await update.message.reply_text("❌ Invalid or ended giveaway link.")
            return

        existing = await db_get_participant_by_user(giveaway["id"], user.id)
        if existing:
            await do_participate(user, giveaway, context)
            return

        joined = await check_membership(context.bot, giveaway["giveaway_channel_id"], user.id)
        if not joined:
            await send_join_participation_prompt(update.effective_chat.id, context, giveaway)
            return

        await do_participate(user, giveaway, context)
        return

    if is_admin(user.id):
        await update.message.reply_text(
            f"👑 Welcome back, Admin!\n\n<b>{BOT_NAME}</b> control panel is ready.",
            parse_mode=ParseMode.HTML,
            reply_markup=ADMIN_MENU,
        )
    else:
        await update.message.reply_text(
            f"👋 Welcome to <b>{BOT_NAME}</b>!\n\n"
            "Use a giveaway link shared by the organizer to participate in a giveaway.",
            parse_mode=ParseMode.HTML,
            reply_markup=ReplyKeyboardRemove(),
        )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Cancelled.", reply_markup=ADMIN_MENU)
    else:
        await update.message.reply_text("❌ Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("👑 Admin Panel", reply_markup=ADMIN_MENU)


# ============================================================================
# CALLBACK: verify_participate
# ============================================================================


async def cb_verify_participate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    token = query.data.split(":", 1)[1]

    if await db_is_banned(user.id):
        await safe_answer(query, "🚫 You are banned from using this bot.", show_alert=True)
        return

    giveaway = await db_get_giveaway_by_token(token)
    if not giveaway or giveaway["status"] != "running":
        await safe_answer(query, "❌ This giveaway is no longer active.", show_alert=True)
        return

    lock = _get_action_lock(f"participate:{giveaway['id']}:{user.id}")
    async with lock:
        joined = await check_membership(context.bot, giveaway["giveaway_channel_id"], user.id)
        if not joined:
            await safe_answer(query, "❌ You haven't joined the channel yet.", show_alert=True)
            return

        await safe_answer(query, "🎉 Participation Confirmed!", show_alert=True)
        await do_participate(user, giveaway, context)
        await safe_edit(
            query.message,
            "✅ <b>Participation Confirmed!</b>\n\nCheck the message below for your details.",
            parse_mode=ParseMode.HTML,
        )


# ============================================================================
# CALLBACK: voting
# ============================================================================


async def send_vote_join_prompt(query, context, participant, giveaway):
    gc_url = await get_join_url(context.bot, giveaway["giveaway_channel_id"], giveaway["giveaway_channel_username"])
    vc_url = await get_join_url(context.bot, giveaway["vote_channel_id"], giveaway["vote_channel_username"])
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 Join Giveaway Channel", url=gc_url)],
            [InlineKeyboardButton("📢 Join Required channel", url=vc_url)],
            [InlineKeyboardButton("✅ Verify", callback_data=f"verify_vote:{participant['id']}")],
        ]
    )
    await safe_answer(query, "⚠ Join all required channels first.", show_alert=True)
    await safe_send(
        context.bot,
        query.from_user.id,
        "⚠ <b>Join all required channels first.</b>\n\nJoin both channels below, then tap Verify.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def process_vote(query, context, participant_id: int):
    voter = query.from_user

    # Fast, local, cached checks first — never spend a Telegram API call on a
    # request we can already reject from data we already have in memory/DB.
    if await db_is_banned(voter.id):
        await safe_answer(query, "🚫 You are banned from voting.", show_alert=True)
        return

    participant = await db_get_participant(participant_id)
    if not participant:
        await safe_answer(query, "❌ Participant not found.", show_alert=True)
        return

    giveaway = await db_get_giveaway(participant["giveaway_id"])
    if not giveaway or giveaway["status"] != "running":
        await safe_answer(query, "⚠ This giveaway has ended.", show_alert=True)
        return

    # A participant can never vote for their own entry.
    if voter.id == participant["user_id"]:
        await safe_answer(query, "⚠ You cannot vote for your own participation.", show_alert=True)
        return

    # One voter = one vote per giveaway (for exactly one participant).
    existing_vote = await db_voter_vote_in_giveaway(giveaway["id"], voter.id)
    if existing_vote:
        if existing_vote["id"] == participant_id:
            await safe_answer(query, "⚠ You already voted.", show_alert=True)
        else:
            await safe_answer(
                query,
                f"⚠ You can only vote for one participant per giveaway.\n\nYou already voted for {existing_vote['name']}.",
                show_alert=True,
            )
        return

    lock = _get_action_lock(f"vote:{participant_id}:{voter.id}")
    async with lock:
        # Re-check inside the lock in case a parallel tap already voted while
        # we were waiting for it.
        existing_vote = await db_voter_vote_in_giveaway(giveaway["id"], voter.id)
        if existing_vote:
            if existing_vote["id"] == participant_id:
                await safe_answer(query, "⚠ You already voted.", show_alert=True)
            else:
                await safe_answer(
                    query,
                    f"⚠ You can only vote for one participant per giveaway.\n\nYou already voted for {existing_vote['name']}.",
                    show_alert=True,
                )
            return

        # Both membership checks run concurrently — halves the wait compared
        # to checking them one after another.
        joined_gc, joined_vc = await check_membership_both(
            context.bot, giveaway["giveaway_channel_id"], giveaway["vote_channel_id"], voter.id
        )
        if not (joined_gc and joined_vc):
            await send_vote_join_prompt(query, context, participant, giveaway)
            return

        success, new_votes, already = await db_cast_vote(participant_id, voter.id)
        if already:
            await safe_answer(query, "⚠ You already voted.", show_alert=True)
            return
        if not success:
            await safe_answer(query, "⚠ Something went wrong. Please try again.", show_alert=True)
            return

        await safe_answer(
            query,
            f"🎉 Vote Submitted Successfully!\n\nUser:\n{participant['name']}\n\nThanks for your support!",
            show_alert=True,
        )

        # Only the button is refreshed — never the message text.
        participant = await db_get_participant(participant_id)
        await update_channel_post_votes(context, participant, giveaway)


async def cb_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    participant_id = int(query.data.split(":", 1)[1])
    await process_vote(query, context, participant_id)


async def cb_verify_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    participant_id = int(query.data.split(":", 1)[1])
    await process_vote(query, context, participant_id)


# ============================================================================
# CALLBACK: leaderboard
# ============================================================================


async def cb_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    giveaway_id = int(query.data.split(":", 1)[1])
    giveaway = await db_get_giveaway(giveaway_id)
    if not giveaway:
        await safe_answer(query, "❌ Giveaway not found.", show_alert=True)
        return
    await safe_answer(query)
    text = await leaderboard_text(giveaway)
    await safe_send(context.bot, query.from_user.id, text, parse_mode=ParseMode.HTML)


# ============================================================================
# PAID VOTES (BDT) — manual bKash/Nagad-style verification flow.
# Buyer picks a package → sends money → submits Transaction ID → an admin
# taps Approve/Reject → votes are credited automatically on approval.
# ============================================================================


async def cb_buy_votes_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    participant_id = int(query.data.split(":", 1)[1])
    participant = await db_get_participant(participant_id)
    if not participant:
        await safe_answer(query, "❌ Participant not found.", show_alert=True)
        return

    if not PAYMENT_NUMBER:
        # No payment method configured — fall back to contacting the admin directly.
        await safe_answer(query)
        if SUPPORT_USERNAME:
            await safe_send(
                context.bot,
                query.from_user.id,
                "💎 <b>Buy Paid Votes</b>\n\nOnline payment isn't set up yet — please contact "
                f"@{esc(SUPPORT_USERNAME)} to buy votes for <b>{esc(participant['name'])}</b>.",
                parse_mode=ParseMode.HTML,
            )
        else:
            await safe_send(
                context.bot,
                query.from_user.id,
                "💎 <b>Buy Paid Votes</b>\n\nPaid votes aren't available right now. Please try again later.",
                parse_mode=ParseMode.HTML,
            )
        return

    await safe_answer(query)
    lines = [
        "💎 <b>Buy Paid Votes</b>",
        f"For: <b>{esc(participant['name'])}</b>",
        "",
        "Choose a package below:",
    ]
    buttons = [
        [
            InlineKeyboardButton(
                f"🗳 {votes} Votes — {CURRENCY_SYMBOL}{fmt_price(price)}",
                callback_data=f"buypkg:{participant_id}:{votes}:{price}",
            )
        ]
        for votes, price in VOTE_PACKAGES
    ]
    await safe_send(
        context.bot,
        query.from_user.id,
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cb_buy_votes_package(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, pid, votes_str, price_str = query.data.split(":", 3)
    participant_id, votes, price = int(pid), int(votes_str), float(price_str)

    participant = await db_get_participant(participant_id)
    if not participant:
        await safe_answer(query, "❌ Participant not found.", show_alert=True)
        return ConversationHandler.END

    order_id = await db_create_vote_order(participant_id, query.from_user.id, votes, price)
    context.user_data["order_id"] = order_id

    await safe_answer(query)
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="buycancel")]])
    await safe_edit(
        query.message,
        "💳 <b>Complete Your Payment</b>\n\n"
        f"🗳 Package: <b>{votes} Votes</b>\n"
        f"💰 Amount: <b>{CURRENCY_SYMBOL}{fmt_price(price)}</b>\n\n"
        f"📲 Send the amount via <b>{esc(PAYMENT_METHOD_NAME)}</b> to:\n"
        f"<code>{esc(PAYMENT_NUMBER)}</code>\n\n"
        "✅ After sending, reply here with your <b>Transaction ID</b> to submit for verification.\n\n"
        "/cancel to abort.",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )
    return BV_TRXID


async def cb_buy_votes_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    order_id = context.user_data.get("order_id")
    if order_id:
        await db_resolve_vote_order(order_id, "rejected")
    context.user_data.clear()
    await safe_answer(query, "Cancelled.")
    await safe_edit(query.message, "❌ Purchase cancelled.")
    return ConversationHandler.END


async def bv_trxid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    trx_id = update.message.text.strip()
    order_id = context.user_data.get("order_id")
    if not order_id:
        await update.message.reply_text("❌ Session expired. Please start again.")
        return ConversationHandler.END
    if not (3 <= len(trx_id) <= 64):
        await update.message.reply_text("❌ Please send a valid Transaction ID.")
        return BV_TRXID

    await db_set_order_trx(order_id, trx_id)
    order = await db_get_vote_order(order_id)
    participant = await db_get_participant(order["participant_id"])
    buyer = update.effective_user

    await update.message.reply_text(
        "✅ <b>Submitted for Verification</b>\n\n"
        "Your payment is being reviewed by an admin. You'll be notified once it's approved.",
        parse_mode=ParseMode.HTML,
    )

    admin_text = (
        "💎 <b>New Vote Purchase Request</b>\n\n"
        f"👤 Buyer: {esc(user_display_name(buyer))} (<code>{buyer.id}</code>)\n"
        f"🏆 Participant: {esc(participant['name'])} (<code>{participant['user_id']}</code>)\n"
        f"🎁 Giveaway ID: {participant['giveaway_id']}\n"
        f"🗳 Package: <b>{order['votes']} votes</b> for <b>{CURRENCY_SYMBOL}{fmt_price(order['price'])}</b>\n"
        f"🧾 Transaction ID: <code>{esc(trx_id)}</code>"
    )
    admin_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"vo_approve:{order_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"vo_reject:{order_id}"),
            ]
        ]
    )
    for admin_id in ADMIN_IDS:
        await safe_send(context.bot, admin_id, admin_text, parse_mode=ParseMode.HTML, reply_markup=admin_keyboard)

    context.user_data.clear()
    return ConversationHandler.END


async def cb_vote_order_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await safe_answer(query, "🚫 Admins only.", show_alert=True)
        return
    order_id = int(query.data.split(":", 1)[1])
    order = await db_get_vote_order(order_id)
    if not order:
        await safe_answer(query, "❌ Order not found.", show_alert=True)
        return
    if not await db_resolve_vote_order(order_id, "approved"):
        await safe_answer(query, "⚠ This order was already decided.", show_alert=True)
        return

    new_votes = await db_add_votes(order["participant_id"], order["votes"])
    participant = await db_get_participant(order["participant_id"])
    giveaway = await db_get_giveaway(participant["giveaway_id"])
    if giveaway:
        await update_channel_post_votes(context, participant, giveaway)

    await safe_answer(query, "✅ Approved and credited.", show_alert=True)
    await safe_edit(
        query.message,
        query.message.text + "\n\n✅ <b>APPROVED</b> — votes credited.",
        parse_mode=ParseMode.HTML,
    )
    await safe_send(
        context.bot,
        order["buyer_id"],
        "✅ <b>Payment Approved!</b>\n\n"
        f"🗳 {order['votes']} votes have been added to <b>{esc(participant['name'])}</b>.\n"
        f"📊 New total: <b>{new_votes}</b> votes.\n\nThanks for your support!",
        parse_mode=ParseMode.HTML,
    )


async def cb_vote_order_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_admin(query.from_user.id):
        await safe_answer(query, "🚫 Admins only.", show_alert=True)
        return
    order_id = int(query.data.split(":", 1)[1])
    order = await db_get_vote_order(order_id)
    if not order:
        await safe_answer(query, "❌ Order not found.", show_alert=True)
        return
    if not await db_resolve_vote_order(order_id, "rejected"):
        await safe_answer(query, "⚠ This order was already decided.", show_alert=True)
        return

    await safe_answer(query, "❌ Rejected.", show_alert=True)
    await safe_edit(
        query.message,
        query.message.text + "\n\n❌ <b>REJECTED</b>",
        parse_mode=ParseMode.HTML,
    )
    contact = f"\n\nContact @{esc(SUPPORT_USERNAME)} for help." if SUPPORT_USERNAME else ""
    await safe_send(
        context.bot,
        order["buyer_id"],
        "❌ <b>Payment Could Not Be Verified</b>\n\n"
        f"Your transaction ID for {order['votes']} votes couldn't be confirmed." + contact,
        parse_mode=ParseMode.HTML,
    )


# ============================================================================
# CHAT MEMBER TRACKING — CHANNEL LEAVE DETECTION
# ============================================================================


async def track_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmu = update.chat_member
    if not cmu:
        return

    chat_id = cmu.chat.id
    user_id = cmu.new_chat_member.user.id
    old_status = cmu.old_chat_member.status
    new_status = cmu.new_chat_member.status

    was_in = old_status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
    )
    now_out = new_status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED)

    if not (was_in and now_out):
        return

    giveaways = await db_giveaways_containing_channel(chat_id)
    if not giveaways:
        return

    leaver_name = user_display_name(cmu.new_chat_member.user)

    for giveaway in giveaways:
        votes = await db_votes_by_voter_in_giveaway(giveaway["id"], user_id)
        for v in votes:
            new_votes = await db_remove_vote(v["vote_id"], v["id"])
            participant = await db_get_participant(v["id"])
            await update_channel_post_votes(context, participant, giveaway)
            alert_text = (
                "⚠️ <b>Vote Deduction Alert!</b>\n\n"
                f"A user ({esc(leaver_name)}) left the required channel.\n"
                "Your vote count has been reduced.\n\n"
                f"📉 New Count: {new_votes}"
            )
            await safe_send(context.bot, participant["user_id"], alert_text, parse_mode=ParseMode.HTML)


# ============================================================================
# ADMIN: CREATE GIVEAWAY (conversation)
# ============================================================================


async def cg_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "🎁 <b>Create Giveaway</b>\n\nStep 1/3 — Send the <b>Giveaway Name</b>.\n\n/cancel to abort.",
        parse_mode=ParseMode.HTML,
        reply_markup=CANCEL_KEYBOARD,
    )
    return CG_NAME


async def cg_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("❌ Please send a valid giveaway name.")
        return CG_NAME
    context.user_data["cg_name"] = name
    await update.message.reply_text(
        "Step 2/3 — Send the <b>Giveaway Channel</b> username (e.g. @mychannel) or numeric ID.\n\n"
        "⚠ Make sure the bot is added as <b>admin</b> in that channel first.",
        parse_mode=ParseMode.HTML,
    )
    return CG_GIVEAWAY_CHANNEL


async def cg_giveaway_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_ref = update.message.text.strip()
    chat, err = await verify_channel_and_admin(context.bot, chat_ref)
    if err:
        await update.message.reply_text(err, parse_mode=ParseMode.HTML)
        return CG_GIVEAWAY_CHANNEL
    context.user_data["cg_giveaway_channel"] = chat
    await update.message.reply_text(
        f"✅ Giveaway channel verified: <b>{esc(chat.title)}</b>\n\n"
        "Step 3/3 — Send the <b>Required Vote Channel</b> username or numeric ID.\n\n"
        "⚠ Make sure the bot is added as <b>admin</b> in that channel too.",
        parse_mode=ParseMode.HTML,
    )
    return CG_VOTE_CHANNEL


async def cg_vote_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_ref = update.message.text.strip()
    chat, err = await verify_channel_and_admin(context.bot, chat_ref)
    if err:
        await update.message.reply_text(err, parse_mode=ParseMode.HTML)
        return CG_VOTE_CHANNEL

    gc = context.user_data["cg_giveaway_channel"]
    name = context.user_data["cg_name"]
    vc = chat

    token = generate_token()
    while await db_get_giveaway_by_token(token):
        token = generate_token()

    await db_create_giveaway(token, name, gc, vc)

    bot_username = context.bot_data.get("bot_username") or (await context.bot.get_me()).username
    link = f"https://t.me/{bot_username}?start={token}"

    await update.message.reply_text(
        "🎉 <b>Giveaway Created Successfully!</b>\n\n"
        f"🏆 <b>Name:</b> {esc(name)}\n"
        f"📢 <b>Giveaway Channel:</b> {esc(gc.title)}\n"
        f"📢 <b>Required Vote Channel:</b> {esc(vc.title)}\n"
        f"🔑 <b>Token:</b> <code>{esc(token)}</code>\n\n"
        f"🔗 <b>Giveaway Link:</b>\n{esc(link)}\n\n"
        "Share this link with your audience to let them participate.",
        parse_mode=ParseMode.HTML,
        reply_markup=ADMIN_MENU,
    )
    context.user_data.clear()
    return ConversationHandler.END


# ============================================================================
# ADMIN: END GIVEAWAY
# ============================================================================


async def end_giveaway_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    giveaways = await db_get_running_giveaways()
    if not giveaways:
        await update.message.reply_text("ℹ There are no running giveaways.")
        return
    buttons = [
        [InlineKeyboardButton(f"🏆 {g['name']}", callback_data=f"end_select:{g['id']}")]
        for g in giveaways
    ]
    await update.message.reply_text(
        "🏁 <b>Select a giveaway to end:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def cb_end_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    giveaway_id = int(query.data.split(":", 1)[1])
    giveaway = await db_get_giveaway(giveaway_id)
    if not giveaway or giveaway["status"] != "running":
        await safe_answer(query, "❌ Giveaway not found or already ended.", show_alert=True)
        return
    await safe_answer(query)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Confirm End", callback_data=f"end_confirm:{giveaway_id}"),
                InlineKeyboardButton("❌ Cancel", callback_data="end_cancel"),
            ]
        ]
    )
    await safe_edit(
        query.message,
        f"⚠ Are you sure you want to end <b>{esc(giveaway['name'])}</b>?",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def cb_end_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    giveaway_id = int(query.data.split(":", 1)[1])
    giveaway = await db_get_giveaway(giveaway_id)
    if not giveaway or giveaway["status"] != "running":
        await safe_answer(query, "❌ Giveaway not found or already ended.", show_alert=True)
        return

    await db_end_giveaway(giveaway_id)
    top3 = await db_top3(giveaway_id)

    medals = ["🥇", "🥈", "🥉"]
    lines = [
        "🏆 <b>GIVEAWAY ENDED!</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        "🥇 <b>Top 3 Winners</b>",
        "",
    ]
    if not top3:
        lines.append("No participants.")
    else:
        for i, p in enumerate(top3):
            lines.append(f"{medals[i]} {esc(p['name'])} — <b>{p['votes']}</b> Votes")
    lines += [
        "━━━━━━━━━━━━━━━━━━━━",
        "",
        "🎉 Congratulations!",
        "Thank you everyone for participating.",
    ]
    result_text = "\n".join(lines)

    await safe_send(context.bot, giveaway["giveaway_channel_id"], result_text, parse_mode=ParseMode.HTML)

    await safe_answer(query, "🏁 Giveaway ended!", show_alert=True)
    await safe_edit(
        query.message,
        f"✅ <b>{esc(giveaway['name'])}</b> has been ended and results posted.",
        parse_mode=ParseMode.HTML,
    )


async def cb_end_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query, "Cancelled.")
    await safe_edit(query.message, "❌ Cancelled.")



# ============================================================================
# ADMIN: BROADCAST (conversation)
# ============================================================================


async def bc_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "📢 <b>Broadcast</b>\n\n"
        "Send the content you want to broadcast: text, photo, video, or document.\n"
        "HTML formatting is supported for text/captions.\n\n/cancel to abort.",
        parse_mode=ParseMode.HTML,
        reply_markup=CANCEL_KEYBOARD,
    )
    return BC_CONTENT


async def bc_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["bc_message_id"] = update.message.message_id
    context.user_data["bc_chat_id"] = update.message.chat_id
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Send", callback_data="bc_send"),
                InlineKeyboardButton("❌ Cancel", callback_data="bc_cancel"),
            ]
        ]
    )
    await update.message.reply_text(
        "Preview above ⬆️\n\nSend this broadcast to all users?",
        reply_markup=keyboard,
    )
    return BC_CONFIRM


async def cb_bc_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query, "📤 Broadcasting...")
    message_id = context.user_data.get("bc_message_id")
    chat_id = context.user_data.get("bc_chat_id")
    if not message_id:
        await safe_edit(query.message, "❌ Broadcast session expired.")
        return ConversationHandler.END

    user_ids = await db_all_user_ids()
    sent, failed = 0, 0
    for uid in user_ids:
        try:
            await context.bot.copy_message(chat_id=uid, from_chat_id=chat_id, message_id=message_id)
            sent += 1
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.5)
            try:
                await context.bot.copy_message(chat_id=uid, from_chat_id=chat_id, message_id=message_id)
                sent += 1
            except TelegramError:
                failed += 1
        except (Forbidden, BadRequest, TelegramError):
            failed += 1
        await asyncio.sleep(0.05)

    await safe_edit(
        query.message,
        f"✅ <b>Broadcast Completed</b>\n\n📨 Sent: {sent}\n❌ Failed: {failed}",
        parse_mode=ParseMode.HTML,
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cb_bc_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await safe_answer(query, "Cancelled.")
    await safe_edit(query.message, "❌ Broadcast cancelled.")
    context.user_data.clear()
    return ConversationHandler.END


# ============================================================================
# ADMIN: ADD VOTES / REMOVE VOTES (conversations, participant picker)
# ============================================================================


def _votes_picker_keyboard(rows, total: int, page: int, page_size: int, mode: str) -> InlineKeyboardMarkup:
    sel_prefix = "avsel" if mode == "add" else "rvsel"
    page_prefix = "avpage" if mode == "add" else "rvpage"
    buttons = [
        [
            InlineKeyboardButton(
                f"👤 {p['name']} | 🏆 {p['giveaway_name']} | 🗳 {p['votes']}",
                callback_data=f"{sel_prefix}:{p['id']}",
            )
        ]
        for p in rows
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅ Prev", callback_data=f"{page_prefix}:{page - 1}"))
    if (page + 1) * page_size < total:
        nav.append(InlineKeyboardButton("Next ➡", callback_data=f"{page_prefix}:{page + 1}"))
    if nav:
        buttons.append(nav)
    return InlineKeyboardMarkup(buttons)


async def _render_votes_picker(page: int, mode: str):
    page_size = 8
    rows, total = await db_running_participants_page(page, page_size)
    label = "Add Votes" if mode == "add" else "Remove Votes"
    icon = "➕" if mode == "add" else "➖"
    if not rows:
        text = f"{icon} <b>{label}</b>\n\nℹ There are no participants in any running giveaway."
        return text, None
    text = f"{icon} <b>{label}</b>\n\nSelect a participant (Total: {total}):"
    keyboard = _votes_picker_keyboard(rows, total, page, page_size, mode)
    return text, keyboard


async def av_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["mode"] = "add"
    text, keyboard = await _render_votes_picker(0, "add")
    if keyboard is None:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=ADMIN_MENU)
        return ConversationHandler.END
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    return AV_LIST


async def rv_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["mode"] = "remove"
    text, keyboard = await _render_votes_picker(0, "remove")
    if keyboard is None:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=ADMIN_MENU)
        return ConversationHandler.END
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    return RV_LIST


async def cb_votes_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    prefix, page_str = query.data.split(":", 1)
    mode = "add" if prefix == "avpage" else "remove"
    page = int(page_str)
    text, keyboard = await _render_votes_picker(page, mode)
    await safe_answer(query)
    if keyboard is None:
        await safe_edit(query.message, text, parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    await safe_edit(query.message, text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    return AV_LIST if mode == "add" else RV_LIST


async def cb_votes_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    prefix, pid = query.data.split(":", 1)
    mode = "add" if prefix == "avsel" else "remove"
    participant = await db_get_participant(int(pid))
    if not participant:
        await safe_answer(query, "❌ Participant not found.", show_alert=True)
        return ConversationHandler.END

    context.user_data["mode"] = mode
    context.user_data["participant_id"] = participant["id"]
    await safe_answer(query)
    await safe_edit(
        query.message,
        f"👤 <b>{esc(participant['name'])}</b>\n"
        f"🆔 <code>{participant['user_id']}</code>\n"
        f"🗳 Current Votes: <b>{participant['votes']}</b>\n\n"
        f"How many votes do you want to {'add' if mode == 'add' else 'remove'}?",
        parse_mode=ParseMode.HTML,
    )
    return AV_AMOUNT if mode == "add" else RV_AMOUNT


async def avrv_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    mode = context.user_data.get("mode")
    if not text.lstrip("-").isdigit() or int(text) <= 0:
        await update.message.reply_text("❌ Please send a positive whole number.")
        return AV_AMOUNT if mode == "add" else RV_AMOUNT

    amount = int(text)
    participant_id = context.user_data.get("participant_id")
    if not participant_id:
        await update.message.reply_text("❌ Session expired. Please start again.", reply_markup=ADMIN_MENU)
        return ConversationHandler.END

    delta = amount if mode == "add" else -amount
    new_votes = await db_add_votes(participant_id, delta)

    participant = await db_get_participant(participant_id)
    giveaway = await db_get_giveaway(participant["giveaway_id"])
    if giveaway:
        await update_channel_post_votes(context, participant, giveaway)

    await update.message.reply_text(
        f"✅ Done. <b>{esc(participant['name'])}</b> now has <b>{new_votes}</b> votes.",
        parse_mode=ParseMode.HTML,
        reply_markup=ADMIN_MENU,
    )
    context.user_data.clear()
    return ConversationHandler.END


# ============================================================================
# ADMIN: BAN / UNBAN (conversations)
# ============================================================================


async def ban_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "🚫 <b>Ban User</b>\n\nSend the <b>User ID</b> to ban.\n\n/cancel to abort.",
        parse_mode=ParseMode.HTML,
        reply_markup=CANCEL_KEYBOARD,
    )
    return BAN_USERID


async def ban_userid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Please send a valid numeric User ID.")
        return BAN_USERID
    ok = await db_set_banned(int(text), True)
    if ok:
        await update.message.reply_text(f"✅ User <code>{text}</code> has been banned.", parse_mode=ParseMode.HTML, reply_markup=ADMIN_MENU)
    else:
        await update.message.reply_text("❌ User not found in database.", reply_markup=ADMIN_MENU)
    return ConversationHandler.END


async def unban_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "✅ <b>Unban User</b>\n\nSend the <b>User ID</b> to unban.\n\n/cancel to abort.",
        parse_mode=ParseMode.HTML,
        reply_markup=CANCEL_KEYBOARD,
    )
    return UNBAN_USERID


async def unban_userid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Please send a valid numeric User ID.")
        return UNBAN_USERID
    ok = await db_set_banned(int(text), False)
    if ok:
        await update.message.reply_text(f"✅ User <code>{text}</code> has been unbanned.", parse_mode=ParseMode.HTML, reply_markup=ADMIN_MENU)
    else:
        await update.message.reply_text("❌ User not found in database.", reply_markup=ADMIN_MENU)
    return ConversationHandler.END


# ============================================================================
# ADMIN: STATISTICS
# ============================================================================


async def statistics_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = await db_stats()
    text = (
        "📊 <b>Bot Statistics</b>\n\n"
        f"👥 Total Users: <b>{stats['total_users']}</b>\n"
        f"🙋 Participants: <b>{stats['total_participants']}</b>\n"
        f"🗳 Total Votes: <b>{stats['total_votes']}</b>\n"
        f"🏁 Running Giveaways: <b>{stats['running']}</b>\n"
        f"🔚 Ended Giveaways: <b>{stats['ended']}</b>"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ============================================================================
# ADMIN: ALL USERS (paginated)
# ============================================================================


async def render_users_page(page: int):
    rows, total = await db_all_users_page(page)
    page_size = 10
    total_pages = max(1, (total + page_size - 1) // page_size)

    lines = [f"👥 <b>All Users</b> (Total: {total})\n"]
    for r in rows:
        uname = f"@{r['username']}" if r["username"] else "—"
        lines.append(
            f"👤 <b>{esc(r['first_name'] or '—')}</b>\n"
            f"   Username: {esc(uname)}\n"
            f"   🆔 <code>{r['user_id']}</code>\n"
        )
    if not rows:
        lines.append("No users yet.")
    text = "\n".join(lines)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅ Prev", callback_data=f"users_page:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ➡", callback_data=f"users_page:{page + 1}"))
    keyboard = InlineKeyboardMarkup([nav]) if nav else None
    return text, keyboard


async def all_users_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, keyboard = await render_users_page(0)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def cb_users_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    page = int(query.data.split(":", 1)[1])
    text, keyboard = await render_users_page(page)
    await safe_answer(query)
    await safe_edit(query.message, text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


# ============================================================================
# ADMIN: GIVEAWAY VOTES (per-giveaway totals)
# ============================================================================


async def giveaway_votes_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await db_giveaway_vote_totals()
    if not rows:
        await update.message.reply_text("ℹ No giveaways have been created yet.")
        return

    lines = ["📈 <b>Giveaway Votes</b>\n"]
    for g in rows:
        status_icon = "🏁" if g["status"] == "running" else "🔚"
        lines.append(
            f"{status_icon} <b>{esc(g['name'])}</b> ({g['status']})\n"
            f"   🙋 Participants: <b>{g['participant_count']}</b>\n"
            f"   🗳 Total Votes: <b>{g['total_votes']}</b>\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ============================================================================
# ADMIN: FIND PARTICIPANT (lookup by User ID, conversation)
# ============================================================================


async def fp_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "🔎 <b>Find Participant</b>\n\nSend the <b>User ID</b> to look up.\n\n/cancel to abort.",
        parse_mode=ParseMode.HTML,
        reply_markup=CANCEL_KEYBOARD,
    )
    return FP_USERID


async def fp_userid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text.isdigit():
        await update.message.reply_text("❌ Please send a valid numeric User ID.")
        return FP_USERID

    target_id = int(text)
    rows = await db_get_participants_by_user(target_id)
    if not rows:
        await update.message.reply_text(
            f"ℹ No participation found for User ID <code>{target_id}</code>.",
            parse_mode=ParseMode.HTML,
            reply_markup=ADMIN_MENU,
        )
        return ConversationHandler.END

    lines = [f"🔎 <b>Results for</b> <code>{target_id}</code>\n"]
    for r in rows:
        status_icon = "🏁" if r["giveaway_status"] == "running" else "🔚"
        lines.append(
            f"{status_icon} <b>{esc(r['giveaway_name'])}</b>\n"
            f"   👤 Name: {esc(r['name'])}\n"
            f"   🗳 Votes: <b>{r['votes']}</b>\n"
        )
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=ADMIN_MENU
    )
    context.user_data.clear()
    return ConversationHandler.END


# ============================================================================
# ERROR HANDLER
# ============================================================================


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, (TimedOut, NetworkError)):
        logger.warning("Transient network error: %s", err)
    elif isinstance(err, RetryAfter):
        logger.warning("Rate limited by Telegram, retry after %s seconds", err.retry_after)
    elif isinstance(err, Forbidden):
        logger.info("Forbidden (user blocked the bot or left a chat): %s", err)
    elif isinstance(err, BadRequest):
        logger.warning("BadRequest: %s", err)
    else:
        logger.error("Unhandled exception while processing update: %s", update, exc_info=err)

    # Never leave a callback query's loading spinner stuck because a handler
    # raised before reaching its own query.answer() call.
    if isinstance(update, Update) and update.callback_query is not None:
        await safe_answer(update.callback_query, "⚠ An error occurred. Please try again.", show_alert=True)


# ============================================================================
# APP SETUP
# ============================================================================


async def post_init(application: Application) -> None:
    await init_db()
    await ensure_users_json()
    await _refresh_banned_cache()
    me = await application.bot.get_me()
    application.bot_data["bot_username"] = BOT_USERNAME or me.username
    logger.info("%s started as @%s", BOT_NAME, me.username)


async def post_shutdown(application: Application) -> None:
    await close_db()
    logger.info("%s shut down cleanly.", BOT_NAME)


# ============================================================================
# MENU SWITCHING (fixes conversations getting "stuck" when another menu
# button is tapped mid-flow — that text used to be swallowed as free-form
# input by whichever conversation was active).
# ============================================================================

MENU_ACTIONS = {
    "🎁 Create Giveaway": cg_start,
    "🏁 End Giveaway": end_giveaway_start,
    "📢 Broadcast": bc_start,
    "📊 Statistics": statistics_handler,
    "👥 All Users": all_users_handler,
    "🚫 Ban User": ban_start,
    "✅ Unban User": unban_start,
    "➕ Add Votes": av_start,
    "➖ Remove Votes": rv_start,
    "📈 Giveaway Votes": giveaway_votes_handler,
    "🔎 Find Participant": fp_start,
}
MENU_LABELS = list(MENU_ACTIONS.keys())
NOT_MENU_LABEL = ~filters.Text(MENU_LABELS)


async def menu_switch_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    action = MENU_ACTIONS.get(update.message.text)
    if action is None:
        return ConversationHandler.END
    result = await action(update, context)
    return result if isinstance(result, int) else ConversationHandler.END


MENU_SWITCH_HANDLER = MessageHandler(filters.Text(MENU_LABELS) & admin_filter(), menu_switch_fallback)


def build_application() -> Application:
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    admins = admin_filter()

    # /start and general commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("admin", admin_command, filters=admins))

    # Create Giveaway conversation
    cg_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Text(["🎁 Create Giveaway"]) & admins, cg_start)],
        states={
            CG_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND & NOT_MENU_LABEL, cg_name)],
            CG_GIVEAWAY_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND & NOT_MENU_LABEL, cg_giveaway_channel)],
            CG_VOTE_CHANNEL: [MessageHandler(filters.TEXT & ~filters.COMMAND & NOT_MENU_LABEL, cg_vote_channel)],
        },
        fallbacks=[MENU_SWITCH_HANDLER, CommandHandler("cancel", cancel_command)],
    )
    application.add_handler(cg_conv)

    # Broadcast conversation
    bc_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Text(["📢 Broadcast"]) & admins, bc_start)],
        states={
            BC_CONTENT: [
                MessageHandler(
                    (filters.TEXT | filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.ALL)
                    & ~filters.COMMAND
                    & NOT_MENU_LABEL
                    & admins,
                    bc_content,
                )
            ],
            BC_CONFIRM: [
                CallbackQueryHandler(cb_bc_send, pattern=r"^bc_send$"),
                CallbackQueryHandler(cb_bc_cancel, pattern=r"^bc_cancel$"),
            ],
        },
        fallbacks=[MENU_SWITCH_HANDLER, CommandHandler("cancel", cancel_command)],
    )
    application.add_handler(bc_conv)

    # Add votes conversation (browse & pick a participant, no manual User ID)
    av_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Text(["➕ Add Votes"]) & admins, av_start)],
        states={
            AV_LIST: [
                CallbackQueryHandler(cb_votes_select, pattern=r"^avsel:"),
                CallbackQueryHandler(cb_votes_page, pattern=r"^avpage:"),
            ],
            AV_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND & NOT_MENU_LABEL, avrv_amount)],
        },
        fallbacks=[MENU_SWITCH_HANDLER, CommandHandler("cancel", cancel_command)],
    )
    application.add_handler(av_conv)

    # Remove votes conversation (browse & pick a participant, no manual User ID)
    rv_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Text(["➖ Remove Votes"]) & admins, rv_start)],
        states={
            RV_LIST: [
                CallbackQueryHandler(cb_votes_select, pattern=r"^rvsel:"),
                CallbackQueryHandler(cb_votes_page, pattern=r"^rvpage:"),
            ],
            RV_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND & NOT_MENU_LABEL, avrv_amount)],
        },
        fallbacks=[MENU_SWITCH_HANDLER, CommandHandler("cancel", cancel_command)],
    )
    application.add_handler(rv_conv)

    # Ban conversation
    ban_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Text(["🚫 Ban User"]) & admins, ban_start)],
        states={BAN_USERID: [MessageHandler(filters.TEXT & ~filters.COMMAND & NOT_MENU_LABEL, ban_userid)]},
        fallbacks=[MENU_SWITCH_HANDLER, CommandHandler("cancel", cancel_command)],
    )
    application.add_handler(ban_conv)

    # Unban conversation
    unban_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Text(["✅ Unban User"]) & admins, unban_start)],
        states={UNBAN_USERID: [MessageHandler(filters.TEXT & ~filters.COMMAND & NOT_MENU_LABEL, unban_userid)]},
        fallbacks=[MENU_SWITCH_HANDLER, CommandHandler("cancel", cancel_command)],
    )
    application.add_handler(unban_conv)

    # Find Participant conversation
    fp_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Text(["🔎 Find Participant"]) & admins, fp_start)],
        states={FP_USERID: [MessageHandler(filters.TEXT & ~filters.COMMAND & NOT_MENU_LABEL, fp_userid)]},
        fallbacks=[MENU_SWITCH_HANDLER, CommandHandler("cancel", cancel_command)],
    )
    application.add_handler(fp_conv)

    # Buy Paid Votes conversation (BDT, manual verification)
    buy_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_buy_votes_package, pattern=r"^buypkg:")],
        states={
            BV_TRXID: [
                CallbackQueryHandler(cb_buy_votes_cancel, pattern=r"^buycancel$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND & NOT_MENU_LABEL, bv_trxid),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(cb_buy_votes_cancel, pattern=r"^buycancel$"),
            CommandHandler("cancel", cancel_command),
        ],
    )
    application.add_handler(buy_conv)

    # Simple admin actions
    application.add_handler(MessageHandler(filters.Text(["🏁 End Giveaway"]) & admins, end_giveaway_start))
    application.add_handler(MessageHandler(filters.Text(["📊 Statistics"]) & admins, statistics_handler))
    application.add_handler(MessageHandler(filters.Text(["👥 All Users"]) & admins, all_users_handler))
    application.add_handler(MessageHandler(filters.Text(["📈 Giveaway Votes"]) & admins, giveaway_votes_handler))

    # Callback queries (non-conversation)
    application.add_handler(CallbackQueryHandler(cb_verify_participate, pattern=r"^verify_participate:"))
    application.add_handler(CallbackQueryHandler(cb_vote, pattern=r"^vote:"))
    application.add_handler(CallbackQueryHandler(cb_verify_vote, pattern=r"^verify_vote:"))
    application.add_handler(CallbackQueryHandler(cb_leaderboard, pattern=r"^leaderboard:"))
    application.add_handler(CallbackQueryHandler(cb_buy_votes_menu, pattern=r"^buyvotes:"))
    application.add_handler(CallbackQueryHandler(cb_vote_order_approve, pattern=r"^vo_approve:"))
    application.add_handler(CallbackQueryHandler(cb_vote_order_reject, pattern=r"^vo_reject:"))
    application.add_handler(CallbackQueryHandler(cb_end_select, pattern=r"^end_select:"))
    application.add_handler(CallbackQueryHandler(cb_end_confirm, pattern=r"^end_confirm:"))
    application.add_handler(CallbackQueryHandler(cb_end_cancel, pattern=r"^end_cancel$"))
    application.add_handler(CallbackQueryHandler(cb_users_page, pattern=r"^users_page:"))

    # Chat member tracking (channel leave detection)
    application.add_handler(ChatMemberHandler(track_chat_member, ChatMemberHandler.CHAT_MEMBER))

    # Global fallback /cancel (outside conversations)
    application.add_handler(CommandHandler("cancel", cancel_command))

    application.add_error_handler(error_handler)

    return application


def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        raise SystemExit(
            "Please set your bot token via the BOT_TOKEN environment variable "
            "or edit the BOT_TOKEN constant in main.py."
        )

    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
