import asyncio
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InputMediaPhoto, TelegramObject,
    ReplyKeyboardMarkup, KeyboardButton, FSInputFile,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import db
from config import (
    BOT_TOKEN, ADMIN_IDS, MAX_PHOTOS, MAX_TAGS, BIO_MAX, SKIP_COOLDOWN_DAYS, DB_PATH
)
from constants import INSTITUTES, COURSES, GENDERS, REPORT_REASONS, DORMITORIES

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

router = Router()

FEEDS = {}
LIKES = {}
ACTIVE = {}
LAST_ALBUM = {}
MATCHES_CACHE = {}
BROADCAST_LOCK = {"running": False, "sent": 0, "failed": 0, "total": 0}

ADMIN_CONTACT = "@str_ls"

# Кнопки
BTN_FEED = "Смотреть анкеты"
BTN_LIKES = "Кто меня лайкнул"
BTN_ME = "Моя анкета"
BTN_INFO = "Инфо"
BTN_TOP = "Топ недели"
BTN_MATCHES = "Взаимные лайки"
BTN_LIKE = "Лайк"
BTN_SKIP = "Скип"
BTN_REPORT = "Жалоба"
BTN_MENU = "Меню"
BTN_BACK = "В меню"
BTN_CANCEL = "Отмена"
BTN_RESTART = "Начать заново"
BTN_SKIP_STEP = "Пропустить"

# Лимиты
NAME_MAX = 50
TAG_MAX_LEN = 100
NAME_SHOW = 50
BIO_SHOW = 1500
TAG_SHOW = 100
TAGS_SHOW_MAX = 10
CAPTION_LIMIT = 1000
TEXT_LIMIT = 3500

# Размер чанка списка матчей
MATCHES_CHUNK = 40

# Час напоминания о лайках (в UTC, у Amvera контейнер в UTC)
REMINDER_HOUR_UTC = 19


# ---------- ЭКРАНИРОВАНИЕ ----------

def esc(s):
    if not s:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ---------- МИДЛВАРЬ: ОШИБКИ ----------

class ErrorMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except TelegramNetworkError as e:
            logging.warning(f"network error: {e}")
            return
        except Exception as e:
            logging.exception(f"handler error: {e}")
            return


# ---------- МИДЛВАРЬ: БАН ----------

class BanMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or user.id in ADMIN_IDS:
            return await handler(event, data)

        u = await db.get(user.id)
        if u and u.get("is_banned"):
            text = "Ваша анкета заблокирована модератором."
            try:
                if isinstance(event, CallbackQuery):
                    await event.answer(text, show_alert=True)
                elif isinstance(event, Message):
                    await event.answer(text)
            except Exception:
                pass
            return
        return await handler(event, data)


# ---------- МИДЛВАРЬ: АЛЬБОМЫ ----------

class AlbumMiddleware(BaseMiddleware):
    def __init__(self, latency: float = 0.5):
        self.latency = latency
        self.albums = defaultdict(list)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or not event.media_group_id:
            return await handler(event, data)

        gid = event.media_group_id
        self.albums[gid].append(event)

        if len(self.albums[gid]) > 1:
            return

        await asyncio.sleep(self.latency)

        data["album"] = self.albums.pop(gid)
        return await handler(event, data)


# ---------- КЛАВИАТУРЫ ----------

def kb_main():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_FEED)],
            [KeyboardButton(text=BTN_LIKES), KeyboardButton(text=BTN_ME)],
            [KeyboardButton(text=BTN_MATCHES), KeyboardButton(text=BTN_TOP)],
            [KeyboardButton(text=BTN_INFO)],
        ],
        resize_keyboard=True,
    )


def kb_choose():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_LIKE), KeyboardButton(text=BTN_SKIP)],
            [KeyboardButton(text=BTN_REPORT), KeyboardButton(text=BTN_MENU)],
        ],
        resize_keyboard=True,
    )


def kb_profile():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Изменить био"), KeyboardButton(text="Изменить запрос")],
            [KeyboardButton(text="Изменить фото")],
            [KeyboardButton(text="Заполнить анкету заново")],
            [KeyboardButton(text="Удалить анкету")],
            [KeyboardButton(text=BTN_BACK)],
        ],
        resize_keyboard=True,
    )


def kb_cancel():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True,
    )


def kb_skip_step():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_SKIP_STEP)]],
        resize_keyboard=True,
    )


def kb_cancel_or_skip():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SKIP_STEP)],
            [KeyboardButton(text=BTN_CANCEL)],
        ],
        resize_keyboard=True,
    )


def kb_start_over():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_RESTART)]],
        resize_keyboard=True,
    )


def kb_broadcast_cancel():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="/cancel")]],
        resize_keyboard=True,
    )


def kb_dict(prefix, d, any_=False, cols=2):
    b = InlineKeyboardBuilder()
    for k, v in d.items():
        b.button(text=v, callback_data=f"{prefix}:{k}")
    if any_:
        b.button(text="Не важно", callback_data=f"{prefix}:any")
    b.adjust(cols)
    return b.as_markup()


def kb_confirm_delete():
    b = InlineKeyboardBuilder()
    b.button(text="Да, удалить", callback_data="confirm_delete:yes")
    b.button(text="Отмена", callback_data="confirm_delete:no")
    b.adjust(1)
    return b.as_markup()


# ---------- КАРТОЧКА ----------

