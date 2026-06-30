"""
Bot Telegram Planificateur — Sans APScheduler (compatible Python 3.13)
python-telegram-bot==20.3 / Python 3.10+
"""

import logging
import json
import os
import uuid
import asyncio
import pg8000.dbapi
from urllib.parse import urlparse
from datetime import datetime, timezone, timedelta
from datetime import time as dt_time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.constants import ChatType, ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)
from telegram.ext import ApplicationHandlerStop
from telegram.error import TelegramError

BOT_TOKEN    = os.environ.get("BOT_TOKEN", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

DATA_FILE = os.path.expanduser("~/planner_data.json")
data: dict = {"owners": {}, "recipients": {}, "scheduled": {}}

# ══════════════════════════════════════════════════════
#  BASE DE DONNÉES
# ══════════════════════════════════════════════════════

def _db_conn():
    u = urlparse(DATABASE_URL)
    return pg8000.dbapi.connect(
        user=u.username,
        password=u.password,
        host=u.hostname,
        port=u.port or 5432,
        database=u.path.lstrip("/"),
    )

def _db_init():
    """Crée la table si elle n'existe pas."""
    try:
        conn = _db_conn()
        cur  = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bot_data (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Erreur init DB : {e}")

def load_data():
    global data
    if not DATABASE_URL:
        # Fallback fichier local (dev)
        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                logger.error(f"Erreur chargement fichier : {e}")
        return
    try:
        _db_init()
        conn = _db_conn()
        cur  = conn.cursor()
        cur.execute("SELECT value FROM bot_data WHERE key = 'main'")
        row = cur.fetchone()
        if row:
            data = json.loads(row[0])
        cur.close()
        conn.close()
        logger.info("✅ Données chargées depuis PostgreSQL")
    except Exception as e:
        logger.error(f"Erreur chargement DB : {e}")

def save_data():
    if not DATABASE_URL:
        # Fallback fichier local (dev)
        try:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Erreur sauvegarde fichier : {e}")
        return
    try:
        conn = _db_conn()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO bot_data (key, value)
            VALUES ('main', %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (json.dumps(data, ensure_ascii=False),))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Erreur sauvegarde DB : {e}")

def is_owner(uid: int) -> bool:
    return str(uid) in data.get("owners", {})

def get_tz(uid: int) -> int:
    o = data.get("owners", {}).get(str(uid), {})
    return o.get("tz_offset", 0) if isinstance(o, dict) else 0

def get_my_recipients(uid: int) -> dict:
    return {k: v for k, v in data.get("recipients", {}).items() if v.get("owner_id") == uid}

def get_my_jobs(uid: int) -> dict:
    return {k: v for k, v in data.get("scheduled", {}).items() if v.get("owner_id") == uid}

wiz: dict[int, dict] = {}

DAY_NAMES = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
DAY_SHORT  = ["Lun",   "Mar",   "Mer",     "Jeu",   "Ven",     "Sam",    "Dim"]

TYPE_EMOJI = {
    "text": "📝", "photo": "🖼️", "video": "🎬", "document": "📁",
    "audio": "🎵", "animation": "🎞️", "voice": "🎤", "video_note": "📹", "sticker": "🎭",
}

# ══════════════════════════════════════════════════════
#  SCHEDULER ASYNCIO (sans APScheduler)
# ══════════════════════════════════════════════════════

async def do_send(app, job_id: str, sched: dict):
    rid = sched["recipient_id"]
    ct  = sched["content_type"]
    fid = sched.get("file_id")
    txt = sched.get("text", "")
    cap = sched.get("caption") or None
    owner = sched.get("owner_id")
    try:
        if   ct == "text":       await app.bot.send_message(chat_id=rid, text=txt)
        elif ct == "photo":      await app.bot.send_photo(chat_id=rid, photo=fid, caption=cap)
        elif ct == "video":      await app.bot.send_video(chat_id=rid, video=fid, caption=cap)
        elif ct == "document":   await app.bot.send_document(chat_id=rid, document=fid, caption=cap)
        elif ct == "audio":      await app.bot.send_audio(chat_id=rid, audio=fid, caption=cap)
        elif ct == "animation":  await app.bot.send_animation(chat_id=rid, animation=fid, caption=cap)
        elif ct == "voice":      await app.bot.send_voice(chat_id=rid, voice=fid, caption=cap)
        elif ct == "video_note": await app.bot.send_video_note(chat_id=rid, video_note=fid)
        elif ct == "sticker":    await app.bot.send_sticker(chat_id=rid, sticker=fid)

        if sched.get("schedule_type") == "once":
            sched["active"] = False
            sched["status"] = "sent"
        sched["last_run"] = datetime.now(timezone.utc).isoformat()
        save_data()

        if owner:
            rname = data.get("recipients", {}).get(str(rid), {}).get("name", str(rid))
            try:
                await app.bot.send_message(
                    chat_id=owner,
                    text=f"✅ Message envoyé vers *{rname}* !\n🆔 `{job_id[:8]}`",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception: pass

    except TelegramError as e:
        logger.error(f"Erreur envoi {job_id} : {e}")
        if owner:
            try:
                await app.bot.send_message(
                    chat_id=owner,
                    text=f"❌ Erreur envoi `{job_id[:8]}` : {e}",
                    parse_mode=ParseMode.MARKDOWN,
                )
            except Exception: pass

async def scheduler_loop(app):
    """Vérifie toutes les 30 secondes si un message doit être envoyé."""
    logger.info("Scheduler démarré.")
    while True:
        try:
            now = datetime.now(timezone.utc)
            h_now = now.hour
            m_now = now.minute
            wd_now = now.weekday()  # 0=Lundi

            for job_id, sched in list(data.get("scheduled", {}).items()):
                if not sched.get("active") or sched.get("status") == "sent":
                    continue

                tz_off = sched.get("tz_offset", 0)
                h_job, m_job = map(int, sched["time"].split(":"))
                utc_h = (h_job - tz_off) % 24

                if h_now != utc_h or m_now != m_job:
                    continue

                # Vérifie qu'on n'a pas déjà envoyé cette minute
                last = sched.get("last_run")
                if last:
                    diff = (now - datetime.fromisoformat(last)).total_seconds()
                    if diff < 60:
                        continue

                stype = sched["schedule_type"]

                if stype == "once":
                    ds = sched.get("date")
                    if not ds or datetime.strptime(ds, "%Y-%m-%d").date() != now.date():
                        continue

                elif stype == "weekly":
                    if wd_now not in sched.get("days", list(range(7))):
                        continue

                asyncio.create_task(do_send(app, job_id, sched))

        except Exception as e:
            logger.error(f"Erreur scheduler : {e}")

        await asyncio.sleep(30)

# ══════════════════════════════════════════════════════
#  COMMANDES
# ══════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Bonjour ! Je suis le *Bot Planificateur*.\n\n"
        "J'envoie automatiquement tes messages, photos, vidéos\n"
        "dans tes canaux et groupes aux heures que tu choisis.\n\n"
        "Tape /setup pour commencer.",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *Commandes*\n\n"
        "*/setup* — Configuration + fuseau horaire\n"
        "*/addrecipient* — Ajouter un canal ou groupe\n"
        "*/listrecipients* — Voir tes destinations\n\n"
        "*/schedule* — Planifier un nouveau message\n"
        "*/list* — Voir tes messages planifiés\n"
        "*/delete <id>* — Supprimer une planification\n"
        "*/pause <id>* — Mettre en pause\n"
        "*/resume <id>* — Reprendre\n\n"
        "*Récurrences :* Une fois | Quotidien | Hebdomadaire\n"
        "*Types :* Texte, Photo, Vidéo, Fichier, Audio, GIF...",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Utilise cette commande en privé.")
        return
    uid = update.effective_user.id
    if str(uid) not in data["owners"]:
        data["owners"][str(uid)] = {"tz_offset": 0}
        save_data()
    wiz[uid] = {"step": "awaiting_tz"}
    kb = [
        [InlineKeyboardButton("UTC-2", callback_data="tz:-2"),
         InlineKeyboardButton("UTC-1", callback_data="tz:-1"),
         InlineKeyboardButton("UTC+0", callback_data="tz:0")],
        [InlineKeyboardButton("UTC+1", callback_data="tz:1"),
         InlineKeyboardButton("UTC+2", callback_data="tz:2"),
         InlineKeyboardButton("UTC+3", callback_data="tz:3")],
        [InlineKeyboardButton("UTC+4", callback_data="tz:4"),
         InlineKeyboardButton("UTC+5", callback_data="tz:5"),
         InlineKeyboardButton("UTC+6", callback_data="tz:6")],
    ]
    await update.message.reply_text(
        "✅ Compte enregistré !\n\n*Choisis ton fuseau horaire :*\n_(France = UTC+2 en été)_",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_addrecipient(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Utilise cette commande en privé.")
        return
    uid = update.effective_user.id
    if not is_owner(uid):
        await update.message.reply_text("❌ Tape /setup d'abord.")
        return

    # Si un ID est fourni directement : /addrecipient -100XXXXXXXXX
    if context.args:
        try:
            chat_id = int(context.args[0])
            chat = await context.bot.get_chat(chat_id)
            key = str(chat_id)
            data["recipients"][key] = {
                "owner_id": uid,
                "name": chat.title or chat.username or str(chat_id),
                "type": chat.type,
            }
            save_data()
            await update.message.reply_text(
                f"✅ *{chat.title or chat_id}* ajouté !\n\nTape /schedule pour planifier.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            await update.message.reply_text(
                f"❌ Impossible d'accéder à ce chat.\n"
                f"Vérifie que le bot est bien membre du groupe/canal.\n\nErreur : {e}"
            )
        return

    wiz[uid] = {"step": "fwd_recipient"}
    await update.message.reply_text(
        "📩 *Ajouter une destination*\n\n"
        "*Option 1 — Canal :*\nTransfère (forward) un message depuis ton canal.\n\n"
        "*Option 2 — Groupe :*\n"
        "1. Ajoute le bot dans ton groupe\n"
        "2. Tape /getid dans le groupe\n"
        "3. Copie l'ID et envoie-le ici :\n"
        "`/addrecipient -100XXXXXXXXX`",
        parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_listrecipients(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Utilise cette commande en privé.")
        return
    uid = update.effective_user.id
    if not is_owner(uid): await update.message.reply_text("❌ Tape /setup d'abord."); return
    recs = get_my_recipients(uid)
    if not recs: await update.message.reply_text("Aucune destination. Tape /addrecipient."); return
    lines = [f"• *{v['name']}*\n  `{k}`" for k, v in recs.items()]
    await update.message.reply_text("📋 *Tes destinations :*\n\n" + "\n\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def cmd_getid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Donne l'ID du chat actuel — à utiliser dans un groupe."""
    chat = update.effective_chat
    user = update.effective_user
    await update.message.reply_text(
        f"📋 *Informations du chat*\n\n"
        f"Nom : *{chat.title or chat.username or 'Privé'}*\n"
        f"🆔 ID du chat : `{chat.id}`\n"
        f"👤 Ton ID : `{user.id}`\n\n"
        f"Pour ajouter ce groupe comme destination, envoie en privé au bot :\n"
        f"`/addrecipient {chat.id}`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Utilise cette commande en privé.")
        return
    uid = update.effective_user.id
    if not is_owner(uid): await update.message.reply_text("❌ Tape /setup d'abord."); return
    recs = get_my_recipients(uid)
    if not recs: await update.message.reply_text("❌ Ajoute d'abord une destination avec /addrecipient."); return
    kb = [[InlineKeyboardButton(v["name"], callback_data=f"recip:{k}")] for k, v in recs.items()]
    kb.append([InlineKeyboardButton("➕ Nouvelle destination", callback_data="recip:new")])
    wiz[uid] = {"step": "pick_recipient", "tz_offset": get_tz(uid)}
    await update.message.reply_text(
        "📅 *Nouveau message planifié*\n\n*Étape 1/4 — Destination :*",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN,
    )

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Utilise cette commande en privé.")
        return
    uid = update.effective_user.id
    if not is_owner(uid): await update.message.reply_text("❌ Tape /setup d'abord."); return
    jobs = get_my_jobs(uid)
    if not jobs: await update.message.reply_text("Aucun message planifié. Tape /schedule."); return
    lines = []
    for jid, s in jobs.items():
        sid   = jid[:8]
        icon  = "▶️" if s.get("active") else "⏸️"
        rname = data.get("recipients", {}).get(str(s["recipient_id"]), {}).get("name", "?")
        emj   = TYPE_EMOJI.get(s.get("content_type", ""), "📨")
        tz    = s.get("tz_offset", 0)
        tz_s  = f"UTC{'+' if tz >= 0 else ''}{tz}"
        st    = s.get("schedule_type", "?")
        if   st == "once":    recur = f"Une fois ({s.get('date','?')})"
        elif st == "daily":   recur = "Quotidien"
        elif st == "weekly":  recur = "Hebdo — " + ", ".join(DAY_SHORT[d] for d in s.get("days", []))
        else:                 recur = st
        lines.append(
            f"{icon} {emj} `{sid}` — *{rname}*\n"
            f"   ⏰ {s.get('time','?')} {tz_s} | 🔁 {recur}\n"
            f"   /delete_{sid}   /pause_{sid}"
        )
    await update.message.reply_text(
        f"📋 *Messages planifiés ({len(jobs)}) :*\n\n" + "\n\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
    )

def _find_job(uid: int, sid: str):
    for jid, s in data.get("scheduled", {}).items():
        if jid.startswith(sid) and s.get("owner_id") == uid:
            return jid, s
    return None, None

async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Utilise cette commande en privé.")
        return
    uid = update.effective_user.id
    if not is_owner(uid): await update.message.reply_text("❌ Tape /setup d'abord."); return
    txt = update.message.text or ""
    sid = context.args[0] if context.args else (txt.split("_", 1)[1] if "_" in txt else None)
    if not sid: await update.message.reply_text("Usage : /delete <id>"); return
    jid, _ = _find_job(uid, sid)
    if not jid: await update.message.reply_text("❌ Non trouvé."); return
    del data["scheduled"][jid]
    save_data()
    await update.message.reply_text(f"🗑️ Supprimé : `{sid}`", parse_mode=ParseMode.MARKDOWN)

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Utilise cette commande en privé.")
        return
    uid = update.effective_user.id
    if not is_owner(uid): await update.message.reply_text("❌ Tape /setup d'abord."); return
    txt = update.message.text or ""
    sid = context.args[0] if context.args else (txt.split("_", 1)[1] if "_" in txt else None)
    if not sid: await update.message.reply_text("Usage : /pause <id>"); return
    jid, sched = _find_job(uid, sid)
    if not jid: await update.message.reply_text("❌ Non trouvé."); return
    sched["active"] = False; save_data()
    await update.message.reply_text(f"⏸️ Mis en pause : `{sid}`", parse_mode=ParseMode.MARKDOWN)

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Utilise cette commande en privé.")
        return
    uid = update.effective_user.id
    if not is_owner(uid): await update.message.reply_text("❌ Tape /setup d'abord."); return
    if not context.args: await update.message.reply_text("Usage : /resume <id>"); return
    jid, sched = _find_job(uid, context.args[0])
    if not jid: await update.message.reply_text("❌ Non trouvé."); return
    sched["active"] = True; save_data()
    await update.message.reply_text(f"▶️ Repris : `{context.args[0]}`", parse_mode=ParseMode.MARKDOWN)

# ══════════════════════════════════════════════════════
#  CALLBACKS
# ══════════════════════════════════════════════════════

async def handle_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    uid = q.from_user.id
    cb  = q.data
    w   = wiz.get(uid, {})

    if cb.startswith("tz:"):
        tz = int(cb.split(":")[1])
        data["owners"][str(uid)] = {"tz_offset": tz}
        save_data()
        wiz[uid] = {"step": "fwd_recipient", "tz_offset": tz}
        tz_s = f"UTC{'+' if tz >= 0 else ''}{tz}"
        await q.edit_message_text(
            f"✅ Fuseau : *{tz_s}*\n\n📩 Transfère un message depuis ton canal ou groupe.",
            parse_mode=ParseMode.MARKDOWN,
        )

    elif cb.startswith("recip:"):
        val = cb.split(":", 1)[1]
        if val == "new":
            wiz[uid] = {**w, "step": "fwd_recipient"}
            await q.edit_message_text("📩 Transfère un message depuis ton canal/groupe.")
        else:
            rid   = int(val)
            rname = data["recipients"].get(val, {}).get("name", val)
            wiz[uid] = {**w, "step": "await_content", "recipient_id": rid}
            await q.edit_message_text(
                f"📍 Destination : *{rname}*\n\n"
                f"*Étape 2/4 — Contenu :*\n"
                f"Envoie-moi le message à planifier\n_(texte, photo, vidéo, fichier...)_",
                parse_mode=ParseMode.MARKDOWN,
            )

    elif cb.startswith("time:"):
        val = cb.split(":", 1)[1]
        if val == "custom":
            wiz[uid] = {**w, "step": "await_time_text"}
            await q.edit_message_text("⏰ Tape l'heure : *HH:MM*\nExemple : `14:30`", parse_mode=ParseMode.MARKDOWN)
        else:
            wiz[uid] = {**w, "step": "pick_stype", "time": val}
            await _show_stype(q, val, w.get("tz_offset", get_tz(uid)))

    elif cb.startswith("stype:"):
        stype = cb.split(":")[1]
        wiz[uid] = {**w, "schedule_type": stype}
        if stype == "once":
            wiz[uid]["step"] = "pick_date"
            today = datetime.now(timezone.utc)
            tmrw  = today + timedelta(days=1)
            kb = [
                [InlineKeyboardButton(f"Aujourd'hui ({today.strftime('%d/%m/%Y')})", callback_data=f"date:{today.strftime('%Y-%m-%d')}")],
                [InlineKeyboardButton(f"Demain ({tmrw.strftime('%d/%m/%Y')})",       callback_data=f"date:{tmrw.strftime('%Y-%m-%d')}")],
                [InlineKeyboardButton("📅 Autre date", callback_data="date:custom")],
            ]
            await q.edit_message_text("*Étape 4/4 — Date d'envoi :*", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
        elif stype == "daily":
            wiz[uid]["step"] = "confirm"
            await _show_confirm(q, uid)
        elif stype == "weekly":
            wiz[uid] = {**wiz[uid], "step": "pick_days", "days": []}
            await _show_days(q, [])

    elif cb.startswith("date:"):
        val = cb.split(":", 1)[1]
        if val == "custom":
            wiz[uid] = {**w, "step": "await_date_text"}
            await q.edit_message_text("📅 Tape la date : *JJ/MM/AAAA*\nExemple : `28/06/2026`", parse_mode=ParseMode.MARKDOWN)
        else:
            wiz[uid] = {**w, "date": val, "step": "confirm"}
            await _show_confirm(q, uid)

    elif cb.startswith("day:"):
        day  = int(cb.split(":")[1])
        days = list(w.get("days", []))
        if day in days: days.remove(day)
        else: days.append(day)
        days.sort()
        wiz[uid] = {**w, "days": days}
        await _show_days(q, days)

    elif cb == "days_ok":
        if not w.get("days"):
            await q.answer("❌ Sélectionne au moins un jour !", show_alert=True); return
        wiz[uid] = {**w, "step": "confirm"}
        await _show_confirm(q, uid)

    elif cb == "yes":
        await _finalize(q, uid)
    elif cb == "no":
        wiz.pop(uid, None)
        await q.edit_message_text("❌ Planification annulée.")

async def _show_stype(q, time_str, tz):
    tz_s = f"UTC{'+' if tz >= 0 else ''}{tz}"
    kb = [
        [InlineKeyboardButton("📅 Une fois",      callback_data="stype:once")],
        [InlineKeyboardButton("🔁 Quotidien",     callback_data="stype:daily")],
        [InlineKeyboardButton("📆 Hebdomadaire",  callback_data="stype:weekly")],
    ]
    await q.edit_message_text(
        f"⏰ Heure : *{time_str}* {tz_s}\n\n*Étape 3/4 — Récurrence :*",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN,
    )

async def _show_days(q, selected):
    kb = []
    row = []
    for i, name in enumerate(DAY_NAMES):
        icon = "✅" if i in selected else "☐"
        row.append(InlineKeyboardButton(f"{icon} {name}", callback_data=f"day:{i}"))
        if len(row) == 2: kb.append(row); row = []
    if row: kb.append(row)
    kb.append([InlineKeyboardButton("✔️ Confirmer les jours", callback_data="days_ok")])
    await q.edit_message_text(
        "*Étape 4/4 — Jours d'envoi :*", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN,
    )

async def _show_confirm(q, uid):
    w    = wiz.get(uid, {})
    tz   = w.get("tz_offset", get_tz(uid))
    tz_s = f"UTC{'+' if tz >= 0 else ''}{tz}"
    rname = data.get("recipients", {}).get(str(w.get("recipient_id")), {}).get("name", "?")
    ct   = w.get("content_type", "?")
    emj  = TYPE_EMOJI.get(ct, "📨")
    st   = w.get("schedule_type", "?")
    if   st == "once":   recur = f"Une fois le *{datetime.strptime(w['date'], '%Y-%m-%d').strftime('%d/%m/%Y')}*"
    elif st == "daily":  recur = "Tous les jours"
    elif st == "weekly": recur = "Chaque *" + ", ".join(DAY_NAMES[d] for d in w.get("days", [])) + "*"
    else:                recur = st
    kb = [[InlineKeyboardButton("✅ Confirmer", callback_data="yes"),
           InlineKeyboardButton("❌ Annuler",   callback_data="no")]]
    await q.edit_message_text(
        f"📋 *Récapitulatif*\n\n"
        f"📍 Destination : *{rname}*\n"
        f"📨 Contenu : {emj} {ct}\n"
        f"⏰ Heure : *{w.get('time','?')}* {tz_s}\n"
        f"🔁 Récurrence : {recur}\n\n"
        f"Tout est correct ?",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN,
    )

async def _finalize(q, uid):
    w = wiz.pop(uid, {})
    job_id = str(uuid.uuid4())
    sched  = {
        "owner_id":      uid,
        "recipient_id":  w["recipient_id"],
        "content_type":  w["content_type"],
        "file_id":       w.get("file_id"),
        "text":          w.get("text", ""),
        "caption":       w.get("caption", ""),
        "schedule_type": w["schedule_type"],
        "time":          w["time"],
        "tz_offset":     w.get("tz_offset", get_tz(uid)),
        "date":          w.get("date"),
        "days":          w.get("days", list(range(7))),
        "active":        True,
        "status":        "pending",
        "created_at":    datetime.now(timezone.utc).isoformat(),
    }
    data["scheduled"][job_id] = sched
    save_data()
    sid = job_id[:8]
    await q.edit_message_text(
        f"✅ *Message planifié !*\n\n🆔 ID : `{sid}`\n\n/list — Voir tous tes messages",
        parse_mode=ParseMode.MARKDOWN,
    )

# ══════════════════════════════════════════════════════
#  MESSAGES PRIVÉS
# ══════════════════════════════════════════════════════

async def handle_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not update.effective_user: return
    uid  = update.effective_user.id
    w    = wiz.get(uid, {})
    step = w.get("step")
    if not step: return  # Pas en mode wizard → laisse les autres handlers agir

    if step == "fwd_recipient":
        fwd = msg.forward_from_chat
        if not fwd:
            await msg.reply_text("❌ Transfère (forward) un message depuis un canal.\nPour un groupe, tape /addrecipient <ID>.")
            raise ApplicationHandlerStop()
        key = str(fwd.id)
        data["recipients"][key] = {"owner_id": uid, "name": fwd.title or key, "type": fwd.type}
        save_data()
        wiz.pop(uid, None)
        await msg.reply_text(f"✅ *{fwd.title}* ajouté !\n\nTape /schedule pour planifier.", parse_mode=ParseMode.MARKDOWN)
        raise ApplicationHandlerStop()

    elif step == "await_content":
        ct = fid = txt = cap = None
        # Texte normal OU commande inconnue (ex: /s 25) → on traite comme texte
        if msg.text:
            ct, txt = "text", msg.text
        elif msg.photo:      ct, fid = "photo",      msg.photo[-1].file_id
        elif msg.video:      ct, fid = "video",      msg.video.file_id
        elif msg.document:   ct, fid = "document",   msg.document.file_id
        elif msg.audio:      ct, fid = "audio",      msg.audio.file_id
        elif msg.animation:  ct, fid = "animation",  msg.animation.file_id
        elif msg.voice:      ct, fid = "voice",      msg.voice.file_id
        elif msg.video_note: ct, fid = "video_note", msg.video_note.file_id
        elif msg.sticker:    ct, fid = "sticker",    msg.sticker.file_id
        else:
            await msg.reply_text("❌ Type non supporté.")
            raise ApplicationHandlerStop()
        cap = msg.caption or ""
        wiz[uid] = {**w, "step": "await_time", "content_type": ct, "file_id": fid, "text": txt or "", "caption": cap}
        tz   = w.get("tz_offset", get_tz(uid))
        tz_s = f"UTC{'+' if tz >= 0 else ''}{tz}"
        kb = [
            [InlineKeyboardButton("06:00", callback_data="time:06:00"),
             InlineKeyboardButton("08:00", callback_data="time:08:00"),
             InlineKeyboardButton("10:00", callback_data="time:10:00")],
            [InlineKeyboardButton("12:00", callback_data="time:12:00"),
             InlineKeyboardButton("14:00", callback_data="time:14:00"),
             InlineKeyboardButton("16:00", callback_data="time:16:00")],
            [InlineKeyboardButton("18:00", callback_data="time:18:00"),
             InlineKeyboardButton("20:00", callback_data="time:20:00"),
             InlineKeyboardButton("22:00", callback_data="time:22:00")],
            [InlineKeyboardButton("✏️ Autre heure", callback_data="time:custom")],
        ]
        await msg.reply_text(
            f"✅ Message reçu !\n\n*Étape 3/4 — Heure d'envoi ({tz_s}) :*",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN,
        )
        raise ApplicationHandlerStop()

    elif step == "await_time_text":
        raw = (msg.text or "").strip().replace("h", ":").replace(".", ":")
        try:
            parts = raw.split(":")
            h, m  = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
            if not (0 <= h <= 23 and 0 <= m <= 59): raise ValueError
            time_str = f"{h:02d}:{m:02d}"
        except (ValueError, IndexError):
            await msg.reply_text("❌ Format invalide. Exemple : `14:30`", parse_mode=ParseMode.MARKDOWN)
            raise ApplicationHandlerStop()
        wiz[uid] = {**w, "step": "pick_stype", "time": time_str}
        tz   = w.get("tz_offset", get_tz(uid))
        tz_s = f"UTC{'+' if tz >= 0 else ''}{tz}"
        kb   = [
            [InlineKeyboardButton("📅 Une fois",     callback_data="stype:once")],
            [InlineKeyboardButton("🔁 Quotidien",    callback_data="stype:daily")],
            [InlineKeyboardButton("📆 Hebdomadaire", callback_data="stype:weekly")],
        ]
        await msg.reply_text(
            f"⏰ Heure : *{time_str}* {tz_s}\n\n*Étape 3/4 — Récurrence :*",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN,
        )
        raise ApplicationHandlerStop()

    elif step == "await_date_text":
        raw = (msg.text or "").strip()
        try:
            d = datetime.strptime(raw, "%d/%m/%Y")
            if d.date() < datetime.now(timezone.utc).date():
                await msg.reply_text("❌ Date dans le passé.")
                raise ApplicationHandlerStop()
            date_str = d.strftime("%Y-%m-%d")
        except ValueError:
            await msg.reply_text("❌ Format invalide. Exemple : `28/06/2026`", parse_mode=ParseMode.MARKDOWN)
            raise ApplicationHandlerStop()
        w2 = {**w, "date": date_str, "step": "confirm"}
        wiz[uid] = w2
        tz   = w2.get("tz_offset", get_tz(uid))
        tz_s = f"UTC{'+' if tz >= 0 else ''}{tz}"
        rname = data.get("recipients", {}).get(str(w2.get("recipient_id")), {}).get("name", "?")
        kb = [[InlineKeyboardButton("✅ Confirmer", callback_data="yes"),
               InlineKeyboardButton("❌ Annuler",   callback_data="no")]]
        await msg.reply_text(
            f"📋 *Récapitulatif*\n\n"
            f"📍 Destination : *{rname}*\n"
            f"📨 Contenu : {TYPE_EMOJI.get(w2.get('content_type',''), '📨')} {w2.get('content_type','?')}\n"
            f"⏰ Heure : *{w2.get('time','?')}* {tz_s}\n"
            f"🔁 Récurrence : Une fois le *{d.strftime('%d/%m/%Y')}*\n\n"
            f"Tout est correct ?",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN,
        )
        raise ApplicationHandlerStop()

# ══════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════

async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("schedule",        "📅 Planifier un nouveau message"),
        BotCommand("list",            "📋 Voir mes messages planifiés"),
        BotCommand("delete",          "🗑️ Supprimer une planification"),
        BotCommand("pause",           "⏸️ Mettre en pause"),
        BotCommand("resume",          "▶️ Reprendre"),
        BotCommand("addrecipient",    "➕ Ajouter un canal/groupe"),
        BotCommand("listrecipients",  "📋 Voir mes destinations"),
        BotCommand("getid",           "🆔 Obtenir l'ID du groupe (dans le groupe)"),
        BotCommand("setup",           "⚙️ Configuration"),
        BotCommand("help",            "❓ Aide"),
    ])
    asyncio.create_task(scheduler_loop(app))

def main():
    if not BOT_TOKEN:
        print("\n⚠️  ERREUR : Variable BOT_TOKEN manquante !\n"); return
    load_data()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",           cmd_start))
    app.add_handler(CommandHandler("help",            cmd_help))
    app.add_handler(CommandHandler("setup",           cmd_setup))
    app.add_handler(CommandHandler("addrecipient",    cmd_addrecipient))
    app.add_handler(CommandHandler("listrecipients",  cmd_listrecipients))
    app.add_handler(CommandHandler("getid",           cmd_getid))
    app.add_handler(CommandHandler("schedule",        cmd_schedule))
    app.add_handler(CommandHandler("list",            cmd_list))
    app.add_handler(CommandHandler("delete",          cmd_delete))
    app.add_handler(CommandHandler("pause",           cmd_pause))
    app.add_handler(CommandHandler("resume",          cmd_resume))
    app.add_handler(CallbackQueryHandler(handle_cb))
    # group=-1 → priorité maximale, intercepte tout y compris les commandes inconnues
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE, handle_private), group=-1)
    app.post_init = post_init
    print("🤖 Bot Planificateur démarré !")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
