import asyncio
import json
import logging
import random
import sqlite3
import time

import httpx
import pyotp

from telethon import TelegramClient, events
from telethon.tl.types import User

from config import (
    API_ID,
    API_HASH,
    NTFY_URL,
    NTFY_TOPIC,
    TOTP_SECRET,
    DB_PATH, SESSION_PATH, NOTIFICATION_DELAY, NTFY_COMMAND_TOPIC, NTFY_TOKEN,
)


# ============================================================
# НАСТРОЙКИ
# ============================================================

# DB_PATH is supplied by config.py.

# Если сообщение прочитано за это время,
# ntfy-уведомление не отправляем
# NOTIFICATION_DELAY is supplied by config.py.

# Количество сообщений для /chat
CHAT_MESSAGE_LIMIT = 10

# Максимум непрочитанных сообщений на диалог для /unread
UNREAD_MESSAGE_LIMIT = 20


# ============================================================
# ЛОГИ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)


# ============================================================
# TELEGRAM
# ============================================================

client = TelegramClient(
    SESSION_PATH,
    API_ID,
    API_HASH
)


# ============================================================
# GOOGLE AUTHENTICATOR / TOTP
# ============================================================

totp = pyotp.TOTP(
    TOTP_SECRET,
    interval=30
)


def check_totp(code):
    try:
        code = str(code).strip()

        if not code.isdigit():
            return False

        if len(code) != 6:
            return False

        # Текущий код + соседнее временное окно.
        # Это защищает от небольшой рассинхронизации часов.
        return totp.verify(
            code,
            valid_window=1
        )

    except Exception:
        return False


# ============================================================
# DATABASE
# ============================================================

def db_connect():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = db_connect()

    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS contacts (
                telegram_id INTEGER PRIMARY KEY,
                short_id INTEGER UNIQUE NOT NULL,
                name TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_commands (
                event_id TEXT PRIMARY KEY,
                processed_at INTEGER NOT NULL
            )
            """
        )

        conn.commit()

    finally:
        conn.close()


def generate_short_id(conn):
    while True:
        short_id = random.randint(10000, 99999)

        row = conn.execute(
            """
            SELECT 1
            FROM contacts
            WHERE short_id = ?
            """,
            (short_id,)
        ).fetchone()

        if row is None:
            return short_id


def get_or_create_short_id(telegram_id, name):
    conn = db_connect()

    try:
        row = conn.execute(
            """
            SELECT short_id
            FROM contacts
            WHERE telegram_id = ?
            """,
            (telegram_id,)
        ).fetchone()

        if row:
            short_id = row[0]

            # Если человек изменил имя — обновляем его в базе,
            # но 5-значный ID остаётся прежним.
            conn.execute(
                """
                UPDATE contacts
                SET name = ?
                WHERE telegram_id = ?
                """,
                (
                    name,
                    telegram_id
                )
            )

            conn.commit()

            return short_id

        short_id = generate_short_id(conn)

        conn.execute(
            """
            INSERT INTO contacts (
                telegram_id,
                short_id,
                name
            )
            VALUES (?, ?, ?)
            """,
            (
                telegram_id,
                short_id,
                name
            )
        )

        conn.commit()

        logging.info(
            "Assigned short ID %s to %s (%s)",
            short_id,
            name,
            telegram_id
        )

        return short_id

    finally:
        conn.close()


def get_contact_by_short_id(short_id):
    conn = db_connect()

    try:
        row = conn.execute(
            """
            SELECT telegram_id, name
            FROM contacts
            WHERE short_id = ?
            """,
            (short_id,)
        ).fetchone()

        if row is None:
            return None

        return {
            "telegram_id": row[0],
            "name": row[1],
        }

    finally:
        conn.close()


def command_already_processed(event_id):
    if not event_id:
        return False

    conn = db_connect()

    try:
        row = conn.execute(
            """
            SELECT 1
            FROM processed_commands
            WHERE event_id = ?
            """,
            (event_id,)
        ).fetchone()

        return row is not None

    finally:
        conn.close()


def mark_command_processed(event_id):
    if not event_id:
        return

    conn = db_connect()

    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO processed_commands (
                event_id,
                processed_at
            )
            VALUES (?, ?)
            """,
            (
                event_id,
                int(time.time())
            )
        )

        # Удаляем историю обработанных ntfy-команд старше 7 дней
        cutoff = int(time.time()) - (7 * 24 * 60 * 60)

        conn.execute(
            """
            DELETE FROM processed_commands
            WHERE processed_at < ?
            """,
            (cutoff,)
        )

        conn.commit()

    finally:
        conn.close()


