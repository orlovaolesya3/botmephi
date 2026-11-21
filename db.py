import aiosqlite
import json
import os
from contextlib import asynccontextmanager
from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  tg_id INTEGER PRIMARY KEY,
  username TEXT,
  name TEXT,
  gender TEXT,
  institute TEXT,
  course INTEGER,
  dorm TEXT,
  bio TEXT,
  photos TEXT,
  want_gender TEXT,
  want_institute TEXT,
  want_course INTEGER,
  tags_self TEXT,
  tags_want TEXT,
  is_banned INTEGER DEFAULT 0,
  views INTEGER DEFAULT 0,
  last_like_reminder TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS likes (
  from_id INTEGER,
  to_id INTEGER,
  is_like INTEGER,
  hidden INTEGER DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (from_id, to_id)
);
CREATE INDEX IF NOT EXISTS idx_likes_to ON likes(to_id);
CREATE INDEX IF NOT EXISTS idx_likes_from ON likes(from_id);
CREATE TABLE IF NOT EXISTS matches (
  user1_id INTEGER,
  user2_id INTEGER,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (user1_id, user2_id)
);
CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  reporter_id INTEGER,
  target_id INTEGER,
  reason TEXT,
  status TEXT DEFAULT 'pending',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


@asynccontextmanager
async def conn():
    db = await aiosqlite.connect(DB_PATH, timeout=10)
    db.row_factory = aiosqlite.Row
    try:
        yield db
    finally:
        await db.close()


async def init():
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)

    async with conn() as db:
        await db.executescript(SCHEMA)
        await db.commit()
        # dorm
        try:
            cur = await db.execute("PRAGMA table_info(users)")
            cols = [r[1] for r in await cur.fetchall()]
            if "dorm" not in cols:
                await db.execute("ALTER TABLE users ADD COLUMN dorm TEXT")
                await db.commit()
        except Exception:
            pass
        # views
        try:
            cur = await db.execute("PRAGMA table_info(users)")
            cols = [r[1] for r in await cur.fetchall()]
            if "views" not in cols:
                await db.execute("ALTER TABLE users ADD COLUMN views INTEGER DEFAULT 0")
                await db.commit()
        except Exception:
            pass
        # last_like_reminder
        try:
            cur = await db.execute("PRAGMA table_info(users)")
            cols = [r[1] for r in await cur.fetchall()]
            if "last_like_reminder" not in cols:
                await db.execute("ALTER TABLE users ADD COLUMN last_like_reminder TEXT")
                await db.commit()
        except Exception:
            pass
        # likes.hidden
        try:
            cur = await db.execute("PRAGMA table_info(likes)")
            cols = [r[1] for r in await cur.fetchall()]
            if "hidden" not in cols:
                await db.execute("ALTER TABLE likes ADD COLUMN hidden INTEGER DEFAULT 0")
                await db.commit()
        except Exception:
            pass


def _u(row):
    if not row:
        return None
    u = dict(row)
    for k in ("photos", "tags_self", "tags_want"):
        u[k] = json.loads(u[k] or "[]")
    return u


async def get(uid):
    async with conn() as db:
        cur = await db.execute("SELECT * FROM users WHERE tg_id=?", (uid,))
        return _u(await cur.fetchone())


async def save(uid, **f):
    for k in ("photos", "tags_self", "tags_want"):
        if k in f:
            f[k] = json.dumps(f[k], ensure_ascii=False)
    async with conn() as db:
        cur = await db.execute("SELECT 1 FROM users WHERE tg_id=?", (uid,))
        if await cur.fetchone():
            if f:
                sets = ",".join(f"{k}=?" for k in f)
                await db.execute(f"UPDATE users SET {sets} WHERE tg_id=?",
                                 (*f.values(), uid))
        else:
            f["tg_id"] = uid
            cols = ",".join(f)
            qs = ",".join("?" * len(f))
            await db.execute(f"INSERT INTO users ({cols}) VALUES ({qs})",
                             tuple(f.values()))
        await db.commit()


