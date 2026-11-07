
import os
import time
import sqlite3
import logging
import threading
import requests

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, jsonify

# ============================================================
# НАСТРОЙКИ
# ============================================================

TELEGRAM_TOKEN ="8660833104:AAFNfu027VL0zymtPLOdSX-oz7A4zs3ol2E"

PARENT_CHAT_ID = "690711648"

# ID сына добавим позднее через настройки Render
SON_CHAT_ID = "1646548357"

TIMEZONE = ZoneInfo("Europe/Kyiv")

DB_FILE = os.getenv("DB_FILE", "alarm_base.db")
PORT = int(os.getenv("PORT", "10000"))

REPEAT_SECONDS = 300
CHECK_SECONDS = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)

app = Flask(__name__)

API_URL = (
    f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
    if TELEGRAM_TOKEN else ""
)

db_lock = threading.Lock()
poll_offset = 0


# ============================================================
# БАЗА ДАННЫХ
# ============================================================

def get_db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_lock:
        conn = get_db()

        conn.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT UNIQUE NOT NULL,
                message TEXT NOT NULL,
                created_at REAL NOT NULL,
                last_sent_at REAL NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1
            )
        """)

        conn.commit()
        conn.close()


# ============================================================
# TELEGRAM API
# ============================================================

def telegram_request(method, data=None):
    if not API_URL:
        logging.error("Не задан TELEGRAM_TOKEN")
        return None

    try:
        response = requests.post(
            f"{API_URL}/{method}",
            json=data or {},
            timeout=35
        )

        result = response.json()

        if not result.get("ok"):
            logging.error("Telegram API: %s", result)

        return result

    except Exception as error:
        logging.error("Ошибка Telegram: %s", error)
        return None


def send_message(chat_id, message, keyboard=None):
    if not chat_id:
        return False

    data = {
        "chat_id": chat_id,
        "text": message
    }

    if keyboard:
        data["reply_markup"] = keyboard

    result = telegram_request("sendMessage", data)

    return bool(result and result.get("ok"))


def send_reminder(reminder):
    reminder_id = reminder["id"]

    message = reminder["message"]

    keyboard = {
        "inline_keyboard": [[
            {
                "text": "Остановить напоминание",
                "callback_data": f"stop:{reminder_id}"
            }
        ]]
    }

    # Отправляем сыну
    son_sent = send_message(SON_CHAT_ID, message)

    if not son_sent:
        logging.warning(
            "Не удалось отправить напоминание сыну"
        )
        return

    # Отправляем родителю
    send_message(
        PARENT_CHAT_ID,
        "Напоминание отправлено:\n" + message,
        keyboard
    )

    # Запоминаем время последней отправки
    with db_lock:
        conn = get_db()

        conn.execute(
            """
            UPDATE reminders
            SET last_sent_at = ?
            WHERE id = ? AND active = 1
            """,
            (time.time(), reminder_id)
        )

        conn.commit()
        conn.close()


# ============================================================
# РАСПИСАНИЕ
# ============================================================

def get_schedule(now):
    schedule = []

    # Посуда ежедневно
    schedule.append((17, 0, "Пора помыть посуду!"))
    schedule.append((20, 0, "Пора помыть посуду!"))

    # Сон: понедельник, вторник, среда,
    # четверг и воскресенье
    if now.weekday() in (0, 1, 2, 3, 6):
        schedule.append((22, 30, "Пора ложиться спать!"))

    return schedule


def create_scheduled_reminders():
    now = datetime.now(TIMEZONE)

    if not SON_CHAT_ID:
        return

    for hour, minute, message in get_schedule(now):

        scheduled = now.replace(
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0
        )

        # Напоминание можно создать в течение
        # 10 минут после назначенного времени
        difference = (now - scheduled).total_seconds()

        if difference < 0 or difference > 600:
            continue

        event_key = (
            f"{now.date()}_{hour:02d}_{minute:02d}"
        )

        with db_lock:
            conn = get_db()

            conn.execute(
                """
                INSERT OR IGNORE INTO reminders
                (event_key, message, created_at)
                VALUES (?, ?, ?)
                """,
                (event_key, message, time.time())
            )

            conn.commit()
            conn.close()


# ============================================================
# ПОВТОРНЫЕ НАПОМИНАНИЯ
# ============================================================

def process_reminders():
    with db_lock:
        conn = get_db()

        reminders = conn.execute(
            """
            SELECT *
            FROM reminders
            WHERE active = 1
            """
        ).fetchall()

        conn.close()

    current_time = time.time()

    for reminder in reminders:

        last_sent = reminder["last_sent_at"]

        if last_sent == 0 or (
            current_time - last_sent >= REPEAT_SECONDS
        ):
            send_reminder(reminder)


def scheduler_loop():
    logging.info("Планировщик запущен")

    while True:
        try:
            create_scheduled_reminders()
            process_reminders()

        except Exception:
            logging.exception("Ошибка планировщика")

        time.sleep(CHECK_SECONDS)


# ============================================================
# ОСТАНОВКА НАПОМИНАНИЙ
# ============================================================

def stop_reminder(reminder_id):
    with db_lock:
        conn = get_db()

        cursor = conn.execute(
            """
            UPDATE reminders
            SET active = 0
            WHERE id = ? AND active = 1
            """,
            (reminder_id,)
        )

        conn.commit()
        stopped = cursor.rowcount > 0
        conn.close()

    return stopped


def stop_all_reminders():
    with db_lock:
        conn = get_db()

        conn.execute(
            """
            UPDATE reminders
            SET active = 0
            WHERE active = 1
            """
        )

        conn.commit()
        conn.close()


def get_active_reminders():
    with db_lock:
        conn = get_db()

        rows = conn.execute(
            """
            SELECT id, message
            FROM reminders
            WHERE active = 1
            """
        ).fetchall()

        conn.close()

    return rows


# ============================================================
# ОБРАБОТКА СООБЩЕНИЙ TELEGRAM
# ============================================================

def process_update(update):

    if "callback_query" in update:

        callback = update["callback_query"]

        chat_id = callback["message"]["chat"]["id"]
        data = callback.get("data", "")

        telegram_request(
            "answerCallbackQuery",
            {"callback_query_id": callback["id"]}
        )

        if chat_id != PARENT_CHAT_ID:
            return

        if data.startswith("stop:"):
            try:
                reminder_id = int(data.split(":")[1])

                if stop_reminder(reminder_id):
                    send_message(
                        PARENT_CHAT_ID,
                        "Напоминание остановлено."
                    )
                else:
                    send_message(
                        PARENT_CHAT_ID,
                        "Это напоминание уже остановлено."
                    )

            except (ValueError, IndexError):
                logging.warning("Некорректная кнопка остановки")

        return

    message = update.get("message")

    if not message:
        return

    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()

    if text == "/start" or text == "/id":
        send_message(
            chat_id,
            f"Твой Telegram Chat ID: {chat_id}\n"
            "Бот работает."
        )
        return

    # Команды управления доступны только родителю
    if chat_id != PARENT_CHAT_ID:
        return

    if text == "/stop":
        stop_all_reminders()

        send_message(
            PARENT_CHAT_ID,
            "Все активные напоминания остановлены."
        )

    elif text == "/status":

        reminders = get_active_reminders()

        if not reminders:
            send_message(
                PARENT_CHAT_ID,
                "Сейчас активных напоминаний нет."
            )
            return

        result = ["Активные напоминания:"]

        for item in reminders:
            result.append(
                f'#{item["id"]}: {item["message"]}'
            )

        send_message(
            PARENT_CHAT_ID,
            "\n".join(result)
        )


# ============================================================
# TELEGRAM POLLING
# ============================================================

def telegram_polling():

    global poll_offset

    if not TELEGRAM_TOKEN:
        logging.error(
            "TELEGRAM_TOKEN не задан. "
            "Polling не запущен."
        )
        return

    # Отключаем webhook для работы через getUpdates
    telegram_request(
        "deleteWebhook",
        {"drop_pending_updates": False}
    )

    logging.info("Telegram polling запущен")

    while True:
        try:
            result = telegram_request(
                "getUpdates",
                {
                    "offset": poll_offset,
                    "timeout": 25,
                    "allowed_updates": [
                        "message",
                        "callback_query"
                    ]
                }
            )

            if not result or not result.get("ok"):
                time.sleep(5)
                continue

            for update in result.get("result", []):

                poll_offset = update["update_id"] + 1

                try:
                    process_update(update)

                except Exception:
                    logging.exception(
                        "Ошибка обработки сообщения"
                    )

        except Exception:
            logging.exception("Ошибка Telegram polling")
            time.sleep(5)


# ============================================================
# RENDER HEALTH CHECK
# ============================================================

@app.route("/")
def home():
    return jsonify({
        "status": "online",
        "bot": "Alarm Bot"
    })


@app.route("/health")
def health():
    return jsonify({"status": "healthy"})


# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":

    init_db()

    threading.Thread(
        target=scheduler_loop,
        daemon=True
    ).start()

    threading.Thread(
        target=telegram_polling,
        daemon=True
    ).start()

    logging.info("Alarm Bot запускается")

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False
    )