def card(u):
    inst = INSTITUTES.get(u['institute'], '?')
    course = COURSES.get(u['course'], '?')
    gender = GENDERS.get(u['gender'], '?')
    dorm_code = u.get('dorm') if isinstance(u, dict) else None
    dorm = DORMITORIES.get(dorm_code, '') if dorm_code else ''

    head = f"{gender}, {course}, {inst}"
    if dorm:
        head += f", {dorm}"

    name = esc((u.get("name") or "").strip())[:NAME_SHOW]

    lines = [
        f"<b>{name}</b>",
        esc(head),
    ]

    if u.get("bio"):
        bio = esc(u["bio"].strip())[:BIO_SHOW]
        lines += ["", bio]

    tags_list = (u.get("tags_want") or [])[:TAGS_SHOW_MAX]
    if tags_list:
        tags = "\n".join(
            f"<blockquote>{esc(t)[:TAG_SHOW]}</blockquote>" for t in tags_list
        )
        lines += ["", "Ищет:", tags]

    text = "\n".join(lines)

    if len(text) > TEXT_LIMIT:
        text = text[:TEXT_LIMIT - 3] + "..."

    return text


def contact(u):
    return f"@{u['username']}" if u["username"] else f"tg://user?id={u['tg_id']}"


async def likes_header(uid: int) -> str:
    try:
        n = await db.count_likes_me(uid)
    except Exception as e:
        logging.exception(f"count_likes_me error: {e}")
        return ""
    if n <= 0:
        return ""
    return f"У тебя новых лайков: <b>{n}</b>\n\n"


async def send_card(bot, u, uid, header: str = "", count_view: bool = False):
    text = (header + card(u)) if header else card(u)

    if len(text) > TEXT_LIMIT:
        text = text[:TEXT_LIMIT - 3] + "..."

    photos = u["photos"] or []
    ids = []

    if not photos:
        sent = await bot.send_message(uid, text, parse_mode="HTML")
        ids.append(sent.message_id)
    elif len(photos) == 1:
        if len(text) <= CAPTION_LIMIT:
            try:
                sent = await bot.send_photo(uid, photos[0], caption=text,
                                            parse_mode="HTML")
                ids.append(sent.message_id)
            except Exception as e:
                logging.warning(f"send_photo failed: {e}")
                sent = await bot.send_photo(uid, photos[0])
                ids.append(sent.message_id)
                msg = await bot.send_message(uid, text, parse_mode="HTML")
                ids.append(msg.message_id)
        else:
            sent = await bot.send_photo(uid, photos[0])
            ids.append(sent.message_id)
            msg = await bot.send_message(uid, text, parse_mode="HTML")
            ids.append(msg.message_id)
    else:
        media = [InputMediaPhoto(media=p) for p in photos]
        try:
            group = await bot.send_media_group(uid, media)
            ids.extend(m.message_id for m in group)
        except Exception as e:
            logging.warning(f"send_media_group failed: {e}")
        msg = await bot.send_message(uid, text, parse_mode="HTML")
        ids.append(msg.message_id)

    LAST_ALBUM[uid] = ids
    ACTIVE[uid] = u

    if count_view and u["tg_id"] != uid:
        try:
            await db.increment_views(u["tg_id"])
        except Exception as e:
            logging.exception(f"increment_views error: {e}")


async def _send_match_card(bot, uid: int, other: dict):
    name = esc(other.get("name") or "Без имени")[:NAME_SHOW]
    inst = INSTITUTES.get(other.get("institute"), "")
    course = COURSES.get(other.get("course"), "")
    link = contact(other)

    head = ", ".join(p for p in [course, inst] if p)

    text = "Взаимная симпатия!\n\n"
    text += f"<b>{name}</b>"
    if head:
        text += f"\n{esc(head)}"
    text += f"\n\nКонтакт: {esc(link)}"
    text += "\n\nНапиши первым(ой)"

    photos = other.get("photos") or []

    try:
        if not photos:
            await bot.send_message(uid, text, parse_mode="HTML")
        elif len(photos) == 1:
            await bot.send_photo(uid, photos[0], caption=text, parse_mode="HTML")
        else:
            media = [InputMediaPhoto(media=p) for p in photos]
            await bot.send_media_group(uid, media)
            await bot.send_message(uid, text, parse_mode="HTML")
    except Exception as e:
        logging.warning(f"send_match_card failed for {uid}: {e}")
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
        except Exception as e2:
            logging.warning(f"send_match_card fallback failed for {uid}: {e2}")


async def _notify_match(bot, me, tid):
    try:
        other, mine = await asyncio.gather(db.get(tid), db.get(me))
        if not other or not mine:
            return
        await asyncio.gather(
            _send_match_card(bot, me, other),
            _send_match_card(bot, tid, mine),
            return_exceptions=True,
        )
    except Exception as e:
        logging.exception(f"notify_match error: {e}")


# ---------- СКОРИНГ И ЛЕНТА ----------

def _score(me, c, my_course, my_inst, my_dorm, my_gender, mw):
    if me["want_gender"] != "any" and c["gender"] != me["want_gender"]:
        return None
    if c["want_gender"] != "any" and c["want_gender"] != my_gender:
        return None

    s = 0.0

    c_course = c.get("course") or 0
    if my_course and c_course:
        s += max(0, 10 - abs(my_course - c_course) * 3)

    if my_inst and c.get("institute") == my_inst:
        s += 6.0

    c_dorm = c.get("dorm")
    if my_dorm and c_dorm and my_dorm != "none" and c_dorm != "none":
        s += 4.0 if my_dorm == c_dorm else -0.5

    s += 4 * len(mw & set(c["tags_want"]))
    return s