# ============================================================
# ИМЯ TELEGRAM-ПОЛЬЗОВАТЕЛЯ
# ============================================================

def get_sender_name(sender):
    parts = [
        sender.first_name or "",
        sender.last_name or "",
    ]

    name = " ".join(
        part for part in parts if part
    ).strip()

    if name:
        return name

    if sender.username:
        return f"@{sender.username}"

    return "Без имени"


# ============================================================
# NTFY
# ============================================================

async def publish_ntfy(
    text,
    title="Telegram",
    priority="high"
):
    url = f"{NTFY_URL}/{NTFY_TOPIC}"

    async with httpx.AsyncClient(
        timeout=15
    ) as http:

        response = await http.post(
            url,
            content=text.encode("utf-8"),
            headers={
                # HTTP-заголовки оставляем ASCII,
                # чтобы не получить UnicodeEncodeError
                "Title": title,
                "Priority": priority,
                "Cache": "no",
                **({"Authorization": f"Bearer {NTFY_TOKEN}"} if NTFY_TOKEN else {}),
            },
        )

        response.raise_for_status()


async def send_new_message_notification(
    sender_name,
    short_id
):
    text = (
        f"[{short_id}] "
        f"{sender_name} написал вам в Telegram"
    )

    await publish_ntfy(
        text,
        title="Telegram"
    )


async def send_command_response(text):
    await publish_ntfy(
        text,
        title="Telegram command"
    )


# ============================================================
# ОТЛОЖЕННЫЕ УВЕДОМЛЕНИЯ
# ============================================================

read_messages = {}
pending_notifications = {}


async def delayed_notification(
    chat_id,
    message_id,
    sender_name,
    short_id
):
    try:
        await asyncio.sleep(
            NOTIFICATION_DELAY
        )

        last_read_id = read_messages.get(
            chat_id,
            0
        )

        if message_id <= last_read_id:
            logging.info(
                "Message %s from [%s] %s already read, skipped",
                message_id,
                short_id,
                sender_name
            )

            return

        await send_new_message_notification(
            sender_name,
            short_id
        )

        logging.info(
            "ntfy notification sent: [%s] %s",
            short_id,
            sender_name
        )

    except asyncio.CancelledError:
        logging.info(
            "Notification cancelled for message %s",
            message_id
        )

    except Exception:
        logging.exception(
            "Failed to send ntfy notification"
        )

    finally:
        pending_notifications.pop(
            (
                chat_id,
                message_id
            ),
            None
        )


# ============================================================
# TELEGRAM: ПРОЧТЕНИЕ СООБЩЕНИЙ
# ============================================================

@client.on(
    events.MessageRead(
        inbox=True
    )
)
async def message_read(event):

    chat_id = event.chat_id

    if chat_id is None:
        return

    max_read_id = event.max_id

    previous = read_messages.get(
        chat_id,
        0
    )

    if max_read_id > previous:
        read_messages[
            chat_id
        ] = max_read_id

    logging.info(
        "Messages read in chat %s up to %s",
        chat_id,
        max_read_id
    )

    # Отменяем ожидающие push для уже прочитанных сообщений
    for (
        pending_chat_id,
        message_id
    ), task in list(
        pending_notifications.items()
    ):

        if (
            pending_chat_id == chat_id
            and message_id <= max_read_id
        ):
            task.cancel()


# ============================================================
# TELEGRAM: НОВОЕ ЛИЧНОЕ СООБЩЕНИЕ
# ============================================================

