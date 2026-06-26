"""
Bot Telegram Auto-Suppression — Version Canaux + Groupes
Compatible : python-telegram-bot 20.3 / Python 3.13 / Termux

SETUP CANAL :
1. Crée un bot via @BotFather et copie le token
2. Ajoute le bot comme admin du canal (permission supprimer messages)
3. Envoie /setowner en privé au bot pour t'enregistrer comme propriétaire
4. Envoie /addchannel <channel_id> en privé pour lier ton canal
5. Envoie /addwhitelist <channel_id> <ton_id> pour t'exempter
6. Envoie /activate <channel_id> en privé pour démarrer

NOUVELLES COMMANDES :
  /setdelay <id> <secondes>   — Supprime X secondes après chaque message
  /settime <id> <HH:MM>      — Supprime TOUS les messages à une heure fixe (UTC)
  /cleartime <id>             — Annule la suppression à heure fixe

Installation :
    pip install python-telegram-bot==20.3

Lancement :
    python bot.py
"""

import logging
import asyncio
import json
import os
from datetime import datetime, timezone, timedelta

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.error import TelegramError

# ══════════════════════════════════════════════
#  ⚠️  REMPLACE PAR TON TOKEN BOTFATHER
# ══════════════════════════════════════════════
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

DATA_FILE = os.path.expanduser("~/bot_data.json")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

data = {
    "owners": {},
    "channels": {},
    "groups": {},
}

DEFAULT_DELAY = 600  # 10 minutes par défaut


# ══════════════════════════════════════════════
#  Persistance
# ══════════════════════════════════════════════
def load_data():
    global data
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r") as f:
                data = json.load(f)
            logger.info("Données chargées.")
        except Exception as e:
            logger.warning(f"Impossible de charger les données : {e}")


def save_data():
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning(f"Impossible de sauvegarder : {e}")


# ══════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════
def now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def format_delay(seconds: int) -> str:
    if seconds <= 0:
        return "désactivé"
    if seconds < 60:
        return f"{seconds} seconde(s)"
    elif seconds < 3600:
        return f"{seconds // 60} minute(s)"
    else:
        return f"{seconds // 3600} heure(s)"


def get_channel_state(channel_id: int, owner_id: int = None) -> dict:
    key = str(channel_id)
    if key not in data["channels"]:
        data["channels"][key] = {
            "owner_id": owner_id,
            "active": False,
            "delay": DEFAULT_DELAY,
            "delete_time": None,
            "activation_ts": None,
            "whitelist": [],
            "pending_message_ids": [],
        }
    s = data["channels"][key]
    if "delete_time" not in s:
        s["delete_time"] = None
    if "pending_message_ids" not in s:
        s["pending_message_ids"] = []
    return s


def get_group_state(chat_id: int) -> dict:
    key = str(chat_id)
    if key not in data["groups"]:
        data["groups"][key] = {
            "owner_id": None,
            "active": False,
            "delay": DEFAULT_DELAY,
            "activation_ts": None,
            "whitelist": [],
        }
    return data["groups"][key]


def is_registered_owner(user_id: int) -> bool:
    return str(user_id) in data["owners"]


async def fetch_group_owner(chat_id: int, bot) -> int | None:
    try:
        admins = await bot.get_chat_administrators(chat_id)
        for admin in admins:
            if admin.status == "creator":
                return admin.user.id
    except TelegramError as e:
        logger.warning(f"Impossible de récupérer les admins : {e}")
    return None