async def feed_for(me):
    users = await db.active_users()
    my_prev = await db.my_likes(me["tg_id"])

    my_course = me.get("course") or 0
    my_inst = me.get("institute")
    my_dorm = me.get("dorm")
    my_gender = me.get("gender")
    mw = set(me["tags_want"])

    fresh = []
    skipped = []

    for c in users:
        if c["tg_id"] == me["tg_id"]:
            continue

        sc = _score(me, c, my_course, my_inst, my_dorm, my_gender, mw)
        if sc is None:
            continue

        prev = my_prev.get(c["tg_id"])
        if prev is None:
            fresh.append((sc, c))
        else:
            is_like, _ts = prev
            if is_like == 1:
                continue
            skipped.append((sc, c))

    fresh.sort(key=lambda x: -x[0])
    skipped.sort(key=lambda x: -x[0])

    return [c for _, c in fresh] + [c for _, c in skipped]


# ---------- ПОКАЗ ----------

async def show_next_feed(bot, uid):
    me = await db.get(uid)
    if not me:
        await bot.send_message(uid, "Сначала /start")
        return

    nxt_list = FEEDS.get(uid, [])
    if not nxt_list:
        full = await feed_for(me)
        nxt_list = full
        if not nxt_list:
            ACTIVE.pop(uid, None)
            header = await likes_header(uid)
            await bot.send_message(uid, header + "Анкеты закончились. Загляни позже.",
                                   reply_markup=kb_main())
            return

    u = nxt_list.pop(0)
    FEEDS[uid] = nxt_list

    header = await likes_header(uid)
    await send_card(bot, u, uid, header=header, count_view=True)


async def show_next_like(bot, uid):
    stack = LIKES.get(uid, [])
    if not stack:
        ACTIVE.pop(uid, None)
        await bot.send_message(uid, "Больше нет входящих лайков.",
                               reply_markup=kb_main())
        return

    u = stack[0]
    await send_card(bot, u, uid, count_view=True)


async def show_menu(m: Message, state: FSMContext):
    await state.clear()
    ACTIVE.pop(m.from_user.id, None)
    header = await likes_header(m.from_user.id)
    await m.answer(header + "Главное меню:", reply_markup=kb_main())


# ---------- РЕГИСТРАЦИЯ ----------

class Reg(StatesGroup):
    name = State()
    gender = State()
    inst = State()
    course = State()
    dorm = State()
    bio = State()
    wgender = State()
    tags_want = State()
    photos = State()


class Ed(StatesGroup):
    bio = State()
    tags = State()
    photos = State()


class MatchesState(StatesGroup):
    waiting_number = State()


def parse_tags(t):
    t = (t or "").strip().lower()
    if t in ("", "-"):
        return []
    tags = []
    for x in t.split(","):
        x = x.strip().lstrip("#")
        if not x:
            continue
        tags.append(x[:TAG_MAX_LEN])
        if len(tags) >= MAX_TAGS:
            break
    return tags


@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    if await db.registered(m.from_user.id):
        header = await likes_header(m.from_user.id)
        await m.answer(header + "Главное меню:", reply_markup=kb_main())
        return
    await m.answer("Это «Знакомства МИФИ».\n\nКак тебя зовут?")
    await state.set_state(Reg.name)


@router.message(Command("help"))
async def cmd_help(m: Message):
    text = (
        "Как пользоваться ботом:\n\n"
        "1. Заполни анкету: имя, пол, институт, курс, общежитие, пара слов о себе.\n"
        "2. Укажи, кого ищешь и что тебе интересно (общение, спорт, учёба).\n"
        "3. Загрузи до 3 фото одним сообщением.\n"
        "4. Жми «Смотреть анкеты» и ставь Лайк или Скип.\n"
        "5. Когда лайки совпадут — бот пришлёт контакт собеседника.\n\n"
        "Кнопки внизу: «Смотреть анкеты», «Кто меня лайкнул», «Моя анкета», «Взаимные лайки», «Топ недели», «Инфо»."
    )
    await m.answer(text)


@router.message(Command("about"))
async def cmd_about(m: Message):
    text = (
        "Знакомства МИФИ\n\n"
        "Бот для знакомств среди студентов НИЯУ МИФИ.\n"
        "Подбирает людей по курсу, институту, общежитию и интересам."
    )
    await m.answer(text)


@router.message(Reg.name)
async def r_name(m: Message, state: FSMContext):
    t = (m.text or "").strip()[:NAME_MAX]
    if not t:
        await m.answer(f"Введи имя (до {NAME_MAX} символов)")
        return
    await state.update_data(name=t)
    await m.answer("Твой пол:", reply_markup=kb_dict("g", GENDERS))
    await state.set_state(Reg.gender)


@router.callback_query(Reg.gender, F.data.startswith("g:"))
async def r_gender(c: CallbackQuery, state: FSMContext):
    await state.update_data(gender=c.data.split(":")[1])
    await c.message.edit_text("Твой институт:", reply_markup=kb_dict("i", INSTITUTES))
    await state.set_state(Reg.inst)


@router.callback_query(Reg.inst, F.data.startswith("i:"))
async def r_inst(c: CallbackQuery, state: FSMContext):
    await state.update_data(institute=c.data.split(":")[1])
    await c.message.edit_text("Курс:", reply_markup=kb_dict("c", COURSES, cols=3))
    await state.set_state(Reg.course)


@router.callback_query(Reg.course, F.data.startswith("c:"))
async def r_course(c: CallbackQuery, state: FSMContext):
    await state.update_data(course=int(c.data.split(":")[1]))
    await c.message.edit_text(
        "Ты живёшь в общежитии?",
        reply_markup=kb_dict("d", DORMITORIES, cols=3),
    )
    await state.set_state(Reg.dorm)


