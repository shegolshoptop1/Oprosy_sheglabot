import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
import sqlite3
from datetime import datetime, timedelta, timezone
import requests
import json
from dateutil import parser
import logging
import threading

# Special pricing for admin (ID: 5734928133)
id_prices = True  # True: 0.01$ for admin, False: normal prices for admin

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

TOKEN = "7926617948:AAHu39MKRzRTB961g-b-XR_aJfEX5fvSs_E"
CRYPTO_PAY_TOKEN = "419832:AADuP4jq9MZszE6rBhrA0F4t61PNsLKGh9x"
CRYPTO_PAY_API_URL = "https://pay.crypt.bot/api/"

bot = telebot.TeleBot(TOKEN)

# Список админов
ADMIN_IDS = ["5734928133", "1553172844"]
reviewer_id = "5734928133"  # только для обратной совместимости, не используйте как список!
channel_id = -1002462929348

user_states = {}
poll_data = {}
pending = []
delayed_polls = {}  # {user_id: {"data": poll_data, "submit_time": datetime, "message_id": int}}

def init_db():
    try:
        conn = sqlite3.connect('bot_data.db')
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            subscription_status TEXT DEFAULT 'none',
            subscription_end TEXT,
            free_requests INTEGER DEFAULT 3,
            last_reset TEXT,
            unlimited_requests TEXT DEFAULT 'no'
        )''')
        # Add unlimited_requests column if it doesn't exist
        c.execute("PRAGMA table_info(users)")
        columns = [col[1] for col in c.fetchall()]
        if 'unlimited_requests' not in columns:
            c.execute("ALTER TABLE users ADD COLUMN unlimited_requests TEXT DEFAULT 'no'")
        c.execute('''CREATE TABLE IF NOT EXISTS payments (
            invoice_id TEXT PRIMARY KEY,
            user_id INTEGER,
            amount REAL,
            duration TEXT,
            status TEXT DEFAULT 'pending'
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS banned_users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT
        )''')
        c.execute("UPDATE banned_users SET first_name = 'Unknown' WHERE first_name IS NULL")
        conn.commit()
        logging.info("Database initialized successfully")
    except Exception as e:
        logging.error(f"Error initializing database: {e}")
    finally:
        conn.close()

init_db()

def set_bot_commands():
    commands = [
        BotCommand("start", "🐦 Запустить бота"),
        BotCommand("new", "📝 Предложить опрос"),
        BotCommand("profile", "👤 Ваш профиль"),
        BotCommand("subscription", "💳 Управление подпиской")
    ]
    bot.set_my_commands(commands=commands)

set_bot_commands()

def is_user_banned(user_id):
    user_id = str(user_id)
    try:
        conn = sqlite3.connect('bot_data.db')
        c = conn.cursor()
        c.execute("SELECT user_id FROM banned_users WHERE user_id = ?", (user_id,))
        banned = c.fetchone() is not None
        conn.close()
        return banned
    except Exception as e:
        logging.error(f"Error checking ban status for user {user_id}: {e}")
        return False

def get_user_data(user_id):
    user_id = str(user_id)
    try:
        conn = sqlite3.connect('bot_data.db')
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        user = c.fetchone()
        conn.close()
        return user
    except Exception as e:
        logging.error(f"Error fetching user data for user {user_id}: {e}")
        return None

def update_user_data(user_id, first_name, subscription_status=None, subscription_end=None, free_requests=None, last_reset=None, unlimited_requests=None):
    user_id = str(user_id)
    try:
        conn = sqlite3.connect('bot_data.db')
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO users (user_id, first_name, subscription_status, free_requests, last_reset, unlimited_requests) VALUES (?, ?, 'none', 3, NULL, 'no')", (user_id, first_name))
        updates = {}
        if subscription_status:
            updates['subscription_status'] = subscription_status
        if subscription_end is not None:
            updates['subscription_end'] = subscription_end
        if free_requests is not None:
            updates['free_requests'] = free_requests
        if last_reset:
            updates['last_reset'] = last_reset
        if unlimited_requests:
            updates['unlimited_requests'] = unlimited_requests
        if updates:
            set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
            c.execute(f"UPDATE users SET {set_clause} WHERE user_id = ?", (*updates.values(), user_id))
        conn.commit()
    except Exception as e:
        logging.error(f"Error updating user data for user {user_id}: {e}")
    finally:
        conn.close()

def check_subscription(user_id):
    user_id = str(user_id)
    user = get_user_data(user_id)
    if not user:
        return False, 0
    subscription_status = user[2] if len(user) > 2 else 'none'
    if subscription_status == 'none':
        return False, 0
    now = datetime.now(timezone.utc)
    try:
        sub_end = parser.parse(user[3]).replace(tzinfo=timezone.utc) if user[3] else now
    except (ValueError, IndexError):
        logging.error(f"Invalid subscription_end for user {user_id}: {user[3] if len(user) > 3 else 'None'}")
        return False, 0
    if subscription_status == 'permanent':
        return True, float('inf')
    if sub_end > now:
        time_left = sub_end - now
        days_left = time_left.days
        hours_left = time_left.total_seconds() // 3600
        if days_left == 0 and hours_left < 24:
            return True, f"{int(hours_left)} ч."
        if days_left == 1:
            return True, "До завтра"
        return True, days_left
    else:
        update_user_data(user_id, user[1] if len(user) > 1 else f"User{user_id}", subscription_status='none', subscription_end=None)
        return False, 0

def check_free_requests(user_id):
    user_id = str(user_id)
    user = get_user_data(user_id)
    if not user:
        return 3
    unlimited_requests = user[6] if len(user) > 6 else 'no'
    subscription_status = user[2] if len(user) > 2 else 'none'
    if unlimited_requests == 'yes' or subscription_status == 'permanent':
        return float('inf')
    now = datetime.now(timezone.utc)
    try:
        last_reset = parser.parse(user[5]).replace(tzinfo=timezone.utc) if user[5] else now - timedelta(days=1)
    except (ValueError, IndexError):
        logging.error(f"Invalid last_reset for user {user_id}: {user[5] if len(user) > 5 else 'None'}")
        last_reset = now - timedelta(days=1)
    if (now - last_reset).total_seconds() >= 86400:
        update_user_data(user_id, user[1] if len(user) > 1 else f"User{user_id}", free_requests=3, last_reset=now.isoformat())
        return 3
    return user[4] if len(user) > 4 else 3

def get_time_until_reset(user_id):
    user_id = str(user_id)
    user = get_user_data(user_id)
    if not user or not user[5] or (len(user) > 6 and user[6] == 'yes') or (len(user) > 2 and user[2] == 'permanent'):
        return "0 ч. 0 мин."
    now = datetime.now(timezone.utc)
    try:
        last_reset = parser.parse(user[5]).replace(tzinfo=timezone.utc)
    except (ValueError, IndexError):
        logging.error(f"Invalid last_reset for user {user_id}: {user[5] if len(user) > 5 else 'None'}")
        return "0 ч. 0 мин."
    seconds_until_reset = 86400 - (now - last_reset).total_seconds()
    if seconds_until_reset <= 0:
        return "0 ч. 0 мин."
    hours = int(seconds_until_reset // 3600)
    minutes = int((seconds_until_reset % 3600) // 60)
    return f"{hours} ч. {minutes} мин."

def deduct_free_request(user_id):
    user_id = str(user_id)
    user = get_user_data(user_id)
    if user and len(user) > 6 and len(user) > 4 and user[4] > 0 and user[6] != 'yes' and user[2] != 'permanent':
        update_user_data(user_id, user[1] if len(user) > 1 else f"User{user_id}", free_requests=user[4] - 1)

def create_crypto_invoice(user_id, amount, duration):
    user_id = str(user_id)
    # Теперь оба админа получают спец. цену
    if user_id in ADMIN_IDS and id_prices:
        amount = 0.01
    if duration == "donation":
        description = "Donation"
    elif duration.startswith("request_"):
        num_requests = duration.split("_")[1]
        description = f"Покупка {num_requests} {'запросов' if num_requests != 'unlimited' else 'неограниченных запросов'}"
    else:
        duration_text = {"1day": "1 день", "3days": "3 дня", "14days": "14 дней", "30days": "30 дней", "permanent": "навсегда"}[duration]
        description = f"Оплата подписки на {duration_text}"
    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN}
    payload = {
        "asset": "USDT",
        "amount": str(amount),
        "description": description,
        "paid_btn_name": "openBot",
        "paid_btn_url": f"https://t.me/{bot.get_me().username}"
    }
    try:
        response = requests.post(CRYPTO_PAY_API_URL + "createInvoice", headers=headers, json=payload)
        if response.status_code == 200:
            data = response.json()['result']
            invoice_id = data['invoice_id']
            pay_url = data['pay_url']
            conn = sqlite3.connect('bot_data.db')
            c = conn.cursor()
            c.execute("INSERT INTO payments (invoice_id, user_id, amount, duration, status) VALUES (?, ?, ?, ?, ?)",
                      (invoice_id, user_id, amount, duration, 'pending'))
            conn.commit()
            conn.close()
            return invoice_id, pay_url
        else:
            logging.error(f"Failed to create invoice: {response.text}")
            return None, None
    except Exception as e:
        logging.error(f"Error creating invoice for user {user_id}: {e}")
        return None, None

def check_payment_status(invoice_id):
    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN}
    try:
        response = requests.get(CRYPTO_PAY_API_URL + "getInvoices", headers=headers, params={"invoice_ids": invoice_id})
        if response.status_code == 200:
            data = response.json()['result']['items']
            if data and data[0]['status'] == 'paid':
                return True
        return False
    except Exception as e:
        logging.error(f"Error checking payment status for invoice {invoice_id}: {e}")
        return False

@bot.message_handler(commands=['start'])
def start(message):
    user_id = str(message.chat.id)
    first_name = message.from_user.first_name or f"User{user_id}"
    
    if is_user_banned(user_id):
        logging.info(f"Banned user {user_id} attempted /start")
        return
    
    update_user_data(user_id, first_name)
    
    is_subscribed, days_left = check_subscription(user_id)
    
    logging.info(f"User {user_id} started bot: subscribed={is_subscribed}, days_left={days_left}")
    
    if is_subscribed:
        text = "🔥 Спасибо, что купили подписку!!"
    else:
        text = ("💸 Купите подписку и вы получите: доступ к отправке бесконечных предложений, "
                "ваши опросы будут отправляться мгновенно и рассматриваться как можно скорее.")
    
    try:
        bot.send_message(chat_id=user_id, text=f"👋 Добро пожаловать!\n\nЗдесь можно предложить опрос для @oprosy_shegla\n\n{text}")
        logging.info(f"Start message sent to user {user_id}")
    except Exception as e:
        logging.error(f"Error sending start message to user {user_id}: {e}")

@bot.message_handler(commands=['profile'])
def profile(message):
    user_id = str(message.chat.id)
    first_name = message.from_user.first_name or f"User{user_id}"
    
    if is_user_banned(user_id):
        logging.info(f"Banned user {user_id} attempted /profile")
        return
    
    user = get_user_data(user_id)
    if not user:
        update_user_data(user_id, first_name)
        user = get_user_data(user_id)
    
    is_subscribed, days_left = check_subscription(user_id)
    status = "✅ Активна" if is_subscribed else "❌ Не активна"
    days_text = "∞" if days_left == float('inf') else (days_left if is_subscribed else "Нет подписки")
    
    requests_left = "∞" if is_subscribed or (len(user) > 6 and user[6] == 'yes') else check_free_requests(user_id)
    
    text = (f"👤 *Профиль*\n\n"
            f"Имя: {user[1] if len(user) > 1 else first_name}\n"
            f"Статус подписки: {status}\n"
            f"Истекает: {days_text}\n"
            f"Осталось запросов: {requests_left}")
    
    try:
        bot.send_message(chat_id=user_id, text=text, parse_mode="Markdown")
        logging.info(f"Profile sent to user {user_id}")
    except Exception as e:
        logging.error(f"Error sending profile to user {user_id}: {e}")

@bot.message_handler(commands=['subscription'])
def subscription(message):
    user_id = str(message.chat.id)
    first_name = message.from_user.first_name or f"User{user_id}"
    
    if is_user_banned(user_id):
        logging.info(f"Banned user {user_id} attempted /subscription")
        return
    
    user = get_user_data(user_id)
    if not user:
        update_user_data(user_id, first_name)
        user = get_user_data(user_id)
        if not user:
            logging.error(f"Failed to initialize user {user_id} in subscription")
            try:
                bot.send_message(chat_id=user_id, text="🚫 Ошибка при загрузке данных. Попробуйте снова.")
            except Exception as e:
                logging.error(f"Error sending error message to user {user_id}: {e}")
            return
    
    try:
        is_subscribed, days_left = check_subscription(user_id)
        status = "✅ Активна" if is_subscribed else "❌ Не активна"
        days_text = "∞" if days_left == float('inf') else (days_left if is_subscribed else "Нет подписки")
        
        price = 0.01 if user_id == str(reviewer_id) and id_prices else None
        donation_prices = [0.01, 0.40, 1.20, 2.40, 3.60] if not price else [price] * 5
        request_prices = [0.16, 0.64, 1.20, 1.84] if not price else [price] * 4
        sub_prices = [0.10, 0.40, 1.20, 2.40, 3.60] if not price else [price] * 5
        
        if (len(user) > 6 and user[6] == 'yes') or is_subscribed:
            text = (f"💳 *Подписка*\n\n"
                    f"Статус: {status}\n"
                    f"Истекает: {days_text}\n\n"
                    f"У вас бесконечный доступ, но вы можете поддержать проект:")
            markup = InlineKeyboardMarkup()
            markup.row(InlineKeyboardButton(f"{donation_prices[0]}$ 💵", callback_data="donation_0.01"))
            markup.row(InlineKeyboardButton(f"{donation_prices[1]}$ 💵 (20% скидка)", callback_data="donation_0.40"))
            markup.row(InlineKeyboardButton(f"{donation_prices[2]}$ 💵 (20% скидка)", callback_data="donation_1.20"))
            markup.row(InlineKeyboardButton(f"{donation_prices[3]}$ 💵 (20% скидка)", callback_data="donation_2.40"))
            markup.row(InlineKeyboardButton(f"{donation_prices[4]}$ 💵 (20% скидка)", callback_data="donation_3.60"))
        else:
            text = (f"💳 *Подписка*\n\n"
                    f"Статус: {status}\n"
                    f"Истекает: {days_text}\n\n"
                    f"Выберите действие:")
            markup = InlineKeyboardMarkup()
            markup.add(
                InlineKeyboardButton("Запросы", callback_data="requests_menu"),
                InlineKeyboardButton("Подписка", callback_data="subscriptions_menu")
            )
        
        bot.send_message(chat_id=user_id, text=text, reply_markup=markup, parse_mode="Markdown")
        logging.info(f"{'Donation' if (len(user) > 6 and user[6] == 'yes') or is_subscribed else 'Main'} menu sent to user {user_id}")
    except Exception as e:
        logging.error(f"Error in subscription for user {user_id}: {e}")
        try:
            bot.send_message(chat_id=user_id, text="🚫 Ошибка при загрузке меню. Попробуйте снова.")
        except Exception as e2:
            logging.error(f"Error sending error message to user {user_id}: {e2}")

@bot.message_handler(commands=['new'])
def new_poll(message):
    user_id = str(message.chat.id)
    first_name = message.from_user.first_name or f"User{user_id}"
    
    if is_user_banned(user_id):
        logging.info(f"Banned user {user_id} attempted /new")
        return
    
    if user_id in user_states or user_id in poll_data or user_id in delayed_polls:
        try:
            bot.send_message(chat_id=user_id, text="🚫 Дождитесь завершения текущего запроса.")
            logging.info(f"User {user_id} attempted new poll while one is in progress")
            return
        except Exception as e:
            logging.error(f"Error sending in-progress message to user {user_id}: {e}")
            return
    
    user = get_user_data(user_id)
    if not user:
        update_user_data(user_id, first_name)
        user = get_user_data(user_id)
        if not user:
            logging.error(f"Failed to initialize user {user_id} in new_poll")
            try:
                bot.send_message(chat_id=user_id, text="🚫 Ошибка при создании опроса. Попробуйте снова.")
            except Exception as e:
                logging.error(f"Error sending error message to user {user_id}: {e}")
            return
    
    is_subscribed, _ = check_subscription(user_id)
    
    if not is_subscribed:
        requests_left = check_free_requests(user_id)
        if requests_left == 0:
            time_until_reset = get_time_until_reset(user_id)
            try:
                bot.send_message(chat_id=user_id, text=f"🚫 У вас закончились бесплатные запросы. "
                                        f"Осталось до восстановления: {time_until_reset}. "
                                        f"Купите подписку или запросы: /subscription 💸")
                logging.info(f"User {user_id} out of free requests, time until reset: {time_until_reset}")
                return
            except Exception as e:
                logging.error(f"Error sending out-of-requests message to user {user_id}: {e}")
                return
        deduct_free_request(user_id)
    
    try:
        user_states[user_id] = {"state": "author", "message_id": None}
        poll_data[user_id] = {"user_id": user_id}
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("❌ Отменить", callback_data="cancel"))
        sent_msg = bot.send_message(chat_id=user_id, text="🎶 Введите имя исполнителя:", reply_markup=markup)
        user_states[user_id]['message_id'] = sent_msg.message_id
        logging.info(f"Started poll creation for user {user_id}")
    except Exception as e:
        logging.error(f"Error starting poll creation for user {user_id}: {e}")
        if user_id in user_states:
            del user_states[user_id]
        if user_id in poll_data:
            del poll_data[user_id]
        try:
            bot.send_message(chat_id=user_id, text="🚫 Ошибка при создании опроса. Попробуйте снова.")
        except Exception as e2:
            logging.error(f"Error sending error message to user {user_id}: {e2}")

@bot.message_handler(commands=['g0ldfinchpan3l'])
def admin_panel(message):
    user_id = str(message.chat.id)
    if user_id not in ADMIN_IDS:
        logging.info(f"Unauthorized user {user_id} attempted /g0ldfinchpan3l")
        return
    
    try:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("📊 Статистика", callback_data="admin_stats"))
        markup.add(InlineKeyboardButton("💳 Подписки", callback_data="admin_subscriptions"))
        markup.add(InlineKeyboardButton("🔒 Пользователи", callback_data="admin_users"))
        markup.add(InlineKeyboardButton("📢 Отправить сообщение всем", callback_data="admin_broadcast"))
        bot.send_message(chat_id=user_id, text="🛠 *Панель управления*", reply_markup=markup, parse_mode="Markdown")
        logging.info(f"Admin panel opened for user {user_id}")
    except Exception as e:
        logging.error(f"Error opening admin panel for user {user_id}: {e}")
        try:
            bot.send_message(chat_id=user_id, text="🚫 Ошибка при открытии панели управления.")
        except Exception as e2:
            logging.error(f"Error sending error message to user {user_id}: {e2}")

@bot.message_handler(commands=['reset_states'])
def reset_states(message):
    user_id = str(message.chat.id)
    if user_id in ADMIN_IDS:
        user_states.clear()
        try:
            bot.send_message(chat_id=user_id, text="✅ Состояния пользователей сброшены.")
            logging.info(f"User states cleared by admin {user_id}")
        except Exception as e:
            logging.error(f"Error sending reset states confirmation to user {user_id}: {e}")
    else:
        logging.info(f"Unauthorized user {user_id} attempted /reset_states")

@bot.message_handler(func=lambda m: str(m.chat.id) in user_states and user_states[str(m.chat.id)].get("state") in ["ban_user", "manage_sub_id", "admin_broadcast"])
def handle_admin_input(message):
    user_id = str(message.chat.id)
    if user_id not in ADMIN_IDS:
        logging.warning(f"Unauthorized user {user_id} attempted admin input")
        try:
            bot.send_message(chat_id=user_id, text="🚫 У вас нет доступа к этой функции.")
        except Exception as e:
            logging.error(f"Error sending unauthorized message to user {user_id}: {e}")
        return
    
    state = user_states[user_id]["state"]
    message_id = user_states[user_id]["message_id"]
    
    try:
        if message.text == "/cancel" and state == "manage_sub_id":
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("📊 Статистика", callback_data="admin_stats"))
            markup.add(InlineKeyboardButton("💳 Подписки", callback_data="admin_subscriptions"))
            markup.add(InlineKeyboardButton("🔒 Пользователи", callback_data="admin_users"))
            markup.add(InlineKeyboardButton("📢 Отправить сообщение всем", callback_data="admin_broadcast"))
            bot.edit_message_text(text="🛠 *Панель управления*", chat_id=user_id, message_id=message_id, reply_markup=markup, parse_mode="Markdown")
            del user_states[user_id]
            logging.info(f"Admin {user_id} cancelled user ID input for subscription management")
            return
        
        if state == "ban_user":
            target_id = message.text.strip()
            if not target_id.isdigit():
                bot.send_message(chat_id=user_id, text="🚫 Пожалуйста, введите числовой ID пользователя.")
                logging.info(f"Invalid ban user ID input by admin {user_id}: {target_id}")
                return
            
            target_id = str(target_id)
            user = get_user_data(target_id)
            if not user:
                bot.send_message(chat_id=user_id, text="🚫 Пользователь не найден.")
                logging.error(f"User {target_id} not found for banning by admin {user_id}")
                del user_states[user_id]
                return
            
            if is_user_banned(target_id):
                bot.send_message(chat_id=user_id, text="🚫 Пользователь уже заблокирован.")
                logging.info(f"User {target_id} already banned, attempted by admin {user_id}")
                del user_states[user_id]
                return
            
            conn = sqlite3.connect('bot_data.db')
            c = conn.cursor()
            first_name = user[1] or f"User{target_id}"
            c.execute("INSERT INTO banned_users (user_id, first_name) VALUES (?, ?)", (target_id, first_name))
            conn.commit()
            conn.close()
            bot.send_message(chat_id=user_id, text=f"✅ Пользователь {first_name} (ID: {target_id}) заблокирован.")
            bot.send_message(chat_id=target_id, text="⚠ Вы были заблокированы и не можете использовать бот.")
            logging.info(f"User {target_id} banned by admin {user_id}")
            del user_states[user_id]
        
        elif state == "manage_sub_id":
            target_id = message.text.strip()
            if not target_id.isdigit():
                bot.send_message(chat_id=user_id, text="🚫 Пожалуйста, введите числовой ID пользователя.")
                logging.info(f"Invalid subscription user ID input by admin {user_id}: {target_id}")
                return
            
            target_id = str(target_id)
            user = get_user_data(target_id)
            if not user:
                bot.send_message(chat_id=user_id, text="🚫 Пользователь не найден.")
                logging.error(f"User {target_id} not found for subscription management by admin {user_id}")
                del user_states[user_id]
                return
            
            first_name = user[1] or f"User{target_id}"
            is_subscribed, days_left = check_subscription(target_id)
            status = "✅ Активна" if is_subscribed else "❌ Не активна"
            days_text = "∞" if days_left == float('inf') else (days_left if is_subscribed else "Нет подписки")
            unlimited = user[6] if len(user) > 6 else "no"
            text = (f"💳 *Управление подпиской*\n\n"
                    f"Пользователь: {first_name} (ID: {target_id})\n"
                    f"Статус подписки: {status}\n"
                    f"Истекает: {days_text}\n"
                    f"Неограниченные запросы: {'✅' if unlimited == 'yes' else '❌'}\n\n"
                    f"Выберите действие:")
            markup = InlineKeyboardMarkup()
            markup.row(InlineKeyboardButton("1 день", callback_data=f"grant_sub_{target_id}_1day"))
            markup.row(InlineKeyboardButton("3 дня", callback_data=f"grant_sub_{target_id}_3days"))
            markup.row(InlineKeyboardButton("14 дней", callback_data=f"grant_sub_{target_id}_14days"))
            markup.row(InlineKeyboardButton("30 дней", callback_data=f"grant_sub_{target_id}_30days"))
            markup.row(InlineKeyboardButton("Навсегда", callback_data=f"grant_sub_{target_id}_permanent"))
            markup.row(InlineKeyboardButton("Сбросить подписку", callback_data=f"reset_sub_{target_id}"))
            markup.row(InlineKeyboardButton("🔙 Вернуться", callback_data="admin_subscriptions"))
            bot.edit_message_text(text=text, chat_id=user_id, message_id=message_id, reply_markup=markup, parse_mode="Markdown")
            logging.info(f"Subscription management for user {target_id} displayed for admin {user_id}")
            del user_states[user_id]
        
        elif state == "admin_broadcast":
            broadcast_text = message.text.strip()
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("📢 Отправить всем", callback_data=f"confirm_broadcast_{message.message_id}"))
            markup.add(InlineKeyboardButton("❌ Отменить", callback_data="cancel_broadcast"))
            bot.send_message(chat_id=user_id, text=f"📢 *Предпросмотр сообщения для рассылки*:\n\n{broadcast_text}", reply_markup=markup, parse_mode="Markdown")
            user_states[user_id]["broadcast_text"] = broadcast_text
            logging.info(f"Broadcast message previewed by admin {user_id}")
    except Exception as e:
        logging.error(f"Error in handle_admin_input for user {user_id}, state {state}: {e}")
        try:
            bot.send_message(chat_id=user_id, text="🚫 Ошибка при обработке ввода.")
            if user_id in user_states:
                del user_states[user_id]
        except Exception as e2:
            logging.error(f"Error sending error message to admin {user_id}: {e2}")

@bot.callback_query_handler(func=lambda call: call.data in ["admin_stats", "admin_subscriptions", "admin_users", "admin_broadcast", "back_to_admin", "cancel_broadcast"] or call.data.startswith(("manage_sub_", "grant_sub_", "reset_sub_", "ban_user_", "unban_user_", "confirm_broadcast_")))
def handle_admin_panel(call):
    user_id = str(call.message.chat.id)
    if user_id not in ADMIN_IDS:
        logging.warning(f"Unauthorized user {user_id} attempted admin callback {call.data}")
        try:
            bot.answer_callback_query(call.id, text="🚫 У вас нет доступа к этой функции.")
        except Exception as e:
            logging.error(f"Error answering unauthorized callback for user {user_id}: {e}")
        return
    
    try:
        if call.data == "admin_stats":
            conn = sqlite3.connect('bot_data.db')
            c = conn.cursor()
            c.execute("SELECT user_id, first_name FROM users")
            users = c.fetchall()
            conn.close()
            user_count = len(users)
            user_list = "\n".join([f"• {user[1] or f'User{user[0]}'} (ID: {user[0]})" for user in users]) or "Нет пользователей."
            text = f"📊 *Статистика бота*\n\nВсего пользователей: {user_count}\n\nСписок:\n{user_list}"
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("🔙 Вернуться", callback_data="back_to_admin"))
            try:
                bot.edit_message_text(text=text, chat_id=user_id, message_id=call.message.message_id, reply_markup=markup, parse_mode="Markdown")
            except telebot.apihelper.ApiTelegramException as e:
                if "message to edit not found" in str(e):
                    bot.send_message(chat_id=user_id, text=text, reply_markup=markup, parse_mode="Markdown")
                else:
                    raise e
            logging.info(f"Stats displayed for admin {user_id}")
        
        elif call.data == "admin_subscriptions":
            user_states[user_id] = {"state": "manage_sub_id", "message_id": call.message.message_id}
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("🔙 Отменить", callback_data="back_to_admin"))
            try:
                bot.edit_message_text(text="💳 Введите ID пользователя для управления подпиской (или /cancel для отмены):", 
                                     chat_id=user_id, message_id=call.message.message_id, reply_markup=markup)
            except telebot.apihelper.ApiTelegramException as e:
                if "message to edit not found" in str(e):
                    bot.send_message(chat_id=user_id, text="💳 Введите ID пользователя для управления подпиской (или /cancel для отмены):", reply_markup=markup)
                else:
                    raise e
            logging.info(f"Admin {user_id} started subscription management process")
        
        elif call.data == "admin_users":
            conn = sqlite3.connect('bot_data.db')
            c = conn.cursor()
            c.execute("SELECT user_id, first_name FROM banned_users")
            banned = c.fetchall()
            conn.close()
            if not banned:
                text = "🔒 *Управление пользователями*\n\nНет заблокированных пользователей."
            else:
                text = "🔒 *Управление пользователями*\n\nЗаблокированные пользователи:\n"
                for user in banned:
                    user_id_banned, first_name = user
                    first_name = first_name or f"User{user_id_banned}"
                    text += f"• {first_name} (ID: {user_id_banned})\n"
            markup = InlineKeyboardMarkup()
            for user in banned:
                user_id_banned, first_name = user
                first_name = first_name or f"User{user_id_banned}"
                markup.add(InlineKeyboardButton(f"Разбанить: {first_name}", callback_data=f"unban_user_{user_id_banned}"))
            markup.add(InlineKeyboardButton("🔙 Вернуться", callback_data="back_to_admin"))
            markup.add(InlineKeyboardButton("Забанить пользователя", callback_data="ban_user_start"))
            try:
                bot.edit_message_text(text=text, chat_id=user_id, message_id=call.message.message_id, reply_markup=markup, parse_mode="Markdown")
            except telebot.apihelper.ApiTelegramException as e:
                if "message to edit not found" in str(e):
                    bot.send_message(chat_id=user_id, text=text, reply_markup=markup, parse_mode="Markdown")
                else:
                    raise e
            logging.info(f"Users management menu displayed for admin {user_id}")
        
        elif call.data == "admin_broadcast":
            user_states[user_id] = {"state": "admin_broadcast", "message_id": call.message.message_id}
            try:
                bot.edit_message_text(text="📢 Введите текст сообщения для рассылки всем пользователям:", 
                                     chat_id=user_id, message_id=call.message.message_id)
            except telebot.apihelper.ApiTelegramException as e:
                if "message to edit not found" in str(e):
                    bot.send_message(chat_id=user_id, text="📢 Введите текст сообщения для рассылки всем пользователям:")
                else:
                    raise e
            logging.info(f"Admin {user_id} started broadcast process")
        
        elif call.data == "back_to_admin":
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("📊 Статистика", callback_data="admin_stats"))
            markup.add(InlineKeyboardButton("💳 Подписки", callback_data="admin_subscriptions"))
            markup.add(InlineKeyboardButton("🔒 Пользователи", callback_data="admin_users"))
            markup.add(InlineKeyboardButton("📢 Отправить сообщение всем", callback_data="admin_broadcast"))
            try:
                bot.edit_message_text(text="🛠 *Панель управления*", chat_id=user_id, message_id=call.message.message_id, reply_markup=markup, parse_mode="Markdown")
            except telebot.apihelper.ApiTelegramException as e:
                if "message to edit not found" in str(e):
                    bot.send_message(chat_id=user_id, text="🛠 *Панель управления*", reply_markup=markup, parse_mode="Markdown")
                else:
                    raise e
            logging.info(f"Returned to admin panel for user {user_id}")
        
        elif call.data.startswith("manage_sub_"):
            target_id = call.data.split("_")[-1]
            user = get_user_data(target_id)
            if not user:
                try:
                    bot.edit_message_text(text="🚫 Пользователь не найден.", chat_id=user_id, message_id=call.message.message_id)
                except telebot.apihelper.ApiTelegramException as e:
                    if "message to edit not found" in str(e):
                        bot.send_message(chat_id=user_id, text="🚫 Пользователь не найден.")
                    else:
                        raise e
                logging.error(f"User {target_id} not found for subscription management")
                bot.answer_callback_query(call.id)
                return
            first_name = user[1] or f"User{target_id}"
            is_subscribed, days_left = check_subscription(target_id)
            status = "✅ Активна" if is_subscribed else "❌ Не активна"
            days_text = "∞" if days_left == float('inf') else (days_left if is_subscribed else "Нет подписки")
            unlimited = user[6] if len(user) > 6 else "no"
            text = (f"💳 *Управление подпиской*\n\n"
                    f"Пользователь: {first_name} (ID: {target_id})\n"
                    f"Статус подписки: {status}\n"
                    f"Истекает: {days_text}\n"
                    f"Неограниченные запросы: {'✅' if unlimited == 'yes' else '❌'}\n\n"
                    f"Выберите действие:")
            markup = InlineKeyboardMarkup()
            markup.row(InlineKeyboardButton("1 день", callback_data=f"grant_sub_{target_id}_1day"))
            markup.row(InlineKeyboardButton("3 дня", callback_data=f"grant_sub_{target_id}_3days"))
            markup.row(InlineKeyboardButton("14 дней", callback_data=f"grant_sub_{target_id}_14days"))
            markup.row(InlineKeyboardButton("30 дней", callback_data=f"grant_sub_{target_id}_30days"))
            markup.row(InlineKeyboardButton("Навсегда", callback_data=f"grant_sub_{target_id}_permanent"))
            markup.row(InlineKeyboardButton("Сбросить подписку", callback_data=f"reset_sub_{target_id}"))
            markup.row(InlineKeyboardButton("🔙 Вернуться", callback_data="admin_subscriptions"))
            try:
                bot.edit_message_text(text=text, chat_id=user_id, message_id=call.message.message_id, reply_markup=markup, parse_mode="Markdown")
            except telebot.apihelper.ApiTelegramException as e:
                if "message to edit not found" in str(e):
                    bot.send_message(chat_id=user_id, text=text, reply_markup=markup, parse_mode="Markdown")
                else:
                    raise e
            logging.info(f"Subscription management for user {target_id} displayed for admin {user_id}")
        
        elif call.data.startswith("grant_sub_"):
            parts = call.data.split("_")
            target_id, duration = parts[2], parts[3]
            user = get_user_data(target_id)
            if not user:
                try:
                    bot.edit_message_text(text="🚫 Пользователь не найден.", chat_id=user_id, message_id=call.message.message_id)
                except telebot.apihelper.ApiTelegramException as e:
                    if "message to edit not found" in str(e):
                        bot.send_message(chat_id=user_id, text="🚫 Пользователь не найден.")
                    else:
                        raise e
                logging.error(f"User {target_id} not found for granting subscription")
                bot.answer_callback_query(call.id)
                return
            now = datetime.now(timezone.utc)
            if duration == "permanent":
                sub_status = "permanent"
                sub_end = None
            else:
                days = {"1day": 1, "3days": 3, "14days": 14, "30days": 30}
                sub_end = (now + timedelta(days=days[duration])).isoformat()
                sub_status = "active"
            update_user_data(target_id, user[1] or f"User{target_id}", subscription_status=sub_status, subscription_end=sub_end)
            duration_text = {"1day": "1 день", "3days": "3 дня", "14days": "14 дней", "30days": "30 дней", "permanent": "навсегда"}[duration]
            try:
                bot.edit_message_text(text=f"✅ Подписка на {duration_text} выдана пользователю {user[1] or f'User{target_id}'}.", 
                                     chat_id=user_id, message_id=call.message.message_id)
                bot.send_message(chat_id=target_id, text=f"🎉 Вам выдана подписка на {duration_text}! Твои предложения будут рассматриваться первыми. 🔥")
                for admin_id in ADMIN_IDS:
                    bot.send_message(chat_id=admin_id, text=f"👤 {user[1] or f'User{target_id}'} получил подписку на {duration_text}! 🎉")
            except telebot.apihelper.ApiTelegramException as e:
                if "message to edit not found" in str(e):
                    bot.send_message(chat_id=user_id, text=f"✅ Подписка на {duration_text} выдана пользователю {user[1] or f'User{target_id}'}.", 
                                    parse_mode="Markdown")
                    bot.send_message(chat_id=target_id, text=f"🎉 Вам выдана подписка на {duration_text}! Твои предложения будут рассматриваться первыми. 🔥")
                    for admin_id in ADMIN_IDS:
                        bot.send_message(chat_id=admin_id, text=f"👤 {user[1] or f'User{target_id}'} получил подписку на {duration_text}! 🎉")
                else:
                    raise e
            logging.info(f"Subscription {duration} granted to user {target_id} by admin {user_id}")
        
        elif call.data.startswith("reset_sub_"):
            target_id = call.data.split("_")[2]
            user = get_user_data(target_id)
            if not user:
                try:
                    bot.edit_message_text(text="🚫 Пользователь не найден.", chat_id=user_id, message_id=call.message.message_id)
                except telebot.apihelper.ApiTelegramException as e:
                    if "message to edit not found" in str(e):
                        bot.send_message(chat_id=user_id, text="🚫 Пользователь не найден.")
                    else:
                        raise e
                logging.error(f"User {target_id} not found for subscription reset")
                bot.answer_callback_query(call.id)
                return
            update_user_data(target_id, user[1] or f"User{target_id}", subscription_status="none", subscription_end=None, unlimited_requests="no")
            try:
                bot.edit_message_text(text=f"✅ Подписка и неограниченные запросы сброшены для пользователя {user[1] or f'User{target_id}'}.", 
                                     chat_id=user_id, message_id=call.message.message_id)
                bot.send_message(chat_id=target_id, text="⚠ Ваша подписка была сброшена администратором.")
            except telebot.apihelper.ApiTelegramException as e:
                if "message to edit not found" in str(e):
                    bot.send_message(chat_id=user_id, text=f"✅ Подписка и неограниченные запросы сброшены для пользователя {user[1] or f'User{target_id}'}.", 
                                    parse_mode="Markdown")
                    bot.send_message(chat_id=target_id, text="⚠ Ваша подписка была сброшена администратором.")
                else:
                    raise e
            logging.info(f"Subscription reset for user {target_id} by admin {user_id}")
        
        elif call.data == "ban_user_start":
            user_states[user_id] = {"state": "ban_user", "message_id": call.message.message_id}
            try:
                bot.edit_message_text(text="🔒 Введите ID пользователя для блокировки:", 
                                     chat_id=user_id, message_id=call.message.message_id)
            except telebot.apihelper.ApiTelegramException as e:
                if "message to edit not found" in str(e):
                    bot.send_message(chat_id=user_id, text="🔒 Введите ID пользователя для блокировки:")
                else:
                    raise e
            logging.info(f"Admin {user_id} started ban user process")
        
        elif call.data.startswith("unban_user_"):
            target_id = call.data.split("_")[2]
            conn = sqlite3.connect('bot_data.db')
            c = conn.cursor()
            c.execute("SELECT first_name FROM banned_users WHERE user_id = ?", (target_id,))
            banned_user = c.fetchone()
            if not banned_user:
                try:
                    bot.edit_message_text(text="🚫 Пользователь не найден в списке заблокированных.", 
                                         chat_id=user_id, message_id=call.message.message_id)
                except telebot.apihelper.ApiTelegramException as e:
                    if "message to edit not found" in str(e):
                        bot.send_message(chat_id=user_id, text="🚫 Пользователь не найден в списке заблокированных.")
                    else:
                        raise e
                logging.error(f"User {target_id} not found in banned_users for unbanning by admin {user_id}")
                bot.answer_callback_query(call.id)
                return
            first_name = banned_user[0] or f"User{target_id}"
            c.execute("DELETE FROM banned_users WHERE user_id = ?", (target_id,))
            conn.commit()
            conn.close()
            try:
                bot.edit_message_text(text=f"✅ Пользователь {first_name} (ID: {target_id}) разблокирован.", 
                                     chat_id=user_id, message_id=call.message.message_id)
                bot.send_message(chat_id=target_id, text="🎉 Вы были разблокированы и можете снова использовать бот.")
            except telebot.apihelper.ApiTelegramException as e:
                if "message to edit not found" in str(e):
                    bot.send_message(chat_id=user_id, text=f"✅ Пользователь {first_name} (ID: {target_id}) разблокирован.", 
                                    parse_mode="Markdown")
                    bot.send_message(chat_id=target_id, text="🎉 Вы были разблокированы и можете снова использовать бот.")
                else:
                    raise e
            logging.info(f"User {target_id} unbanned by admin {user_id}")
        
        elif call.data.startswith("confirm_broadcast_"):
            if user_id not in user_states or "broadcast_text" not in user_states[user_id]:
                try:
                    bot.edit_message_text(text="🚫 Текст рассылки не найден.", chat_id=user_id, message_id=call.message.message_id)
                    bot.answer_callback_query(call.id)
                    return
                except telebot.apihelper.ApiTelegramException as e:
                    if "message to edit not found" in str(e):
                        bot.send_message(chat_id=user_id, text="🚫 Текст рассылки не найден.")
                    else:
                        raise e
            broadcast_text = user_states[user_id]["broadcast_text"]
            conn = sqlite3.connect('bot_data.db')
            c = conn.cursor()
            c.execute("SELECT user_id FROM users")
            users = c.fetchall()
            conn.close()
            sent_count = 0
            for user in users:
                target_id = str(user[0])
                if not is_user_banned(target_id):
                    try:
                        bot.send_message(chat_id=target_id, text=broadcast_text, parse_mode="Markdown")
                        sent_count += 1
                    except Exception as e:
                        logging.error(f"Error sending broadcast to user {target_id}: {e}")
            try:
                bot.edit_message_text(text=f"✅ Сообщение отправлено {sent_count} пользователям.", chat_id=user_id, message_id=call.message.message_id)
                if user_id in user_states:
                    del user_states[user_id]
            except telebot.apihelper.ApiTelegramException as e:
                if "message to edit not found" in str(e):
                    bot.send_message(chat_id=user_id, text=f"✅ Сообщение отправлено {sent_count} пользователям.")
                else:
                    raise e
            logging.info(f"Broadcast sent to {sent_count} users by admin {user_id}")
        
        elif call.data == "cancel_broadcast":
            try:
                bot.edit_message_text(text="🚫 Рассылка отменена.", chat_id=user_id, message_id=call.message.message_id)
                if user_id in user_states:
                    del user_states[user_id]
            except telebot.apihelper.ApiTelegramException as e:
                if "message to edit not found" in str(e):
                    bot.send_message(chat_id=user_id, text="🚫 Рассылка отменена.")
                else:
                    raise e
            logging.info(f"Broadcast cancelled by admin {user_id}")
        
        bot.answer_callback_query(call.id)
    except Exception as e:
        logging.error(f"Error in handle_admin_panel for user {user_id}, callback {call.data}: {e}")
        try:
            bot.answer_callback_query(call.id, text="Ошибка при обработке запроса.")
        except Exception as e2:
            logging.error(f"Error answering callback for admin panel error, user {user_id}: {e2}")

@bot.message_handler(func=lambda m: str(m.chat.id) in user_states and user_states[str(m.chat.id)].get("state") not in ["ban_user", "manage_sub_id", "admin_broadcast"])
def collect_data(message):
    user_id = str(message.chat.id)
    user_state = user_states.get(user_id)
    if not user_state:
        logging.warning(f"No state found for user {user_id} in collect_data")
        return
    
    state = user_state['state']
    message_id = user_state['message_id']
    text = message.text
    
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("❌ Отменить", callback_data="cancel"))
    
    try:
        if state == "author":
            poll_data[user_id]["author"] = text
            user_state["state"] = "opt1"
            bot.edit_message_text(text="🎵 Введите трек 1:", 
                                 chat_id=user_id, 
                                 message_id=message_id, 
                                 reply_markup=markup)
        elif state == "opt1":
            poll_data[user_id]["opt1"] = text
            user_state["state"] = "opt2"
            bot.edit_message_text(text="🎵 Введите трек 2:", 
                                 chat_id=user_id, 
                                 message_id=message_id, 
                                 reply_markup=markup)
        elif state == "opt2":
            poll_data[user_id]["opt2"] = text
            user_state["state"] = "opt3"
            bot.edit_message_text(text="🎵 Введите трек 3:", 
                                 chat_id=user_id, 
                                 message_id=message_id, 
                                 reply_markup=markup)
        elif state == "opt3":
            poll_data[user_id]["opt3"] = text
            user_state["state"] = "opt4"
            bot.edit_message_text(text="🎵 Введите трек 4:", 
                                 chat_id=user_id, 
                                 message_id=message_id, 
                                 reply_markup=markup)
        elif state == "opt4":
            poll_data[user_id]["opt4"] = text
            del user_states[user_id]
            
            data = poll_data[user_id]
            text = f"📊 Предпросмотр опроса:\n\n👤 *{data['author']}*\n\n1. {data['opt1']}\n2. {data['opt2']}\n3. {data['opt3']}\n4. {data['opt4']}"
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("📩 Отправить", callback_data=f"submit_{user_id}"))
            markup.add(InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_submit_{user_id}"))
            bot.edit_message_text(text=text, chat_id=user_id, message_id=message_id, reply_markup=markup, parse_mode="Markdown")
            
            is_subscribed, _ = check_subscription(user_id)
            if is_subscribed:
                start_text = "🔥 Спасибо за подписку!!"
            else:
                start_text = ("💸 Купите подписку и вы получите: доступ к отправке бесконечных предложений, "
                              "ваши опросы будут отправляться мгновенно!")
            bot.send_message(chat_id=user_id, text=f"👋 Добро пожаловать!\n\nЗдесь можно предложить опрос для @oprosy_shegla\n\n{start_text}")
            logging.info(f"Poll preview sent to user {user_id}")
    except Exception as e:
        logging.error(f"Error in collect_data for user {user_id}, state {state}: {e}")
        try:
            bot.send_message(chat_id=user_id, text="🚫 Ошибка при создании опроса. Попробуйте снова.")
            if user_id in user_states:
                del user_states[user_id]
            if user_id in poll_data:
                del poll_data[user_id]
        except Exception as e2:
            logging.error(f"Error sending error message to user {user_id}: {e2}")

@bot.callback_query_handler(func=lambda call: call.data in ["requests_menu", "subscriptions_menu", "back_to_main"] or call.data.startswith("donation_"))
def handle_menu_navigation(call):
    user_id = str(call.message.chat.id)
    first_name = call.from_user.first_name or f"User{user_id}"
    
    if is_user_banned(user_id):
        logging.info(f"Banned user {user_id} attempted menu navigation")
        bot.answer_callback_query(call.id)
        return
    
    user = get_user_data(user_id)
    if not user:
        update_user_data(user_id, first_name)
        user = get_user_data(user_id)
    
    try:
        is_subscribed, days_left = check_subscription(user_id)
        status = "✅ Активна" if is_subscribed else "❌ Не активна"
        days_text = "∞" if days_left == float('inf') else (days_left if is_subscribed else "Нет подписки")
        
        price = 0.01 if user_id == str(reviewer_id) and id_prices else None  # Changed from 0.0001$ to 0.01$
        donation_prices = [0.01, 0.40, 1.20, 2.40, 3.60] if not price else [price] * 5
        request_prices = [0.16, 0.64, 1.20, 1.84] if not price else [price] * 4
        sub_prices = [0.10, 0.40, 1.20, 2.40, 3.60] if not price else [price] * 5
        
        if call.data == "requests_menu":
            text = (f"💳 *Покупка запросов*\n\n"
                    f"Статус подписки: {status}\n"
                    f"Истекает: {days_text}\n\n"
                    f"Выберите количество запросов:")
            markup = InlineKeyboardMarkup()
            markup.row(InlineKeyboardButton(f"3 запроса - {request_prices[0]}$ 💵 (20% скидка)", callback_data=f"buy_request_3_{request_prices[0]}"))
            markup.row(InlineKeyboardButton(f"10 запросов - {request_prices[1]}$ 💵 (20% скидка)", callback_data=f"buy_request_10_{request_prices[1]}"))
            markup.row(InlineKeyboardButton(f"35 запросов - {request_prices[2]}$ 💵 (25% скидка)", callback_data=f"buy_request_35_{request_prices[2]}"))
            markup.row(InlineKeyboardButton(f"∞ запросов - {request_prices[3]}$ 💎 (30% скидка)", callback_data=f"buy_request_unlimited_{request_prices[3]}"))
            markup.row(InlineKeyboardButton("🔙 Вернуться", callback_data="back_to_main"))
            try:
                bot.edit_message_text(text=text, chat_id=user_id, message_id=call.message.message_id, reply_markup=markup, parse_mode="Markdown")
            except telebot.apihelper.ApiTelegramException as e:
                if "message to edit not found" in str(e):
                    bot.send_message(chat_id=user_id, text=text, reply_markup=markup, parse_mode="Markdown")
                else:
                    raise e
            logging.info(f"Requests menu sent to user {user_id}")
        
        elif call.data == "subscriptions_menu":
            text = (f"💳 *Подписка*\n\n"
                    f"Статус: {status}\n"
                    f"Истекает: {days_text}\n\n"
                    f"Выберите подписку:")
            markup = InlineKeyboardMarkup()
            markup.row(InlineKeyboardButton(f"1 день - {sub_prices[0]}$ 💵 (20% скидка)", callback_data=f"sub_1day_{sub_prices[0]}"))
            markup.row(InlineKeyboardButton(f"3 дня - {sub_prices[1]}$ 💵 (20% скидка)", callback_data=f"sub_3days_{sub_prices[1]}"))
            markup.row(InlineKeyboardButton(f"14 дней - {sub_prices[2]}$ 💵 (20% скидка)", callback_data=f"sub_14days_{sub_prices[2]}"))
            markup.row(InlineKeyboardButton(f"30 дней - {sub_prices[3]}$ 💵 (20% скидка)", callback_data=f"sub_30days_{sub_prices[3]}"))
            markup.row(InlineKeyboardButton(f"Навсегда - {sub_prices[4]}$ 💎 (20% скидка)", callback_data=f"sub_permanent_{sub_prices[4]}"))
            markup.row(InlineKeyboardButton("🔙 Вернуться", callback_data="back_to_main"))
            try:
                bot.edit_message_text(text=text, chat_id=user_id, message_id=call.message.message_id, reply_markup=markup, parse_mode="Markdown")
            except telebot.apihelper.ApiTelegramException as e:
                if "message to edit not found" in str(e):
                    bot.send_message(chat_id=user_id, text=text, reply_markup=markup, parse_mode="Markdown")
                else:
                    raise e
            logging.info(f"Subscriptions menu sent to user {user_id}")
        
        elif call.data == "back_to_main":
            text = (f"💳 *Подписка*\n\n"
                    f"Статус: {status}\n"
                    f"Истекает: {days_text}\n\n"
                    f"Выберите действие:")
            markup = InlineKeyboardMarkup()
            markup.add(
                InlineKeyboardButton("Запросы", callback_data="requests_menu"),
                InlineKeyboardButton("Подписка", callback_data="subscriptions_menu")
            )
            try:
                bot.edit_message_text(text=text, chat_id=user_id, message_id=call.message.message_id, reply_markup=markup, parse_mode="Markdown")
            except telebot.apihelper.ApiTelegramException as e:
                if "message to edit not found" in str(e):
                    bot.send_message(chat_id=user_id, text=text, reply_markup=markup, parse_mode="Markdown")
                else:
                    raise e
            logging.info(f"Main menu sent to user {user_id}")
        
        elif call.data.startswith("donation_"):
            amount = float(call.data.split("_")[1])
            invoice_id, pay_url = create_crypto_invoice(user_id, amount, "donation")
            if pay_url:
                markup = InlineKeyboardMarkup()
                markup.add(InlineKeyboardButton("💸 Оплатить", url=pay_url))
                markup.add(InlineKeyboardButton("🔄 Проверить", callback_data=f"check_payment_{invoice_id}"))
                try:
                    bot.edit_message_text(text=f"💳 Счёт на {amount}$ для доната создан!\nОплатите по ссылке:", 
                                         chat_id=user_id, message_id=call.message.message_id, reply_markup=markup)
                except telebot.apihelper.ApiTelegramException as e:
                    if e.error_code == 400:
                        logging.warning(f"Message to edit not found for donation, sending new message for user {user_id}")
                        bot.send_message(chat_id=user_id, text=f"💳 Счёт на {amount}$ для доната создан!\nОплатите по ссылке:", reply_markup=markup)
                    else:
                        raise e
                logging.info(f"Donation invoice created for user {user_id}, amount {amount}")
            else:
                try:
                    bot.edit_message_text(text="❌ Ошибка при создании счёта. Попробуйте снова.", 
                                         chat_id=user_id, message_id=call.message.message_id)
                except telebot.apihelper.ApiTelegramException as e:
                    if e.error_code == 400:
                        logging.warning(f"Message to edit not found for donation error, sending new message for user {user_id}")
                        bot.send_message(chat_id=user_id, text="❌ Ошибка при создании счёта. Попробуйте снова.")
                    else:
                        raise e
                logging.error(f"Donation invoice creation failed for user {user_id}")
        
        bot.answer_callback_query(call.id)
    except Exception as e:
        logging.error(f"Error in handle_menu for user {user_id}, callback {call.data}: {e}")
        try:
            bot.answer_callback_query(call.id, text="Ошибка при обработке меню.")
        except Exception as e2:
            logging.error(f"Error answering callback for menu navigation, user {user_id}: {e2}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("sub_") or call.data.startswith("buy_request_"))
def handle_purchase(call):
    user_id = str(call.message.chat.id)
    
    if is_user_banned(user_id):
        logging.info(f"Banned user {user_id} attempted purchase")
        bot.answer_callback_query(call.id)
        return
    
    data = call.data.split("_")
    action = data[0]
    duration = data[1]
    amount = float(data[2])
    
    invoice_id, pay_url = create_crypto_invoice(user_id, amount, duration if action == "sub" else f"request_{duration}")
    try:
        if pay_url:
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("💸 Оплатить", url=pay_url))
            markup.add(InlineKeyboardButton("🔄 Проверить", callback_data=f"check_payment_{invoice_id}"))
            try:
                bot.edit_message_text(text=f"💳 Счёт на {amount}$ для {'подписки' if action == 'sub' else 'покупки запросов'} создан!", 
                                     chat_id=user_id, message_id=call.message.message_id, reply_markup=markup)
            except telebot.apihelper.ApiTelegramException as e:
                if "message to edit not found" in str(e):
                    bot.send_message(chat_id=user_id, text=f"💳 Счёт на {amount}$ для {'подписки' if action == 'sub' else 'покупки запросов'} создан!", 
                                    reply_markup=markup)
                else:
                    raise e
            logging.info(f"Invoice created for user {user_id}, action={action}, duration={duration}, amount={amount}")
        else:
            try:
                bot.edit_message_text(text="🚫 Ошибка при создании счёта!", 
                                     chat_id=user_id, message_id=call.message.message_id)
            except telebot.apihelper.ApiTelegramException as e:
                if "message to edit not found" in str(e):
                    bot.send_message(chat_id=user_id, text="🚫 Ошибка при создании счёта!")
                else:
                    raise e
            logging.error(f"Invoice creation failed for user {user_id}")
    except Exception as e:
        logging.error(f"Error in handle_purchase for user {user_id}: {e}")
    try:
        bot.answer_callback_query(call.id)
    except Exception as e:
        logging.error(f"Error answering callback for purchase, user {user_id}: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("check_payment_"))
def check_payment(call):
    user_id = str(call.message.chat.id)
    
    if is_user_banned(user_id):
        logging.info(f"Banned user {user_id} attempted to check payment")
        bot.answer_callback_query(call.id)
        return
    
    invoice_id = call.data.split("_")[2]
    
    conn = sqlite3.connect('bot_data.db')
    c = conn.cursor()
    c.execute("SELECT user_id, amount, duration FROM payments WHERE user_id = ? AND invoice_id = ? AND status = 'pending'", 
                     (user_id, invoice_id))
    payment = c.fetchone()
    conn.close()
    
    if payment and check_payment_status(invoice_id):
        user = get_user_data(user_id)
        duration = payment[2]
        now = datetime.now(timezone.utc)
        
        if duration == "donation":
            text = f"🎉 Спасибо за донат на {payment[1]}$! 😊 Вы поддержали опросы Щегла!"
        elif duration.startswith("request_"):
            num_requests = duration.split("_")[1]
            if num_requests == "unlimited":
                update_user_data(user_id, user[1] if user and len(user) > 1 else f"User{user_id}", unlimited_requests='yes')
                text = "🎉 Оплата подтверждена! Добавлены неограниченные запросы (∞)."
            else:
                num_requests = int(num_requests)
                new_requests = (user[4] if user and len(user) > 4 else 0) + num_requests
                update_user_data(user_id, user[1] if user and len(user) > 1 else f"User{user_id}", free_requests=new_requests)
                text = f"🎉 Оплата подтверждена! Добавлено {num_requests} запрос(ов). Всего доступно: {new_requests}."
        else:
            if duration == "permanent":
                sub_status = "permanent"
                sub_end = None
            else:
                days = {"1day": 1, "3days": 3, "14days": 14, "30days": 30}
                sub_end = (now + timedelta(days=days[duration])).isoformat()
                sub_status = "active"
            update_user_data(user_id, user[1] if user and len(user) > 1 else f"User{user_id}", subscription_status=sub_status, subscription_end=sub_end)
            duration_text = {"1day": "1 день", "3days": "3 дня", "14days": "14 дней", "30days": "30 дней", "permanent": "навсегда"}[duration]
            text = (f"🎉 Оплата подтверждена! Подписка на {duration_text} активирована.\n\n"
                    f"Спасибо, {user[1] if user and len(user) > 1 else f'User{user_id}'}! 😊 Ты поддержал опросы Щегла и даёшь мотивацию делать видео! "
                    f"Твои предложения будут рассматриваться первыми. 🔥")
        
        conn = sqlite3.connect('bot_data.db')
        c = conn.cursor()
        c.execute("UPDATE payments SET status = 'paid' WHERE invoice_id = ?", (invoice_id,))
        conn.commit()
        conn.close()
        
        try:
            bot.edit_message_text(text=text, chat_id=user_id, message_id=call.message.message_id)
            if duration not in ["request_3", "request_10", "request_35", "request_unlimited", "donation"]:
                for admin_id in ADMIN_IDS:
                    bot.send_message(chat_id=admin_id, text=f"👤 {user[1] if user and len(user) > 1 else f'User{user_id}'} купил подписку на {duration_text}! 🎉")
            logging.info(f"Payment confirmed for user {user_id}, invoice {invoice_id}, duration {duration}")
        except Exception as e:
            logging.error(f"Error notifying payment confirmation for user {user_id}: {e}")
    else:
        try:
            bot.answer_callback_query(call.id, text="Оплата не найдена или ещё не подтверждена.")
            logging.info(f"Payment check for user {user_id}, invoice {invoice_id}: no payment or not paid")
        except Exception as e:
            logging.error(f"Error answering callback for payment check, user {user_id}: {e}")
    
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "cancel" or call.data.startswith("cancel_submit_"))
def cancel_poll_creation(call):
    user_id = str(call.message.chat.id)
    
    try:
        if call.data.startswith("cancel_submit_"):
            uid = str(call.data.split("_")[2])
            if uid == user_id:
                if user_id in poll_data:
                    del poll_data[user_id]
                if user_id in delayed_polls:
                    del delayed_polls[user_id]
                bot.edit_message_text(text="🚫 Отправка опроса отменена.", 
                                     chat_id=user_id, message_id=call.message.message_id)
                logging.info(f"Poll submission cancelled for user {user_id}")
                return
        else:
            if user_id in user_states:
                del user_states[user_id]
            if user_id in poll_data:
                del poll_data[user_id]
            if user_id in delayed_polls:
                del delayed_polls[user_id]
            bot.edit_message_text(text="🚫 Создание опроса отменено.", 
                                 chat_id=user_id, message_id=call.message.message_id)
            logging.info(f"Poll creation cancelled for user {user_id}")
        bot.answer_callback_query(call.id)
    except Exception as e:
        logging.error(f"Error in cancel_poll_creation for user {user_id}, callback {call.data}: {e}")
        try:
            bot.answer_callback_query(call.id, text="Ошибка при отмене. Попробуйте снова.")
        except Exception as e2:
            logging.error(f"Error answering callback for cancel, user {user_id}: {e2}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("submit_"))
def submit_poll(call):
    user_id = str(call.message.chat.id)
    
    try:
        uid = str(call.data.split("_")[1])
        if uid != user_id:
            bot.answer_callback_query(call.id, text="🚫 Неверный пользователь.")
            logging.warning(f"Invalid user in submit_poll: callback user {uid}, actual user {user_id}")
            return
        
        if user_id not in poll_data:
            bot.edit_message_text(text="🚫 Ошибка: данные опроса не найдены. Попробуйте создать опрос заново с помощью /new.", 
                                 chat_id=user_id, message_id=call.message.message_id)
            logging.error(f"Poll data not found for user {user_id} in submit_poll")
            return
        
        data = poll_data[user_id]
        is_subscribed, _ = check_subscription(user_id)
        
        if is_subscribed:
            pending.append(data)
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("👀 Посмотреть", callback_data=f"review_{len(pending)-1}"))
            for admin_id in ADMIN_IDS:
                bot.send_message(chat_id=admin_id, text=f"📗 Новый запрос! Всего: {len(pending)}", reply_markup=markup)
            bot.edit_message_text(text="✔ Опрос отправлен на проверку!", chat_id=user_id, message_id=call.message.message_id)
            logging.info(f"Poll submitted instantly by subscribed user {user_id}: {data}")
        else:
            submit_time = datetime.now(timezone.utc) + timedelta(minutes=5)
            delayed_polls[user_id] = {"data": data, "submit_time": submit_time, "message_id": call.message.message_id}
            text = f"⏳ Ваш опрос будет отправлен на проверку через 5 мин."
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_submit_{user_id}"))
            bot.edit_message_text(text=text, chat_id=user_id, message_id=call.message.message_id, reply_markup=markup)
            threading.Timer(10, update_countdown, args=(user_id, submit_time, call.message.message_id)).start()
            logging.info(f"Delayed poll submission scheduled for user {user_id}: {data}")
        
        if user_id in poll_data:
            del poll_data[user_id]
        bot.answer_callback_query(call.id)
    except Exception as e:
        logging.error(f"Error in submit_poll for user {user_id}, callback {call.data}: {e}")
        try:
            bot.edit_message_text(text="🚫 Ошибка при отправке опроса. Попробуйте снова.", 
                                 chat_id=user_id, message_id=call.message.message_id)
            if user_id in poll_data:
                del poll_data[user_id]
            if user_id in delayed_polls:
                del delayed_polls[user_id]
        except Exception as e2:
            logging.error(f"Error sending error message in submit_poll for user {user_id}: {e2}")
        try:
            bot.answer_callback_query(call.id, text="Ошибка при отправке. Попробуйте снова.")
        except Exception as e3:
            logging.error(f"Error answering callback in submit_poll for user {user_id}: {e3}")

def submit_delayed_poll(user_id, data, message_id):
    try:
        if user_id not in delayed_polls:
            logging.warning(f"Delayed poll for user {user_id} not found in delayed_polls during submission")
            bot.edit_message_text(text="🚫 Ошибка: данные опроса не найдены.", chat_id=user_id, message_id=message_id)
            return
        pending.append(data)
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("👀 Посмотреть", callback_data=f"review_{len(pending)-1}"))
        for admin_id in ADMIN_IDS:
            bot.send_message(chat_id=admin_id, text=f"📗 Новый запрос! Всего: {len(pending)}", reply_markup=markup)
        bot.edit_message_text(text="✔ Опрос отправлен на проверку!", chat_id=user_id, message_id=message_id)
        logging.info(f"Delayed poll submitted by user {user_id}: {data}")
        del delayed_polls[user_id]
    except Exception as e:
        logging.error(f"Error submitting delayed poll for user {user_id}: {e}")
        try:
            bot.edit_message_text(text="🚫 Ошибка при отправке опроса после задержки.", chat_id=user_id, message_id=message_id)
        except Exception as e2:
            logging.error(f"Error updating delayed poll message for user {user_id}: {e2}")

def update_countdown(user_id, submit_time, message_id):
    try:
        if user_id not in delayed_polls:
            logging.info(f"Countdown update stopped: user {user_id} not in delayed_polls")
            return
        now = datetime.now(timezone.utc)
        submit_time = submit_time.replace(tzinfo=timezone.utc)  # Ensure submit_time is offset-aware
        time_left = submit_time - now
        seconds_left = time_left.total_seconds()
        if seconds_left <=  0:
            logging.info(f"Countdown reached zero for user {user_id}")
            if user_id in delayed_polls:
                submit_delayed_poll(user_id, delayed_polls[user_id]["data"], message_id)
            return
        minutes = int(seconds_left // 60)
        seconds = int(seconds_left % 60)
        text = f"⏳ Ваш опрос будет отправлен на проверку через {minutes} мин {seconds} сек."
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_submit_{user_id}"))
        bot.edit_message_text(text=text, chat_id=user_id, message_id=message_id, reply_markup=markup)
        logging.info(f"Countdown updated for user {user_id}: {minutes} min {seconds} sec")
        threading.Timer(10, update_countdown, args=(user_id, submit_time, message_id)).start()
    except telebot.apihelper.ApiTelegramException as e:
        logging.error(f"Telegram API error updating countdown for user {user_id}: {e}")
        if e.error_code == 400:  # Message deleted
            if user_id in delayed_polls:
                del delayed_polls[user_id]
    except Exception as e:
        logging.error(f"Error updating countdown for user {user_id}: {e}")
        try:
            bot.edit_message_text(text="🚫 Ошибка таймера. Опрос не отправлен.", chat_id=user_id, message_id=message_id)
            if user_id in delayed_polls:
                del delayed_polls[user_id]
        except Exception as e2:
            logging.error(f"Error handling countdown error for user {user_id}: {e2}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("review_"))
def review_poll(call):
    user_id = str(call.message.chat.id)
    if user_id not in ADMIN_IDS:
        logging.warning(f"Unauthorized user {user_id} attempted to review poll")
        try:
            bot.answer_callback_query(call.id, text="🚫 У вас нет доступа к этой функции.")
        except Exception as e:
            logging.error(f"Error answering unauthorized callback for user {user_id}: {e}")
        return
    
    try:
        poll_index = int(call.data.split("_")[1])
        if poll_index >= len(pending) or poll_index < 0:
            bot.edit_message_text(text="🚫 Опрос не найден.", chat_id=user_id, message_id=call.message.message_id)
            logging.error(f"Invalid poll index {poll_index} accessed by admin {user_id}")
            bot.answer_callback_query(call.id)
            return
        
        data = pending[poll_index]
        text = (f"📊 *Опрос #{poll_index}*\n\n"
                f"👤 *{data['author']}* (ID: {data['user_id']})\n\n"
                f"1. {data['opt1']}\n2. {data['opt2']}\n3. {data['opt3']}\n4. {data['opt4']}")
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("✅ Принять", callback_data=f"accept_{poll_index}"))
        markup.add(InlineKeyboardButton("❌ Отклонить", callback_data=f"decline_{poll_index}"))
        bot.edit_message_text(text=text, chat_id=user_id, message_id=call.message.message_id, reply_markup=markup, parse_mode="Markdown")
        logging.info(f"Poll #{poll_index} displayed for review to admin {user_id}")
        bot.answer_callback_query(call.id)
    except Exception as e:
        logging.error(f"Error in review_poll for user {user_id}, callback {call.data}: {e}")
        try:
            bot.edit_message_text(text="🚫 Ошибка при просмотре опроса.", chat_id=user_id, message_id=call.message.message_id)
            bot.answer_callback_query(call.id, text="Ошибка при просмотре.")
        except Exception as e2:
            logging.error(f"Error handling review_poll error for user {user_id}: {e2}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("accept_") or call.data.startswith("decline_"))
def finalize(call):
    user_id = str(call.message.chat.id)
    if user_id not in ADMIN_IDS:
        logging.warning(f"Unauthorized user {user_id} attempted to finalize poll")
        try:
            bot.answer_callback_query(call.id, text="🚫 У вас нет доступа к этой функции.")
        except Exception as e:
            logging.error(f"Error answering unauthorized callback for user {user_id}: {e}")
        return
    
    try:
        action, poll_index = call.data.split("_")
        poll_index = int(poll_index)
        if poll_index >= len(pending) or poll_index < 0:
            bot.edit_message_text(text="🚫 Опрос не найден.", chat_id=user_id, message_id=call.message.message_id)
            logging.error(f"Invalid poll index {poll_index} in finalize by admin {user_id}")
            bot.answer_callback_query(call.id)
            return
        
        data = pending[poll_index]
        poll_user_id = data['user_id']
        
        if action == "accept":
            poll_text = (f"📊 Новый опрос от @{bot.get_chat(poll_user_id).username or 'anonymous'}!\n\n"
                         f"👤 *{data['author']}*\n\n"
                         f"1. {data['opt1']}\n2. {data['opt2']}\n3. {data['opt3']}\n4. {data['opt4']}")
            bot.send_message(chat_id=channel_id, text=poll_text, parse_mode="Markdown")
            bot.send_message(chat_id=poll_user_id, text="✅ Ваш опрос принят и опубликован в канале @oprosy_shegla!")
            bot.edit_message_text(text=f"✅ Опрос #{poll_index} принят и опубликован.", 
                                 chat_id=user_id, message_id=call.message.message_id)
            logging.info(f"Poll #{poll_index} accepted by admin {user_id}, published to channel {channel_id}")
        else:  # decline
            bot.send_message(chat_id=poll_user_id, text="❌ Ваш опрос был отклонён администратором.")
            bot.edit_message_text(text=f"❌ Опрос #{poll_index} отклонён.", 
                                 chat_id=user_id, message_id=call.message.message_id)
            logging.info(f"Poll #{poll_index} declined by admin {user_id}")
        
        pending.pop(poll_index)
        bot.answer_callback_query(call.id)
    except Exception as e:
        logging.error(f"Error in finalize for user {user_id}, callback {call.data}: {e}")
        try:
            bot.edit_message_text(text="🚫 Ошибка при обработке опроса.", chat_id=user_id, message_id=call.message.message_id)
            bot.answer_callback_query(call.id, text="Ошибка при обработке.")
        except Exception as e2:
            logging.error(f"Error handling finalize error for user {user_id}: {e2}")

# Start the bot
if __name__ == "__main__":
    try:
        logging.info("Bot starting...")
        bot.infinity_polling()
    except Exception as e:
        logging.error(f"Bot crashed: {e}")