async def registered(uid):
    async with conn() as db:
        cur = await db.execute(
            "SELECT 1 FROM users WHERE tg_id=? AND photos IS NOT NULL AND photos!='[]'",
            (uid,))
        return (await cur.fetchone()) is not None


async def active_users():
    async with conn() as db:
        cur = await db.execute(
            "SELECT * FROM users WHERE is_banned=0 AND photos IS NOT NULL AND photos!='[]'")
        return [_u(r) for r in await cur.fetchall()]


async def all_users():
    async with conn() as db:
        cur = await db.execute("SELECT * FROM users ORDER BY created_at DESC")
        return [_u(r) for r in await cur.fetchall()]


async def my_likes(uid):
    async with conn() as db:
        cur = await db.execute(
            "SELECT to_id, is_like, created_at FROM likes WHERE from_id=?", (uid,))
        return {r["to_id"]: (r["is_like"], r["created_at"]) for r in await cur.fetchall()}


async def liked_me(uid):
    async with conn() as db:
        cur = await db.execute("""
            SELECT u.* FROM likes l
            JOIN users u ON u.tg_id = l.from_id
            WHERE l.to_id=? AND l.is_like=1 AND u.is_banned=0
              AND (l.hidden IS NULL OR l.hidden=0)
              AND NOT EXISTS (
                SELECT 1 FROM likes l2
                WHERE l2.from_id=? AND l2.to_id=l.from_id AND l2.is_like=1
              )
        """, (uid, uid))
        return [_u(r) for r in await cur.fetchall()]


async def count_likes_me(uid):
    async with conn() as db:
        cur = await db.execute("""
            SELECT COUNT(*) FROM likes l
            JOIN users u ON u.tg_id = l.from_id
            WHERE l.to_id=? AND l.is_like=1 AND u.is_banned=0
              AND (l.hidden IS NULL OR l.hidden=0)
              AND NOT EXISTS (
                SELECT 1 FROM likes l2
                WHERE l2.from_id=? AND l2.to_id=l.from_id AND l2.is_like=1
              )
        """, (uid, uid))
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def count_by_gender():
    async with conn() as db:
        cur = await db.execute("""
            SELECT gender, COUNT(*) FROM users
            WHERE is_banned=0 AND photos IS NOT NULL AND photos!='[]'
            GROUP BY gender
        """)
        result = {"m": 0, "f": 0}
        for row in await cur.fetchall():
            g = row[0]
            if g in result:
                result[g] = int(row[1])
        return result["m"], result["f"]


async def all_active_ids():
    async with conn() as db:
        cur = await db.execute(
            "SELECT tg_id FROM users WHERE is_banned=0 AND photos IS NOT NULL AND photos!='[]'"
        )
        return [int(r[0]) for r in await cur.fetchall()]


async def set_like(f, t, v):
    async with conn() as db:
        await db.execute(
            "INSERT OR REPLACE INTO likes (from_id,to_id,is_like,hidden) VALUES (?,?,?,0)",
            (f, t, v))
        await db.commit()


async def hide_from_incoming(me, other):
    async with conn() as db:
        await db.execute(
            "UPDATE likes SET hidden=1 WHERE from_id=? AND to_id=?",
            (other, me))
        await db.commit()


async def match_exists(a, b):
    async with conn() as db:
        cur = await db.execute(
            "SELECT 1 FROM likes WHERE from_id=? AND to_id=? AND is_like=1", (b, a))
        return (await cur.fetchone()) is not None


async def add_match(a, b):
    x, y = sorted((a, b))
    async with conn() as db:
        await db.execute(
            "INSERT OR IGNORE INTO matches (user1_id, user2_id) VALUES (?,?)",
            (x, y),
        )
        await db.commit()


async def increment_views(uid):
    async with conn() as db:
        await db.execute(
            "UPDATE users SET views = COALESCE(views,0) + 1 WHERE tg_id=?",
            (uid,))
        await db.commit()


