import os
import asyncio
import aiohttp
import asyncpg
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes
import nest_asyncio

# ==== КОНФИГ ====
DATABASE_URL = os.getenv("DATABASE_URL")  # Railway variable
FACEIT_API_KEY = os.getenv("FACEIT_API_KEY")  # Лучше хранить в переменной Railway
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")  # Токен бота

pool = None  # Глобальный пул подключений


# ==== ИНИЦИАЛИЗАЦИЯ БАЗЫ ====
async def create_pool():
    global pool
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL is not set. Add it in Railway Variables.")

    pool = await asyncpg.create_pool(DATABASE_URL, ssl="require")

    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS players (
                chat_id TEXT NOT NULL,
                nickname TEXT NOT NULL,
                player_id TEXT NOT NULL,
                elo INTEGER NOT NULL,
                PRIMARY KEY (chat_id, nickname)
            );
        """)


# ==== РАБОТА С БАЗОЙ ====
async def get_players(chat_id: str):
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT nickname, player_id, elo FROM players WHERE chat_id=$1", chat_id)
        return {row['nickname']: {'id': row['player_id'], 'elo': row['elo']} for row in rows}


async def add_or_update_player(chat_id, nickname, player_id, elo):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO players(chat_id, nickname, player_id, elo) 
            VALUES($1, $2, $3, $4)
            ON CONFLICT (chat_id, nickname) DO UPDATE 
            SET player_id=EXCLUDED.player_id, elo=EXCLUDED.elo
        """, chat_id, nickname, player_id, elo)


async def remove_player(chat_id, nickname):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM players WHERE chat_id=$1 AND nickname=$2", chat_id, nickname)


async def get_all_chats():
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT DISTINCT chat_id FROM players")
        return [row['chat_id'] for row in rows]


# ==== FACEIT API ====
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


async def get_current_elo(session, player_id):
    url = f"https://open.faceit.com/data/v4/players/{player_id}"
    headers = {"Authorization": f"Bearer {FACEIT_API_KEY}"}
    async with session.get(url, headers=headers) as resp:
        if resp.status != 200:
            return None
        data = await resp.json()
        return data.get("games", {}).get("cs2", {}).get("faceit_elo")


# ==== ХЭНДЛЕРЫ ====
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
        await update.message.reply_text(
            "👋 Привет, я теперь слежу за игроками в этой группе!\n\n"
            "Добавляйте игроков: /register ник"
        )


async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)

    if not context.args:
        await update.message.reply_text("⚠ Укажи никнейм: /register <ник>")
        return

    nickname = context.args[0]

    player_id, elo = await get_player_data(nickname)
    if not player_id:
        await update.message.reply_text("🚫 Игрок не найден или нет CS2 в профиле.")
        return

    await add_or_update_player(chat_id, nickname, player_id, elo)
    await update.message.reply_text(f"✅ {nickname} добавлен в список. 🎯 {elo} ELO")


async def unregister(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❗ Используй: /unregister nickname")
        return
    nickname = context.args[0]
    chat_id = str(update.effective_chat.id)

    players = await get_players(chat_id)
    if nickname not in players:
        await update.message.reply_text(f"🚫 {nickname} нет в списке.")
        return

    await remove_player(chat_id, nickname)
    await update.message.reply_text(f"🗑 {nickname} удалён из списка.")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    players = await get_players(chat_id)

    if not players:
        await update.message.reply_text("📭 Список пуст.")
        return

    msg_lines = ["\n📊 Статистика игроков\n"]

    async with aiohttp.ClientSession() as session:
        tasks = []
        players_list = list(players.items())

        for nickname, pdata in players_list:
            tasks.append(get_current_elo(session, pdata["id"]))

        elos = await asyncio.gather(*tasks)

    players_with_elo = []
    for (nickname, pdata), current_elo in zip(players_list, elos):
        if current_elo is None:
            continue
        players_with_elo.append((nickname, current_elo, pdata["elo"]))

    players_with_elo.sort(key=lambda x: x[1], reverse=True)

    for i, (nickname, current_elo, prev_elo) in enumerate(players_with_elo, start=1):
        change = current_elo - prev_elo
        sign = "+" if change > 0 else ""
        msg_lines.append(f"{i}. {nickname} — {current_elo} ELO ({sign}{change})")
        await add_or_update_player(chat_id, nickname, players[nickname]["id"], current_elo)

    await update.message.reply_text("\n".join(msg_lines))


# ==== АВТО-ОБНОВЛЕНИЕ ELO ====
async def check_elo_changes(app):
    while True:
        chats = await get_all_chats()
        changed_chats = {}

        async with aiohttp.ClientSession() as session:
            tasks = []
            player_map = []

            for chat_id in chats:
                players = await get_players(chat_id)
                for nickname, pdata in players.items():
                    tasks.append(get_current_elo(session, pdata["id"]))
                    player_map.append((chat_id, nickname, pdata["id"], pdata["elo"]))

            results = await asyncio.gather(*tasks)

        for (chat_id, nickname, player_id, prev_elo), elo in zip(player_map, results):
            if elo is not None and elo != prev_elo:
                if chat_id not in changed_chats:
                    changed_chats[chat_id] = []
                changed_chats[chat_id].append((nickname, elo, elo - prev_elo))
                await add_or_update_player(chat_id, nickname, player_id, elo)

        for chat_id, changes in changed_chats.items():
            msg_lines = ["📊 Обновление ELO:\n"]
            for i, (nickname, elo, change) in enumerate(sorted(changes, key=lambda x: x[1], reverse=True), start=1):
                sign = "+" if change > 0 else ""
                msg_lines.append(f"{i}. {nickname} — {elo} ({sign}{change})")
            try:
                await app.bot.send_message(chat_id=int(chat_id), text="\n".join(msg_lines))
            except Exception:
                pass

        await asyncio.sleep(60)  # Проверка каждую минуту


# ==== ЗАПУСК ====
async def main():
    await create_pool()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("register", register))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("unregister", unregister))

    asyncio.create_task(check_elo_changes(app))
    await app.run_polling()


if __name__ == "__main__":
    nest_asyncio.apply()
    asyncio.run(main())