@client.on(
    events.NewMessage(
        incoming=True
    )
)
async def new_message(event):

    # Только личные сообщения
    if not event.is_private:
        return

    sender = await event.get_sender()

    # Только Telegram-пользователи
    if not isinstance(
        sender,
        User
    ):
        return

    # Telegram-ботов игнорируем
    if sender.bot:
        return

    sender_name = get_sender_name(
        sender
    )

    short_id = get_or_create_short_id(
        sender.id,
        sender_name
    )

    chat_id = event.chat_id
    message_id = event.id

    logging.info(
        "Private message from [%s] %s (%s), message_id=%s",
        short_id,
        sender_name,
        sender.id,
        message_id
    )

    task = asyncio.create_task(
        delayed_notification(
            chat_id,
            message_id,
            sender_name,
            short_id
        )
    )

    pending_notifications[
        (
            chat_id,
            message_id
        )
    ] = task


# ============================================================
# /HELP
# ============================================================

async def command_help():

    text = (
        "Telegram Remote - команды\n\n"

        "/help\n"
        "Показать список команд\n\n"

        "/unread CODE\n"
        "Все непрочитанные личные сообщения\n\n"

        "/unread ID CODE\n"
        "Непрочитанные сообщения конкретного человека\n\n"

        "/chat ID CODE\n"
        "Последние 10 сообщений диалога\n\n"

        "/r ID CODE ТЕКСТ\n"
        "Ответить человеку\n\n"

        "/read ID CODE\n"
        "Отметить сообщения прочитанными\n\n"

        "ID = постоянный 5-значный номер собеседника\n"
        "CODE = код Google Authenticator\n\n"

        "Пример:\n"
        "/r 38142 123456 Буду через 20 минут"
    )

    await send_command_response(
        text
    )


# ============================================================
# /UNREAD
# ============================================================

async def command_unread(short_id=None):
    dialogs = await client.get_dialogs()

    results = []

    for dialog in dialogs:
        entity = dialog.entity

        # Только личные диалоги
        if not isinstance(entity, User):
            continue

        if entity.bot:
            continue

        unread_count = dialog.unread_count

        if unread_count <= 0:
            continue

        sender_name = get_sender_name(entity)

        contact_short_id = get_or_create_short_id(
            entity.id,
            sender_name
        )

        if (
            short_id is not None
            and contact_short_id != short_id
        ):
            continue

        # Берём последние N сообщений,
        # где N = количество непрочитанных
        limit = min(
            unread_count,
            UNREAD_MESSAGE_LIMIT
        )

        messages = []

        async for message in client.iter_messages(
            entity,
            limit=limit
        ):
            # Наши исходящие сообщения пропускаем
            if message.out:
                continue

            text = message.message

            if not text:
                text = "[медиа/вложение]"

            text = text.replace(
                "\n",
                " "
            ).strip()

            messages.append(text)

        messages.reverse()

        results.append(
            {
                "short_id": contact_short_id,
                "name": sender_name,
                "count": unread_count,
                "messages": messages,
            }
        )

    if not results:
        if short_id is not None:
            await send_command_response(
                f"[{short_id}] Непрочитанных сообщений нет."
            )
        else:
            await send_command_response(
                "Непрочитанных личных сообщений нет."
            )

        return

    parts = []

    for item in results:
        parts.append(
            f"[{item['short_id']}] "
            f"{item['name']} "
            f"({item['count']} непрочит.)"
        )

        for message in item["messages"]:
            parts.append(
                f"> {message}"
            )

        parts.append("")

    text = "\n".join(parts)

    if len(text) > 3500:
        text = (
            text[:3500]
            + "\n\n[сообщение сокращено]"
        )

    await send_command_response(text)


# ============================================================
# /CHAT
# ============================================================