@router.callback_query(Reg.dorm, F.data.startswith("d:"))
async def r_dorm(c: CallbackQuery, state: FSMContext):
    await state.update_data(dorm=c.data.split(":")[1])
    await c.message.answer(
        f"О себе (до {BIO_MAX} символов) или нажми «Пропустить»:",
        reply_markup=kb_skip_step(),
    )
    await state.set_state(Reg.bio)


@router.message(Reg.bio, F.text == BTN_SKIP_STEP)
async def r_bio_skip(m: Message, state: FSMContext):
    await state.update_data(bio="")
    await m.answer("Кого ищешь:", reply_markup=kb_dict("wg", GENDERS, any_=True))
    await state.set_state(Reg.wgender)


@router.message(Reg.bio)
async def r_bio(m: Message, state: FSMContext):
    t = (m.text or "").strip()[:BIO_MAX]
    await state.update_data(bio=t)
    await m.answer("Кого ищешь:", reply_markup=kb_dict("wg", GENDERS, any_=True))
    await state.set_state(Reg.wgender)


@router.callback_query(Reg.wgender, F.data.startswith("wg:"))
async def r_wg(c: CallbackQuery, state: FSMContext):
    await state.update_data(want_gender=c.data.split(":")[1])
    await c.message.answer(
        f"Что ищешь? Перечисли через запятую (до {MAX_TAGS}).\n"
        f"Слова пиши в начальной форме, например: общение, спорт, учёба.\n\n"
        f"Или нажми «Пропустить»:",
        reply_markup=kb_skip_step(),
    )
    await state.set_state(Reg.tags_want)


@router.message(Reg.tags_want, F.text == BTN_SKIP_STEP)
async def r_tw_skip(m: Message, state: FSMContext):
    await state.update_data(tags_want=[], photos=[])
    await m.answer(f"Пришли до {MAX_PHOTOS} фото одним сообщением.")
    await state.set_state(Reg.photos)


@router.message(Reg.tags_want)
async def r_tw(m: Message, state: FSMContext):
    await state.update_data(tags_want=parse_tags(m.text), photos=[])
    await m.answer(f"Пришли до {MAX_PHOTOS} фото одним сообщением.")
    await state.set_state(Reg.photos)


@router.message(Reg.photos, F.photo, F.media_group_id)
async def r_photo_album(m: Message, state: FSMContext, album: list[Message]):
    d = await state.get_data()
    ps = [msg.photo[-1].file_id for msg in album[:MAX_PHOTOS]]
    await db.save(
        m.from_user.id,
        username=m.from_user.username,
        name=d["name"],
        gender=d["gender"],
        institute=d["institute"],
        course=d["course"],
        dorm=d.get("dorm"),
        bio=d.get("bio", ""),
        photos=ps,
        want_gender=d["want_gender"],
        tags_want=d["tags_want"],
    )
    await state.clear()
    header = await likes_header(m.from_user.id)
    await m.answer(header + "Анкета готова.", reply_markup=kb_main())


@router.message(Reg.photos, F.photo)
async def r_photo_single(m: Message, state: FSMContext):
    d = await state.get_data()
    await db.save(
        m.from_user.id,
        username=m.from_user.username,
        name=d["name"],
        gender=d["gender"],
        institute=d["institute"],
        course=d["course"],
        dorm=d.get("dorm"),
        bio=d.get("bio", ""),
        photos=[m.photo[-1].file_id],
        want_gender=d["want_gender"],
        tags_want=d["tags_want"],
    )
    await state.clear()
    header = await likes_header(m.from_user.id)
    await m.answer(header + "Анкета готова.", reply_markup=kb_main())


@router.message(Reg.photos)
async def r_photo_wrong(m: Message, state: FSMContext):
    await m.answer(f"Пришли до {MAX_PHOTOS} фото одним сообщением.")


# ---------- МЕНЮ ----------

@router.message(F.text == BTN_MENU)
@router.message(F.text == BTN_BACK)
async def to_menu(m: Message, state: FSMContext):
    await show_menu(m, state)


@router.message(F.text == BTN_FEED)
async def btn_feed(m: Message, state: FSMContext):
    await state.clear()
    me = await db.get(m.from_user.id)
    if not me:
        await m.answer("Сначала /start", reply_markup=kb_main())
        return
    await m.answer("Показываю анкеты.", reply_markup=kb_choose())
    await show_next_feed(m.bot, m.from_user.id)


