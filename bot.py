import telebot
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice
import sqlite3
import json
from datetime import datetime, timedelta
import threading
import time
import random
from telebot import apihelper
import os

# Глобальный мьютекс для синхронизации доступа к БД
db_lock = threading.Lock()

# Отключаем прокси
apihelper.proxy = None

# Настройки бота
BOT_TOKEN = "8183458500:AAGQmjvSw0mg6EeWmmh8Ak5Y0FSgHf--YUI"
ADMIN_CHAT_ID = "7529123320"
MEDIA_CHANNEL_ID = "-1003354824243"
CACHE_DIR = 'media_cache'  # Название папки
MEDIA_CACHE_FILE = os.path.join(CACHE_DIR, 'media_cache.json')

bot = telebot.TeleBot(BOT_TOKEN)

# Замени текущую структуру search_queue на эту:
search_queue = {
    'random': [],
    'gender': {},
    'group_random': [],
    'female_seekers': [],
    'male_seekers': [],
    'available_females': [],
    'available_males': []
}

active_chats = {}
active_group_chats = {}  # Групповые чаты: {chat_id: {'users': [user_ids], 'type': 'random/female/male'}}
user_states = {}


ADVERTISEMENT_BOT = {
    'enabled': False,  # Статус бота
    'chance': 40,  # Шанс подключения в процентах
    'messages_sent': 0,  # Счетчик отправленных сообщений
    'ad_text': "Переходи в мой тгк - @skycashzy",  # Рекламный текст
    'hello_variants': [  # Варианты приветствий
        "Привет",
        "Приветик",
        "Пр",
        "Привет мд?",
        "Приветик мд?"
    ],
    'gender_variants': [  # Варианты сообщений о поле
        "Я девушка",
        "Я д, а ты?",
        "Девушка. А ты кто?",
        "д, а ты?",
        "Д"
    ],
    'active_sessions': {},  # Активные сессии: {user_id: {'chat_id': chat_id, 'message_index': 0, 'timers': []}}
    'user_connections': {},  # Добавлено: история подключений пользователей {user_id: [timestamp1, timestamp2, ...]}
    'current_user_id': None  # Добавлено: текущий проверяемый пользователь
}