def seconds_until(time_str: str) -> float:
    """Secondes jusqu'à la prochaine occurrence de HH:MM UTC."""
    now = datetime.now(timezone.utc)
    hh, mm = map(int, time_str.split(":"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


# ══════════════════════════════════════════════
#  Suppression différée (délai après message)
# ══════════════════════════════════════════════
async def delete_after(bot, chat_id: int, message_id: int, delay: int, label: str):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info(f"[{chat_id}] Supprimé msg {message_id} ({label})")
        # Retire l'ID de la liste pending si présent
        key = str(chat_id)
        if key in data["channels"]:
            pending = data["channels"][key].get("pending_message_ids", [])
            if message_id in pending:
                pending.remove(message_id)
                save_data()
    except TelegramError as e:
        logger.warning(f"[{chat_id}] Echec suppression {message_id} : {e}")


# ══════════════════════════════════════════════
#  Suppression à heure fixe
# ══════════════════════════════════════════════
async def scheduled_delete_task(bot, channel_id: int, time_str: str):
    """Boucle qui supprime tous les messages en attente à l'heure programmée."""
    logger.info(f"[{channel_id}] Tâche programmée démarrée pour {time_str} UTC")
    while True:
        wait = seconds_until(time_str)
        logger.info(f"[{channel_id}] Prochaine suppression dans {wait:.0f}s")
        await asyncio.sleep(wait)

        key = str(channel_id)
        if key not in data["channels"]:
            break
        state = data["channels"][key]
        if not state["active"] or state.get("delete_time") != time_str:
            logger.info(f"[{channel_id}] Tâche annulée.")
            break

        pending = state.get("pending_message_ids", []).copy()
        logger.info(f"[{channel_id}] Suppression programmée : {len(pending)} messages")

        deleted = 0
        for msg_id in pending:
            try:
                await bot.delete_message(chat_id=channel_id, message_id=msg_id)
                deleted += 1
                await asyncio.sleep(0.05)
            except TelegramError:
                pass

        state["pending_message_ids"] = []
        save_data()
        logger.info(f"[{channel_id}] {deleted} messages supprimés.")

        await asyncio.sleep(65)  # Évite double déclenchement


# Tâches actives
scheduled_tasks: dict[str, asyncio.Task] = {}


def start_scheduled_task(bot, channel_id: int, time_str: str):
    key = str(channel_id)
    if key in scheduled_tasks:
        scheduled_tasks[key].cancel()
    task = asyncio.create_task(scheduled_delete_task(bot, channel_id, time_str))
    scheduled_tasks[key] = task


def stop_scheduled_task(channel_id: int):
    key = str(channel_id)
    if key in scheduled_tasks:
        scheduled_tasks[key].cancel()
        del scheduled_tasks[key]


# ══════════════════════════════════════════════
#  COMMANDES
# ══════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Bot Auto-Suppression\n\n"
        "=== SETUP RAPIDE ===\n"
        "1. /setowner\n"
        "2. /addchannel <id_canal>\n"
        "3. /addwhitelist <id_canal> <ton_id>  ← IMPORTANT\n"
        "4. /activate <id_canal>\n\n"
        "=== SUPPRESSION ===\n"
        "/setdelay <id> <secondes>\n"
        "  Supprime chaque message après X secondes\n"
        "  Ex: /setdelay -1001234 600  (10 minutes)\n\n"
        "/settime <id> <HH:MM>\n"
        "  Supprime TOUS les messages à cette heure UTC\n"
        "  Ex: /settime -1001234 20:00\n\n"
        "/cleartime <id>\n"
        "  Annule la suppression à heure fixe\n\n"
        "⚠️ Les deux modes peuvent fonctionner ensemble !\n"
        "Tape /help pour tous les détails."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 AIDE COMPLÈTE\n\n"
        "/setowner\n"
        "  → T'enregistre comme propriétaire\n\n"
        "/addchannel -1001234567890\n"
        "  → Lie un canal (ID via @userinfobot)\n\n"
        "/addwhitelist -1001234567890 <ton_id>\n"
        "  → Tes messages ne seront JAMAIS supprimés\n\n"
        "/activate -1001234567890\n"
        "  → Active le bot sur ce canal\n\n"
        "/deactivate -1001234567890\n"
        "  → Désactive\n\n"
        "/setdelay -1001234567890 600\n"
        "  → Supprime chaque message 10min après\n"
        "  → /setdelay -1001234567890 0 pour désactiver\n\n"
        "/settime -1001234567890 20:00\n"
        "  → Supprime TOUS les messages à 20h00 UTC\n"
        "  → France été : heure française - 2h\n"
        "  → France hiver : heure française - 1h\n\n"
        "/cleartime -1001234567890\n"
        "  → Annule l'heure programmée\n\n"
        "/status -1001234567890\n"
        "  → Voir l'état et la prochaine suppression\n\n"
        "/listchannels\n"
        "  → Liste tes canaux\n\n"
        "=== DÉLAIS UTILES ===\n"
        "60=1min | 300=5min | 600=10min\n"
        "1800=30min | 3600=1h | 86400=24h"
    )


async def cmd_setowner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Utilise cette commande en privé avec le bot.")
        return
    user_id = update.effective_user.id
    data["owners"][str(user_id)] = True
    save_data()
    await update.message.reply_text(
        f"✅ Enregistré comme propriétaire !\n"
        f"Ton ID : {user_id}\n\n"
        f"Prochaine étape : /addchannel <channel_id>"
    )


async def cmd_addchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Utilise cette commande en privé.")
        return
    user_id = update.effective_user.id
    if not is_registered_owner(user_id):
        await update.message.reply_text("❌ Tape /setowner d'abord.")
        return
    if not context.args:
        await update.message.reply_text("Usage : /addchannel <channel_id>")
        return
    try:
        channel_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID invalide.")
        return
    try:
        chat = await context.bot.get_chat(channel_id)
        bot_member = await context.bot.get_chat_member(channel_id, context.bot.id)
        if bot_member.status not in ("administrator", "creator"):
            await update.message.reply_text("❌ Le bot n'est pas admin de ce canal.")
            return
    except TelegramError as e:
        await update.message.reply_text(f"❌ Impossible d'accéder au canal : {e}")
        return

    state = get_channel_state(channel_id, owner_id=user_id)
    state["owner_id"] = user_id
    save_data()
    await update.message.reply_text(
        f"✅ Canal lié !\n"
        f"Nom : {chat.title}\n"
        f"ID : {channel_id}\n\n"
        f"Étapes suivantes :\n"
        f"1. /addwhitelist {channel_id} {user_id}\n"
        f"   (pour que tes messages ne soient pas supprimés)\n"
        f"2. /setdelay {channel_id} 600  (optionnel)\n"
        f"3. /settime {channel_id} 20:00  (optionnel)\n"
        f"4. /activate {channel_id}"
    )


async def cmd_listchannels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Utilise cette commande en privé.")
        return
    user_id = update.effective_user.id
    if not is_registered_owner(user_id):
        await update.message.reply_text("❌ Tape /setowner d'abord.")
        return
    mes_canaux = [(cid, s) for cid, s in data["channels"].items() if s.get("owner_id") == user_id]
    if not mes_canaux:
        await update.message.reply_text("Aucun canal. Utilise /addchannel.")
        return
    lines = ["📋 Tes canaux :\n"]
    for cid, s in mes_canaux:
        icon = "🟢" if s["active"] else "🔴"
        lines.append(
            f"{icon} ID: {cid}\n"
            f"   Délai: {format_delay(s.get('delay', 0))}\n"
            f"   Heure fixe: {s.get('delete_time') or 'aucune'}\n"
            f"   Whitelist: {len(s.get('whitelist', []))} personne(s)\n"
        )
    await update.message.reply_text("\n".join(lines))


async def cmd_activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_type = update.effective_chat.type
    user_id = update.effective_user.id

    if chat_type == ChatType.PRIVATE:
        if not is_registered_owner(user_id):
            await update.message.reply_text("❌ Tape /setowner d'abord.")
            return
        if not context.args:
            await update.message.reply_text("Usage : /activate <channel_id>")
            return
        try:
            channel_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ ID invalide.")
            return
        key = str(channel_id)
        if key not in data["channels"] or data["channels"][key].get("owner_id") != user_id:
            await update.message.reply_text("❌ Canal non trouvé. Utilise /addchannel d'abord.")
            return
        state = data["channels"][key]
        state["active"] = True
        state["activation_ts"] = now_ts()
        save_data()

        if state.get("delete_time"):
            start_scheduled_task(context.bot, channel_id, state["delete_time"])

        delay_info = format_delay(state.get("delay", 0))
        time_info = state.get("delete_time") or "aucune"
        wl = state.get("whitelist", [])

        await update.message.reply_text(
            f"✅ Bot activé pour le canal {channel_id} !\n\n"
            f"Suppression par délai : {delay_info}\n"
            f"Suppression à heure fixe : {time_info} UTC\n"
            f"Whitelist : {len(wl)} personne(s)\n\n"
            + ("⚠️ Tu n'es pas dans la whitelist !\nTes messages seront supprimés.\n"
               f"Tape : /addwhitelist {channel_id} {user_id}"
               if user_id not in wl else "✅ Tu es dans la whitelist.")
        )
        return

    # Groupe
    chat_id = update.effective_chat.id
    state = get_group_state(chat_id)
    if state["owner_id"] is None:
        state["owner_id"] = await fetch_group_owner(chat_id, context.bot)
    if user_id != state["owner_id"]:
        await update.message.reply_text("❌ Seul le propriétaire du groupe peut faire ça.")
        return
    if state["active"]:
        await update.message.reply_text(f"✅ Déjà actif — délai : {format_delay(state['delay'])}")
        return
    state["active"] = True
    state["activation_ts"] = now_ts()
    save_data()
    await update.message.reply_text(
        f"✅ Bot activé !\n"
        f"Messages supprimés après {format_delay(state['delay'])}."
    )


async def cmd_deactivate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_type = update.effective_chat.type
    user_id = update.effective_user.id

    if chat_type == ChatType.PRIVATE:
        if not is_registered_owner(user_id):
            await update.message.reply_text("❌ Tu n'es pas enregistré.")
            return
        if not context.args:
            await update.message.reply_text("Usage : /deactivate <channel_id>")
            return
        try:
            channel_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ ID invalide.")
            return
        key = str(channel_id)
        if key not in data["channels"] or data["channels"][key].get("owner_id") != user_id:
            await update.message.reply_text("❌ Canal non trouvé.")
            return
        data["channels"][key]["active"] = False
        data["channels"][key]["activation_ts"] = None
        stop_scheduled_task(channel_id)
        save_data()
        await update.message.reply_text(f"⏹️ Bot désactivé pour le canal {channel_id}.")
        return

    chat_id = update.effective_chat.id
    state = get_group_state(chat_id)
    if state["owner_id"] is None:
        state["owner_id"] = await fetch_group_owner(chat_id, context.bot)
    if user_id != state["owner_id"]:
        await update.message.reply_text("❌ Seul le propriétaire peut faire ça.")
        return
    state["active"] = False
    state["activation_ts"] = None
    save_data()
    await update.message.reply_text("⏹️ Bot désactivé.")


async def cmd_setdelay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_type = update.effective_chat.type
    user_id = update.effective_user.id

    if chat_type == ChatType.PRIVATE:
        if not is_registered_owner(user_id):
            await update.message.reply_text("❌ Tu n'es pas enregistré.")
            return
        if len(context.args) < 2:
            await update.message.reply_text(
                "Usage : /setdelay <channel_id> <secondes>\n"
                "Exemples :\n"
                "  /setdelay -1001234567890 600   (10 minutes)\n"
                "  /setdelay -1001234567890 3600  (1 heure)\n"
                "  /setdelay -1001234567890 0     (désactiver)"
            )
            return
        try:
            channel_id = int(context.args[0])
            delay = int(context.args[1])
        except ValueError:
            await update.message.reply_text("❌ Valeurs invalides.")
            return
        key = str(channel_id)
        if key not in data["channels"] or data["channels"][key].get("owner_id") != user_id:
            await update.message.reply_text("❌ Canal non trouvé.")
            return
        if delay < 0 or delay > 86400:
            await update.message.reply_text("⚠️ Délai entre 0 et 86400 secondes.")
            return
        data["channels"][key]["delay"] = delay
        save_data()
        await update.message.reply_text(
            f"⏱️ Délai mis à jour : {format_delay(delay)}\n"
            f"(0 = suppression par délai désactivée)"
        )
        return

    chat_id = update.effective_chat.id
    state = get_group_state(chat_id)
    if state["owner_id"] is None:
        state["owner_id"] = await fetch_group_owner(chat_id, context.bot)
    if user_id != state["owner_id"]:
        await update.message.reply_text("❌ Seul le propriétaire peut faire ça.")
        return
    if not context.args:
        await update.message.reply_text("Usage : /setdelay <secondes>")
        return
    try:
        delay = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Valeur invalide.")
        return
    if delay < 0 or delay > 86400:
        await update.message.reply_text("⚠️ Délai entre 0 et 86400 secondes.")
        return
    state["delay"] = delay
    save_data()
    await update.message.reply_text(f"⏱️ Délai mis à jour : {format_delay(delay)}")


async def cmd_settime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Programme une suppression à heure fixe chaque jour."""
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Utilise cette commande en privé.")
        return
    user_id = update.effective_user.id
    if not is_registered_owner(user_id):
        await update.message.reply_text("❌ Tu n'es pas enregistré.")
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage : /settime <channel_id> <HH:MM>\n\n"
            "Exemple : /settime -1001234567890 20:00\n\n"
            "⚠️ L'heure est en UTC !\n"
            "France été (CEST) = UTC+2\n"
            "  → 22h00 heure française = 20:00 UTC\n"
            "France hiver (CET) = UTC+1\n"
            "  → 22h00 heure française = 21:00 UTC"
        )
        return
    try:
        channel_id = int(context.args[0])
        time_str = context.args[1]
        hh, mm = map(int, time_str.split(":"))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
        time_str = f"{hh:02d}:{mm:02d}"
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Format invalide. Exemple : 20:00")
        return

    key = str(channel_id)
    if key not in data["channels"] or data["channels"][key].get("owner_id") != user_id:
        await update.message.reply_text("❌ Canal non trouvé.")
        return

    data["channels"][key]["delete_time"] = time_str
    save_data()

    if data["channels"][key].get("active"):
        start_scheduled_task(context.bot, channel_id, time_str)

    wait = seconds_until(time_str)
    h = int(wait // 3600)
    m = int((wait % 3600) // 60)

    await update.message.reply_text(
        f"🕐 Suppression programmée à {time_str} UTC chaque jour !\n\n"
        f"Prochaine suppression dans : {h}h {m}min\n\n"
        f"Tous les messages du canal seront supprimés,\n"
        f"sauf ceux des personnes dans la whitelist."
    )


async def cmd_cleartime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Utilise cette commande en privé.")
        return
    user_id = update.effective_user.id
    if not is_registered_owner(user_id):
        await update.message.reply_text("❌ Tu n'es pas enregistré.")
        return
    if not context.args:
        await update.message.reply_text("Usage : /cleartime <channel_id>")
        return
    try:
        channel_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID invalide.")
        return
    key = str(channel_id)
    if key not in data["channels"] or data["channels"][key].get("owner_id") != user_id:
        await update.message.reply_text("❌ Canal non trouvé.")
        return
    data["channels"][key]["delete_time"] = None
    stop_scheduled_task(channel_id)
    save_data()
    await update.message.reply_text("✅ Heure de suppression annulée.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_type = update.effective_chat.type
    user_id = update.effective_user.id

    if chat_type == ChatType.PRIVATE:
        if not is_registered_owner(user_id):
            await update.message.reply_text("❌ Tu n'es pas enregistré.")
            return
        if not context.args:
            await update.message.reply_text("Usage : /status <channel_id>")
            return
        try:
            channel_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ ID invalide.")
            return
        key = str(channel_id)
        if key not in data["channels"] or data["channels"][key].get("owner_id") != user_id:
            await update.message.reply_text("❌ Canal non trouvé.")
            return
        state = data["channels"][key]
        icon = "🟢 Actif" if state["active"] else "🔴 Inactif"
        wl = state.get("whitelist", [])
        pending = len(state.get("pending_message_ids", []))
        delay_info = format_delay(state.get("delay", 0))
        time_info = state.get("delete_time") or "aucune"

        next_del = ""
        if state.get("delete_time") and state["active"]:
            wait = seconds_until(state["delete_time"])
            h = int(wait // 3600)
            m = int((wait % 3600) // 60)
            next_del = f"\nProchaine suppression dans : {h}h {m}min"

        await update.message.reply_text(
            f"📊 Canal {channel_id}\n\n"
            f"Statut : {icon}\n"
            f"Délai par message : {delay_info}\n"
            f"Heure fixe (UTC) : {time_info}{next_del}\n"
            f"Messages en attente : {pending}\n"
            f"Whitelist : {len(wl)} personne(s)"
        )
        return

    chat_id = update.effective_chat.id
    state = get_group_state(chat_id)
    icon = "🟢 Actif" if state["active"] else "🔴 Inactif"
    await update.message.reply_text(
        f"📊 État du bot\n\n"
        f"Statut : {icon}\n"
        f"Délai : {format_delay(state['delay'])}\n"
        f"Whitelist : {len(state.get('whitelist', []))} membre(s)"
    )


async def cmd_addwhitelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_type = update.effective_chat.type
    user_id = update.effective_user.id

    if chat_type == ChatType.PRIVATE:
        if not is_registered_owner(user_id):
            await update.message.reply_text("❌ Tu n'es pas enregistré.")
            return
        if len(context.args) < 2:
            await update.message.reply_text("Usage : /addwhitelist <channel_id> <user_id>")
            return
        try:
            channel_id = int(context.args[0])
            target_id = int(context.args[1])
        except ValueError:
            await update.message.reply_text("❌ IDs invalides.")
            return
        key = str(channel_id)
        if key not in data["channels"] or data["channels"][key].get("owner_id") != user_id:
            await update.message.reply_text("❌ Canal non trouvé.")
            return
        wl = data["channels"][key].setdefault("whitelist", [])
        if target_id not in wl:
            wl.append(target_id)
            save_data()
            await update.message.reply_text(f"✅ Utilisateur {target_id} exempté.")
        else:
            await update.message.reply_text("Déjà dans la whitelist.")
        return

    chat_id = update.effective_chat.id
    state = get_group_state(chat_id)
    if state["owner_id"] is None:
        state["owner_id"] = await fetch_group_owner(chat_id, context.bot)
    if user_id != state["owner_id"]:
        await update.message.reply_text("❌ Seul le propriétaire peut faire ça.")
        return
    if not context.args:
        await update.message.reply_text("Usage : /addwhitelist <user_id>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID invalide.")
        return
    wl = state.setdefault("whitelist", [])
    if target_id not in wl:
        wl.append(target_id)
        save_data()
        await update.message.reply_text(f"✅ Utilisateur {target_id} exempté.")
    else:
        await update.message.reply_text("Déjà dans la whitelist.")


async def cmd_removewhitelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_type = update.effective_chat.type
    user_id = update.effective_user.id

    if chat_type == ChatType.PRIVATE:
        if not is_registered_owner(user_id):
            await update.message.reply_text("❌ Tu n'es pas enregistré.")
            return
        if len(context.args) < 2:
            await update.message.reply_text("Usage : /removewhitelist <channel_id> <user_id>")
            return
        try:
            channel_id = int(context.args[0])
            target_id = int(context.args[1])
        except ValueError:
            await update.message.reply_text("❌ IDs invalides.")
            return
        key = str(channel_id)
        if key not in data["channels"] or data["channels"][key].get("owner_id") != user_id:
            await update.message.reply_text("❌ Canal non trouvé.")
            return
        wl = data["channels"][key].get("whitelist", [])
        if target_id in wl:
            wl.remove(target_id)
            save_data()
            await update.message.reply_text(f"✅ Utilisateur {target_id} retiré.")
        else:
            await update.message.reply_text("Pas dans la whitelist.")
        return

    chat_id = update.effective_chat.id
    state = get_group_state(chat_id)
    if state["owner_id"] is None:
        state["owner_id"] = await fetch_group_owner(chat_id, context.bot)
    if user_id != state["owner_id"]:
        await update.message.reply_text("❌ Seul le propriétaire peut faire ça.")
        return
    if not context.args:
        await update.message.reply_text("Usage : /removewhitelist <user_id>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID invalide.")
        return
    wl = state.get("whitelist", [])
    if target_id in wl:
        wl.remove(target_id)
        save_data()
        await update.message.reply_text(f"✅ Utilisateur {target_id} retiré.")
    else:
        await update.message.reply_text("Pas dans la whitelist.")



# Canaux en mode apprentissage : channel_id → owner_user_id
learning_mode: dict[str, int] = {}

# Utilisateurs en attente de setup : user_id → True
pending_setup: dict[int, bool] = {}


async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Configuration rapide : tout en un seul message transféré."""
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Utilise cette commande en privé avec le bot.")
        return
    user_id = update.effective_user.id
    data["owners"][str(user_id)] = True
    save_data()
    pending_setup[user_id] = True
    await update.message.reply_text(
        "🚀 Configuration rapide démarrée !\n\n"
        "Ton compte a été enregistré automatiquement.\n\n"
        "📩 Fais maintenant :\n"
        "Transfère (forward) n'importe quel message\n"
        "depuis ton canal vers ce chat.\n\n"
        "Le bot va tout configurer automatiquement !\n"
        "(canal + propriétaire + activation)"
    )


async def handle_setup_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reçoit le message transféré et configure tout automatiquement."""
    msg = update.message
    if not msg or not update.effective_user:
        return
    user_id = update.effective_user.id
    if user_id not in pending_setup:
        return

    forward_chat = msg.forward_from_chat
    if not forward_chat or forward_chat.type != "channel":
        await msg.reply_text(
            "❌ Ce n'est pas un message transféré depuis un canal.\n\n"
            "Transfère (forward) un message depuis TON canal."
        )
        return

    channel_id = forward_chat.id
    key = str(channel_id)
    channel_name = forward_chat.title or str(channel_id)

    if key not in data["channels"]:
        data["channels"][key] = {
            "owner_id": user_id,
            "active": True,
            "delay": 600,
            "delete_time": None,
            "pending_message_ids": [],
            "whitelist": [user_id],
            "whitelist_signatures": [],
            "activation_ts": datetime.now(timezone.utc).timestamp(),
        }
    else:
        state = data["channels"][key]
        state["owner_id"] = user_id
        state["active"] = True
        state["activation_ts"] = datetime.now(timezone.utc).timestamp()
        wl = state.setdefault("whitelist", [])
        if user_id not in wl:
            wl.append(user_id)

    save_data()
    del pending_setup[user_id]
    learning_mode[key] = user_id

    await msg.reply_text(
        f"✅ Canal configuré avec succès !\n\n"
        f"📺 Canal : {channel_name}\n"
        f"🆔 ID : {channel_id}\n"
        f"⏱️ Délai suppression : 10 min (modifiable avec /setdelay)\n"
        f"⚪ Whitelist : toi uniquement\n\n"
        f"🎓 Mode apprentissage activé !\n\n"
        f"Poste maintenant N'IMPORTE QUEL message\n"
        f"dans ton canal avec ta signature.\n"
        f"Le bot va capturer ta signature et tes messages\n"
        f"ne seront plus jamais supprimés. ✅"
    )



    """Active le mode apprentissage : le prochain message du proprio dans le canal
    sera utilisé pour capturer sa signature automatiquement."""
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Utilise cette commande en privé.")
        return
    user_id = update.effective_user.id
    if not is_registered_owner(user_id):
        await update.message.reply_text("❌ Tape /setowner d'abord.")
        return
    if not context.args:
        await update.message.reply_text("Usage : /learnsig <channel_id>")
        return
    try:
        channel_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID canal invalide.")
        return
    key = str(channel_id)
    if key not in data["channels"] or data["channels"][key].get("owner_id") != user_id:
        await update.message.reply_text("❌ Canal non trouvé.")
        return
    learning_mode[key] = user_id
    await update.message.reply_text(
        "🎓 Mode apprentissage activé !\n\n"
        "Poste maintenant N'IMPORTE QUEL message dans ton canal.\n"
        "Le bot va capturer ta signature exacte automatiquement\n"
        "et te confirmer ici en privé.\n\n"
        "⚠️ Ce message NE sera PAS supprimé."
    )


async def cmd_removesig(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Retire une signature de la whitelist."""
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.message.reply_text("Utilise cette commande en privé.")
        return
    user_id = update.effective_user.id
    if not is_registered_owner(user_id):
        await update.message.reply_text("❌ Tu n'es pas enregistré.")
        return
    if not context.args:
        await update.message.reply_text("Usage : /removesig <channel_id>")
        return
    try:
        channel_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID canal invalide.")
        return
    key = str(channel_id)
    if key not in data["channels"] or data["channels"][key].get("owner_id") != user_id:
        await update.message.reply_text("❌ Canal non trouvé.")
        return
    wl = data["channels"][key].get("whitelist_signatures", [])
    if not wl:
        await update.message.reply_text("❌ Aucune signature enregistrée.")
        return
    lines = "\n".join(f"{i+1}. \"{s}\"" for i, s in enumerate(wl))
    data["channels"][key]["whitelist_signatures"] = []
    save_data()
    await update.message.reply_text(f"✅ Signatures supprimées :\n{lines}")


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if not msg:
        return

    channel_id = update.effective_chat.id
    key = str(channel_id)

    if key not in data["channels"]:
        return

    state = data["channels"][key]

    if not state["active"]:
        return

    if state["activation_ts"] and msg.date:
        msg_ts = msg.date.replace(tzinfo=timezone.utc).timestamp()
        if msg_ts < state["activation_ts"]:
            return

    whitelist_ids = state.get("whitelist", [])
    whitelist_sigs = state.get("whitelist_signatures", [])
    sig = msg.author_signature  # Signature réelle reçue par l'API Telegram
    author = msg.from_user

    logger.info(f"[{channel_id}] msg={msg.message_id} author_signature={repr(sig)} from_user={author}")

    # ── Mode apprentissage ────────────────────────────────────────────────
    if key in learning_mode:
        owner_user_id = learning_mode.pop(key)
        if sig:
            wl = state.setdefault("whitelist_signatures", [])
            if sig not in wl:
                wl.append(sig)
                save_data()
            try:
                await context.bot.send_message(
                    chat_id=owner_user_id,
                    text=(
                        f"✅ Signature apprise avec succès !\n\n"
                        f"Signature détectée : \"{sig}\"\n\n"
                        f"Tes messages avec cette signature ne seront plus jamais supprimés."
                    )
                )
            except Exception:
                pass
        else:
            try:
                await context.bot.send_message(
                    chat_id=owner_user_id,
                    text=(
                        "❌ Signature non détectée sur ce message.\n\n"
                        "Vérifie que 'Signer les messages' est bien activé\n"
                        "dans tes paramètres d'admin du canal,\n"
                        "puis retape /learnsig et réessaie."
                    )
                )
            except Exception:
                pass
        return  # Ne supprime jamais le message d'apprentissage

    # ── Vérification whitelist normale ───────────────────────────────────
    if author is not None and author.id in whitelist_ids:
        return
    if sig and sig in whitelist_sigs:
        return
    # Sinon → on supprime

    # Stocke le message pour la suppression à heure fixe
    pending = state.setdefault("pending_message_ids", [])
    if msg.message_id not in pending:
        pending.append(msg.message_id)
        if len(pending) > 1000:
            state["pending_message_ids"] = pending[-1000:]
        save_data()

    # Suppression par délai
    delay = state.get("delay", 0)
    if delay > 0:
        asyncio.create_task(
            delete_after(context.bot, channel_id, msg.message_id, delay, "canal délai")
        )


# ══════════════════════════════════════════════
#  HANDLER — Groupe
# ══════════════════════════════════════════════
async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user

    if not msg or not user:
        return

    chat_id = update.effective_chat.id
    state = get_group_state(chat_id)

    if not state["active"]:
        return

    if state["owner_id"] is None:
        state["owner_id"] = await fetch_group_owner(chat_id, context.bot)

    if user.id == state["owner_id"]:
        return
    if user.id in state.get("whitelist", []):
        return
    if user.is_bot:
        return

    if state["activation_ts"] and msg.date:
        msg_ts = msg.date.replace(tzinfo=timezone.utc).timestamp()
        if msg_ts < state["activation_ts"]:
            return

    delay = state.get("delay", 0)
    if delay > 0:
        asyncio.create_task(
            delete_after(context.bot, chat_id, msg.message_id, delay, f"groupe user {user.id}")
        )


# ══════════════════════════════════════════════
#  Relance les tâches au démarrage
# ══════════════════════════════════════════════
async def restore_scheduled_tasks(app):
    for key, state in data["channels"].items():
        if state.get("active") and state.get("delete_time"):
            channel_id = int(key)
            logger.info(f"Restauration tâche canal {channel_id} à {state['delete_time']}")
            start_scheduled_task(app.bot, channel_id, state["delete_time"])


# ══════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════
async def post_init(app):
    """Initialisation : menu commandes + restauration tâches."""
    from telegram import BotCommand
    commands = [
        BotCommand("setup",           "⚡ Configuration rapide d'un canal"),
        BotCommand("status",          "📊 État d'un canal"),
        BotCommand("setdelay",        "⏱️ Délai de suppression (secondes)"),
        BotCommand("settime",         "🕐 Heure fixe de suppression (HH:MM UTC)"),
        BotCommand("cleartime",       "❌ Annuler l'heure programmée"),
        BotCommand("learnsig",        "🎓 Apprendre ma signature"),
        BotCommand("removesig",       "🗑️ Supprimer ma signature"),
        BotCommand("activate",        "✅ Activer le bot sur un canal"),
        BotCommand("deactivate",      "⛔ Désactiver le bot"),
        BotCommand("listchannels",    "📋 Lister mes canaux"),
        BotCommand("addwhitelist",    "➕ Exempter un utilisateur"),
        BotCommand("removewhitelist", "➖ Retirer une exemption"),
        BotCommand("addchannel",      "📡 Ajouter un canal manuellement"),
        BotCommand("help",            "❓ Aide complète"),
    ]
    await app.bot.set_my_commands(commands)
    await restore_scheduled_tasks(app)


def main():
    if not BOT_TOKEN:
        print("\n⚠️  ERREUR : Variable BOT_TOKEN manquante dans les variables Railway !\n")
        return

    load_data()

    app = Application.builder().token(BOT_TOKEN).build()

    # ── Commandes ──────────────────────────────────────────────────────────
    app.add_handler(CommandHandler("start",           cmd_start))
    app.add_handler(CommandHandler("help",            cmd_help))
    app.add_handler(CommandHandler("setup",           cmd_setup))
    app.add_handler(CommandHandler("setowner",        cmd_setowner))
    app.add_handler(CommandHandler("addchannel",      cmd_addchannel))
    app.add_handler(CommandHandler("listchannels",    cmd_listchannels))
    app.add_handler(CommandHandler("activate",        cmd_activate))
    app.add_handler(CommandHandler("deactivate",      cmd_deactivate))
    app.add_handler(CommandHandler("setdelay",        cmd_setdelay))
    app.add_handler(CommandHandler("settime",         cmd_settime))
    app.add_handler(CommandHandler("cleartime",       cmd_cleartime))
    app.add_handler(CommandHandler("status",          cmd_status))
    app.add_handler(CommandHandler("addwhitelist",    cmd_addwhitelist))
    app.add_handler(CommandHandler("removewhitelist", cmd_removewhitelist))
    app.add_handler(CommandHandler("learnsig",        cmd_learnsig))
    app.add_handler(CommandHandler("removesig",       cmd_removesig))

    # ── Messages privés : setup via message transféré ─────────────────────
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & ~filters.COMMAND,
        handle_setup_forward,
    ))

    # ── Messages canal et groupe ───────────────────────────────────────────
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POSTS, handle_channel_post))
    app.add_handler(MessageHandler(
        filters.ChatType.GROUPS & filters.ALL & ~filters.COMMAND,
        handle_group_message,
    ))

    app.post_init = post_init

    print("🤖 Bot démarré ! Ctrl+C pour arrêter.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
