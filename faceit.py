import json
import os
import requests
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.ext import CallbackQueryHandler
from telegram.ext import Application, MessageHandler, filters
import asyncio
import aiohttp

# ==== КОНФИГ ====
FACEIT_API_KEY = "5929d726-8eb2-482b-9ca8-b4f5f1fbd13f"
TELEGRAM_TOKEN = "8054498045:AAG6dXSRgz6D1LeDqt7PjMZcTYIGfHan80U"
DATA_FILE = "players.json"

# ==== ЗАГРУЗКА/СОХРАНЕНИЕ ====
def load_players():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_players(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ==== FACEIT API ====


async def get_faceit_stats(session, nickname, player_id=None):
    headers = {"Authorization": f"Bearer {FACEIT_API_KEY}"}

    # Если player_id нет в базе — ищем его
    if not player_id:
        user_url = f"https://open.faceit.com/data/v4/players?nickname={nickname}"
        async with session.get(user_url, headers=headers) as resp:
            user_data = await resp.json()

        if "player_id" not in user_data or "cs2" not in user_data.get("games", {}):
            return nickname, None, None

        player_id = user_data["player_id"]

    # Получаем ELO
    player_url = f"https://open.faceit.com/data/v4/players/{player_id}"
    async with session.get(player_url, headers=headers) as resp:
        player_data = await resp.json()

    if "cs2" not in player_data.get("games", {}):
        return nickname, None, None

    elo = player_data["games"]["cs2"]["faceit_elo"]

    # Получаем последний матч
    matches_url = f"https://open.faceit.com/data/v4/players/{player_id}/history?game=cs2&limit=1"
    async with session.get(matches_url, headers=headers) as resp:
        matches_data = await resp.json()

    if not matches_data.get("items"):
        return nickname, elo, None

    last_match_id = matches_data["items"][0]["match_id"]

    # Получаем изменения ELO
    match_url = f"https://open.faceit.com/data/v4/matches/{last_match_id}"
    async with session.get(match_url, headers=headers) as resp:
        match_data = await resp.json()

    elo_diff = None
    for team in match_data.get("teams", {}).values():
        for player in team.get("players", []):
            if player["nickname"].lower() == nickname.lower():
                elo_diff = int(player["player_stats"].get("elo_change", 0))
                break

    return nickname, elo, elo_diff

# ==== /start ====


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type == "private":
        keyboard = [
            [InlineKeyboardButton(
                "➕ Добавить в группу", url=f"https://t.me/{context.bot.username}?startgroup=true")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "👋 Привет! Я бот для отслеживания Faceit ELO.\n"
            "Добавь меня в группу, чтобы следить за прогрессом игроков.",
            reply_markup=reply_markup
        )
    else:
        await update.message.reply_text("👋 Привет, я теперь слежу за игроками в этой группе!\n"
                                        "\nДобавляйте игроков: /register ник")

# ==== /register ====
# ==== /register ====
async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)

    if not context.args:
        await update.message.reply_text("⚠ Укажи никнейм: /register <ник>")
        return

    nickname = context.args[0]
    # теперь возвращает player_id, elo, cs2_id
    player_data = await get_player_data(nickname)

    if not player_data:
        await update.message.reply_text("🚫 Игрок не найден.")
        return

    player_id, elo = player_data
    if player_id is None:
        await update.message.reply_text("🚫 У игрока нет CS2 в профиле.")
        return

    data = load_players()
    if chat_id not in data:
        data[chat_id] = {}

    # Сохраняем ID и ELO
    data[chat_id][nickname] = {
        "id": player_id,
        "elo": elo
    }
    save_players(data)

    await update.message.reply_text(f"✅ {nickname} добавлен в список. 🎯 {elo} ELO")

async def get_current_elo(session, player_id):
    url = f"https://open.faceit.com/data/v4/players/{player_id}"
    headers = {"Authorization": f"Bearer {FACEIT_API_KEY}"}
    async with session.get(url, headers=headers) as resp:
        if resp.status != 200:
            return None
        data = await resp.json()
        return data.get("games", {}).get("cs2", {}).get("faceit_elo")

# ==== Получение player_id + ELO одним запросом ====
async def get_player_data(nickname):
    url = f"https://open.faceit.com/data/v4/players?nickname={nickname}"
    headers = {"Authorization": f"Bearer {FACEIT_API_KEY}"}

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

    cs2_id = None
    for game in data.get("games", {}):
        if game == "cs2":
            cs2_id = data["games"][game]["game_player_id"]

    if not cs2_id:
        return None  # нет CS2

    return data["player_id"], data["games"]["cs2"]["faceit_elo"]


# ==== /unregister ====
async def unregister(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❗ Используй: /unregister nickname")
        return
    nickname = context.args[0]
    chat_id = str(update.effective_chat.id)

    data = load_players()
    if chat_id not in data or nickname not in data[chat_id]:
        await update.message.reply_text(f"🚫 {nickname} нет в списке.")
        return

    del data[chat_id][nickname]
    save_players(data)
    await update.message.reply_text(f"🗑 {nickname} удалён из списка.")

# ==== /stats ====
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    data = load_players()

    if chat_id not in data or not data[chat_id]:
        await update.message.reply_text("📭 Список пуст.")
        return

    msg_lines = ["\n📊 Статистика игроков\n"]

    async with aiohttp.ClientSession() as session:
        tasks = []
        players_list = list(data[chat_id].items())  # [(nickname, pdata), ...]

        for nickname, pdata in players_list:
            tasks.append(get_current_elo(session, pdata["id"]))

        elos = await asyncio.gather(*tasks)

    # Создаем список кортежей (nickname, elo, prev_elo)
    players_with_elo = []
    for (nickname, pdata), current_elo in zip(players_list, elos):
        players_with_elo.append((nickname, current_elo, pdata["elo"]))

    # Фильтруем тех, у кого нет эло (None) - по желанию
    players_with_elo = [p for p in players_with_elo if p[1] is not None]

    # Сортируем по текущему elo по убыванию
    players_with_elo.sort(key=lambda x: x[1], reverse=True)

    # Формируем сообщение и обновляем локальные данные
    for i, (nickname, current_elo, prev_elo) in enumerate(players_with_elo, start=1):
        change = current_elo - prev_elo
        sign = "+" if change > 0 else ""
        msg_lines.append(f"{i}. {nickname} — {current_elo} ELO ({sign}{change})")
        data[chat_id][nickname]["elo"] = current_elo  # обновляем локально

    save_players(data)

    await update.message.reply_text("\n".join(msg_lines))

# ==== Фоновая проверка изменений ELO ====
async def check_elo_changes(app):
    while True:
        data = load_players()
        changed_chats = {}

        async with aiohttp.ClientSession() as session:
            tasks = []
            player_map = []

            for chat_id, players in data.items():
                for nickname, pdata in players.items():
                    tasks.append(get_elo_by_id(session, pdata["id"]))
                    player_map.append((chat_id, nickname, pdata["elo"]))

            results = await asyncio.gather(*tasks)

        for (chat_id, nickname, prev_elo), elo in zip(player_map, results):
            if elo is not None and elo != prev_elo:
                if chat_id not in changed_chats:
                    changed_chats[chat_id] = []
                changed_chats[chat_id].append((nickname, elo, elo - prev_elo))
                data[chat_id][nickname]["elo"] = elo

        if changed_chats:
            save_players(data)
            for chat_id, changes in changed_chats.items():
                msg_lines = ["📊 Обновление ELO:\n"]
                for i, (nickname, elo, change) in enumerate(sorted(changes, key=lambda x: x[1], reverse=True), start=1):
                    sign = "+" if change > 0 else ""
                    msg_lines.append(
                        f"{i}. {nickname} — {elo} ({sign}{change})")
                try:
                    await app.bot.send_message(chat_id=int(chat_id), text="\n".join(msg_lines))
                except:
                    pass

        await asyncio.sleep(10)


# ==== Получение ELO напрямую по player_id ====
async def get_elo_by_id(session, player_id):
    url = f"https://open.faceit.com/data/v4/players/{player_id}"
    headers = {"Authorization": f"Bearer {FACEIT_API_KEY}"}
    async with session.get(url, headers=headers) as resp:
        if resp.status != 200:
            return None
        data = await resp.json()
        return data.get("games", {}).get("cs2", {}).get("faceit_elo")

    # ==== Миграция старого формата players.json ====


async def migrate_players():
    data = load_players()
    updated = False

    async with aiohttp.ClientSession() as session:
        for chat_id, players in list(data.items()):
            for nickname, value in list(players.items()):
                # Если формат старый (elo — число, без id)
                if isinstance(value, int):
                    print(f"🔄 Миграция игрока {nickname}...")
                    player_id, elo = await get_player_data(nickname, session)
                    if player_id:
                        data[chat_id][nickname] = {
                            "id": player_id,
                            "elo": elo if elo is not None else value
                        }
                        updated = True
                    else:
                        print(f"⚠ Игрок {nickname} не найден, пропускаю.")

    if updated:
        save_players(data)
        print("✅ Миграция завершена — все игроки переведены на новый формат.")


# ==== Получение player_id и ELO (для регистрации и миграции) ====
async def get_player_data(nickname, session=None):
    url = f"https://open.faceit.com/data/v4/players?nickname={nickname}"
    headers = {"Authorization": f"Bearer {FACEIT_API_KEY}"}

    own_session = False
    if session is None:
        session = aiohttp.ClientSession()
        own_session = True

    async with session.get(url, headers=headers) as resp:
        if resp.status != 200:
            if own_session:
                await session.close()
            return None, None
        data = await resp.json()

    cs2_data = data.get("games", {}).get("cs2")
    if not cs2_data:
        if own_session:
            await session.close()
        return None, None

    if own_session:
        await session.close()

    return data.get("player_id"), cs2_data.get("faceit_elo")


async def migrate_and_start():
    await migrate_players()

import nest_asyncio
nest_asyncio.apply()

async def main():
    await migrate_players()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("register", register))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("unregister", unregister))

    await app.run_polling()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

