import secrets
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import db
from config import ADMIN_USER, ADMIN_PASS, BOT_TOKEN

_bot = None


def get_bot():
    global _bot
    if _bot is None:
        from aiogram import Bot
        _bot = Bot(BOT_TOKEN)
    return _bot


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    yield


app = FastAPI(lifespan=lifespan)
security = HTTPBasic()


def auth(c: HTTPBasicCredentials = Depends(security)):
    if not (
        secrets.compare_digest(c.username, ADMIN_USER)
        and secrets.compare_digest(c.password, ADMIN_PASS)
    ):
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})


HEAD = """
<style>
body { font-family: system-ui, sans-serif; max-width: 960px; margin: 2rem auto; padding: 0 1rem; }
.stat { display: inline-block; padding: .6rem 1rem; background: #f2f2f2; border-radius: 8px; margin: .2rem; }
.r { border: 1px solid #ddd; border-radius: 8px; padding: 1rem; margin: 1rem 0; }
img { max-width: 140px; border-radius: 6px; margin: 4px; }
.btn { padding: .4rem .8rem; border: none; border-radius: 6px; cursor: pointer; color: #fff; }
.ban { background: #e74c3c; } .rej { background: #95a5a6; } .unb { background: #2ecc71; }
a { color: #6c5ce7; }
table { border-collapse: collapse; width: 100%; }
td, th { border-bottom: 1px solid #eee; padding: .5rem; text-align: left; vertical-align: top; }
.card { border: 1px solid #ddd; border-radius: 8px; padding: 1rem; margin: 1rem 0; }
.card h3 { margin: 0 0 .5rem 0; }
.muted { color: #888; font-size: .9rem; }
</style>
"""


BAN_TEXT = "Ваша анкета заблокирована модератором."

UNBAN_TEXT = "Ваша анкета разблокирована. Можно снова пользоваться ботом."


async def _notify(uid: int, text: str):
    try:
        bot = get_bot()
        await bot.send_message(uid, text)
    except Exception:
        pass


@app.get("/", response_class=HTMLResponse)
async def index(_=Depends(auth)):
    s = await db.stats()
    rows = await db.reports()
    html = f"<html><head><meta charset='utf-8'><title>Знакомства МИФИ — админка</title>{HEAD}</head><body>"
    html += "<h1>Знакомства МИФИ — админка</h1>"
    html += (
        f"<span class='stat'>Пользователей: {s['users']}</span>"
        f"<span class='stat'>Лайков: {s['likes']}</span>"
        f"<span class='stat'>Матчей: {s['matches']}</span>"
        f"<span class='stat'>Жалоб: {s['reports']}</span>"
    )
    html += "<p><a href='/users'>Список пользователей</a></p>"
    html += "<h2>Жалобы</h2>"
    if not rows:
        html += "<p>Жалоб нет</p>"
    for r in rows:
        imgs = "".join(
            f"<a href='/photo/{p}' target='_blank'><img src='/photo/{p}'></a>"
            for p in r["photos"]
        )
        uname = f"@{r['username']}" if r["username"] else ""
        html += f"""
        <div class='r'>
          <b>На: {r['name']}</b> {uname}<br>
          <span class='muted'>tg_id: {r['target_id']}</span><br>
          Причина: {r['reason']}<br>
          От: {r['reporter_id']}<br>
          <div>{imgs}</div>
          <form method='post' action='/ban/{r['target_id']}/{r['id']}' style='display:inline'>
            <button class='btn ban'>Забанить</button>
          </form>
          <form method='post' action='/reject/{r['id']}' style='display:inline'>
            <button class='btn rej'>Отклонить</button>
          </form>
          <form method='get' action='/user/{r['target_id']}' style='display:inline'>
            <button class='btn' style='background:#6c5ce7'>Открыть анкету</button>
          </form>
        </div>"""
    return html + "</body></html>"