async def command_chat(short_id):

    contact = get_contact_by_short_id(
        short_id
    )

    if not contact:
        await send_command_response(
            f"ID [{short_id}] не найден."
        )

        return

    telegram_id = contact[
        "telegram_id"
    ]

    try:
        entity = await client.get_entity(
            telegram_id
        )

    except Exception:
        logging.exception(
            "Failed to get Telegram entity"
        )

        await send_command_response(
            f"Не удалось открыть [{short_id}]."
        )

        return

    sender_name = get_sender_name(
        entity
    )

    messages = []

    async for message in client.iter_messages(
        entity,
        limit=CHAT_MESSAGE_LIMIT
    ):

        text = message.message

        if not text:
            text = "[медиа/вложение]"

        text = text.replace(
            "\n",
            " "
        ).strip()

        if message.out:
            prefix = "Вы"
        else:
            prefix = sender_name

        messages.append(
            f"{prefix}: {text}"
        )

    messages.reverse()

    text = (
        f"[{short_id}] {sender_name}\n\n"
        + "\n".join(messages)
    )

    if len(text) > 3500:
        text = (
            text[:3500]
            + "\n\n[сообщение сокращено]"
        )

    await send_command_response(
        text
    )


# ============================================================
# /R И /REPLY
# ============================================================

async def command_reply(
    short_id,
    text
):

    contact = get_contact_by_short_id(
        short_id
    )

    if not contact:
        await send_command_response(
            f"ID [{short_id}] не найден."
        )

        return

    if not text.strip():
        await send_command_response(
            "Нельзя отправить пустое сообщение."
        )

        return

    telegram_id = contact[
        "telegram_id"
    ]

    try:
        entity = await client.get_entity(
            telegram_id
        )

        await client.send_message(
            entity,
            text
        )

        sender_name = get_sender_name(
            entity
        )

        await send_command_response(
            f"Отправлено [{short_id}] {sender_name}:\n{text}"
        )

        logging.info(
            "Reply sent to [%s] %s",
            short_id,
            sender_name
        )

    except Exception:
        logging.exception(
            "Failed to send Telegram message"
        )

        await send_command_response(
            f"Ошибка отправки сообщения [{short_id}]."
        )


# ============================================================
# /READ
# ============================================================

async def command_read(short_id):

    contact = get_contact_by_short_id(
        short_id
    )

    if not contact:
        await send_command_response(
            f"ID [{short_id}] не найден."
        )

        return

    telegram_id = contact[
        "telegram_id"
    ]

    try:
        entity = await client.get_entity(
            telegram_id
        )

        await client.send_read_acknowledge(
            entity
        )

        sender_name = get_sender_name(
            entity
        )

        await send_command_response(
            f"[{short_id}] {sender_name}: отмечено прочитанным."
        )

    except Exception:
        logging.exception(
            "Failed to mark messages as read"
        )

        await send_command_response(
            f"Ошибка /read для [{short_id}]."
        )


# ============================================================
# ОБРАБОТКА КОМАНД
# ============================================================