@router.message(F.text == BTN_LIKES)
async def btn_likes(m: Message, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    LIKES[uid] = await db.liked_me(uid)
    if not LIKES[uid]:
        await m.answer("Пока никто не лайкнул.", reply_markup=kb_main())
        return
    await m.answer("Входящие лайки:", reply_markup=kb_choose())
    await show_next_like(m.bot, uid)


@router.message(F.text == BTN_MATCHES)
async def btn_matches(m: Message, state: FSMContext):
    await state.clear()
    uid = m.from_user.id

    try:
        matches = await db.my_matches(uid)
    except Exception as e:
        logging.exception(f"my_matches error: {e}")
        matches = []

    if not matches:
        await m.answer("Пока нет взаимных лайков.", reply_markup=kb_main())
        return

    MATCHES_CACHE[uid] = matches

    all_lines = []
    for i, u in enumerate(matches, 1):
        name = esc(u["name"])
        if u.get("username"):
            all_lines.append(
                f"{i}. <a href=\"https://t.me/{u['username']}\">{name}</a>"
            )
        else:
            all_lines.append(f"{i}. {name}")

    total = len(all_lines)
    header_text = "Взаимные лайки:"

    if total <= MATCHES_CHUNK:
        text = header_text + "\n\n" + "\n".join(all_lines)
        text += "\n\nОтправь номер, чтобы посмотреть анкету."
        await m.answer(text, reply_markup=kb_cancel(), parse_mode="HTML",
                       disable_web_page_preview=True)
    else:
        first = True
        for start in range(0, total, MATCHES_CHUNK):
            chunk = all_lines[start:start + MATCHES_CHUNK]
            if first:
                text = header_text + "\n\n" + "\n".join(chunk)
                first = False
            else:
                text = "\n".join(chunk)
            await m.answer(text, parse_mode="HTML",
                           disable_web_page_preview=True)
            await asyncio.sleep(0.1)

        await m.answer(
            "Отправь номер, чтобы посмотреть анкету.",
            reply_markup=kb_cancel(),
        )

    await state.set_state(MatchesState.waiting_number)


@router.message(MatchesState.waiting_number, F.text == BTN_CANCEL)
async def matches_cancel(m: Message, state: FSMContext):
    await state.clear()
    MATCHES_CACHE.pop(m.from_user.id, None)
    await show_menu(m, state)


@router.message(MatchesState.waiting_number)
async def matches_number(m: Message, state: FSMContext):
    uid = m.from_user.id
    matches = MATCHES_CACHE.get(uid)

    if not matches:
        await state.clear()
        await m.answer("Список пуст. Открой «Взаимные лайки» заново.",
                       reply_markup=kb_main())
        return

    text = (m.text or "").strip()
    if not text.isdigit():
        await m.answer("Пришли номер из списка или нажми «Отмена».",
                       reply_markup=kb_cancel())
        return

    n = int(text)
    if n < 1 or n > len(matches):
        await m.answer(f"Такого номера нет. Введи от 1 до {len(matches)}.",
                       reply_markup=kb_cancel())
        return

    u = matches[n - 1]
    await send_card(m.bot, u, uid, count_view=False)

    await state.clear()
    MATCHES_CACHE.pop(uid, None)
    await m.answer("Готово.", reply_markup=kb_main())


@router.message(F.text == BTN_TOP)
async def btn_top(m: Message, state: FSMContext):
    await state.clear()
    try:
        users = await db.top_users(limit=10)
    except Exception as e:
        logging.exception(f"top_users error: {e}")
        users = []

    if not users:
        await m.answer("Пока топ пуст. Поставь лайки в ленте — топ оживёт.",
                       reply_markup=kb_main())
        return

    media = []
    for i, u in enumerate(users[:3]):
        photos = u.get("photos") or []
        if not photos:
            continue
        media.append(InputMediaPhoto(
            media=photos[0],
            caption=f"{i+1}. {esc(u['name'])} - {u.get('likes_count', 0)} лайков",
            parse_mode="HTML",
        ))

    if media:
        try:
            await m.answer_media_group(media)
        except Exception as e:
            logging.warning(f"top media_group failed: {e}")

    lines = ["Топ недели", ""]
    for i, u in enumerate(users):
        rank = f"{i+1}."
        likes_count = u.get("likes_count", 0)
        name = esc(u['name'])
        if i < 3:
            lines.append(f"{rank} <b>{name}</b> - {likes_count} лайков")
        else:
            lines.append(f"{rank} {name} - {likes_count} лайков")
    text = "\n".join(lines)

    await m.answer(text, reply_markup=kb_main())


@router.message(F.text == BTN_ME)
async def btn_me(m: Message, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    u = await db.get(uid)
    if not u:
        await m.answer("Анкета не найдена. /start", reply_markup=kb_main())
        return

    text = "Твоя анкета\n\n" + card(u)
    views = u.get("views") or 0
    text += f"\n\nПросмотров: <b>{views}</b>"

    if len(text) > TEXT_LIMIT:
        text = text[:TEXT_LIMIT - 3] + "..."

    photos = u["photos"] or []
    if not photos:
        await m.answer(text, reply_markup=kb_profile(), parse_mode="HTML")
    elif len(photos) == 1:
        if len(text) <= CAPTION_LIMIT:
            try:
                await m.answer_photo(photos[0], caption=text,
                                     reply_markup=kb_profile(), parse_mode="HTML")
            except Exception:
                await m.answer_photo(photos[0])
                await m.answer(text, reply_markup=kb_profile(), parse_mode="HTML")
        else:
            await m.answer_photo(photos[0])
            await m.answer(text, reply_markup=kb_profile(), parse_mode="HTML")
    else:
        media = [InputMediaPhoto(media=p) for p in photos]
        try:
            await m.answer_media_group(media)
        except Exception:
            pass
        await m.answer(text, reply_markup=kb_profile(), parse_mode="HTML")


@router.message(F.text == BTN_INFO)
async def btn_info(m: Message, state: FSMContext):
    await state.clear()
    try:
        boys, girls = await db.count_by_gender()
    except Exception as e:
        logging.exception(f"count_by_gender error: {e}")
        boys, girls = 0, 0
    total = boys + girls

    text = (
        "Знакомства МИФИ\n"
        "Бот для знакомств среди студентов НИЯУ МИФИ.\n\n"

        "Как пользоваться:\n"
        "1. Заполни анкету: имя, пол, институт, курс, общежитие, пара слов о себе.\n"
        "2. Укажи, кого ищешь и что тебе интересно.\n"
        "3. Загрузи до 3 фото одним сообщением.\n"
        "4. Жми «Смотреть анкеты» и ставь Лайк или Скип.\n"
        "5. При взаимной симпатии бот пришлёт контакт собеседника.\n\n"

        "Правила:\n"
        "- Старайся ставить реальные фото\n"
        "- Без негативичка\n\n"

        "Статистика:\n"
        f"Всего анкет: <b>{total}</b>\n"
        f"Парней: <b>{boys}</b>\n"
        f"Девушек: <b>{girls}</b>\n\n"

        f"Замечания и предложения: {ADMIN_CONTACT}"
    )
    await m.answer(text, reply_markup=kb_main())


# ---------- ЛАЙК / СКИП / ЖАЛОБА ----------

@router.message(F.text == BTN_LIKE)
async def btn_like(m: Message):
    uid = m.from_user.id
    target = ACTIVE.get(uid)
    if not target:
        await m.answer("Сейчас нет активной анкеты.", reply_markup=kb_main())
        return
    tid = target["tg_id"]

    if uid in LIKES and LIKES[uid]:
        await db.set_like(uid, tid, 1)
        if await db.match_exists(uid, tid):
            await db.add_match(uid, tid)
            await _notify_match(m.bot, uid, tid)
        LIKES[uid] = [x for x in LIKES[uid] if x["tg_id"] != tid]
        if not LIKES[uid]:
            ACTIVE.pop(uid, None)
            await m.answer("Больше нет входящих лайков.", reply_markup=kb_main())
            return
        await show_next_like(m.bot, uid)
        return

    await db.set_like(uid, tid, 1)
    if await db.match_exists(uid, tid):
        await db.add_match(uid, tid)
        await _notify_match(m.bot, uid, tid)
    await show_next_feed(m.bot, uid)


@router.message(F.text == BTN_SKIP)
async def btn_skip(m: Message):
    uid = m.from_user.id
    target = ACTIVE.get(uid)
    if not target:
        await m.answer("Сейчас нет активной анкеты.", reply_markup=kb_main())
        return
    tid = target["tg_id"]

    if uid in LIKES and LIKES[uid]:
        await db.set_like(uid, tid, 0)
        await db.hide_from_incoming(uid, tid)
        LIKES[uid] = [x for x in LIKES[uid] if x["tg_id"] != tid]
        if not LIKES[uid]:
            ACTIVE.pop(uid, None)
            await m.answer("Больше нет входящих лайков.", reply_markup=kb_main())
            return
        await show_next_like(m.bot, uid)
        return

    await db.set_like(uid, tid, 0)
    await show_next_feed(m.bot, uid)


@router.message(F.text == BTN_REPORT)
async def btn_report(m: Message):
    uid = m.from_user.id
    target = ACTIVE.get(uid)
    if not target:
        await m.answer("Сейчас нет активной анкеты.", reply_markup=kb_main())
        return
    tid = target["tg_id"]
    if tid == uid:
        await m.answer("Нельзя жаловаться на свою анкету.")
        return
    b = InlineKeyboardBuilder()
    for k, v in REPORT_REASONS.items():
        b.button(text=v, callback_data=f"rr:{tid}:{k}")
    b.button(text="Отмена", callback_data="rc")
    b.adjust(1)
    await m.answer("Причина жалобы:", reply_markup=b.as_markup())


# ---------- ПРОФИЛЬ: РЕДАКТИРОВАНИЕ ----------

@router.message(F.text == "Изменить био")
async def eb(m: Message, state: FSMContext):
    await m.answer(
        f"Новое био (до {BIO_MAX} символов).\n"
        f"Нажми «Пропустить», чтобы очистить:",
        reply_markup=kb_cancel_or_skip(),
    )
    await state.set_state(Ed.bio)


@router.message(Ed.bio, F.text == BTN_CANCEL)
async def eb_cancel(m: Message, state: FSMContext):
    await state.clear()
    await show_menu(m, state)


@router.message(Ed.bio, F.text == BTN_SKIP_STEP)
async def eb_skip(m: Message, state: FSMContext):
    await db.save(m.from_user.id, bio="")
    await state.clear()
    await show_menu(m, state)


@router.message(Ed.bio)
async def eb_save(m: Message, state: FSMContext):
    t = (m.text or "").strip()[:BIO_MAX]
    await db.save(m.from_user.id, bio=t)
    await state.clear()
    await show_menu(m, state)


@router.message(F.text == "Изменить запрос")
async def et(m: Message, state: FSMContext):
    await m.answer(
        f"Что ищешь? Перечисли через запятую (до {MAX_TAGS}).\n"
        f"Нажми «Пропустить», чтобы очистить:",
        reply_markup=kb_cancel_or_skip(),
    )
    await state.set_state(Ed.tags)


@router.message(Ed.tags, F.text == BTN_CANCEL)
async def et_cancel(m: Message, state: FSMContext):
    await state.clear()
    await show_menu(m, state)


@router.message(Ed.tags, F.text == BTN_SKIP_STEP)
async def et_skip(m: Message, state: FSMContext):
    await db.save(m.from_user.id, tags_want=[])
    await state.clear()
    await show_menu(m, state)


@router.message(Ed.tags)
async def et_save(m: Message, state: FSMContext):
    await db.save(m.from_user.id, tags_want=parse_tags(m.text))
    await state.clear()
    await show_menu(m, state)


@router.message(F.text == "Изменить фото")
async def ep(m: Message, state: FSMContext):
    await m.answer(f"Пришли новые фото одним сообщением (до {MAX_PHOTOS}).",
                   reply_markup=kb_cancel())
    await state.set_state(Ed.photos)


@router.message(Ed.photos, F.text == BTN_CANCEL)
async def ep_cancel(m: Message, state: FSMContext):
    await state.clear()
    await show_menu(m, state)


@router.message(Ed.photos, F.photo, F.media_group_id)
async def ep_add_album(m: Message, state: FSMContext, album: list[Message]):
    ps = [msg.photo[-1].file_id for msg in album[:MAX_PHOTOS]]
    await db.save(m.from_user.id, photos=ps)
    await state.clear()
    await show_menu(m, state)


@router.message(Ed.photos, F.photo)
async def ep_add_single(m: Message, state: FSMContext):
    await db.save(m.from_user.id, photos=[m.photo[-1].file_id])
    await state.clear()
    await show_menu(m, state)


@router.message(Ed.photos)
async def ep_wrong(m: Message, state: FSMContext):
    await m.answer(f"Пришли до {MAX_PHOTOS} фото одним сообщением или «Отмена».")


@router.message(F.text == "Заполнить анкету заново")
async def restart(m: Message, state: FSMContext):
    uid = m.from_user.id
    async with db.conn() as d:
        await d.execute("DELETE FROM users WHERE tg_id=?", (uid,))
        await d.execute("DELETE FROM likes WHERE from_id=? OR to_id=?", (uid, uid))
        await d.execute("DELETE FROM matches WHERE user1_id=? OR user2_id=?", (uid, uid))
        await d.commit()

    FEEDS.pop(uid, None)
    LIKES.pop(uid, None)
    ACTIVE.pop(uid, None)
    LAST_ALBUM.pop(uid, None)
    MATCHES_CACHE.pop(uid, None)

    await state.clear()
    await m.answer("Анкета удалена. Заполним заново.\n\nКак тебя зовут?")
    await state.set_state(Reg.name)


# ---------- УДАЛЕНИЕ АНКЕТЫ ----------

@router.message(F.text == "Удалить анкету")
async def delete_request(m: Message, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    u = await db.get(uid)
    if not u:
        await m.answer("Анкета не найдена. /start", reply_markup=kb_main())
        return
    await m.answer(
        "Ты уверен(а), что хочешь удалить анкету?\n\n"
        "Все данные будут стёрты: анкета, лайки, матчи.\n"
        "Восстановить их будет невозможно.",
        reply_markup=kb_confirm_delete(),
    )


@router.callback_query(F.data == "confirm_delete:no")
async def delete_cancel(c: CallbackQuery, state: FSMContext):
    try:
        await c.answer()
    except Exception:
        pass
    try:
        await c.message.delete()
    except Exception:
        pass
    uid = c.from_user.id
    await c.bot.send_message(uid, "Удаление отменено.", reply_markup=kb_main())


@router.callback_query(F.data == "confirm_delete:yes")
async def delete_confirm(c: CallbackQuery, state: FSMContext):
    try:
        await c.answer()
    except Exception:
        pass
    uid = c.from_user.id

    async with db.conn() as d:
        await d.execute("DELETE FROM users WHERE tg_id=?", (uid,))
        await d.execute("DELETE FROM likes WHERE from_id=? OR to_id=?", (uid, uid))
        await d.execute("DELETE FROM matches WHERE user1_id=? OR user2_id=?", (uid, uid))
        await d.execute("DELETE FROM reports WHERE reporter_id=? OR target_id=?", (uid, uid))
        await d.commit()

    FEEDS.pop(uid, None)
    LIKES.pop(uid, None)
    ACTIVE.pop(uid, None)
    LAST_ALBUM.pop(uid, None)
    MATCHES_CACHE.pop(uid, None)

    try:
        await c.message.delete()
    except Exception:
        pass

    await state.clear()
    await c.bot.send_message(
        uid,
        "Твоя анкета удалена. Больше ты не появишься в лентах других пользователей.\n\n"
        "Если захочешь вернуться — нажми «Начать заново».",
        reply_markup=kb_start_over(),
    )


@router.message(F.text == BTN_RESTART)
async def start_over(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Это «Знакомства МИФИ».\n\nКак тебя зовут?")
    await state.set_state(Reg.name)


# ---------- РАССЫЛКА ----------

class Broadcast(StatesGroup):
    text = State()


@router.message(Command("broadcast"))
async def cmd_broadcast(m: Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS:
        await m.answer("Команда недоступна.")
        return
    if BROADCAST_LOCK["running"]:
        left = BROADCAST_LOCK["total"] - BROADCAST_LOCK["sent"] - BROADCAST_LOCK["failed"]
        await m.answer(
            f"Рассылка уже идёт. Осталось ~{left} из {BROADCAST_LOCK['total']}.\n"
            f"Дождись завершения.",
            reply_markup=kb_main(),
        )
        return
    await m.answer(
        "Пришли текст для рассылки. Отправь /cancel, чтобы отменить.",
        reply_markup=kb_broadcast_cancel(),
    )
    await state.set_state(Broadcast.text)


@router.message(Broadcast.text, F.text == "/cancel")
async def broadcast_cancel(m: Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS:
        return
    await state.clear()
    await m.answer("Рассылка отменена.", reply_markup=kb_main())


@router.message(Broadcast.text)
async def broadcast_send(m: Message, state: FSMContext):
    if m.from_user.id not in ADMIN_IDS:
        return

    if BROADCAST_LOCK["running"]:
        left = BROADCAST_LOCK["total"] - BROADCAST_LOCK["sent"] - BROADCAST_LOCK["failed"]
        await m.answer(
            f"Рассылка уже идёт. Осталось ~{left} из {BROADCAST_LOCK['total']}.\n"
            f"Дождись завершения.",
            reply_markup=kb_main(),
        )
        await state.clear()
        return

    text = m.text or m.caption
    if not text:
        await m.answer("Пришли текст (не фото/видео).")
        return

    await state.clear()
    ids = await db.all_active_ids()
    total = len(ids)

    if total == 0:
        await m.answer("Нет активных пользователей для рассылки.",
                       reply_markup=kb_main())
        return

    BROADCAST_LOCK.update(running=True, sent=0, failed=0, total=total)

    status = await m.answer(f"Рассылаю… 0/{total}")
    step = max(1, total // 20)

    try:
        for i, uid in enumerate(ids, 1):
            try:
                await m.bot.send_message(uid, text)
                BROADCAST_LOCK["sent"] += 1
            except Exception as e:
                try:
                    await m.bot.send_message(uid, text, parse_mode=None)
                    BROADCAST_LOCK["sent"] += 1
                    logging.info(f"broadcast fallback {uid}")
                except Exception as e2:
                    BROADCAST_LOCK["failed"] += 1
                    logging.warning(f"broadcast fail {uid}: {e} | fallback: {e2}")

            if i % step == 0 or i == total:
                try:
                    await status.edit_text(
                        f"Рассылаю… {i}/{total}\n"
                        f"Отправлено: {BROADCAST_LOCK['sent']}\n"
                        f"Ошибок: {BROADCAST_LOCK['failed']}"
                    )
                except Exception:
                    pass

            await asyncio.sleep(0.03)
    finally:
        sent = BROADCAST_LOCK["sent"]
        failed = BROADCAST_LOCK["failed"]
        BROADCAST_LOCK["running"] = False

        try:
            await status.edit_text(
                f"Рассылка завершена.\n\n"
                f"Отправлено: <b>{sent}</b>\n"
                f"Не доставлено: <b>{failed}</b>"
            )
        except Exception:
            pass
        await m.answer("Готово.", reply_markup=kb_main())


# ---------- БЭКАП ----------

@router.message(Command("backup"))
async def cmd_backup(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        await m.answer("Команда недоступна.")
        return
    if not os.path.exists(DB_PATH):
        await m.answer(f"Файл не найден: {DB_PATH}")
        return
    try:
        file = FSInputFile(DB_PATH, filename="bot.db")
        await m.answer_document(file, caption=f"Бэкап базы: {DB_PATH}")
    except Exception as e:
        logging.exception(f"backup error: {e}")
        await m.answer(f"Ошибка: {e}")


# ---------- ЖАЛОБЫ ----------

@router.callback_query(F.data == "rc")
async def rc(c: CallbackQuery):
    try:
        await c.answer()
    except Exception:
        pass
    try:
        await c.message.delete()
    except Exception:
        pass


@router.callback_query(F.data.startswith("rr:"))
async def rr(c: CallbackQuery):
    try:
        await c.answer()
    except Exception:
        pass

    _, tid, reason = c.data.split(":")
    await db.add_report(c.from_user.id, int(tid), reason)
    try:
        await c.message.edit_text("Жалоба отправлена.")
    except Exception:
        pass
    for a in ADMIN_IDS:
        try:
            await c.bot.send_message(a, "Новая жалоба.")
        except Exception:
            pass


# ---------- НАПОМИНАНИЯ О ЛАЙКАХ ----------

async def _send_like_reminder(bot, uid):
    """Отправляет напоминание и ставит отметку, чтобы не отправить повторно сегодня."""
    try:
        await bot.send_message(
            uid,
            "У тебя новый лайк. Загляни в «Кто меня лайкнул» — может быть, это он.",
        )
        await db.set_like_reminder(uid)
        return True
    except Exception as e:
        logging.warning(f"reminder failed for {uid}: {e}")
        try:
            await db.set_like_reminder(uid)
        except Exception:
            pass
        return False


async def like_reminder_scheduler(bot):
    """
    Каждый день в REMINDER_HOUR_UTC отправляет напоминания тем,
    у кого есть непросмотренные лайки и сегодня ещё не отправляли.
    """
    await asyncio.sleep(30)

    while True:
        try:
            now = datetime.now()
            target = now.replace(hour=REMINDER_HOUR_UTC, minute=0, second=0, microsecond=0)
            if now >= target:
                target = target + timedelta(days=1)

            wait = (target - now).total_seconds()
            logging.info(f"like reminder: next run at {target}, wait {int(wait)} s")
            await asyncio.sleep(wait)

            try:
                ids = await db.get_users_to_remind(limit=500)
                logging.info(f"like reminder: {len(ids)} users to notify")
                for uid in ids:
                    try:
                        await _send_like_reminder(bot, uid)
                    except Exception as e:
                        logging.exception(f"reminder send error for {uid}: {e}")
                    await asyncio.sleep(0.05)
            except Exception as e:
                logging.exception(f"like_reminder_scheduler inner error: {e}")

            await asyncio.sleep(60)

        except asyncio.CancelledError:
            logging.info("like reminder scheduler cancelled")
            return
        except Exception as e:
            logging.exception(f"like_reminder_scheduler error: {e}")
            await asyncio.sleep(300)


# ---------- ЗАПУСК ----------

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")
    await db.init()
    session = AiohttpSession(timeout=30)
    bot = Bot(BOT_TOKEN, session=session,
              default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())

    dp.message.middleware(ErrorMiddleware())
    dp.callback_query.middleware(ErrorMiddleware())
    dp.message.middleware(BanMiddleware())
    dp.callback_query.middleware(BanMiddleware())
    router.message.middleware(AlbumMiddleware())

    dp.include_router(router)
    logging.info("Bot started")

    asyncio.create_task(like_reminder_scheduler(bot))

    await dp.start_polling(bot)