async def top_users(limit=10):
    """Топ по лайкам за текущую неделю (с понедельника 00:00)."""
    async with conn() as db:
        cur = await db.execute("""
            SELECT u.*, COUNT(l.from_id) AS likes_count
            FROM users u
            JOIN likes l ON l.to_id = u.tg_id
            WHERE l.is_like=1
              AND date(l.created_at) >= date('now', 'weekday 1', '-7 days')
              AND u.is_banned=0
              AND u.photos IS NOT NULL AND u.photos != '[]'
            GROUP BY u.tg_id
            ORDER BY likes_count DESC
            LIMIT ?
        """, (limit,))
        return [_u(r) for r in await cur.fetchall()]


async def my_matches(uid):
    """Все, с кем взаимный лайк. Возвращает список dict (анкет собеседников)."""
    async with conn() as db:
        cur = await db.execute("""
            SELECT u.* FROM matches m
            JOIN users u ON (u.tg_id = m.user2_id AND m.user1_id = ?)
                         OR (u.tg_id = m.user1_id AND m.user2_id = ?)
            WHERE u.is_banned = 0
        """, (uid, uid))
        return [_u(r) for r in await cur.fetchall()]


async def get_users_to_remind(limit=500):
    """
    Пользователи, у которых есть непросмотренные лайки
    и которым ещё не отправляли напоминание СЕГОДНЯ.
    Сравнение по дате (YYYY-MM-DD).
    """
    async with conn() as db:
        cur = await db.execute("""
            SELECT u.tg_id FROM users u
            WHERE u.is_banned = 0
              AND u.photos IS NOT NULL AND u.photos != '[]'
              AND (
                u.last_like_reminder IS NULL
                OR date(u.last_like_reminder) < date('now')
              )
              AND EXISTS (
                SELECT 1 FROM likes l
                WHERE l.to_id = u.tg_id
                  AND l.is_like = 1
                  AND (l.hidden IS NULL OR l.hidden = 0)
                  AND NOT EXISTS (
                    SELECT 1 FROM likes l2
                    WHERE l2.from_id = u.tg_id
                      AND l2.to_id = l.from_id
                      AND l2.is_like = 1
                  )
              )
            LIMIT ?
        """, (limit,))
        return [int(r[0]) for r in await cur.fetchall()]


async def set_like_reminder(uid):
    """Ставит отметку: напоминание отправлено сейчас."""
    async with conn() as db:
        await db.execute(
            "UPDATE users SET last_like_reminder = CURRENT_TIMESTAMP WHERE tg_id=?",
            (uid,))
        await db.commit()


async def add_report(rep, tgt, reason):
    async with conn() as db:
        await db.execute(
            "INSERT INTO reports (reporter_id,target_id,reason) VALUES (?,?,?)",
            (rep, tgt, reason))
        await db.commit()


async def reports():
    async with conn() as db:
        cur = await db.execute("""
            SELECT r.*, u.name, u.username, u.photos
            FROM reports r JOIN users u ON u.tg_id=r.target_id
            WHERE r.status='pending' ORDER BY r.created_at DESC
        """)
        out = []
        for r in await cur.fetchall():
            d = dict(r)
            d["photos"] = json.loads(d["photos"] or "[]")
            out.append(d)
        return out


async def report_done(rid, status):
    async with conn() as db:
        await db.execute("UPDATE reports SET status=? WHERE id=?", (status, rid))
        await db.commit()


async def ban(uid, v):
    async with conn() as db:
        await db.execute("UPDATE users SET is_banned=? WHERE tg_id=?", (v, uid))
        await db.commit()


async def stats():
    async with conn() as db:
        async def one(q):
            c = await db.execute(q)
            return (await c.fetchone())[0]
        return {
            "users": await one("SELECT COUNT(*) FROM users"),
            "likes": await one("SELECT COUNT(*) FROM likes WHERE is_like=1"),
            "matches": await one("SELECT COUNT(*) FROM matches"),
            "reports": await one("SELECT COUNT(*) FROM reports WHERE status='pending'"),
        }