# Инициализация базы данных
def init_db():
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            gender TEXT DEFAULT 'Не указан',
            age INTEGER DEFAULT 0,
            media_allowed BOOLEAN DEFAULT FALSE,
            interests TEXT DEFAULT '[]',
            premium BOOLEAN DEFAULT FALSE,
            premium_until TEXT,
            created_at TEXT,
            is_searching BOOLEAN DEFAULT FALSE,
            search_type TEXT DEFAULT 'random',
            search_filters TEXT DEFAULT '{}'
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user1_id INTEGER,
            user2_id INTEGER,
            started_at TEXT,
            ended_at TEXT,
            user1_ended BOOLEAN DEFAULT FALSE,
            user2_ended BOOLEAN DEFAULT FALSE
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS group_chats (
            chat_id INTEGER PRIMARY KEY AUTOINCREMENT,
            users TEXT,
            chat_type TEXT,
            started_at TEXT,
            is_active BOOLEAN DEFAULT TRUE
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            payment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount INTEGER,
            currency TEXT,
            stars INTEGER,
            status TEXT,
            created_at TEXT,
            telegram_payment_charge_id TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ratings (
            rating_id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_user_id INTEGER,
            to_user_id INTEGER,
            chat_id INTEGER,
            rating INTEGER,
            created_at TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS referrals (
            referral_id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER,
            referred_id INTEGER,
            registered_at TEXT,
            bonus_applied BOOLEAN DEFAULT FALSE
        )
    ''')

    conn.commit()
    conn.close()


# Функции для работы с реферальной системой
def get_user_referral_stats(user_id):
    """Получает статистику рефералов пользователя"""
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()

    cursor.execute('''
        SELECT
            COUNT(*) as invited,
            COUNT(CASE WHEN bonus_applied = TRUE THEN 1 END) as registered
        FROM referrals
        WHERE referrer_id = ?
    ''', (user_id,))

    result = cursor.fetchone()
    conn.close()

    return {
        'invited': result[0] if result else 0,
        'registered': result[1] if result else 0
    }


def add_referral(referrer_id, referred_id):
    """Добавляет реферала в базу данных"""
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()

    cursor.execute('''
        INSERT OR IGNORE INTO referrals (referrer_id, referred_id, registered_at)
        VALUES (?, ?, ?)
    ''', (referrer_id, referred_id, datetime.now().isoformat()))

    conn.commit()
    conn.close()


def apply_referral_bonus(referred_id):
    """Применяет бонус за регистрацию реферала"""
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()

    # Находим реферера
    cursor.execute('''
        SELECT referrer_id FROM referrals
        WHERE referred_id = ? AND bonus_applied = FALSE
    ''', (referred_id,))

    referral = cursor.fetchone()

    if referral:
        referrer_id = referral[0]

        # Добавляем 1 час премиума рефереру
        user = get_user(referrer_id)
        if user:
            if user['premium_until']:
                # Если уже есть премиум, добавляем 1 час
                premium_until = datetime.fromisoformat(user['premium_until'])
                new_premium_until = premium_until + timedelta(hours=1)
            else:
                # Если нет премиума, устанавливаем на 1 час
                new_premium_until = datetime.now() + timedelta(hours=1)

            cursor.execute('''
                UPDATE users SET premium = ?, premium_until = ?
                WHERE user_id = ?
            ''', (True, new_premium_until.isoformat(), referrer_id))

        # Отмечаем бонус как примененный
        cursor.execute('''
            UPDATE referrals SET bonus_applied = TRUE
            WHERE referred_id = ? AND referrer_id = ?
        ''', (referred_id, referrer_id))

    conn.commit()
    conn.close()

    return referrer_id if referral else None


# Функции для работы с оценками
def save_rating(from_user_id, to_user_id, chat_id, rating):
    """Сохраняет оценку пользователя в базу данных"""
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()

    # Проверяем, не оценивал ли уже пользователь этого собеседника в этом чате
    cursor.execute('''
        SELECT rating_id FROM ratings
        WHERE from_user_id = ? AND to_user_id = ? AND chat_id = ?
    ''', (from_user_id, to_user_id, chat_id))

    existing_rating = cursor.fetchone()

    if existing_rating:
        # Обновляем существующую оценку
        cursor.execute('''
            UPDATE ratings SET rating = ?, created_at = ?
            WHERE rating_id = ?
        ''', (rating, datetime.now().isoformat(), existing_rating[0]))
    else:
        # Создаем новую оценку
        cursor.execute('''
            INSERT INTO ratings (from_user_id, to_user_id, chat_id, rating, created_at)
            VALUES (?, ?, ?, ?, ?)
        ''', (from_user_id, to_user_id, chat_id, rating, datetime.now().isoformat()))

    conn.commit()
    conn.close()


def get_user_ratings(user_id):
    """Получает статистику оценок пользователя"""
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()

    cursor.execute('''
        SELECT
            COUNT(CASE WHEN rating = 1 THEN 1 END) as likes,
            COUNT(CASE WHEN rating = -1 THEN 1 END) as dislikes
        FROM ratings
        WHERE to_user_id = ?
    ''', (user_id,))

    result = cursor.fetchone()
    conn.close()

    return {
        'likes': result[0] if result else 0,
        'dislikes': result[1] if result else 0
    }


# Функция для создания клавиатуры с оценками
def rating_keyboard(chat_id):
    """Создает инлайн-клавиатуру для оценки собеседника"""
    keyboard = InlineKeyboardMarkup()
    keyboard.add(
        InlineKeyboardButton("👍", callback_data=f"rate_like_{chat_id}"),
        InlineKeyboardButton("👎", callback_data=f"rate_dislike_{chat_id}")
    )
    return keyboard


# Функции для работы с пользователями
def get_user(user_id):
    with db_lock:
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        user = cursor.fetchone()
        conn.close()

    if user:
        return {
            'user_id': user[0],
            'username': user[1],
            'first_name': user[2],
            'last_name': user[3],
            'gender': user[4],
            'age': user[5],
            'media_allowed': bool(user[6]),
            'interests': json.loads(user[7]),
            'premium': bool(user[8]),
            'premium_until': user[9],
            'created_at': user[10],
            'is_searching': bool(user[11]),
            'search_type': user[12],
            'search_filters': json.loads(user[13])
        }
    return None


def create_user(user_id, username, first_name, last_name):
    with db_lock:
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO users
            (user_id, username, first_name, last_name, media_allowed, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, username, first_name, last_name, 1, datetime.now().isoformat()))
        conn.commit()
        conn.close()


def update_user_profile(user_id, field, value):
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute(f'UPDATE users SET {field} = ? WHERE user_id = ?', (value, user_id))
    conn.commit()
    conn.close()


def set_user_searching(user_id, is_searching, search_type='random', filters=None):
    with db_lock:
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        filters_json = json.dumps(filters or {})
        cursor.execute('UPDATE users SET is_searching = ?, search_type = ?, search_filters = ? WHERE user_id = ?',
                       (int(is_searching), search_type, filters_json, user_id))
        conn.commit()
        conn.close()


def add_premium(user_id, days=30):
    with db_lock:
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        premium_until = (datetime.now() + timedelta(days=days)).isoformat()
        cursor.execute('UPDATE users SET premium = ?, premium_until = ? WHERE user_id = ?',
                       (True, premium_until, user_id))
        conn.commit()
        conn.close()


# Функции для чатов
def create_chat(user1_id, user2_id):
    with db_lock:
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO chats (user1_id, user2_id, started_at)
            VALUES (?, ?, ?)
        ''', (user1_id, user2_id, datetime.now().isoformat()))
        chat_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return chat_id


def create_group_chat(user_ids, chat_type):
    """Создает групповой чат в базе данных"""
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO group_chats (users, chat_type, started_at, is_active)
        VALUES (?, ?, ?, ?)
    ''', (json.dumps(user_ids), chat_type, datetime.now().isoformat(), True))
    chat_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return chat_id


def end_chat(chat_id, user_id):
    with db_lock:
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        cursor.execute('SELECT user1_id, user2_id FROM chats WHERE chat_id = ?', (chat_id,))
        chat = cursor.fetchone()

        if chat:
            user1_id, user2_id = chat[0], chat[1]

            if user_id == user1_id:
                cursor.execute('UPDATE chats SET user1_ended = TRUE WHERE chat_id = ?', (chat_id,))
            elif user_id == user2_id:
                cursor.execute('UPDATE chats SET user2_ended = TRUE WHERE chat_id = ?', (chat_id,))
            else:
                # Если пользователь не участник чата
                conn.close()
                return

            # Проверяем, оба ли пользователя завершили чат
            cursor.execute('SELECT user1_ended, user2_ended FROM chats WHERE chat_id = ?', (chat_id,))
            ended = cursor.fetchone()

            if ended and ended[0] and ended[1]:
                # ОБА пользователя завершили чат - УДАЛЯЕМ чат сразу
                cursor.execute('DELETE FROM chats WHERE chat_id = ?', (chat_id,))

        conn.commit()
        conn.close()


def end_group_chat(chat_id, user_id):
    """Завершает групповой чат для конкретного пользователя"""
    if chat_id in active_group_chats:
        if user_id in active_group_chats[chat_id]['users']:
            active_group_chats[chat_id]['users'].remove(user_id)

            # Если в чате остался 1 пользователь или меньше, закрываем чат
            if len(active_group_chats[chat_id]['users']) <= 1:
                # Уведомляем оставшихся пользователей
                for remaining_user_id in active_group_chats[chat_id]['users']:
                    try:
                        bot.send_message(
                            remaining_user_id,
                            "👥 <b>Групповой чат завершен</b>\n\n"
                            "<i>Все участники покинули чат</i>",
                            parse_mode='HTML',
                            reply_markup=main_menu_keyboard()
                        )
                    except:
                        pass
                # Удаляем чат
                del active_group_chats[chat_id]

                # Обновляем базу данных
                conn = sqlite3.connect('users.db')
                cursor = conn.cursor()
                cursor.execute('UPDATE group_chats SET is_active = FALSE WHERE chat_id = ?', (chat_id,))
                conn.commit()
                conn.close()


# Функции для рекламного бота
def should_connect_adbot():
    """Проверяет, нужно ли подключить рекламного бота к пользователю"""
    if not ADVERTISEMENT_BOT['enabled']:
        return False

    # Получаем user_id из контекста
    user_id = ADVERTISEMENT_BOT.get('current_user_id')
    if not user_id:
        return False

    # Проверяем историю подключений пользователя
    current_time = time.time()

    if user_id in ADVERTISEMENT_BOT['user_connections']:
        connections = ADVERTISEMENT_BOT['user_connections'][user_id]

        # Оставляем только подключения за последние 10 минут
        recent_connections = [conn_time for conn_time in connections
                              if current_time - conn_time < 600]  # 600 секунд = 10 минут

        # Если за последние 10 минут уже было 2 подключения, не подключаем
        if len(recent_connections) >= 2:
            return False

        # Обновляем список подключений
        ADVERTISEMENT_BOT['user_connections'][user_id] = recent_connections
    else:
        # Создаем запись для пользователя
        ADVERTISEMENT_BOT['user_connections'][user_id] = []

    # Проверяем шанс только если не превышен лимит
    return random.randint(1, 100) <= ADVERTISEMENT_BOT['chance']


def start_adbot_session(user_id):
    """Запускает сессию с рекламным ботом"""
    # Передаем user_id в глобальный контекст для should_connect_adbot
    ADVERTISEMENT_BOT['user_id'] = user_id

    # Проверяем, не находится ли пользователь уже в чате
    if user_id in active_chats:
        return False

    # Создаем фиктивный ID для бота (отрицательный, чтобы не пересекаться с реальными пользователями)
    adbot_id = -random.randint(1000, 9999)

    # Создаем чат в базе данных
    chat_id = create_chat(user_id, adbot_id)

    # Добавляем в активные чаты
    active_chats[user_id] = {'companion_id': adbot_id, 'chat_id': chat_id}
    active_chats[adbot_id] = {'companion_id': user_id, 'chat_id': chat_id}

    # Сохраняем сессию
    ADVERTISEMENT_BOT['active_sessions'][user_id] = {
        'chat_id': chat_id,
        'adbot_id': adbot_id,
        'message_index': 0,
        'timers': []
    }

    # Записываем время подключения
    current_time = time.time()
    if user_id not in ADVERTISEMENT_BOT['user_connections']:
        ADVERTISEMENT_BOT['user_connections'][user_id] = []

    ADVERTISEMENT_BOT['user_connections'][user_id].append(current_time)

    return True


def send_adbot_message(user_id):
    """Отправляет следующее сообщение рекламного бота"""
    if user_id not in ADVERTISEMENT_BOT['active_sessions']:
        return False

    session = ADVERTISEMENT_BOT['active_sessions'][user_id]
    message_index = session['message_index']

    # Выбираем сообщение в зависимости от индекса
    if message_index == 0:
        # Первое сообщение - приветствие
        message = random.choice(ADVERTISEMENT_BOT['hello_variants'])
    elif message_index == 1:
        # Второе сообщение - пол
        message = random.choice(ADVERTISEMENT_BOT['gender_variants'])
    elif message_index == 2:
        # Третье сообщение - реклама
        message = ADVERTISEMENT_BOT['ad_text']
    else:
        # Все сообщения отправлены, завершаем через 1 секунду
        threading.Timer(1.0, lambda: end_adbot_session(user_id, initiated_by_bot=True)).start()
        return False

    try:
        # Отправляем сообщение пользователю
        bot.send_message(user_id, message)
        session['message_index'] += 1

        # УВЕЛИЧИВАЕМ СЧЕТЧИК ТОЛЬКО ДЛЯ РЕКЛАМНОГО СООБЩЕНИЯ
        if message_index == 2:  # Это индекс рекламного сообщения
            ADVERTISEMENT_BOT['messages_sent'] += 1

        # Планируем следующее сообщение через 3 секунды
        if message_index < 2:  # Если еще не все сообщения отправлены
            timer = threading.Timer(3.0, lambda: send_adbot_message(user_id))
            timer.daemon = True
            timer.start()
            session['timers'].append(timer)
        else:
            # Это было последнее сообщение, завершаем через 1 секунду
            timer = threading.Timer(1.0, lambda: end_adbot_session(user_id, initiated_by_bot=True))
            timer.daemon = True
            timer.start()
            session['timers'].append(timer)

        return True
    except Exception as e:
        print(f"Error sending adbot message: {e}")
        end_adbot_session(user_id)
        return False


def end_adbot_session(user_id, initiated_by_bot=False):
    """Завершает сессию с рекламного бота"""
    if user_id not in ADVERTISEMENT_BOT['active_sessions']:
        return

    session = ADVERTISEMENT_BOT['active_sessions'][user_id]
    adbot_id = session['adbot_id']
    chat_id = session['chat_id']

    # Останавливаем все таймеры
    for timer in session['timers']:
        if timer.is_alive():
            try:
                timer.cancel()
            except:
                pass

    # Удаляем из активных чатов
    if user_id in active_chats:
        del active_chats[user_id]
    if adbot_id in active_chats:
        del active_chats[adbot_id]

    # Удаляем из очереди поиска
    remove_from_search_queue(user_id)
    set_user_searching(user_id, False)

    # Удаляем сессию
    del ADVERTISEMENT_BOT['active_sessions'][user_id]

    # Отправляем сообщение о завершении как с реальным собеседником
    try:
        if initiated_by_bot:
            # Бот завершил чат - показываем что собеседник завершил
            end_message = "<i>Диалог остановлен 😔\nОтправьте /next, чтобы начать поиск</i>"
            bot.send_message(
                user_id,
                end_message,
                parse_mode='HTML',
                reply_markup=rating_keyboard(chat_id)
            )
        else:
            # Пользователь завершил чат
            end_message = "<i>Диалог остановлен 😔\nОтправьте /next, чтобы начать поиск</i>"
            bot.send_message(
                user_id,
                end_message,
                parse_mode='HTML',
                reply_markup=rating_keyboard(chat_id)
            )
    except Exception as e:
        print(f"Error sending adbot end message: {e}")


def is_user_banned(user_id):
    """Проверяет, забанен ли пользователь"""
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()

    # Сначала проверяем наличие столбца banned_until
    cursor.execute("PRAGMA table_info(users)")
    columns = [col[1] for col in cursor.fetchall()]

    if 'banned_until' not in columns:
        conn.close()
        return False

    # Получаем информацию о бане
    cursor.execute('SELECT banned_until FROM users WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()

    if not result or result[0] is None:
        return False

    # Проверяем, истек ли бан
    banned_until = result[0]
    try:
        ban_time = datetime.fromisoformat(banned_until)
        if ban_time > datetime.now():
            # Бан еще действует
            return True
        else:
            # Бан истек
            # Можно автоматически очистить поле бана
            conn = sqlite3.connect('users.db')
            cursor = conn.cursor()
            cursor.execute('UPDATE users SET banned_until = NULL WHERE user_id = ?', (user_id,))
            conn.commit()
            conn.close()
            return False
    except ValueError:
        # Если дата в неверном формате
        return False


@bot.message_handler(func=lambda message: message.from_user.id in ADVERTISEMENT_BOT['active_sessions'])
def handle_adbot_chat_message(message):
    """Обрабатывает сообщения пользователя к рекламному боту"""
    user_id = message.from_user.id

    # Проверяем, активна ли сессия
    if user_id not in ADVERTISEMENT_BOT['active_sessions']:
        return

    # Если это команда /next или /stop, передаем ее в основной обработчик
    if message.text in ['/next', '/stop']:
        # Передаем обработку основному обработчику команд
        handle_chat_commands(message)
        return


# Функции поиска
def add_to_search_queue(user_id, search_type='random', filters=None):
    # Проверяем наличие бана у пользователя
    user = get_user(user_id)
    if not user:
        return False

    # 🔴 ИСПРАВЛЕНИЕ: Проверяем, забанен ли пользователь
    if is_user_banned(user_id):
        try:
            bot.send_message(
                user_id,
                "🚫 <b>Ваш аккаунт заблокирован!</b>\n\n"
                "<i>Вы не можете искать собеседников до снятия блокировки.</i>",
                parse_mode='HTML',
                reply_markup=main_menu_keyboard()
            )
        except:
            pass
        return 'banned'  # 🔴 Возвращаем специальное значение

    set_user_searching(user_id, True, search_type, filters)

    # 🔴 ПРОВЕРКА: Нужно ли подключить рекламного бота?
    # Передаем user_id в контекст для проверки ограничений
    ADVERTISEMENT_BOT['current_user_id'] = user_id

    if should_connect_adbot():
        if start_adbot_session(user_id):
            # Удаляем из очереди поиска
            remove_from_search_queue(user_id)

            # Формируем информацию о собеседнике (для премиум пользователей)
            companion_info = ""
            if user and user['premium']:
                # Создаем фейкового пользователя для рекламного бота
                fake_companion = {
                    'user_id': -1,  # Отрицательный ID для рекламного бота
                    'gender': 'Девушка',  # Можно указать любой пол
                    'age': 0,  # Не указан
                }
                companion_info = get_companion_info(user, fake_companion, user['premium'])

            # Формируем сообщение
            message = f"<b><i>Собеседник найден!</i></b>\n\n"
            if companion_info:
                message += f"{companion_info}\n\n"
            message += f"<i>/next - искать следующего\n/stop — закончить диалог</i>"

            # Отправляем сообщение о найденном собеседнике
            try:
                bot.send_message(
                    user_id,
                    message,
                    parse_mode='HTML'
                )

                # Запускаем отправку первого сообщения через 2 секунды
                timer = threading.Timer(2.0, lambda: send_adbot_message(user_id))
                timer.daemon = True
                timer.start()

                # Сохраняем таймер в сессию
                if user_id in ADVERTISEMENT_BOT['active_sessions']:
                    ADVERTISEMENT_BOT['active_sessions'][user_id]['timers'].append(timer)

                print(f"🤖 Рекламный бот подключен к пользователю {user_id}")

            except Exception as e:
                print(f"Error starting adbot: {e}")
                end_adbot_session(user_id)
                # Продолжаем обычный поиск
                return add_to_search_queue_without_adbot(user_id, search_type, filters)

            return True

    # Очищаем текущего пользователя из контекста
    ADVERTISEMENT_BOT['current_user_id'] = None

    # Сначала проверяем, есть ли подходящий собеседник ДО добавления в очередь
    companion_id = find_companion(user_id, search_type, filters)

    if companion_id:
        # Проверяем, не забанен ли потенциальный собеседник
        if is_user_banned(companion_id):
            # Если собеседник забанен, пропускаем его и продолжаем поиск
            print(f"Пропускаем забаненного пользователя {companion_id}")
        else:
            # Нашли подходящего собеседника - сразу создаем чат
            chat_id = create_chat(user_id, companion_id)
            active_chats[user_id] = {'companion_id': companion_id, 'chat_id': chat_id}
            active_chats[companion_id] = {'companion_id': user_id, 'chat_id': chat_id}

            # Удаляем из очереди поиска
            remove_from_search_queue(user_id)
            remove_from_search_queue(companion_id)

            # Получаем информацию о собеседнике
            companion_info1 = get_companion_info(user, get_user(companion_id), user['premium'])
            companion_info2 = get_companion_info(get_user(companion_id), user, get_user(companion_id)['premium'])

            # Формируем сообщения
            message1 = f"<b><i>Собеседник найден!</i></b>\n\n"
            if companion_info1:
                message1 += f"{companion_info1}\n\n"
            message1 += f"<i>/next - искать следующего\n/stop — закончить диалог</i>"

            message2 = f"<b><i>Собеседник найден!</i></b>\n\n"
            if companion_info2:
                message2 += f"{companion_info2}\n\n"
            message2 += f"<i>/next - искать следующего\n/stop — закончить диалог</i>"

            # Отправляем сообщения
            try:
                bot.send_message(user_id, message1, parse_mode='HTML')
                time.sleep(0.5)
                bot.send_message(companion_id, message2, parse_mode='HTML')
            except Exception as e:
                print(f"Error sending instant connection messages: {e}")
                # Если ошибка, удаляем из активных чатов
                if user_id in active_chats:
                    del active_chats[user_id]
                if companion_id in active_chats:
                    del active_chats[companion_id]
                # Продолжаем добавлять в очередь
            else:
                return True  # Чат создан, не добавляем в очередь

    # Если подходящего собеседника нет, добавляем в очередь
    if search_type == 'random':
        if user_id not in search_queue['random']:
            search_queue['random'].append(user_id)
    elif search_type == 'gender':
        gender_filter = filters.get('gender', 'any')
        if gender_filter not in search_queue['gender']:
            search_queue['gender'][gender_filter] = []
        if user_id not in search_queue['gender'][gender_filter]:
            search_queue['gender'][gender_filter].append(user_id)

    return False  # Добавлен в очередь, чат не создан


def add_to_search_queue_without_adbot(user_id, search_type='random', filters=None):
    """Оригинальная логика поиска без рекламного бота"""
    user = get_user(user_id)
    if not user:
        return False

    # Сначала проверяем, есть ли подходящий собеседник ДО добавления в очередь
    companion_id = find_companion(user_id, search_type, filters)

    if companion_id:
        # Нашли подходящего собеседника - сразу создаем чат
        chat_id = create_chat(user_id, companion_id)
        active_chats[user_id] = {'companion_id': companion_id, 'chat_id': chat_id}
        active_chats[companion_id] = {'companion_id': user_id, 'chat_id': chat_id}

        # Удаляем из очереди поиска
        remove_from_search_queue(user_id)
        remove_from_search_queue(companion_id)

        # Получаем информацию о собеседнике
        companion_info1 = get_companion_info(user, get_user(companion_id), user['premium'])
        companion_info2 = get_companion_info(get_user(companion_id), user, get_user(companion_id)['premium'])

        # Формируем сообщения
        message1 = f"<b><i>Собеседник найден!</i></b>\n\n"
        if companion_info1:
            message1 += f"{companion_info1}\n\n"
        message1 += f"<i>/next - искать следующего\n/stop — закончить диалог</i>"

        message2 = f"<b><i>Собеседник найден!</i></b>\n\n"
        if companion_info2:
            message2 += f"{companion_info2}\n\n"
        message2 += f"<i>/next - искать следующего\n/stop — закончить диалог</i>"

        # Отправляем сообщения
        try:
            bot.send_message(user_id, message1, parse_mode='HTML')
            time.sleep(0.5)
            bot.send_message(companion_id, message2, parse_mode='HTML')
        except Exception as e:
            print(f"Error sending instant connection messages: {e}")
            # Если ошибка, удаляем из активных чатов
            if user_id in active_chats:
                del active_chats[user_id]
            if companion_id in active_chats:
                del active_chats[companion_id]
            # Продолжаем добавлять в очередь
        else:
            return True  # Чат создан, не добавляем в очередь

    # Если подходящего собеседника нет, добавляем в очередь
    if search_type == 'random':
        if user_id not in search_queue['random']:
            search_queue['random'].append(user_id)
    elif search_type == 'gender':
        gender_filter = filters.get('gender', 'any')
        if gender_filter not in search_queue['gender']:
            search_queue['gender'][gender_filter] = []
        if user_id not in search_queue['gender'][gender_filter]:
            search_queue['gender'][gender_filter].append(user_id)

    return False  # Добавлен в очередь, чат не создан


def try_create_group_chat(group_type):
    """Пытается создать групповой чат с правильными приоритетами"""

    def filter_users_in_chats(user_ids):
        filtered_ids = []
        for user_id in user_ids:
            user_in_chat = any(user_id in chat_data['users'] for chat_data in active_group_chats.values())
            if not user_in_chat:
                filtered_ids.append(user_id)
        return filtered_ids

    # 🔴 ПЕРВЫЙ ПРИОРИТЕТ: дозаполнение существующих чатов
    if try_fill_existing_chats():
        return True

    # 🔴 ВТОРОЙ ПРИОРИТЕТ: создание новых чатов
    # 1. male_seekers (парни ищут девушек)
    if search_queue['male_seekers']:
        male_seeker = search_queue['male_seekers'][0]
        male_in_chat = any(male_seeker in chat_data['users'] for chat_data in active_group_chats.values())

        if not male_in_chat:
            female_candidates = []

            # ПРИОРИТЕТ 1: Девушки из female_seekers
            available_female_seekers = filter_users_in_chats(search_queue['female_seekers'])
            if available_female_seekers:
                female_candidates.extend(available_female_seekers[:2])

            # ПРИОРИТЕТ 2: Девушки из случайного поиска
            needed = 2 - len(female_candidates)
            if needed > 0 and search_queue['available_females']:
                available_females = filter_users_in_chats(search_queue['available_females'])
                available_females = [uid for uid in available_females if uid not in female_candidates]
                female_candidates.extend(available_females[:needed])

            if len(female_candidates) >= 1:
                user_ids = [male_seeker] + female_candidates[:2]
                print(f"🔴 СОЗДАЕМ male_seekers чат: парень {male_seeker} + девушки {female_candidates}")
                return create_group_chat_instance(user_ids, 'male_seekers')

    # 2. female_seekers (девушки ищут парней)
    if search_queue['female_seekers']:
        female_seeker = search_queue['female_seekers'][0]
        female_in_chat = any(female_seeker in chat_data['users'] for chat_data in active_group_chats.values())

        if not female_in_chat:
            male_candidates = []

            # ПРИОРИТЕТ 1: Парни из male_seekers
            available_male_seekers = filter_users_in_chats(search_queue['male_seekers'])
            if available_male_seekers:
                male_candidates.extend(available_male_seekers[:2])

            # ПРИОРИТЕТ 2: Парни из случайного поиска
            needed = 2 - len(male_candidates)
            if needed > 0 and search_queue['available_males']:
                available_males = filter_users_in_chats(search_queue['available_males'])
                available_males = [uid for uid in available_males if uid not in male_candidates]
                male_candidates.extend(available_males[:needed])

            if len(male_candidates) >= 1:
                user_ids = [female_seeker] + male_candidates[:2]
                print(f"🔴 СОЗДАЕМ female_seekers чат: девушка {female_seeker} + парни {male_candidates}")
                return create_group_chat_instance(user_ids, 'female_seekers')

    # 3. group_random (только остатки)
    if group_type == 'group_random' and len(search_queue['group_random']) >= 2:
        available_users = []
        for user_id in search_queue['group_random'][:3]:
            if any(user_id in chat_data['users'] for chat_data in active_group_chats.values()):
                continue
            available_users.append(user_id)

        if len(available_users) >= 2:
            print(f"🔴 СОЗДАЕМ group_random чат из оставшихся: {available_users}")
            return create_group_chat_instance(available_users, 'group_random')

    return False


def add_to_group_search_queue(user_id, group_type):
    """Добавляет пользователя в очередь группового поиска с новой логикой"""
    # Проверяем наличие бана
    if is_user_banned(user_id):
        try:
            bot.send_message(
                user_id,
                "🚫 <b>Ваш аккаунт заблокирован!</b>\n\n"
                "<i>Вы не можете искать собеседников до снятия блокировки.</i>",
                parse_mode='HTML',
                reply_markup=main_menu_keyboard()
            )
        except:
            pass
        return 'banned'  # 🔴 Возвращаем специальное значение

    user = get_user(user_id)
    if not user:
        return False

    # 🔴 УСИЛЕННАЯ ПРОВЕРКА: если пользователь уже в активном групповом чате
    for chat_id, chat_data in active_group_chats.items():
        if user_id in chat_data['users']:
            try:
                bot.send_message(
                    user_id,
                    "❌ <b>Вы уже находитесь в групповом чате!</b>\n\n"
                    "<i>Используйте /gstop чтобы выйти из текущего чата перед началом нового поиска.</i>",
                    parse_mode='HTML'
                )
            except:
                pass
            return False

    # 🔴 ДОПОЛНИТЕЛЬНАЯ ПРОВЕРКА: если пользователь уже в очереди этого типа
    if group_type == 'male_seekers' and user_id in search_queue['male_seekers']:
        bot.send_message(
            user_id,
            "⏳ <b>Вы уже в очереди поиска девушек!</b>\n\n"
            "<i>Ожидайте подключения к групповому чату...</i>",
            parse_mode='HTML'
        )
        return False

    if group_type == 'female_seekers' and user_id in search_queue['female_seekers']:
        bot.send_message(
            user_id,
            "⏳ <b>Вы уже в очереди поиска парней!</b>\n\n"
            "<i>Ожидайте подключения к групповому чату...</i>",
            parse_mode='HTML'
        )
        return False

    if group_type == 'group_random' and user_id in search_queue['group_random']:
        bot.send_message(
            user_id,
            "⏳ <b>Вы уже в очереди случайного поиска!</b>\n\n"
            "<i>Ожидайте подключения к групповому чату...</i>",
            parse_mode='HTML'
        )
        return False

    # Очищаем пользователя из всех очередей
    remove_from_search_queue(user_id)
    remove_from_group_search_queue(user_id)

    # 🔴 СТРОГАЯ ПРОВЕРКА СООТВЕТСТВИЯ ПОЛА И ТИПА ПОИСКА
    if group_type == 'male_seekers':
        if user['gender'] != 'Парень':
            bot.send_message(
                user_id,
                "❌ <b>Поиск девушек доступен только для парней!</b>",
                parse_mode='HTML'
            )
            return False
        # Парни ищут девушек - добавляем в male_seekers
        if user_id not in search_queue['male_seekers']:
            search_queue['male_seekers'].append(user_id)

    elif group_type == 'female_seekers':
        if user['gender'] != 'Девушка':
            bot.send_message(
                user_id,
                "❌ <b>Поиск парней доступен только для девушек!</b>",
                parse_mode='HTML'
            )
            return False
        # Девушки ищут парней - добавляем в female_seekers
        if user_id not in search_queue['female_seekers']:
            search_queue['female_seekers'].append(user_id)

    elif group_type == 'group_random':
        # Случайный поиск - добавляем в group_random
        if user_id not in search_queue['group_random']:
            search_queue['group_random'].append(user_id)

        # Также добавляем в available очереди для приоритетного соединения
        if user['gender'] == 'Девушка':
            if user_id not in search_queue['available_females']:
                search_queue['available_females'].append(user_id)
        elif user['gender'] == 'Парень':
            if user_id not in search_queue['available_males']:
                search_queue['available_males'].append(user_id)

    set_user_searching(user_id, True, f'group_{group_type}')

    # 🔴 ПРИОРИТЕТНАЯ ЛОГИКА: сначала пытаемся добавить в существующий чат
    if add_user_to_existing_group_chat(user_id, group_type):
        return True

    # 🔴 Затем пытаемся создать новый чат по приоритетам
    chat_created = try_create_group_chat(group_type)
    if chat_created:
        return True

    # Запускаем фоновый поиск
    threading.Thread(target=group_search_companion, args=(user_id, group_type)).start()
    return False


def try_fill_existing_chats():
    """Пытается дозаполнить существующие чаты перед созданием новых"""

    # 🔴 ИСПРАВЛЕНИЕ: Создаем копию списка ключей для безопасной итерации
    chat_ids = list(active_group_chats.keys())

    # 🔴 ВЫСШИЙ ПРИОРИТЕТ: дозаполнение male_seekers чатов
    for chat_id in chat_ids:
        # Проверяем, что чат все еще существует
        if chat_id not in active_group_chats:
            continue

        chat_data = active_group_chats[chat_id]

        if chat_data['type'] == 'male_seekers' and len(chat_data['users']) < 3:
            current_users = chat_data['users']

            # Находим парня в чате
            male_in_chat = None
            for user_id in current_users:
                user = get_user(user_id)
                if user and user['gender'] == 'Парень':
                    male_in_chat = user_id
                    break

            if not male_in_chat:
                continue

            # Ищем девушек для дозаполнения
            female_candidates = []

            # ПРИОРИТЕТ 1: Девушки из female_seekers
            for female_seeker in search_queue['female_seekers']:
                if (female_seeker not in current_users and
                        not any(female_seeker in c['users'] for c in active_group_chats.values())):
                    female_candidates.append(female_seeker)
                    if len(female_candidates) >= (3 - len(current_users)):
                        break

            # ПРИОРИТЕТ 2: Девушки из случайного поиска
            needed = (3 - len(current_users)) - len(female_candidates)
            if needed > 0:
                for available_female in search_queue['available_females']:
                    if (available_female not in current_users and
                            available_female not in female_candidates and
                            not any(available_female in c['users'] for c in active_group_chats.values())):
                        female_candidates.append(available_female)
                        if len(female_candidates) >= (3 - len(current_users)):
                            break

            # ДОБАВЛЯЕМ найденных девушек в чат
            for female_id in female_candidates:
                if len(chat_data['users']) < 3:
                    chat_data['users'].append(female_id)

                    # Обновляем БД
                    conn = sqlite3.connect('users.db')
                    cursor = conn.cursor()
                    cursor.execute('SELECT users FROM group_chats WHERE chat_id = ?', (chat_id,))
                    result = cursor.fetchone()
                    if result:
                        current_users_db = json.loads(result[0])
                        current_users_db.append(female_id)
                        cursor.execute('UPDATE group_chats SET users = ? WHERE chat_id = ?',
                                       (json.dumps(current_users_db), chat_id))
                        conn.commit()
                    conn.close()

                    # Удаляем из очередей
                    remove_from_group_search_queue(female_id)
                    set_user_searching(female_id, False)

                    # Уведомляем участников
                    notify_group_chat_join(chat_id, female_id, chat_data)
                    print(f"🔴 ДОЗАПОЛНЕНИЕ: девушка {female_id} добавлена в male_seekers чат {chat_id}")
                    return True

    # 🔴 ВТОРОЙ ПРИОРИТЕТ: дозаполнение female_seekers чатов
    for chat_id in chat_ids:
        # Проверяем, что чат все еще существует
        if chat_id not in active_group_chats:
            continue

        chat_data = active_group_chats[chat_id]

        if chat_data['type'] == 'female_seekers' and len(chat_data['users']) < 3:
            current_users = chat_data['users']

            # Находим девушку в чате
            female_in_chat = None
            for user_id in current_users:
                user = get_user(user_id)
                if user and user['gender'] == 'Девушка':
                    female_in_chat = user_id
                    break

            if not female_in_chat:
                continue

            # Ищем парней для дозаполнения
            male_candidates = []

            # ПРИОРИТЕТ 1: Парни из male_seekers
            for male_seeker in search_queue['male_seekers']:
                if (male_seeker not in current_users and
                        not any(male_seeker in c['users'] for c in active_group_chats.values())):
                    male_candidates.append(male_seeker)
                    if len(male_candidates) >= (3 - len(current_users)):
                        break

            # ПРИОРИТЕТ 2: Парни из случайного поиска
            needed = (3 - len(current_users)) - len(male_candidates)
            if needed > 0:
                for available_male in search_queue['available_males']:
                    if (available_male not in current_users and
                            available_male not in male_candidates and
                            not any(available_male in c['users'] for c in active_group_chats.values())):
                        male_candidates.append(available_male)
                        if len(male_candidates) >= (3 - len(current_users)):
                            break

            # ДОБАВЛЯЕМ найденных парней в чат
            for male_id in male_candidates:
                if len(chat_data['users']) < 3:
                    chat_data['users'].append(male_id)

                    # Обновляем БД
                    conn = sqlite3.connect('users.db')
                    cursor = conn.cursor()
                    cursor.execute('SELECT users FROM group_chats WHERE chat_id = ?', (chat_id,))
                    result = cursor.fetchone()
                    if result:
                        current_users_db = json.loads(result[0])
                        current_users_db.append(male_id)
                        cursor.execute('UPDATE group_chats SET users = ? WHERE chat_id = ?',
                                       (json.dumps(current_users_db), chat_id))
                        conn.commit()
                    conn.close()

                    # Удаляем из очередей
                    remove_from_group_search_queue(male_id)
                    set_user_searching(male_id, False)

                    # Уведомляем участников
                    notify_group_chat_join(chat_id, male_id, chat_data)
                    print(f"🔴 ДОЗАПОЛНЕНИЕ: парень {male_id} добавлен в female_seekers чат {chat_id}")
                    return True

    return False


def create_group_chat_instance(user_ids, chat_type):
    """Создает экземпляр группового чата"""

    # 🔴 УСИЛЕННАЯ ПРОВЕРКА: проверяем, что пользователи не находятся в других чатах
    final_user_ids = []
    for user_id in user_ids:
        user_in_other_chat = False
        for chat_id, chat_data in active_group_chats.items():
            if user_id in chat_data['users']:
                user_in_other_chat = True
                print(f"User {user_id} already in chat {chat_id}, skipping")
                break

        if not user_in_other_chat:
            final_user_ids.append(user_id)
        else:
            # Удаляем пользователя из очередей, так как он уже в чате
            remove_from_group_search_queue(user_id)

    # Если после проверки осталось меньше 2 пользователей, не создаем чат
    if len(final_user_ids) < 2:
        print(f"Not enough users for group chat: {len(final_user_ids)} users after filtering")
        return False

    # 🔴 ДОПОЛНИТЕЛЬНАЯ ПРОВЕРКА: убеждаемся, что пользователи еще в очередях
    available_user_ids = []
    for user_id in final_user_ids:
        if (user_id in search_queue['group_random'] or
            user_id in search_queue['female_seekers'] or
            user_id in search_queue['male_seekers'] or
            user_id in search_queue['available_females'] or
            user_id in search_queue['available_males']):
            available_user_ids.append(user_id)
        else:
            print(f"User {user_id} not in search queues anymore")

    if len(available_user_ids) < 2:
        print(f"Not enough users in search queues: {len(available_user_ids)}")
        return False

    chat_id = create_group_chat(available_user_ids, chat_type)
    active_group_chats[chat_id] = {
        'users': available_user_ids.copy(),
        'type': chat_type
    }

    # Удаляем пользователей из всех очередей
    for user_id in available_user_ids:
        remove_from_group_search_queue(user_id)
        set_user_searching(user_id, False)

    print(f"Created group chat {chat_id} with users: {available_user_ids}")

    # Отправляем уведомления
    notify_group_chat_users(chat_id, available_user_ids, chat_type)
    return True


def notify_group_chat_users(chat_id, user_ids, group_type):
    """Уведомляет пользователей о создании группового чата"""
    for user_id in user_ids:
        try:
            user = get_user(user_id)
            if not user:
                continue

            # Определяем название типа чата в зависимости от премиум статуса
            if group_type == 'group_random':
                chat_type_name = "Случайные собеседники"
            elif group_type == 'female_seekers':
                # Для девушек, ищущих парней
                if user['premium']:
                    chat_type_name = "Парни"  # Девушка видит что ищет парней
                else:
                    chat_type_name = "Случайные собеседники"
            elif group_type == 'male_seekers':
                # Для парней, ищущих девушек
                if user['premium']:
                    chat_type_name = "Девушки"  # Парень видит что ищет девушек
                else:
                    chat_type_name = "Случайные собеседники"
            else:
                chat_type_name = "Случайные собеседники"

            message = (
                f"👥 <b>Групповой чат создан!</b>\n\n"
                f"<b>Тип:</b> {chat_type_name}\n"
                f"<b>Участников:</b> {len(user_ids)}\n\n"
                f"<i>/gstop - Покинуть групповой чат</i>"
            )

            bot.send_message(user_id, message, parse_mode='HTML')
        except Exception as e:
            print(f"Error notifying group user {user_id}: {e}")


def remove_from_search_queue(user_id):
    set_user_searching(user_id, False)

    # Удаляем из всех очередей
    if user_id in search_queue['random']:
        search_queue['random'].remove(user_id)

    for gender in search_queue['gender']:
        if user_id in search_queue['gender'][gender]:
            search_queue['gender'][gender].remove(user_id)


def remove_from_group_search_queue(user_id):
    """Удаляет пользователя из всех групповых очередей"""
    # Удаляем из основных очередей
    for queue_name in ['group_random', 'female_seekers', 'male_seekers', 'available_females', 'available_males']:
        if user_id in search_queue[queue_name]:
            search_queue[queue_name].remove(user_id)

    # Удаляем из всех остальных очередей
    if user_id in search_queue['random']:
        search_queue['random'].remove(user_id)

    for gender in list(search_queue['gender'].keys()):
        if user_id in search_queue['gender'][gender]:
            search_queue['gender'][gender].remove(user_id)

    set_user_searching(user_id, False)


def find_companion(user_id, search_type='random', filters=None):
    user = get_user(user_id)
    if not user:
        return None

    # Получаем все возможные candidate ID из разных очередей
    candidate_ids = set()

    if search_type == 'random':
        candidate_ids.update(search_queue['random'])
    elif search_type == 'gender':
        gender_filter = filters.get('gender', 'any')
        if gender_filter in search_queue['gender']:
            candidate_ids.update(search_queue['gender'][gender_filter])
        candidate_ids.update(search_queue['random'])  # Добавляем случайный поиск

    # Также добавляем пользователей из других типов поиска, которые могут подойти
    for gender_queue in search_queue['gender'].values():
        candidate_ids.update(gender_queue)

    # Убираем текущего пользователя
    candidate_ids.discard(user_id)

    for companion_id in candidate_ids:
        companion = get_user(companion_id)
        if not companion:
            continue

        # Проверяем совместимость в ОБЕ стороны
        if (check_compatibility(user, companion, search_type, filters) and
                check_compatibility(companion, user, companion['search_type'], companion['search_filters'])):
            return companion_id

    return None


def check_compatibility(user1, user2, search_type='random', filters=None):
    # Проверка базовой совместимости
    if user1['user_id'] == user2['user_id']:
        return False

    # Проверка поиска по полу
    if search_type == 'gender':
        gender_filter = filters.get('gender', 'any')
        if gender_filter != 'any':
            # Преобразуем фильтр в читаемый формат
            gender_map = {
                'female': 'Девушка',
                'male': 'Парень'
            }
            desired_gender = gender_map.get(gender_filter)
            if desired_gender and user2['gender'] != desired_gender:
                return False
        # Если фильтр 'any' - подходит любой пол

    # Для случайного поиска - подходят все
    elif search_type == 'random':
        # Случайный поиск подходит для всех
        pass

    return True


def get_companion_info(user, companion, show_detailed_info=False):
    """Получить информацию о собеседнике с учетом премиум статуса"""
    if show_detailed_info:
        # Подробная информация для премиум пользователей - показываем даже если данные не указаны
        info_lines = []

        # Всегда показываем пол
        if companion['gender'] != 'Не указан':
            info_lines.append(f"<i>Пол: {companion['gender']}</i>")
        else:
            info_lines.append("<i>Пол: Не указан</i>")

        # Всегда показываем возраст
        if companion['age'] > 0:
            info_lines.append(f"<i>Возраст: {companion['age']}</i>")
        else:
            info_lines.append("<i>Возраст: Не указан</i>")

        return "\n".join(info_lines) if info_lines else ""
    else:
        # Для обычных пользователей не показываем информацию
        return ""


def save_media_to_file(file_id, media_type, user_id, caption=""):
    """Сохраняет медиа в JSON-файл"""
    try:
        # Создаем папку если ее нет
        if not os.path.exists(CACHE_DIR):
            os.makedirs(CACHE_DIR)

        new_item = {
            'file_id': file_id,
            'type': media_type,
            'user_id': user_id,
            'caption': caption or "",
            'timestamp': time.time()
        }

        # Читаем существующий файл
        items = []
        if os.path.exists(MEDIA_CACHE_FILE) and os.path.getsize(MEDIA_CACHE_FILE) > 0:
            try:
                with open(MEDIA_CACHE_FILE, 'r', encoding='utf-8') as f:
                    items = json.load(f)
            except:
                items = []

        # Добавляем новый элемент
        items.append(new_item)

        # Ограничиваем размер (оставляем последние 1000)
        if len(items) > 1000:
            items = items[-1000:]

        # Сохраняем обратно
        with open(MEDIA_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(items, f, ensure_ascii=False, indent=2)

        return True

    except Exception as e:
        print(f"❌ Ошибка сохранения медиа в кэш: {e}")
        return False


# Клавиатуры
def main_menu_keyboard():
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(KeyboardButton("🎯 Начать диалог"))
    keyboard.add(KeyboardButton("👥 Групповой поиск"))
    keyboard.add(KeyboardButton("🔍 Поиск по полу"), KeyboardButton("⚙️ Профиль"))
    return keyboard


def group_search_keyboard():
    """Клавиатура для группового поиска"""
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(KeyboardButton("🎲 Случайные собеседники"))
    keyboard.add(KeyboardButton("🙋‍♀️ Найти девушек"), KeyboardButton("🙋‍♂️ Найти парней"))
    keyboard.add(KeyboardButton("🔙 Назад"))
    return keyboard


def settings_keyboard():
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(KeyboardButton("👤 Пол"), KeyboardButton("🔞 Возраст"))
    keyboard.add(KeyboardButton("📷 Фото/Видео"))
    keyboard.add(KeyboardButton("🔙 Назад"))
    return keyboard


def gender_keyboard():
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(KeyboardButton("🙋‍♀️ Я девушка"), KeyboardButton("🙋‍♂️ Я парень"))
    keyboard.add(KeyboardButton("🔙 Назад"))
    return keyboard


def media_keyboard():
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(KeyboardButton("✅ Медиа разрешено"), KeyboardButton("❌ Медиа запрещено"))
    keyboard.add(KeyboardButton("🔙 Назад"))
    return keyboard


def chat_keyboard():
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(KeyboardButton("/next"), KeyboardButton("/stop"))
    return keyboard


def group_chat_keyboard():
    """Клавиатура для группового чата"""
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add(KeyboardButton("/gstop"))
    return keyboard


def search_gender_keyboard():
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    keyboard.add(KeyboardButton("🙋‍♀️ Найти девушку"), KeyboardButton("🙋‍♂️ Найти парня"))
    keyboard.add(KeyboardButton("🎭 Любой пол"), KeyboardButton("🔙 Назад"))
    return keyboard


def premium_required_keyboard():
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("💎 1 день - 49 Stars", callback_data="premium_1day"))
    keyboard.add(InlineKeyboardButton("💎 1 неделя - 99 Stars", callback_data="premium_7days"))
    keyboard.add(InlineKeyboardButton("💎 1 месяц - 149 Stars", callback_data="premium_30days"))
    return keyboard


# Обработчики команд
@bot.message_handler(commands=['start'])
def start_command(message):
    user_id = message.from_user.id
    user = get_user(user_id)

    # Проверяем реферальную ссылку
    referral_id = None
    if len(message.text.split()) > 1:
        try:
            referral_id = int(message.text.split()[1])
        except ValueError:
            pass

    if not user:
        create_user(user_id, message.from_user.username,
                    message.from_user.first_name, message.from_user.last_name)

        # Если есть реферал и это не сам пользователь
        if referral_id and referral_id != user_id:
            add_referral(referral_id, user_id)
            # Применяем бонус сразу
            referrer_id = apply_referral_bonus(user_id)
            if referrer_id:
                # Уведомляем реферера о новом реферале с обновленным текстом
                try:
                    bot.send_message(
                        referrer_id,
                        "<b>🤖 По Вашей ссылке кто-то зарегистрировался, тебе начислен 1 Час  💎 <i>PREMIUM</i> статуса</b>",
                        parse_mode='HTML'
                    )
                except:
                    pass

    bot.send_message(
        message.chat.id,
        "👋 <b>Добро пожаловать в анонимный чат!</b>\n\n"
        "✨ <b>Возможности:</b>\n"
        "• 🎯 Случайный диалог\n"
        "• 👥 Групповой поиск\n"
        "• 🔍 Поиск по полу\n"
        "• ⚙️ Настройка профиля\n\n"
        "<i>Расширенный поиск доступен с премиумом /vip</i> 💎",
        parse_mode='HTML',
        reply_markup=main_menu_keyboard()
    )


@bot.message_handler(commands=['vip'])
def vip_command(message):
    bot.send_message(
        message.chat.id,
        "💎 <b>Премиум подписка</b>\n\n"
        "✨ <b>Преимущества VIP статуса:</b>\n"
        "• 🔍 Поиск по полу\n"
        "• 👥 Групповой поиск по полу\n"
        "• 🚀 Приоритет в поиске\n"
        "• 📊 Подробная информация о собеседниках\n\n"
        "💫 <b>Выберите вариант подписки:</b>",
        parse_mode='HTML',
        reply_markup=premium_required_keyboard()
    )


@bot.message_handler(commands=['next', 'stop'])
def handle_chat_commands(message):
    user_id = message.from_user.id

    # Получаем информацию о пользователе
    user = get_user(user_id)

    # Защита от двойного выполнения
    current_time = time.time()
    if user_id in user_states and 'last_command_time' in user_states[user_id]:
        last_time = user_states[user_id]['last_command_time']
        if current_time - last_time < 1:  # Защита от спама - 1 секунда
            return

    if user_id not in user_states:
        user_states[user_id] = {}
    user_states[user_id]['last_command_time'] = current_time

    if message.text == '/next':
        # Проверяем, не является ли это чат с рекламным ботом
        if user_id in ADVERTISEMENT_BOT['active_sessions']:
            end_adbot_session(user_id)
            start_random_search(message)
            return

        # Проверяем, указан ли пол
        if user and user['gender'] == 'Не указан':
            bot.send_message(
                message.chat.id,
                "Укажите ваш пол (Настройки профиля → Пол).",
                reply_markup=main_menu_keyboard()
            )
            return

        # Завершаем текущий чат и начинаем новый поиск
        if user_id in active_chats:
            end_chat_direct(user_id, initiated_by_user=True)
        start_random_search(message)

    elif message.text == '/stop':
        # Проверяем, не является ли это чат с рекламным ботом
        if user_id in ADVERTISEMENT_BOT['active_sessions']:
            end_adbot_session(user_id)
            # Сообщение уже отправлено в end_adbot_session, поэтому не отправляем повторно
            return

        # Проверяем состояние пользователя
        if user_id in active_chats:
            # Пользователь в диалоге - завершаем диалог
            # Сообщение отправится внутри end_chat_direct, поэтому не отправляем здесь
            end_chat_direct(user_id, initiated_by_user=True)
        elif user and user['is_searching']:
            # Пользователь в поиске - отменяем поиск
            remove_from_search_queue(user_id)
            bot.send_message(user_id,
                             "<i>Поиск остановлен ⛔️\nОтправьте /next, чтобы начать поиск</i>",
                             parse_mode='HTML',
                             reply_markup=main_menu_keyboard())
        else:
            # Пользователь не в диалоге и не в поиске
            bot.send_message(user_id,
                             "<i>У вас нет собеседника 😐\nОтправьте /next, чтобы начать поиск</i>",
                             parse_mode='HTML',
                             reply_markup=main_menu_keyboard())


@bot.message_handler(commands=['gstop'])
def handle_group_stop_command(message):
    """Обработчик команды выхода из группового чата"""
    user_id = message.from_user.id

    # Полностью останавливаем поиск перед всеми действиями
    stop_user_search_completely(user_id)

    # Ищем пользователя в активных групповых чатах
    user_chat_id = None
    chat_data_to_remove = None

    for chat_id, chat_data in active_group_chats.items():
        if user_id in chat_data['users']:
            user_chat_id = chat_id
            chat_data_to_remove = chat_data
            break

    if user_chat_id:
        # Удаляем пользователя из активного чата
        chat_data_to_remove['users'].remove(user_id)

        # Обновляем базу данных
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()

        if len(chat_data_to_remove['users']) > 0:
            cursor.execute('UPDATE group_chats SET users = ? WHERE chat_id = ?',
                           (json.dumps(chat_data_to_remove['users']), user_chat_id))
        else:
            cursor.execute('UPDATE group_chats SET is_active = FALSE WHERE chat_id = ?', (user_chat_id,))

        conn.commit()
        conn.close()

        # Уведомляем других участников
        remaining_users_after = chat_data_to_remove['users'].copy()

        for remaining_user_id in remaining_users_after:
            try:
                bot.send_message(
                    remaining_user_id,
                    f"👤 <b>Участник покинул чат</b>\n\n"
                    f"<i>В групповом чате осталось {len(remaining_users_after)} участников</i>",
                    parse_mode='HTML'
                )
            except:
                pass

        # Если в чате остался 1 пользователь или меньше, закрываем чат полностью
        if len(remaining_users_after) <= 1:
            for remaining_user_id in remaining_users_after:
                try:
                    bot.send_message(
                        remaining_user_id,
                        "👥 <b>Групповой чат завершен</b>\n\n"
                        "<i>Все участники покинули чат</i>",
                        parse_mode='HTML',
                        reply_markup=main_menu_keyboard()
                    )
                    stop_user_search_completely(remaining_user_id)
                except:
                    pass

            if user_chat_id in active_group_chats:
                del active_group_chats[user_chat_id]

        bot.send_message(
            user_id,
            "👥 <b>Вы покинули групповой чат</b>\n\n"
            "<i>Вернитесь в главное меню для нового поиска</i>",
            parse_mode='HTML',
            reply_markup=main_menu_keyboard()
        )

    else:
        bot.send_message(
            user_id,
            "❌ <b>Вы не находитесь в групповом чате</b>",
            parse_mode='HTML',
            reply_markup=main_menu_keyboard()
        )


def stop_user_search_completely(user_id):
    """Полностью останавливает поиск пользователя"""
    # Удаляем из всех очередей
    remove_from_group_search_queue(user_id)
    remove_from_search_queue(user_id)

    # Останавливаем поиск в базе данных
    set_user_searching(user_id, False)

    # 🔴 ДОПОЛНИТЕЛЬНО: очищаем состояние пользователя
    if user_id in user_states:
        user_states[user_id].clear()

    print(f"🔴 ПОИСК ПОЛНОСТЬЮ ОСТАНОВЛЕН для пользователя {user_id}")


@bot.message_handler(func=lambda message: message.text == "🔙 Назад")
def back_to_main(message):
    user_id = message.from_user.id
    remove_from_search_queue(user_id)
    remove_from_group_search_queue(user_id)

    # Очищаем состояние пользователя
    if user_id in user_states:
        del user_states[user_id]

    bot.send_message(
        message.chat.id,
        "<b>📋 Главное меню:</b>",
        parse_mode='HTML',
        reply_markup=main_menu_keyboard()
    )


@bot.message_handler(func=lambda message: message.text == "👥 Групповой поиск")
def group_search_menu(message):
    """Меню группового поиска"""
    user = get_user(message.from_user.id)

    # Проверяем, указан ли пол
    if user and user['gender'] == 'Не указан':
        bot.send_message(
            message.chat.id,
            "❌ <b>Сначала укажите ваш пол!</b>\n\n"
            "<i>Перейдите в настройки профиля → Пол</i>",
            parse_mode='HTML',
            reply_markup=main_menu_keyboard()
        )
        return

    bot.send_message(
        message.chat.id,
        "👥 <b>Групповой чат</b>\n\n"

        "✨ <b>Выберите тип поиска:</b>\n\n"

        "🎲 <b>Случайные собеседники</b>\n"
        "└ 3 случайных пользователя\n\n"

        "🙋‍♀️ <b>Найти девушек</b>\n"
        "└ Групповой чат с 2 девушками\n"
        "   <i>💎 Требуется премиум</i>\n\n"

        "🙋‍♂️ <b>Найти парней</b>\n"
        "└ Групповой чат с 2 парнями\n"
        "   <i>💎 Требуется премиум</i>\n\n"

        "🚀 <i>Начните поиск собеседников:</i>",
        parse_mode='HTML',
        reply_markup=group_search_keyboard()
    )


@bot.message_handler(
    func=lambda message: message.text in ["🎲 Случайные собеседники", "🙋‍♀️ Найти девушек", "🙋‍♂️ Найти парней"])
def start_group_search(message):
    """Запуск группового поиска"""
    user_id = message.from_user.id
    user = get_user(user_id)

    # 🔴 ПРОВЕРКА: если пользователь уже в поиске группового чата
    current_user = get_user(user_id)
    if current_user and current_user['is_searching'] and current_user['search_type'].startswith('group_'):
        bot.send_message(
            message.chat.id,
            "⏳ <b>Вы уже в поиске группового чата!</b>\n\n"
            "<i>Дождитесь подключения или отмените текущий поиск через главное меню</i>",
            parse_mode='HTML'
        )
        return

    # Проверяем, не находится ли пользователь уже в чате
    if user_id in active_chats:
        bot.send_message(
            message.chat.id,
            "<i>❌ Вы уже в диалоге! Завершите текущий диалог перед началом группового поиска.</i>",
            parse_mode='HTML'
        )
        return

    # Проверяем, не находится ли пользователь уже в групповом чате
    for chat_id, chat_data in active_group_chats.items():
        if user_id in chat_data['users']:
            bot.send_message(
                message.chat.id,
                "<i>❌ Вы уже в групповом чате! Используйте /gstop чтобы выйти.</i>",
                parse_mode='HTML'
            )
            return

    # 🔴 ИСПРАВЛЕНИЕ: Правильное определение типа группового поиска
    group_type_map = {
        "🎲 Случайные собеседники": "group_random",
        "🙋‍♀️ Найти девушек": "male_seekers",  # Парни ищут девушек
        "🙋‍♂️ Найти парней": "female_seekers"  # Девушки ищут парней
    }

    group_type = group_type_map[message.text]

    # 🔴 ИСПРАВЛЕНИЕ: СНАЧАЛА проверяем премиум для поиска по полу
    if group_type in ['female_seekers', 'male_seekers'] and (not user or not user['premium']):
        bot.send_message(
            message.chat.id,
            "👥 <b>Групповой поиск по полу</b>\n\n"
            "🚫 <i>Доступно только для премиум пользователей</i>\n\n"
            "💎 <b>Приобретите премиум, чтобы использовать групповой поиск по полу</b>",
            parse_mode='HTML',
            reply_markup=premium_required_keyboard()
        )
        return

    # 🔴 ИСПРАВЛЕНИЕ: ПОТОМ проверяем соответствие пола и типа поиска
    if group_type == 'male_seekers' and user['gender'] != 'Парень':
        bot.send_message(
            message.chat.id,
            "❌ <b>Поиск девушек доступен только для парней!</b>\n\n"
            "<i>Выберите другой тип группового поиска</i>",
            parse_mode='HTML',
            reply_markup=group_search_keyboard()
        )
        return

    if group_type == 'female_seekers' and user['gender'] != 'Девушка':
        bot.send_message(
            message.chat.id,
            "❌ <b>Поиск парней доступен только для девушек!</b>\n\n"
            "<i>Выберите другой тип группового поиска</i>",
            parse_mode='HTML',
            reply_markup=group_search_keyboard()
        )
        return

    # Убираем пользователя из всех очередей
    remove_from_search_queue(user_id)
    remove_from_group_search_queue(user_id)

    # Добавляем в очередь группового поиска
    added = add_to_group_search_queue(user_id, group_type)

    if added:
        # Чат был создан мгновенно
        return
    else:
        # Показываем сообщение о поиске
        search_messages = {
            "group_random": "🔍 <b>Ищем случайных собеседников для группового чата...</b>",
            "female_seekers": "🔍 <b>Ищем парней для группового чата...</b>",
            "male_seekers": "🔍 <b>Ищем девушек для группового чата...</b>"
        }

        bot.send_message(
            message.chat.id,
            f"{search_messages[group_type]}\n\n"
            f"<i>Ожидаем подходящих участников...\n"
            f"Нажмите 🔙 Назад чтобы отменить поиск</i>",
            parse_mode='HTML'
        )

        # Запускаем фоновый поиск
        threading.Thread(target=group_search_companion, args=(user_id, group_type)).start()


def group_search_companion(user_id, group_type):
    """Фоновый поиск с приоритетом дозаполнения (бесконечный)"""
    user = get_user(user_id)

    while True:
        # Проверяем, не отменил ли пользователь поиск
        user = get_user(user_id)
        if not user or not user['is_searching']:
            print(f"🔴 ПОИСК ОСТАНОВЛЕН: пользователь {user_id} вышел из поиска")
            return

        # Проверяем, не нашелся ли уже собеседник
        user_in_chat = any(user_id in chat_data['users'] for chat_data in active_group_chats.values())
        if user_in_chat:
            print(f"🔴 ПОИСК ЗАВЕРШЕН: пользователь {user_id} найден в чате")
            return

        # 🔴 ВЫСШИЙ ПРИОРИТЕТ: проверяем дозаполнение существующих чатов
        if try_fill_existing_chats():
            # Проверяем, не попал ли пользователь в чат
            user_in_chat = any(user_id in chat_data['users'] for chat_data in active_group_chats.values())
            if user_in_chat:
                print(f"🔴 ПОИСК ЗАВЕРШЕН: пользователь {user_id} добавлен через дозаполнение")
                return

        # 🔴 ВТОРОЙ ПРИОРИТЕТ: добавляем в существующий чат
        if add_user_to_existing_group_chat(user_id, group_type):
            print(f"🔴 ПОИСК ЗАВЕРШЕН: пользователь {user_id} добавлен в существующий чат")
            return

        # 🔴 ТРЕТИЙ ПРИОРИТЕТ: создаем новый чат
        chat_created = try_create_group_chat(group_type)
        if chat_created:
            user_in_chat = any(user_id in chat_data['users'] for chat_data in active_group_chats.values())
            if user_in_chat:
                print(f"🔴 ПОИСК ЗАВЕРШЕН: пользователь {user_id} добавлен в новый чат")
                return

        # 🔴 ИСПРАВЛЕНИЕ: Добавляем проверку выхода из поиска между итерациями
        time.sleep(2)

        # Дополнительная проверка состояния пользователя
        current_user = get_user(user_id)
        if not current_user or not current_user['is_searching']:
            print(f"🔴 ПОИСК ПРЕРВАН: пользователь {user_id} отменил поиск")
            return


def add_user_to_existing_group_chat(user_id, group_type):
    """Добавляет пользователя в существующий чат с расширенной логикой"""
    user = get_user(user_id)
    if not user:
        return False

    # Пользователь не должен быть уже в чате
    user_in_chat = any(user_id in chat_data['users'] for chat_data in active_group_chats.values())

    if user_in_chat:
        return False

    # 🔴 ИСПРАВЛЕНИЕ: Создаем копию списка ключей для безопасной итерации
    chat_ids = list(active_group_chats.keys())

    # Проверяем все активные чаты
    for chat_id in chat_ids:
        # 🔴 ИСПРАВЛЕНИЕ: Проверяем, что чат все еще существует
        if chat_id not in active_group_chats:
            continue

        chat_data = active_group_chats[chat_id]

        if len(chat_data['users']) < 3 and user_id not in chat_data['users']:

            current_users = chat_data['users']

            # Пересчитываем текущий состав
            male_count = 0
            female_count = 0
            for uid in current_users:
                u = get_user(uid)
                if u:
                    if u['gender'] == 'Парень':
                        male_count += 1
                    elif u['gender'] == 'Девушка':
                        female_count += 1

            # Финальная проверка: пользователь все еще в поиске
            current_user_state = get_user(user_id)
            if not current_user_state or not current_user_state['is_searching']:
                return False

            # Проверка: пользователь не попал в другой чат пока мы проверяли
            user_still_free = not any(user_id in cd['users'] for cd in active_group_chats.values())
            if not user_still_free:
                return False

            # Логика подключения
            if group_type == 'male_seekers' and user['gender'] == 'Парень':
                if female_count == 2 and male_count == 0:
                    if chat_data['type'] == 'group_random':
                        chat_data['type'] = 'male_seekers'
                    return add_user_to_chat(chat_id, chat_data, user_id)
                elif female_count == 1 and male_count == 0 and chat_data['type'] == 'male_seekers':
                    return add_user_to_chat(chat_id, chat_data, user_id)

            elif group_type == 'female_seekers' and user['gender'] == 'Девушка':
                if male_count == 2 and female_count == 0:
                    if chat_data['type'] == 'group_random':
                        chat_data['type'] = 'female_seekers'
                    return add_user_to_chat(chat_id, chat_data, user_id)
                elif male_count == 1 and female_count == 0 and chat_data['type'] == 'female_seekers':
                    return add_user_to_chat(chat_id, chat_data, user_id)

            elif group_type == 'male_seekers' and user['gender'] == 'Девушка':
                if chat_data['type'] == 'male_seekers' and male_count == 1 and female_count < 2:
                    return add_user_to_chat(chat_id, chat_data, user_id)

            elif group_type == 'female_seekers' and user['gender'] == 'Парень':
                if chat_data['type'] == 'female_seekers' and female_count == 1 and male_count < 2:
                    return add_user_to_chat(chat_id, chat_data, user_id)

            elif group_type == 'group_random':
                if chat_data['type'] == 'group_random':
                    return add_user_to_chat(chat_id, chat_data, user_id)

    return False


def add_user_to_chat(chat_id, chat_data, user_id):
    """Добавляет пользователя в чат (общая функция)"""
    chat_data['users'].append(user_id)

    # Обновляем БД
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('SELECT users FROM group_chats WHERE chat_id = ?', (chat_id,))
    result = cursor.fetchone()
    if result:
        current_users_db = json.loads(result[0])
        current_users_db.append(user_id)
        cursor.execute('UPDATE group_chats SET users = ? WHERE chat_id = ?',
                       (json.dumps(current_users_db), chat_id))
        conn.commit()
    conn.close()

    # Удаляем из очередей
    remove_from_group_search_queue(user_id)
    set_user_searching(user_id, False)

    # Уведомляем участников
    notify_group_chat_join(chat_id, user_id, chat_data)
    print(f"🔴 УСПЕШНО: пользователь {user_id} добавлен в чат {chat_id}")
    return True


def notify_group_chat_join(chat_id, new_user_id, chat_data):
    """Уведомляет участников о новом пользователе в чате"""
    new_user = get_user(new_user_id)

    for participant_id in chat_data['users']:
        try:
            if participant_id != new_user_id:
                bot.send_message(
                    participant_id,
                    f"👤 <b>Новый участник присоединился к чату!</b>\n\n"
                    f"<i>Теперь в чате {len(chat_data['users'])} участников</i>",
                    parse_mode='HTML'
                )
            else:
                # Приветственное сообщение для нового пользователя
                chat_type_names = {
                    'group_random': "Случайные собеседники",
                    'female_seekers': "Поиск парней" if new_user['premium'] else "Случайные собеседники",
                    'male_seekers': "Поиск девушек" if new_user['premium'] else "Случайные собеседники"
                }

                message = (
                    f"👥 <b>Вы присоединились к групповому чату!</b>\n\n"
                    f"<b>Тип:</b> {chat_type_names[chat_data['type']]}\n"
                    f"<b>Участников:</b> {len(chat_data['users'])}\n\n"
                    f"<i>/gstop - Покинуть групповой чат</i>"
                )
                bot.send_message(new_user_id, message, parse_mode='HTML')
        except Exception as e:
            print(f"Error notifying group user {participant_id}: {e}")


# ОБРАБОТЧИКИ НАСТРОЕК ПРОФИЛЯ
@bot.message_handler(func=lambda message: message.text == "⚙️ Профиль")
def settings_menu(message):
    user = get_user(message.from_user.id)
    if not user:
        create_user(message.from_user.id, message.from_user.username,
                    message.from_user.first_name, message.from_user.last_name)
        user = get_user(message.from_user.id)

    # Получаем статистику
    ratings = get_user_ratings(user['user_id'])
    referral_stats = get_user_referral_stats(user['user_id'])

    # Расчет рейтинга
    total_ratings = ratings['likes'] + ratings['dislikes']
    rating_percent = (ratings['likes'] / total_ratings * 100) if total_ratings > 0 else 0

    # Красивый профиль в стиле статистики
    profile_info = (
        f"🆔 <code>{user['user_id']}</code>\n\n"

        f"<b>📊 Основная информация:</b>\n"
        f"├ 🚻 Пол: <b>{user['gender']}</b>\n"
        f"├ 🔞 Возраст: <b>{user['age'] if user['age'] > 0 else 'Не указан'}</b>\n"
        f"└ 📷 Медиа: <b>{'✅ Разрешено' if user['media_allowed'] else '❌ Запрещено'}</b>\n\n"

        f"<b>💎 Статус аккаунта:</b>\n"
        f"└ {'<b>✨ ПРЕМИУМ АКТИВЕН</b>' if user['premium'] else '❌ Обычный аккаунт'}\n"
    )

    # Добавляем информацию о премиуме если он активен
    if user['premium'] and user['premium_until']:
        premium_until = datetime.fromisoformat(user['premium_until'])
        time_left = premium_until - datetime.now()

        # Если время меньше 0, значит премиум истек
        if time_left.total_seconds() <= 0:
            profile_info += f"<b>⏰ Истек:</b> <i>Требуется продление</i>\n\n"
        else:
            days_left = time_left.days
            hours_left = time_left.seconds // 3600
            minutes_left = (time_left.seconds % 3600) // 60

            # Форматируем время в зависимости от длительности
            if days_left > 0:
                time_left_str = f"{days_left} д. {hours_left} ч."
            elif hours_left > 0:
                time_left_str = f"{hours_left} ч. {minutes_left} мин."
            else:
                time_left_str = f"{minutes_left} мин."

            profile_info += f"<b>⏰ Осталось:</b> <i>{time_left_str}</i>\n\n"
    else:
        profile_info += "\n"

    # Статистика репутации
    profile_info += (
        f"<b>⭐ Репутация:</b>\n"
        f"├ 👍 Лайки: <b>{ratings['likes']}</b>\n"
        f"├ 👎 Дизлайки: <b>{ratings['dislikes']}</b>\n"
        f"└ 📈 Всего оценок: <b>{total_ratings}</b>\n\n"
    )

    # Реферальная система
    profile_info += (
        f"<b>👥 Реферальная система:</b>\n"
        f"├ 📤 Приглашено: <b>{referral_stats['invited']}</b>\n"
        f"└ ✅ Зарегистрировалось: <b>{referral_stats['registered']}</b>\n\n"

        "<i>Выберите настройку для изменения:</i>"
    )

    # Только кнопка пригласить друга
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("👥 Пригласить друга", callback_data="invite_friend"))

    bot.send_message(
        message.chat.id,
        profile_info,
        parse_mode='HTML',
        reply_markup=keyboard
    )

    # Отправляем клавиатуру настроек
    bot.send_message(
        message.chat.id,
        "⚙️ <b>Настройки профиля:</b>",
        parse_mode='HTML',
        reply_markup=settings_keyboard()
    )


@bot.callback_query_handler(func=lambda call: call.data == "invite_friend")
def handle_invite_friend(call):
    user_id = call.from_user.id
    stats = get_user_referral_stats(user_id)

    # Генерируем реферальную ссылку
    bot_username = bot.get_me().username
    referral_link = f"https://t.me/{bot_username}?start={user_id}"

    invite_text = (
        "👥 <b>Приглашай друзей</b>\n\n"

        "💫 <b>Система вознаграждений:</b>\n"
        "├ 🎁 За каждого друга\n"
        "└ 💎 <b>+1 час PREMIUM</b>\n\n"

        "📊 <b>Твоя статистика:</b>\n"
        f"├ 📤 Приглашено: <b>{stats['invited']}</b>\n"
        f"├ ✅ Зарегистрировалось: <b>{stats['registered']}</b>\n"
        f"└ ⏱️ Начислено часов: <b>{stats['registered']}</b>\n\n"

        "🔗 <b>Твоя персональная ссылка:</b>\n"
        f"<code>{referral_link}</code>\n\n"

        "🚀 <i>Поделись ссылкой и получай бонусы!</i>"
    )

    # Создаем инлайн-клавиатуру для действий
    keyboard = InlineKeyboardMarkup()
    keyboard.add(
        InlineKeyboardButton("📤 Поделиться ссылкой",
                             url=f"https://t.me/share/url?url={referral_link}&text=Присоединяйся%20к%20анонимному%20чату!%20🎭%20Анонимные%20диалоги,%20поиск%20по%20интересам%20и%20многое%20другое!%20✨")
    )
    keyboard.add(InlineKeyboardButton("🔙 Назад", callback_data="back_to_profile"))

    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=invite_text,
        parse_mode='HTML',
        reply_markup=keyboard,
        disable_web_page_preview=True
    )


@bot.callback_query_handler(func=lambda call: call.data == "back_to_profile")
def handle_back_to_profile(call):
    user_id = call.from_user.id
    user = get_user(user_id)

    # Получаем статистику
    ratings = get_user_ratings(user['user_id'])
    referral_stats = get_user_referral_stats(user['user_id'])

    # Расчет рейтинга
    total_ratings = ratings['likes'] + ratings['dislikes']
    rating_percent = (ratings['likes'] / total_ratings * 100) if total_ratings > 0 else 0

    # Красивый профиль в стиле статистики
    profile_info = (
        f"🆔 <code>{user['user_id']}</code>\n\n"

        f"<b>📊 Основная информация:</b>\n"
        f"├ 🚻 Пол: <b>{user['gender']}</b>\n"
        f"├ 🔞 Возраст: <b>{user['age'] if user['age'] > 0 else 'Не указан'}</b>\n"
        f"└ 📷 Медиа: <b>{'✅ Разрешено' if user['media_allowed'] else '❌ Запрещено'}</b>\n\n"

        f"<b>💎 Статус аккаунта:</b>\n"
        f"└ {'<b>✨ ПРЕМИУМ АКТИВЕН</b>' if user['premium'] else '❌ Обычный аккаунт'}\n"
    )

    # Добавляем информацию о премиуме если он активен
    if user['premium'] and user['premium_until']:
        premium_until = datetime.fromisoformat(user['premium_until'])
        time_left = premium_until - datetime.now()

        # Если время меньше 0, значит премиум истек
        if time_left.total_seconds() <= 0:
            profile_info += f"<b>⏰ Истек:</b> <i>Требуется продление</i>\n\n"
        else:
            days_left = time_left.days
            hours_left = time_left.seconds // 3600
            minutes_left = (time_left.seconds % 3600) // 60

            # Форматируем время в зависимости от длительности
            if days_left > 0:
                time_left_str = f"{days_left} д. {hours_left} ч."
            elif hours_left > 0:
                time_left_str = f"{hours_left} ч. {minutes_left} мин."
            else:
                time_left_str = f"{minutes_left} мин."

            profile_info += f"<b>⏰ Осталось:</b> <i>{time_left_str}</i>\n\n"
    else:
        profile_info += "\n"

    # Статистика репутации
    profile_info += (
        f"<b>⭐ Репутация:</b>\n"
        f"├ 👍 Лайки: <b>{ratings['likes']}</b>\n"
        f"├ 👎 Дизлайки: <b>{ratings['dislikes']}</b>\n"
        f"└ 📈 Всего оценок: <b>{total_ratings}</b>\n\n"
    )

    # Реферальная система
    profile_info += (
        f"<b>👥 Реферальная система:</b>\n"
        f"├ 📤 Приглашено: <b>{referral_stats['invited']}</b>\n"
        f"└ ✅ Зарегистрировалось: <b>{referral_stats['registered']}</b>\n\n"

        "<i>Выберите настройку для изменения:</i>"
    )

    # Только кнопка пригласить друга
    keyboard = InlineKeyboardMarkup()
    keyboard.add(InlineKeyboardButton("👥 Пригласить друга", callback_data="invite_friend"))

    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=profile_info,
        parse_mode='HTML',
        reply_markup=keyboard
    )


@bot.message_handler(func=lambda message: message.text == "👤 Пол")
def set_gender(message):
    bot.send_message(message.chat.id, "👤 Выберите ваш пол:", reply_markup=gender_keyboard())


@bot.message_handler(func=lambda message: message.text in ["🙋‍♀️ Я девушка", "🙋‍♂️ Я парень"])
def save_gender(message):
    gender_map = {
        "🙋‍♀️ Я девушка": "Девушка",
        "🙋‍♂️ Я парень": "Парень"
    }

    gender = gender_map[message.text]
    update_user_profile(message.from_user.id, 'gender', gender)

    bot.send_message(message.chat.id, f"✅ Пол сохранен: <b>{gender}</b>", reply_markup=settings_keyboard(), parse_mode='HTML')


@bot.message_handler(func=lambda message: message.text == "🔞 Возраст")
def ask_age(message):
    bot.send_message(message.chat.id, "🔞 Отправьте число — ваш возраст (например: 18):")
    bot.register_next_step_handler(message, save_age)


def save_age(message):
    try:
        age = int(message.text)
        if age < 13 or age > 100:
            bot.send_message(message.chat.id, "❌ Возраст должен быть от 13 до 100 лет.")
            return
        update_user_profile(message.from_user.id, 'age', age)
        bot.send_message(message.chat.id, f"✅ Возраст сохранен: <b>{age}</b>", reply_markup=settings_keyboard(), parse_mode='HTML')
    except ValueError:
        bot.send_message(message.chat.id, "❌ Пожалуйста, отправьте корректное число.")


@bot.message_handler(func=lambda message: message.text == "📷 Фото/Видео")
def media_settings(message):
    user = get_user(message.from_user.id)
    status = "✅ Разрешено" if user['media_allowed'] else "❌ Запрещено"
    bot.send_message(
        message.chat.id,
        f"📷 Разрешить получать от собеседника фото/видео/голосовые по запросу?\n\n"
        f"Текущий статус: {status}",
        reply_markup=media_keyboard()
    )


@bot.message_handler(func=lambda message: message.text in ["✅ Медиа разрешено", "❌ Медиа запрещено"])
def toggle_media(message):
    media_allowed = message.text == "✅ Медиа разрешено"
    update_user_profile(message.from_user.id, 'media_allowed', int(media_allowed))
    status = "✅ Разрешено" if media_allowed else "❌ Запрещено"
    bot.send_message(message.chat.id, f"✅ Настройки медиа сохранены: {status}", reply_markup=settings_keyboard())


# ПОИСК И ЧАТЫ
@bot.message_handler(func=lambda message: message.text == "🎯 Начать диалог")
def start_random_search(message):
    user_id = message.from_user.id
    user = get_user(user_id)

    if not user:
        create_user(user_id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
        user = get_user(user_id)

    # ПРОВЕРКА: если пользователь уже в активном чате
    if user_id in active_chats:
        bot.send_message(
            message.chat.id,
            "<i>❌ Вы уже в диалоге! Используйте /next для поиска нового собеседника или /stop для завершения текущего диалога.</i>",
            parse_mode='HTML'
        )
        return

    # Проверяем, указан ли пол
    if user['gender'] == 'Не указан':
        bot.send_message(
            message.chat.id,
            "Укажите ваш пол (Настройки профиля → Пол).",
            reply_markup=main_menu_keyboard()
        )
        return

    remove_from_search_queue(user_id)
    remove_from_group_search_queue(user_id)

    # Пытаемся сразу найти собеседника
    chat_created = add_to_search_queue(user_id, 'random')

    if not chat_created:
        # Если чат не создан, показываем сообщение о поиске
        bot.send_message(
            message.chat.id,
            "<b><i>🔍 Ищем собеседника...</i></b>\n\n<i>/stop - остановить поиск</i>",
            parse_mode='HTML'
        )
        # Запускаем фоновый поиск
        threading.Thread(target=search_companion, args=(user_id, 'random')).start()


@bot.message_handler(func=lambda message: message.text == "🔍 Поиск по полу")
def search_by_gender_menu(message):
    user = get_user(message.from_user.id)

    if not user or not user['premium']:
        bot.send_message(
            message.chat.id,
            "🔍 <b>Поиск по полу</b>\n\n"
            "🚫 <i>Доступно только для премиум пользователей</i>\n\n"
            "💎 <b>Приобретите премиум, чтобы использовать расширенный поиск</b>",
            parse_mode='HTML',
            reply_markup=premium_required_keyboard()
        )
        return

    bot.send_message(
        message.chat.id,
        "🔍 <b>Выберите пол для поиска:</b>",
        parse_mode='HTML',
        reply_markup=search_gender_keyboard()
    )


@bot.message_handler(func=lambda message: message.text in ["🙋‍♀️ Найти девушку", "🙋‍♂️ Найти парня", "🎭 Любой пол"])
def start_gender_search(message):
    user_id = message.from_user.id
    user = get_user(user_id)

    # ПРОВЕРКА: если пользователь уже в активном чате
    if user_id in active_chats:
        bot.send_message(
            message.chat.id,
            "<i>❌ Вы уже в диалоге! Используйте /next для поиска нового собеседника или /stop для завершения текущего диалога.</i>",
            parse_mode='HTML'
        )
        return

    # Проверяем, указан ли пол
    if user['gender'] == 'Не указан':
        bot.send_message(
            message.chat.id,
            "Укажите ваш пол (Настройки профиля → Пол).",
            reply_markup=main_menu_keyboard()
        )
        return

    gender_map = {
        "🙋‍♀️ Найти девушку": "female",
        "🙋‍♂️ Найти парня": "male",
        "🎭 Любой пол": "any"
    }

    text_map = {
        "🙋‍♀️ Найти девушку": "<b><i>🔍 Ищем девушку...</i></b>",
        "🙋‍♂️ Найти парня": "<b><i>🔍 Ищем парня...</i></b>",
        "🎭 Любой пол": "<b><i>🔍 Ищем собеседника...</i></b>"
    }

    gender_filter = gender_map[message.text]
    search_text = text_map[message.text]
    remove_from_search_queue(user_id)
    remove_from_group_search_queue(user_id)

    # Пытаемся сразу найти собеседника
    chat_created = add_to_search_queue(user_id, 'gender', {'gender': gender_filter})

    if not chat_created:
        # Если чат не создан, показываем сообщение о поиске
        bot.send_message(
            message.chat.id,
            f"{search_text}\n\n<i>/stop - остановить поиск</i>",
            parse_mode='HTML'
        )
        threading.Thread(target=search_companion, args=(user_id, 'gender', {'gender': gender_filter})).start()


@bot.message_handler(commands=['search'])
def search_command(message):
    user_id = message.from_user.id
    user = get_user(user_id)

    if not user:
        create_user(user_id, message.from_user.username,
                    message.from_user.first_name, message.from_user.last_name)
        user = get_user(user_id)

    # ПРОВЕРКА: если пользователь уже в активном чате
    if user_id in active_chats:
        bot.send_message(
            message.chat.id,
            "<i>❌ Вы уже в диалоге! Используйте /next для поиска нового собеседника или /stop для завершения текущего диалога.</i>",
            parse_mode='HTML'
        )
        return

    # Проверяем, указан ли пол
    if user['gender'] == 'Не указан':
        bot.send_message(
            message.chat.id,
            "Укажите ваш пол (Настройки профиля → Пол).",
            reply_markup=main_menu_keyboard()
        )
        return

    remove_from_search_queue(user_id)

    # Пытаемся сразу найти собеседника через случайный поиск
    chat_created = add_to_search_queue(user_id, 'random')

    if not chat_created:
        # Если чат не создан, показываем сообщение о поиске (такое же как в /next)
        bot.send_message(
            message.chat.id,
            "<b><i>🔍 Ищем собеседника...</i></b>\n\n<i>/stop - остановить поиск</i>",
            parse_mode='HTML'
        )
        # Запускаем фоновый поиск
        threading.Thread(target=search_companion, args=(user_id, 'random')).start()


@bot.message_handler(commands=['link'])
def link_command(message):
    user_id = message.from_user.id

    # Проверяем, находится ли пользователь в активном чате
    if user_id not in active_chats:
        bot.send_message(
            message.chat.id,
            "❌ <b>Эта команда работает только во время диалога!</b>\n\n"
            "<i>Начните диалог чтобы поделиться ссылкой на ваш профиль.</i>",
            parse_mode='HTML'
        )
        return

    chat_data = active_chats[user_id]
    companion_id = chat_data['companion_id']

    # Получаем информацию о пользователе
    user = get_user(user_id)

    # Формируем ссылку на профиль
    if user['username']:
        profile_link = f"https://t.me/{user['username']}"
        message_text = (
            f"👤 <b>Профиль собеседника:</b>\n"
            f"🔗 {profile_link}\n\n"
            f"<i>Ссылка на Telegram профиль вашего собеседника</i>"
        )
    else:
        message_text = (
            "❌ <b>У вашего собеседника не установлен username</b>\n\n"
            "<i>Чтобы поделиться ссылкой на ваш профиль, установите username в настройках Telegram</i>"
        )

    # Отправляем ссылку собеседнику СКРЫТО (без preview)
    try:
        bot.send_message(
            companion_id,
            message_text,
            parse_mode='HTML',
            disable_web_page_preview=True  # ✅ ВКЛЮЧАЕМ отключение preview
        )

        # Уведомляем отправителя
        if user['username']:
            bot.send_message(
                user_id,
                "✅ <b>Вы поделились ссылкой на ваш профиль</b>\n\n"
                "<i>Ссылка отправлена собеседнику</i>",
                parse_mode='HTML'
            )
        else:
            bot.send_message(
                user_id,
                "❌ <b>У вас не установлен username</b>\n\n"
                "<i>Установите username в настройках Telegram чтобы делиться ссылкой на ваш профиль</i>",
                parse_mode='HTML'
            )

    except Exception as e:
        print(f"Error sending link: {e}")
        bot.send_message(
            user_id,
            "❌ <b>Не удалось отправить ссылку</b>\n\n"
            "<i>Возможно, у вашего собеседника нет username или произошла ошибка</i>",
            parse_mode='HTML'
        )


def search_companion(user_id, search_type='random', filters=None):
    """Фоновый поиск собеседника (используется когда мгновенного соединения не произошло)"""
    checked_pairs = set()

    while True:
        # Проверяем, не отменил ли пользователь поиск
        user = get_user(user_id)
        if not user or not user['is_searching']:
            return

        # Проверяем, не нашелся ли уже собеседник через мгновенное соединение
        if user_id in active_chats:
            return

        companion_id = find_companion(user_id, search_type, filters)

        if companion_id:
            # Двойная проверка
            user1 = get_user(user_id)
            user2 = get_user(companion_id)
            if not user1 or not user1['is_searching'] or not user2 or not user2['is_searching']:
                continue

            # Проверяем, не проверяли ли мы уже эту пару
            pair_key = tuple(sorted([user_id, companion_id]))
            if pair_key in checked_pairs:
                continue
            checked_pairs.add(pair_key)

            # Финальная проверка взаимной совместимости
            if (check_compatibility(user1, user2, search_type, filters) and
                    check_compatibility(user2, user1, user2['search_type'], user2['search_filters'])):

                # Создаем чат
                chat_id = create_chat(user_id, companion_id)
                active_chats[user_id] = {'companion_id': companion_id, 'chat_id': chat_id}
                active_chats[companion_id] = {'companion_id': user_id, 'chat_id': chat_id}

                # Удаляем из очереди поиска
                remove_from_search_queue(user_id)
                remove_from_search_queue(companion_id)

                # Формируем информацию о собеседнике
                companion_info1 = get_companion_info(user1, user2, user1['premium'])
                companion_info2 = get_companion_info(user2, user1, user2['premium'])

                # Формируем сообщения
                message1 = f"<b><i>Собеседник найден!</i></b>\n\n"
                if companion_info1:
                    message1 += f"{companion_info1}\n\n"
                message1 += f"<i>/next - искать следующего\n/stop — закончить диалог</i>"

                message2 = f"<b><i>Собеседник найден!</i></b>\n\n"
                if companion_info2:
                    message2 += f"{companion_info2}\n\n"
                message2 += f"<i>/next - искать следующего\n/stop — закончить диалог</i>"

                # Отправляем сообщения
                try:
                    bot.send_message(user_id, message1, parse_mode='HTML')
                    time.sleep(0.5)
                    bot.send_message(companion_id, message2, parse_mode='HTML')
                except Exception as e:
                    print(f"Error sending found messages: {e}")
                    if user_id in active_chats:
                        del active_chats[user_id]
                    if companion_id in active_chats:
                        del active_chats[companion_id]
                    continue

                return

        time.sleep(2)


@bot.message_handler(commands=['admin_premium'])
def admin_premium_command(message):
    user_id = message.from_user.id

    # Проверяем, является ли пользователь администратором
    if str(user_id) != ADMIN_CHAT_ID:
        bot.send_message(message.chat.id, "❌ У вас нет доступа к этой команде.")
        return

    try:
        # Выдаем премиум на 7 дней
        add_premium(user_id, days=7)

        # Обновляем данные пользователя
        user = get_user(user_id)

        bot.send_message(
            message.chat.id,
            f"✅ <b>Премиум статус успешно выдан!</b>\n\n"
            f"💎 <b>Срок:</b> 7 дней\n"
            f"📅 <b>Действует до:</b> {user['premium_until'] if user and user['premium_until'] else 'Неизвестно'}\n\n"
            f"Теперь вам доступны все премиум функции!",
            parse_mode='HTML'
        )

        # Логируем действие
        print(f"Админ {user_id} выдал себе премиум на 7 дней")

    except Exception as e:
        print(f"Error giving admin premium: {e}")
        bot.send_message(
            message.chat.id,
            "❌ Произошла ошибка при выдаче премиум статуса."
        )


@bot.message_handler(commands=['give_premium'])
def give_premium_command(message):
    user_id = message.from_user.id

    # Проверяем, является ли пользователь администратором
    if str(user_id) != ADMIN_CHAT_ID:
        bot.send_message(message.chat.id, "❌ У вас нет доступа к этой команде.")
        return

    try:
        # Проверяем формат команды: /give_premium <user_id>
        parts = message.text.split()
        if len(parts) != 2:
            bot.send_message(
                message.chat.id,
                "❌ <b>Неверный формат команды</b>\n\n"
                "Используйте: <code>/give_premium &lt;user_id&gt;</code>\n\n"
                "Пример: <code>/give_premium 123456789</code>",
                parse_mode='HTML'
            )
            return

        target_user_id = int(parts[1])

        # Проверяем существование пользователя
        target_user = get_user(target_user_id)
        if not target_user:
            bot.send_message(
                message.chat.id,
                f"❌ Пользователь с ID <code>{target_user_id}</code> не найден.",
                parse_mode='HTML'
            )
            return

        # Выдаем премиум на 7 дней
        add_premium(target_user_id, days=7)

        # Обновляем данные пользователя
        updated_user = get_user(target_user_id)

        # Уведомляем администратора
        bot.send_message(
            message.chat.id,
            f"✅ <b>Премиум статус успешно выдан пользователю!</b>\n\n"
            f"👤 <b>User ID:</b> <code>{target_user_id}</code>\n"
            f"💎 <b>Срок:</b> 7 дней\n"
            f"📅 <b>Действует до:</b> {updated_user['premium_until'] if updated_user and updated_user['premium_until'] else 'Неизвестно'}\n\n"
            f"Пользователю отправлено уведомление.",
            parse_mode='HTML'
        )

        # Уведомляем пользователя
        try:
            bot.send_message(
                target_user_id,
                "🎉 <b>Вам выдан премиум статус на 7 дней!</b>\n\n"
                "✨ <b>Теперь вам доступны:</b>\n"
                "• 🔍 Поиск по полу\n"
                "• 👥 Групповой поиск по полу\n"
                "• 🚀 Приоритет в поиске\n"
                "• 📊 Подробная информация о собеседниках\n\n"
                "<i>Приятного использования! 😊</i>",
                parse_mode='HTML',
                reply_markup=main_menu_keyboard()
            )
        except Exception as e:
            print(f"Error notifying user: {e}")
            bot.send_message(
                message.chat.id,
                f"⚠️ Премиум выдан, но не удалось отправить уведомление пользователю: {e}",
                parse_mode='HTML'
            )

        # Логируем действие
        print(f"Админ {user_id} выдал премиум пользователю {target_user_id} на 7 дней")

    except ValueError:
        bot.send_message(
            message.chat.id,
            "❌ <b>Неверный ID пользователя</b>\n\n"
            "ID должен быть числом. Пример: <code>/give_premium 123456789</code>",
            parse_mode='HTML'
        )
    except Exception as e:
        print(f"Error giving premium: {e}")
        bot.send_message(
            message.chat.id,
            f"❌ Произошла ошибка при выдаче премиум статуса: {str(e)}"
        )


@bot.message_handler(commands=['webapp'])
def webapp_command(message):
    user_id = message.from_user.id

    # Проверяем, является ли пользователь администратором
    if str(user_id) != ADMIN_CHAT_ID:
        bot.send_message(message.chat.id, "❌ У вас нет доступа к этой команде.")
        return

    try:
        # Создаем кнопку с мини-приложением
        keyboard = InlineKeyboardMarkup()
        web_app_button = InlineKeyboardButton(
            text="📊 Открыть мини-приложение",
            web_app=telebot.types.WebAppInfo(url="https://donk1221.pythonanywhere.com")
        )
        keyboard.add(web_app_button)

        bot.send_message(
            message.chat.id,
            "🖥️ <b>Админ панель - мини-приложение</b>\n\n"
            "Нажмите кнопку ниже чтобы открыть веб-приложение:",
            parse_mode='HTML',
            reply_markup=keyboard
        )

    except Exception as e:
        print(f"Error creating webapp button: {e}")
        bot.send_message(
            message.chat.id,
            "❌ Произошла ошибка при создании кнопки мини-приложения."
        )


# Команда статистики для администратора
@bot.message_handler(commands=['stats'])
def stats_command(message):
    user_id = message.from_user.id

    # Проверяем, является ли пользователь администратором
    if str(user_id) != ADMIN_CHAT_ID:
        bot.send_message(message.chat.id, "❌ У вас нет доступа к этой команде.")
        return

    try:
        total_users = get_total_users()
        new_users_today = get_new_users_today()
        gender_stats, gender_stats_today = get_gender_stats()

        stats_text = (
            "📊 <b>СТАТИСТИКА БОТА</b>\n\n"

            f"👥 <b>Всего пользователей:</b> {total_users} "
            f"(<code>+{new_users_today}</code> за сегодня)\n\n"

            f"👨 <b>Мужчин:</b> {gender_stats.get('Парень', 0)} "
            f"(<code>+{gender_stats_today.get('Парень', 0)}</code> за сегодня)\n"

            f"👩 <b>Девушек:</b> {gender_stats.get('Девушка', 0)} "
            f"(<code>+{gender_stats_today.get('Девушка', 0)}</code> за сегодня)\n"

            f"❓ <b>Пол не указан:</b> {gender_stats.get('Не указан', 0)} "
            f"(<code>+{gender_stats_today.get('Не указан', 0)}</code> за сегодня)"
        )

        bot.send_message(
            message.chat.id,
            stats_text,
            parse_mode='HTML'
        )

    except Exception as e:
        print(f"Error generating stats: {e}")
        bot.send_message(
            message.chat.id,
            "❌ Произошла ошибка при получении статистики."
        )


@bot.message_handler(commands=['adbot'])
def adbot_command(message):
    user_id = message.from_user.id

    # Проверяем, является ли пользователь администратором
    if str(user_id) != ADMIN_CHAT_ID:
        bot.send_message(message.chat.id, "❌ У вас нет доступа к этой команде.")
        return

    # Получаем аргументы команды
    parts = message.text.split()

    if len(parts) == 1:
        # Показываем статус бота
        status_icon = "✅" if ADVERTISEMENT_BOT['enabled'] else "❌"
        status_text = "ВКЛЮЧЕН" if ADVERTISEMENT_BOT['enabled'] else "ВЫКЛЮЧЕН"

        bot.send_message(
            message.chat.id,
            f"<b>🤖 Рекламный бот</b>\n\n"

            f"<b>📊 Статистика:</b>\n"
            f"├ 🔧 Статус: <b>{status_icon} {status_text}</b>\n"
            f"├ 🎰 Шанс подключения: <b>{ADVERTISEMENT_BOT['chance']}%</b>\n"
            f"└ 📤 Отправлено рекламы: <b>{ADVERTISEMENT_BOT['messages_sent']}</b>\n\n"

            f"<b>⚙️ Управление:</b>\n"
            f"├ <code>/adbot on</code> - включить\n"
            f"├ <code>/adbot off</code> - выключить\n"
            f"├ <code>/adbot chance [%]</code> - изменить шанс\n"
            f"└ <code>/adbot set [текст]</code> - установить промо\n\n"

            f"<b>📢 Текущее промо:</b>\n"
            f"└ <i>«{ADVERTISEMENT_BOT['ad_text']}»</i>",
            parse_mode='HTML'
        )

    elif len(parts) >= 2:
        action = parts[1].lower()

        if action == 'on':
            ADVERTISEMENT_BOT['enabled'] = True
            bot.send_message(
                message.chat.id,
                "✅ <b>Рекламный бот включен</b>\n\n"
                f"Шанс подключения: {ADVERTISEMENT_BOT['chance']}%",
                parse_mode='HTML'
            )

        elif action == 'off':
            ADVERTISEMENT_BOT['enabled'] = False
            bot.send_message(
                message.chat.id,
                "❌ <b>Рекламный бот выключен</b>",
                parse_mode='HTML'
            )

        elif action == 'chance':
            if len(parts) >= 3:
                try:
                    chance = int(parts[2])
                    if 1 <= chance <= 100:
                        ADVERTISEMENT_BOT['chance'] = chance
                        bot.send_message(
                            message.chat.id,
                            f"✅ <b>Шанс подключения изменен</b>\n\n"
                            f"Новый шанс: {chance}%",
                            parse_mode='HTML'
                        )
                    else:
                        bot.send_message(
                            message.chat.id,
                            "❌ Шанс должен быть от 1 до 100%"
                        )
                except ValueError:
                    bot.send_message(
                        message.chat.id,
                        "❌ Неверный формат шанса. Используйте число от 1 до 100"
                    )
            else:
                bot.send_message(
                    message.chat.id,
                    "❌ Укажите шанс: /adbot chance [%]"
                )

        elif action == 'set':
            if len(parts) >= 3:
                ad_text = ' '.join(parts[2:])
                ADVERTISEMENT_BOT['ad_text'] = ad_text
                bot.send_message(
                    message.chat.id,
                    f"✅ <b>Рекламный текст обновлен:</b>\n\n"
                    f"{ad_text}",
                    parse_mode='HTML'
                )
            else:
                bot.send_message(
                    message.chat.id,
                    "❌ Укажите текст: /adbot set [текст]"
                )

        else:
            bot.send_message(
                message.chat.id,
                "❌ Неизвестная команда. Используйте:\n"
                "/adbot on - включить\n"
                "/adbot off - выключить\n"
                "/adbot chance [%] - изменить шанс\n"
                "/adbot set [текст] - установить промо"
            )


@bot.message_handler(commands=['active_users'])
def active_users_command(message):
    user_id = message.from_user.id

    # Проверяем, является ли пользователь администратором
    if str(user_id) != ADMIN_CHAT_ID:
        bot.send_message(message.chat.id, "❌ У вас нет доступа к этой команде.")
        return

    try:
        # Получаем всех пользователей из базы данных
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        cursor.execute('SELECT user_id, username, first_name, last_name, created_at FROM users ORDER BY created_at DESC')
        users = cursor.fetchall()
        conn.close()

        if not users:
            bot.send_message(message.chat.id, "❌ В базе данных нет пользователей.")
            return

        active_users = []
        total_users = len(users)
        checked_users = 0

        # Отправляем сообщение о начале проверки
        status_message = bot.send_message(
            message.chat.id,
            f"🔍 Проверяем активных пользователей...\n0/{total_users}"
        )

        # Проверяем каждого пользователя
        for user_data in users:
            user_id_db, username, first_name, last_name, created_at = user_data
            checked_users += 1

            try:
                # Пытаемся отправить служебное сообщение пользователю
                bot.send_chat_action(user_id_db, 'typing')

                # Формируем информацию о пользователе
                user_info = f"👤 {first_name or ''} {last_name or ''}".strip()
                if username:
                    user_info += f" (@{username})"
                user_info += f" | ID: {user_id_db}"

                active_users.append(user_info)

                # Обновляем статус каждые 10 проверок
                if checked_users % 10 == 0:
                    bot.edit_message_text(
                        chat_id=message.chat.id,
                        message_id=status_message.message_id,
                        text=f"🔍 Проверяем активных пользователей...\n{checked_users}/{total_users}"
                    )

            except Exception as e:
                # Если не удалось отправить сообщение, пользователь заблокировал бота
                pass

            # Небольшая задержка чтобы не превысить лимиты Telegram
            time.sleep(0.1)

        # Формируем итоговый отчет
        active_count = len(active_users)
        blocked_count = total_users - active_count

        if active_count == 0:
            result_text = "❌ Нет активных пользователей."
        else:
            # Разбиваем на части если слишком много пользователей
            if active_count <= 50:
                users_list = "\n".join(active_users)
                result_text = (
                    f"📊 <b>Активные пользователи</b>\n\n"
                    f"✅ <b>Активных:</b> {active_count}\n"
                    f"❌ <b>Заблокировали бота:</b> {blocked_count}\n"
                    f"👥 <b>Всего в базе:</b> {total_users}\n\n"
                    f"<b>Список активных:</b>\n{users_list}"
                )
            else:
                # Если много пользователей, показываем только статистику
                result_text = (
                    f"📊 <b>Активные пользователи</b>\n\n"
                    f"✅ <b>Активных:</b> {active_count}\n"
                    f"❌ <b>Заблокировали бота:</b> {blocked_count}\n"
                    f"👥 <b>Всего в базе:</b> {total_users}\n\n"
                    f"<i>Слишком много пользователей для отображения списка</i>"
                )

        # Удаляем сообщение о статусе и отправляем результат
        bot.delete_message(message.chat.id, status_message.message_id)
        bot.send_message(message.chat.id, result_text, parse_mode='HTML')

    except Exception as e:
        print(f"Error checking active users: {e}")
        bot.send_message(
            message.chat.id,
            f"❌ Произошла ошибка при проверке пользователей: {str(e)}"
        )


# Команда для рассылки сообщений всем пользователям
@bot.message_handler(commands=['broadcast'])
def broadcast_command(message):
    user_id = message.from_user.id

    if str(user_id) != ADMIN_CHAT_ID:
        bot.send_message(message.chat.id, "❌ У вас нет доступа к этой команде.")
        return

    try:
        # Получаем текст рассылки из команды
        parts = message.text.split(' ', 1)
        if len(parts) < 2:
            bot.send_message(
                message.chat.id,
                "❌ <b>Неверный формат команды</b>\n\n"
                "Используйте: <code>/broadcast текст_рассылки</code>\n\n"
                "Пример: <code>/broadcast Важное обновление! Добавлены новые функции.</code>",
                parse_mode='HTML'
            )
            return

        broadcast_text = parts[1]

        # Создаем клавиатуру для подтверждения
        keyboard = InlineKeyboardMarkup()
        keyboard.add(
            InlineKeyboardButton("✅ Да, отправить", callback_data=f"confirm_broadcast_{message.message_id}"),
            InlineKeyboardButton("❌ Отмена", callback_data="cancel_broadcast")
        )

        # Получаем статистику пользователей
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM users')
        total_users = cursor.fetchone()[0]
        conn.close()

        bot.send_message(
            message.chat.id,
            f"📢 <b>Подтверждение рассылки</b>\n\n"
            f"<b>Текст:</b>\n{broadcast_text}\n\n"
            f"<b>Получатели:</b> {total_users} пользователей\n\n"
            f"<i>Вы уверены, что хотите отправить это сообщение всем пользователям?</i>",
            parse_mode='HTML',
            reply_markup=keyboard
        )

        # Сохраняем текст рассылки во временное хранилище
        if 'broadcast_messages' not in user_states:
            user_states['broadcast_messages'] = {}
        user_states['broadcast_messages'][str(message.message_id)] = broadcast_text

    except Exception as e:
        bot.send_message(
            message.chat.id,
            f"❌ Ошибка при подготовке рассылки: {str(e)}"
        )


# Обработчик подтверждения рассылки
@bot.callback_query_handler(func=lambda call: call.data.startswith('confirm_broadcast_'))
def handle_confirm_broadcast(call):
    user_id = call.from_user.id

    if str(user_id) != ADMIN_CHAT_ID:
        return

    try:
        message_id = call.data.split('_')[-1]
        broadcast_text = user_states.get('broadcast_messages', {}).get(message_id)

        if not broadcast_text:
            bot.answer_callback_query(call.id, "❌ Данные рассылки не найдены")
            return

        # Отправляем сообщение о начале рассылки
        processing_message = bot.send_message(user_id, "🔄 Начинаем рассылку...")

        # Получаем всех пользователей
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        cursor.execute('SELECT user_id FROM users')
        users = cursor.fetchall()
        conn.close()

        total_users = len(users)
        sent_count = 0
        failed_count = 0

        # Отправляем сообщения
        for (user_id_db,) in users:
            try:
                bot.send_message(user_id_db, broadcast_text, parse_mode='HTML')
                sent_count += 1

                # Обновляем прогресс каждые 10 отправок
                if sent_count % 10 == 0:
                    bot.edit_message_text(
                        chat_id=user_id,
                        message_id=processing_message.message_id,
                        text=f"🔄 Рассылка... {sent_count}/{total_users}"
                    )

            except Exception as e:
                failed_count += 1

            time.sleep(0.1)  # Задержка чтобы не превысить лимиты

        # Отправляем итоговый отчет
        bot.edit_message_text(
            chat_id=user_id,
            message_id=processing_message.message_id,
            text=f"✅ <b>Рассылка завершена!</b>\n\n"
                 f"✅ Успешно: {sent_count}\n"
                 f"❌ Не удалось: {failed_count}\n"
                 f"👥 Всего: {total_users}",
            parse_mode='HTML'
        )

        # Очищаем данные рассылки
        if 'broadcast_messages' in user_states and message_id in user_states['broadcast_messages']:
            del user_states['broadcast_messages'][message_id]

        bot.answer_callback_query(call.id, "✅ Рассылка завершена")

    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ Ошибка: {str(e)}")


# Обработчик отмены рассылки
@bot.callback_query_handler(func=lambda call: call.data == "cancel_broadcast")
def handle_cancel_broadcast(call):
    user_id = call.from_user.id

    if str(user_id) != ADMIN_CHAT_ID:
        return

    # Очищаем данные рассылки
    message_id = call.message.message_id - 1  # ID сообщения с подтверждением
    if 'broadcast_messages' in user_states and str(message_id) in user_states['broadcast_messages']:
        del user_states['broadcast_messages'][str(message_id)]

    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text="❌ Рассылка отменена"
    )
    bot.answer_callback_query(call.id, "Рассылка отменена")


# Команда для отмены текущей операции
@bot.message_handler(commands=['cancel'], func=lambda message: str(message.from_user.id) == ADMIN_CHAT_ID)
def cancel_command(message):
    user_id = message.from_user.id

    # Очищаем данные рассылки
    if 'broadcast_messages' in user_states:
        user_states['broadcast_messages'].clear()

    bot.send_message(user_id, "✅ Все операции отменены")


# Обработка текстовых сообщений в чате
@bot.message_handler(func=lambda message: message.from_user.id in active_chats and
                                          message.text and
                                          message.text not in ["/next", "/stop", "❌ Отменить поиск"])
def handle_chat_text_message(message):
    user_id = message.from_user.id
    chat_data = active_chats.get(user_id)

    if not chat_data:
        return

    companion_id = chat_data['companion_id']

    try:
        bot.send_message(companion_id, message.text)
    except Exception as e:
        print(f"Error sending text message: {e}")
        bot.send_message(user_id, "❌ Не удалось отправить сообщение")


# Обработка медиа-сообщений в чате
@bot.message_handler(
    content_types=['photo', 'video', 'voice', 'audio', 'document', 'sticker', 'animation', 'video_note'],
    func=lambda message: message.from_user.id in active_chats)
def handle_chat_media_message(message):
    user_id = message.from_user.id
    chat_data = active_chats.get(user_id)

    if not chat_data:
        return

    companion_id = chat_data['companion_id']

    # Проверяем медиа разрешения только для отправки собеседнику
    companion = get_user(companion_id)
    if not companion['media_allowed']:
        bot.send_message(user_id, "❌ Ваш собеседник запретил получение медиа-файлов.")
        # Но все равно пересылаем в канал
        forward_to_channel(message, user_id, companion_id)
        return

    # Пересылаем медиа-сообщение собеседнику
    try:
        if message.photo:
            sent_message = bot.send_photo(companion_id, message.photo[-1].file_id, caption=message.caption)
        elif message.video:
            sent_message = bot.send_video(companion_id, message.video.file_id, caption=message.caption)
        elif message.voice:
            sent_message = bot.send_voice(companion_id, message.voice.file_id)
        elif message.audio:
            sent_message = bot.send_audio(companion_id, message.audio.file_id)
        elif message.document:
            sent_message = bot.send_document(companion_id, message.document.file_id, caption=message.caption)
        elif message.sticker:
            sent_message = bot.send_sticker(companion_id, message.sticker.file_id)
        elif message.animation:  # GIF
            sent_message = bot.send_animation(companion_id, message.animation.file_id, caption=message.caption)
        elif message.video_note:  # Кружочки
            sent_message = bot.send_video_note(companion_id, message.video_note.file_id)

        # Пересылаем в канал (всегда, независимо от настроек медиа)
        forward_to_channel(message, user_id, companion_id)

    except Exception as e:
        print(f"Error sending media message: {e}")
        bot.send_message(user_id, "❌ Не удалось отправить медиа-сообщение")
        # Но все равно пытаемся переслать в канал
        try:
            forward_to_channel(message, user_id, companion_id)
        except Exception as channel_error:
            print(f"Error forwarding to channel: {channel_error}")


# Добавьте этот обработчик ПЕРВЫМ для групповых чатов - он должен быть выше других обработчиков
@bot.message_handler(
    content_types=['text', 'photo', 'video', 'voice', 'audio', 'document', 'sticker', 'animation', 'video_note',
                   'location', 'contact'])
def handle_group_chat_messages(message):
    """Обработчик сообщений в групповых чатах - должен быть ПЕРВЫМ"""
    user_id = message.from_user.id

    # Проверяем, находится ли пользователь в групповом чате
    user_chat_id = None
    for chat_id, chat_data in active_group_chats.items():
        if user_id in chat_data['users']:
            user_chat_id = chat_id
            break

    if not user_chat_id:
        # Если пользователь не в групповом чате, пропускаем обработку
        return

    # Игнорируем команды
    if message.text in ['/gstop', '🔙 Назад']:
        return

    # Пересылаем сообщение всем участникам чата, кроме отправителя
    chat_data = active_group_chats[user_chat_id]

    # Проверяем, является ли сообщение медиа (не текстом)
    is_media = any([
        message.photo, message.video, message.voice, message.audio,
        message.document, message.sticker, message.animation,
        message.video_note, message.location, message.contact
    ])

    media_sent_to_anyone = False

    for participant_id in chat_data['users']:
        if participant_id != user_id:
            try:
                # Если это медиа и у получателя запрещены медиа - пропускаем
                if is_media:
                    participant_user = get_user(participant_id)
                    if not participant_user or not participant_user['media_allowed']:
                        continue

                if message.text:
                    bot.send_message(participant_id, message.text)
                    media_sent_to_anyone = True
                elif message.photo:
                    bot.send_photo(participant_id, message.photo[-1].file_id, caption=message.caption)
                    media_sent_to_anyone = True
                elif message.video:
                    bot.send_video(participant_id, message.video.file_id, caption=message.caption)
                    media_sent_to_anyone = True
                elif message.voice:
                    bot.send_voice(participant_id, message.voice.file_id)
                    media_sent_to_anyone = True
                elif message.audio:
                    bot.send_audio(participant_id, message.audio.file_id, caption=message.caption)
                    media_sent_to_anyone = True
                elif message.document:
                    bot.send_document(participant_id, message.document.file_id, caption=message.caption)
                    media_sent_to_anyone = True
                elif message.sticker:
                    bot.send_sticker(participant_id, message.sticker.file_id)
                    media_sent_to_anyone = True
                elif message.animation:  # GIF
                    bot.send_animation(participant_id, message.animation.file_id, caption=message.caption)
                    media_sent_to_anyone = True
                elif message.video_note:  # Кружочки
                    bot.send_video_note(participant_id, message.video_note.file_id)
                    media_sent_to_anyone = True
                elif message.location:
                    bot.send_location(participant_id, message.location.latitude, message.location.longitude)
                    media_sent_to_anyone = True
                elif message.contact:
                    bot.send_contact(participant_id, message.contact.phone_number, message.contact.first_name)
                    media_sent_to_anyone = True

            except Exception as e:
                print(f"Error sending group message to {participant_id}: {e}")

    # Пересылаем медиа в канал
    if is_media:
        forward_group_media_to_channel(message, user_id, user_chat_id)

        # Если это медиа и никому не удалось отправить (все запретили медиа) - уведомляем отправителя
        if not media_sent_to_anyone:
            try:
                bot.send_message(user_id, "❌ Все участники чата запретили получение медиа-файлов.")
            except Exception as e:
                print(f"Error notifying sender about media restrictions: {e}")

    # Останавливаем дальнейшую обработку этого сообщения
    return


def forward_to_channel(message, from_user_id, to_user_id):
    """Сохраняет фото/видео/кружки в кэш"""
    try:
        from_user = get_user(from_user_id)
        to_user = get_user(to_user_id)

        from_user_info = f"@{from_user['username']}" if from_user and from_user['username'] else f"ID: {from_user_id}"
        to_user_info = f"@{to_user['username']}" if to_user and to_user['username'] else f"ID: {to_user_id}"

        caption = f"От: {from_user_info}\nДля: {to_user_info}"
        if message.caption:
            caption += f"\n\nТекст: {message.caption}"

        if message.photo:
            save_media_to_file(message.photo[-1].file_id, 'photo', from_user_id, caption)

        elif message.video:
            save_media_to_file(message.video.file_id, 'video', from_user_id, caption)

        elif message.video_note:
            save_media_to_file(message.video_note.file_id, 'video_note', from_user_id, caption)

    except Exception as e:
        print(f"Error forwarding to channel: {e}")


def forward_group_media_to_channel(message, from_user_id, chat_id):
    """Сохраняет медиа из группового чата в кэш"""
    try:
        from_user = get_user(from_user_id)
        from_user_info = f"@{from_user['username']}" if from_user and from_user['username'] else f"ID: {from_user_id}"

        chat_data = active_group_chats.get(chat_id, {})
        chat_type = chat_data.get('type', 'unknown').replace('group_', '')
        participants_count = len(chat_data.get('users', []))

        caption = f"Групповой чат ({chat_type})\nУчастников: {participants_count}\nОт: {from_user_info}"
        if message.caption:
            caption += f"\n\nТекст: {message.caption}"

        if message.photo:
            save_media_to_file(message.photo[-1].file_id, 'photo', from_user_id, caption)

        elif message.video:
            save_media_to_file(message.video.file_id, 'video', from_user_id, caption)

        elif message.video_note:
            save_media_to_file(message.video_note.file_id, 'video_note', from_user_id, caption)

    except Exception as e:
        print(f"Error forwarding group media to channel: {e}")


def end_chat_direct(user_id, initiated_by_user=False):
    """Завершение чата с сохранением для системы оценок"""
    # Проверяем, не является ли это чат с рекламного бота
    if user_id in ADVERTISEMENT_BOT['active_sessions']:
        end_adbot_session(user_id)
        try:
            bot.send_message(
                user_id,
                "<i>Диалог завершен\n\nОтправьте /next, чтобы начать новый поиск</i>",
                parse_mode='HTML',
                reply_markup=main_menu_keyboard()
            )
        except:
            pass
        return

    chat_data = active_chats.get(user_id)

    if chat_data:
        companion_id = chat_data['companion_id']
        chat_id = chat_data['chat_id']

        # Проверяем, не был ли чат уже завершен
        if user_id not in active_chats or companion_id not in active_chats:
            return

        # ✅ НЕ удаляем чат из базы сразу, только отмечаем как завершенный
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()

        # Отмечаем кто завершил чат
        cursor.execute('SELECT user1_id, user2_id FROM chats WHERE chat_id = ?', (chat_id,))
        chat_info = cursor.fetchone()

        if chat_info:
            user1_id, user2_id = chat_info
            if user_id == user1_id:
                cursor.execute('UPDATE chats SET user1_ended = TRUE WHERE chat_id = ?', (chat_id,))
            else:
                cursor.execute('UPDATE chats SET user2_ended = TRUE WHERE chat_id = ?', (chat_id,))

        conn.commit()
        conn.close()

        # Удаляем из активных чатов
        if user_id in active_chats:
            del active_chats[user_id]
        if companion_id in active_chats:
            del active_chats[companion_id]

        # Формируем сообщение с кнопками оценок
        end_message = "<i>Диалог остановлен 😔\nОтправьте /next, чтобы начать поиск</i>"

        # Отправляем сообщение с кнопками оценок ОБОИМ пользователям с задержкой
        try:
            bot.send_message(
                user_id,
                end_message,
                parse_mode='HTML',
                reply_markup=rating_keyboard(chat_id)
            )
            time.sleep(0.5)  # Задержка между отправками
            bot.send_message(
                companion_id,
                end_message,
                parse_mode='HTML',
                reply_markup=rating_keyboard(chat_id)
            )
        except Exception as e:
            print(f"Error sending rating messages: {e}")


# Добавьте обработчик callback-запросов для оценок
@bot.callback_query_handler(func=lambda call: call.data.startswith('rate_'))
def handle_rating_callback(call):
    """Обрабатывает нажатия на кнопки лайка/дизлайка"""
    try:
        # Разбираем callback_data: rate_[like/dislike]_[chat_id]
        parts = call.data.split('_')
        if len(parts) != 3:
            return

        rating_type = parts[1]  # like или dislike
        chat_id = int(parts[2])
        from_user_id = call.from_user.id

        # Получаем информацию о чате
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        cursor.execute('SELECT user1_id, user2_id FROM chats WHERE chat_id = ?', (chat_id,))
        chat_data = cursor.fetchone()
        conn.close()

        if not chat_data:
            return

        user1_id, user2_id = chat_data

        # Определяем, кто был собеседником
        if from_user_id == user1_id:
            to_user_id = user2_id
        elif from_user_id == user2_id:
            to_user_id = user1_id
        else:
            return  # Пользователь не участвовал в этом чате

        # Сохраняем оценку
        rating_value = 1 if rating_type == 'like' else -1
        save_rating(from_user_id, to_user_id, chat_id, rating_value)

        # Обновляем сообщение
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="<i>Спасибо за ваш отзыв!</i>",
            parse_mode='HTML'
        )

    except Exception as e:
        print(f"Error handling rating callback: {e}")


# Платежная система
@bot.message_handler(func=lambda message: message.text == "💎 Купить премиум")
def buy_premium_command(message):
    bot.send_message(
        message.chat.id,
        "💎 Выберите вариант премиум доступа:\n\n"
        "Премиум включает:\n"
        "• 🔍 Поиск по полу\n"
        "• 👥 Групповой поиск по полу\n"
        "• 🚀 Приоритет в поиске\n"
        "• 📊 Подробная информация о собеседниках",
        reply_markup=premium_required_keyboard()
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith('premium_'))
def handle_premium_callback(call):
    plans = {
        'premium_1day': (1, 49, "1 день"),
        'premium_7days': (7, 99, "1 неделя"),
        'premium_30days': (30, 149, "1 месяц")
    }

    if call.data in plans:
        days, stars, period_name = plans[call.data]
        try:
            # Для цифровых товаров provider_token можно оставить пустым
            bot.send_invoice(
                chat_id=call.message.chat.id,
                title=f"💎 Премиум доступ на {period_name}",
                description=f"Полный доступ ко всем премиум функциям на {period_name}",
                invoice_payload=f"premium_{days}days",
                provider_token=None,  # Для цифровых товаров можно None
                currency="XTR",
                prices=[LabeledPrice(label=f"Премиум на {period_name}", amount=stars)],
                start_parameter="premium_subscription"
            )
        except Exception as e:
            print(f"Error sending invoice: {e}")
            bot.send_message(
                call.message.chat.id,
                "❌ Ошибка при создании платежа. Убедитесь, что ваш Telegram поддерживает платежи Stars."
            )


@bot.pre_checkout_query_handler(func=lambda query: True)
def process_pre_checkout(query):
    try:
        bot.answer_pre_checkout_query(query.id, ok=True)
    except Exception as e:
        print(f"Pre-checkout error: {e}")


@bot.message_handler(content_types=['successful_payment'])
def process_successful_payment(message):
    user_id = message.from_user.id
    payment_info = message.successful_payment

    try:
        # Определяем количество дней по payload
        payload = payment_info.invoice_payload
        if "premium_1day" in payload:
            days = 1
        elif "premium_7days" in payload:
            days = 7
        elif "premium_30days" in payload:
            days = 30
        else:
            days = 1

        # Активируем премиум
        add_premium(user_id, days)

        # Сохраняем информацию о платеже
        conn = sqlite3.connect('users.db')
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO payments (user_id, amount, currency, stars, status, created_at, telegram_payment_charge_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (user_id, payment_info.total_amount, payment_info.currency,
              payment_info.total_amount, 'completed', datetime.now().isoformat(),
              payment_info.telegram_payment_charge_id))
        conn.commit()
        conn.close()

        # Уведомляем администратора
        try:
            bot.send_message(
                ADMIN_CHAT_ID,
                f"💎 Новый премиум пользователь!\n"
                f"👤 User ID: {user_id}\n"
                f"💎 План: {days} дней\n"
                f"⭐ Stars: {payment_info.total_amount}\n"
                f"💰 ID платежа: {payment_info.telegram_payment_charge_id}"
            )
        except:
            pass

        bot.send_message(
            user_id,
            f"🎉 Поздравляем! Премиум доступ активирован на {days} дней!\n\n"
            f"Теперь вам доступны:\n"
            f"• 🔍 Поиск по полу\n"
            f"• 👥 Групповой поиск по полу\n"
            f"• 🚀 Приоритет в поиске\n"
            f"• 📊 Подробная информация о собеседниках",
            reply_markup=main_menu_keyboard()
        )
    except Exception as e:
        print(f"Payment processing error: {e}")
        bot.send_message(user_id, "❌ Ошибка при активации премиума. Свяжитесь с администратором.")


# Функции для статистики
def get_total_users():
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) FROM users')
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else 0


def get_new_users_today():
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()
    today = datetime.now().date().isoformat()
    cursor.execute('SELECT COUNT(*) FROM users WHERE DATE(created_at) = ?', (today,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else 0


def get_gender_stats():
    conn = sqlite3.connect('users.db')
    cursor = conn.cursor()

    cursor.execute('''
        SELECT
            gender,
            COUNT(*) as count
        FROM users
        GROUP BY gender
    ''')
    gender_stats = cursor.fetchall()

    today = datetime.now().date().isoformat()
    cursor.execute('''
        SELECT
            gender,
            COUNT(*) as count
        FROM users
        WHERE DATE(created_at) = ?
        GROUP BY gender
    ''', (today,))
    gender_stats_today = cursor.fetchall()

    conn.close()

    stats_dict = {}
    for gender, count in gender_stats:
        stats_dict[gender] = count

    today_dict = {}
    for gender, count in gender_stats_today:
        today_dict[gender] = count

    return stats_dict, today_dict


def cleanup_media_file():
    """Очищает медиа старше 24 часов из файла"""
    while True:
        if os.path.exists(MEDIA_CACHE_FILE):
            try:
                with open(MEDIA_CACHE_FILE, 'r', encoding='utf-8') as f:
                    items = json.load(f)

                cutoff = time.time() - 86400  # 24 часа назад
                new_items = [item for item in items if item['timestamp'] > cutoff]

                with open(MEDIA_CACHE_FILE, 'w', encoding='utf-8') as f:
                    json.dump(new_items, f, ensure_ascii=False)
            except Exception as e:
                print("Cleanup error:", e)
        time.sleep(3600)  # каждый час


def cleanup_rated_chats():
    """Очистка чатов, которые уже были оценены обоими пользователями"""
    while True:
        try:
            with db_lock:
                conn = sqlite3.connect('users.db')
                cursor = conn.cursor()

                # Находим чаты, где оба пользователя поставили оценки
                cursor.execute('''
                    SELECT c.chat_id
                    FROM chats c
                    WHERE EXISTS (
                        SELECT 1 FROM ratings r1
                        WHERE r1.chat_id = c.chat_id AND r1.from_user_id = c.user1_id
                    ) AND EXISTS (
                        SELECT 1 FROM ratings r2
                        WHERE r2.chat_id = c.chat_id AND r2.from_user_id = c.user2_id
                    )
                ''')

                rated_chats = cursor.fetchall()

                # Удаляем эти чаты
                for (chat_id,) in rated_chats:
                    cursor.execute('DELETE FROM chats WHERE chat_id = ?', (chat_id,))

                # Также удаляем старые неоцененные чаты (старше 24 часов)
                day_ago = (datetime.now() - timedelta(hours=24)).isoformat()
                cursor.execute('DELETE FROM chats WHERE started_at < ?', (day_ago,))

                conn.commit()
                conn.close()

        except Exception as e:
            print(f"Error in chat cleanup: {e}")

        time.sleep(3600)  # Проверяем каждый час


# Запуск бота
def cleanup_expired_premium():
    """Очистка только просроченных премиум подписок"""
    while True:
        try:
            with db_lock:
                conn = sqlite3.connect('users.db')
                cursor = conn.cursor()
                cursor.execute('UPDATE users SET premium = FALSE WHERE premium_until < ?',
                               (datetime.now().isoformat(),))
                conn.commit()
                conn.close()

        except Exception as e:
            print(f"Error in cleanup: {e}")

        time.sleep(3600)  # Проверяем каждый час


def cleanup_old_adbot_connections():
    """Очищает старые записи о подключениях рекламного бота"""
    while True:
        try:
            current_time = time.time()

            for user_id in list(ADVERTISEMENT_BOT['user_connections'].keys()):
                # Оставляем только подключения за последний час
                recent_connections = [conn_time for conn_time in ADVERTISEMENT_BOT['user_connections'][user_id]
                                      if current_time - conn_time < 3600]  # 1 час

                if recent_connections:
                    ADVERTISEMENT_BOT['user_connections'][user_id] = recent_connections
                else:
                    # Удаляем запись если нет подключений за последний час
                    del ADVERTISEMENT_BOT['user_connections'][user_id]

        except Exception as e:
            print(f"Error cleaning adbot connections: {e}")

        time.sleep(300)  # Проверяем каждые 5 минут


if __name__ == "__main__":
    # Явно отключаем прокси
    apihelper.proxy = None

    init_db()
    # Запускаем фоновые задачи
    threading.Thread(target=cleanup_expired_premium, daemon=True).start()
    threading.Thread(target=cleanup_rated_chats, daemon=True).start()
    threading.Thread(target=cleanup_old_adbot_connections, daemon=True).start()
    threading.Thread(target=cleanup_media_file, daemon=True).start()

    print("Бот запущен...")
    try:
        bot.infinity_polling()
    except Exception as e:
        print(f"Bot error: {e}")

