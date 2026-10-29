# Telegram → ntfy Bridge

Get ntfy notifications for unread personal Telegram messages, then inspect unread
chats, reply or mark messages read using TOTP-authenticated commands.
This is a Telethon **user-account client**, not a BotFather bot.

## Setup

Python 3.10+:

```sh
python -m venv .venv
# Activate the virtual environment, then:
python -m pip install -r requirements.txt
cp .env.example .env
python -c "import pyotp; print(pyotp.random_base32())"
```

Put the generated secret in your private `.env` and add the same secret to your
authenticator app. Obtain `TG_API_ID` and `TG_API_HASH` at https://my.telegram.org.
Configure your ntfy URL, private topics and optional bearer token. Subscribe to the
notification topic in your ntfy app. Run `python notifier.py` interactively once to
complete Telegram login; keep `data/` private. The same command then runs the bridge.

## Behavior

- Incoming personal messages from users schedule a notification after 20 seconds.
- A Telegram read event cancels a pending notification for that message.
- Contacts receive stable local five-digit aliases stored in SQLite.
- The command listener deduplicates ntfy event IDs and reconnects after failures.
- Notification and command topics may be separate; `NTFY_COMMAND_TOPIC` defaults
  to the notification topic when not set.

Publish these messages to the command topic (`CODE` is your current six-digit TOTP):

```text
/help
/unread CODE
/unread ID CODE
/chat ID CODE
/read ID CODE
/reply ID CODE Your reply text
```

`/help` only describes the commands; reading and sending require TOTP.
The initial push describes the sender. Requested chat/unread responses and replies
can contain message text, so use an access-controlled ntfy server/topics and HTTPS.
TOTP protects command execution; it does not encrypt notifications or replace ntfy topic permissions.
The configured validity window accepts the adjacent 30-second time windows.

## Offline checks

```sh
python -m unittest discover -s tests -v
```

Tests use temporary sessions/databases and mocked sends; they do not log into Telegram.
This first public version has not been revalidated against a live Telegram/ntfy account.

Русский: уведомления о непрочитанных личных сообщениях Telegram через ntfy,
с командами чтения и ответа, защищёнными TOTP.