async def process_command(message):

    message = message.strip()

    # В этот же topic сервер публикует свои ответы.
    # Всё, что не начинается с "/", игнорируем.
    if not message.startswith("/"):
        return

    parts = message.split()

    command = parts[0].lower()


    # --------------------------------------------------------
    # /help
    # Без TOTP
    # --------------------------------------------------------

    if (
        command == "/help"
        and len(parts) == 1
    ):
        await command_help()
        return


    # --------------------------------------------------------
    # /unread CODE
    # --------------------------------------------------------

    if (
        command == "/unread"
        and len(parts) == 2
    ):

        code = parts[1]

        if not check_totp(code):
            logging.warning(
                "Invalid TOTP for /unread"
            )
            return

        await command_unread()
        return


    # --------------------------------------------------------
    # /unread ID CODE
    # --------------------------------------------------------

    if (
        command == "/unread"
        and len(parts) == 3
    ):

        try:
            short_id = int(
                parts[1]
            )

        except ValueError:
            return

        code = parts[2]

        if not check_totp(code):
            logging.warning(
                "Invalid TOTP for /unread ID"
            )
            return

        await command_unread(
            short_id
        )

        return


    # --------------------------------------------------------
    # /chat ID CODE
    # --------------------------------------------------------

    if (
        command == "/chat"
        and len(parts) == 3
    ):

        try:
            short_id = int(
                parts[1]
            )

        except ValueError:
            return

        code = parts[2]

        if not check_totp(code):
            logging.warning(
                "Invalid TOTP for /chat"
            )
            return

        await command_chat(
            short_id
        )

        return


    # --------------------------------------------------------
    # /read ID CODE
    # --------------------------------------------------------

    if (
        command == "/read"
        and len(parts) == 3
    ):

        try:
            short_id = int(
                parts[1]
            )

        except ValueError:
            return

        code = parts[2]

        if not check_totp(code):
            logging.warning(
                "Invalid TOTP for /read"
            )
            return

        await command_read(
            short_id
        )

        return


    # --------------------------------------------------------
    # /r ID CODE ТЕКСТ
    # /reply ID CODE ТЕКСТ
    # --------------------------------------------------------

    if (
        command in (
            "/r",
            "/reply"
        )
        and len(parts) >= 4
    ):

        try:
            short_id = int(
                parts[1]
            )

        except ValueError:
            return

        code = parts[2]

        if not check_totp(code):
            logging.warning(
                "Invalid TOTP for /reply"
            )
            return

        text = " ".join(
            parts[3:]
        )

        await command_reply(
            short_id,
            text
        )

        return


    # Если команда существует, но написана неправильно
    logging.warning(
        "Unknown or malformed command: %s",
        command
    )


# ============================================================
# NTFY LISTENER
# ============================================================

async def ntfy_command_listener():

    # Используем ОДИН И ТОТ ЖЕ topic
    # и для уведомлений, и для команд
    url = (
        f"{NTFY_URL}/"
        f"{NTFY_COMMAND_TOPIC}/json"
    )

    while True:

        try:
            logging.info(
                "Connecting to ntfy topic listener..."
            )

            timeout = httpx.Timeout(
                connect=15,
                read=None,
                write=15,
                pool=15
            )

            async with httpx.AsyncClient(
                timeout=timeout
            ) as http:

                async with http.stream(
                    "GET",
                    url,
                    params={
                        "since": "10s"
                    },
                    headers=({"Authorization": f"Bearer {NTFY_TOKEN}"} if NTFY_TOKEN else {})
                ) as response:

                    response.raise_for_status()

                    logging.info(
                        "ntfy topic listener connected"
                    )

                    async for line in response.aiter_lines():

                        if not line:
                            continue

                        try:
                            data = json.loads(
                                line
                            )

                        except json.JSONDecodeError:
                            continue

                        if data.get(
                            "event"
                        ) != "message":
                            continue

                        event_id = data.get(
                            "id"
                        )

                        if command_already_processed(
                            event_id
                        ):
                            continue

                        message = data.get(
                            "message",
                            ""
                        ).strip()

                        # Сервер видит и собственные ntfy-ответы.
                        # Они не начинаются с "/", поэтому игнорируются.
                        if not message.startswith("/"):
                            continue

                        logging.info(
                            "Command received: %s",
                            message.split()[0]
                        )

                        mark_command_processed(
                            event_id
                        )

                        try:
                            await process_command(
                                message
                            )

                        except Exception:
                            logging.exception(
                                "Command processing failed"
                            )

        except asyncio.CancelledError:
            raise

        except Exception:
            logging.exception(
                "ntfy listener disconnected"
            )

            await asyncio.sleep(5)


# ============================================================
# MAIN
# ============================================================

async def main():

    init_db()

    await client.start()

    me = await client.get_me()

    logging.info(
        "Telegram notifier started for %s (%s)",
        me.first_name,
        me.id
    )

    logging.info(
        "Notification delay: %s seconds",
        NOTIFICATION_DELAY
    )

    logging.info(
        "ntfy notification and command topics configured"
    )

    asyncio.create_task(
        ntfy_command_listener()
    )

    await client.run_until_disconnected()


if __name__ == "__main__":
    with client:
        client.loop.run_until_complete(main())