@app.get("/users", response_class=HTMLResponse)
async def users(_=Depends(auth)):
    us = await db.all_users()
    html = f"<html><head><meta charset='utf-8'><title>Знакомства МИФИ — админка</title>{HEAD}</head><body>"
    html += "<p><a href='/'>Назад</a></p><h1>Пользователи</h1>"

    if not us:
        return html + "<p>Пока никого</p></body></html>"

    html += "<table><tr><th>Фото</th><th>Имя</th><th>Инфо</th><th>Ищет</th><th>Действия</th></tr>"
    for u in us:
        if u["photos"]:
            thumb = "".join(
                f"<a href='/photo/{p}' target='_blank'><img src='/photo/{p}'></a>"
                for p in u["photos"][:3]
            )
        else:
            thumb = "<span class='muted'>нет фото</span>"

        uname = f"@{u['username']}" if u["username"] else ""
        status = "<span style='color:#e74c3c'>забанен</span>" if u["is_banned"] else "ок"

        tags_want = " ".join(f"#{t}" for t in u["tags_want"]) or "—"

        info = (
            f"tg_id: {u['tg_id']}<br>"
            f"{uname}<br>"
            f"пол: {u['gender'] or '?'}<br>"
            f"институт: {u['institute'] or '?'}<br>"
            f"курс: {u['course'] or '?'}<br>"
            f"общага: {u.get('dorm') or '—'}<br>"
            f"статус: {status}"
        )
        tags_html = f"<b>Ищет:</b> {tags_want}"
        if u["bio"]:
            tags_html += f"<br><span class='muted'>{u['bio']}</span>"

        action = "unban" if u["is_banned"] else "ban"
        label = "Разбанить" if u["is_banned"] else "Забанить"
        cls = "unb" if u["is_banned"] else "ban"

        html += (
            f"<tr>"
            f"<td>{thumb}</td>"
            f"<td><a href='/user/{u['tg_id']}'>{u['name']}</a></td>"
            f"<td>{info}</td>"
            f"<td>{tags_html}</td>"
            f"<td>"
            f"<form method='post' action='/{action}/{u['tg_id']}' style='display:inline'>"
            f"<button class='btn {cls}'>{label}</button></form>"
            f"</td>"
            f"</tr>"
        )
    return html + "</table></body></html>"


@app.get("/user/{uid}", response_class=HTMLResponse)
async def user_card(uid: int, _=Depends(auth)):
    u = await db.get(uid)
    if not u:
        raise HTTPException(404)

    html = f"<html><head><meta charset='utf-8'><title>Знакомства МИФИ — админка</title>{HEAD}</head><body>"
    html += "<p><a href='/users'>К списку</a> | <a href='/'>На главную</a></p>"

    uname = f"@{u['username']}" if u["username"] else "—"
    status = "забанен" if u["is_banned"] else "ок"
    tags_want = " ".join(f"#{t}" for t in u["tags_want"]) or "—"

    imgs = "".join(
        f"<a href='/photo/{p}' target='_blank'><img src='/photo/{p}'></a>"
        for p in u["photos"]
    ) or "<p class='muted'>фото нет</p>"

    html += f"""
    <div class='card'>
      <h3>{u['name']}</h3>
      <p>
        tg_id: {u['tg_id']}<br>
        username: {uname}<br>
        пол: {u['gender'] or '—'}<br>
        институт: {u['institute'] or '—'}<br>
        курс: {u['course'] or '—'}<br>
        общага: {u.get('dorm') or '—'}<br>
        статус: {status}<br>
        создан: {u['created_at']}
      </p>
      <p><b>Ищет:</b> {tags_want}</p>
      <p><b>Био:</b> {u['bio'] or '—'}</p>
      <div>{imgs}</div>
    </div>
    """
    return html + "</body></html>"


@app.get("/photo/{fid}")
async def photo(fid: str, _=Depends(auth)):
    try:
        bot = get_bot()
        f = await bot.get_file(fid)
        buf = await bot.download_file(f.file_path)
        return Response(content=buf.read(), media_type="image/jpeg")
    except Exception as e:
        raise HTTPException(404, detail=str(e))


@app.post("/ban/{uid}/{rid}")
async def ban_with_report(uid: int, rid: int, _=Depends(auth)):
    await db.ban(uid, 1)
    await db.report_done(rid, "resolved")
    await _notify(uid, BAN_TEXT)
    return RedirectResponse("/", status_code=303)


@app.post("/ban/{uid}")
async def ban_only(uid: int, _=Depends(auth)):
    await db.ban(uid, 1)
    await _notify(uid, BAN_TEXT)
    return RedirectResponse("/users", status_code=303)


@app.post("/unban/{uid}")
async def unban(uid: int, _=Depends(auth)):
    await db.ban(uid, 0)
    await _notify(uid, UNBAN_TEXT)
    return RedirectResponse("/users", status_code=303)


@app.post("/reject/{rid}")
async def reject(rid: int, _=Depends(auth)):
    await db.report_done(rid, "rejected")
    return RedirectResponse("/", status_code=303)