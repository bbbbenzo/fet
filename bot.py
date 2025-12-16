import asyncio
import logging
import os
from typing import Optional, Dict
import asyncpg
from contextlib import asynccontextmanager

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton, CallbackQuery
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

# ========== НАСТРОЙКИ APPLE-СТИЛЯ ==========
load_dotenv()


class AppleDesign:
    """Дизайн в стиле Apple — минимализм и элегантность"""

    EMOJI = {
        "welcome": "👋",
        "search": "🔍",
        "found": "✅",
        "chat": "💬",
        "stop": "⏹",
        "stats": "📊",
        "cancel": "✖",
        "warning": "⚠",
        "success": "✓",
        "error": "✗",
        "time": "⏱",
        "user": "👤",
        "bot": "🤖",
        "lock": "🔒",
        "shield": "🛡",
        "sparkle": "✨",
        "rocket": "🚀",
        "check": "✔",
        "loading": "⏳",
        "connection": "🔗",
        "privacy": "🕶",
        "typing": "✍",
        "notification": "🔔",
        "sad": "😔",
        "confused": "😐",
        "stop_sign": "⛔️",
        "male": "👨",
        "female": "👩",
        "any": "👥",
        "edit": "✏️",
        "back": "↩️",
        "info": "ℹ️"
    }

    @staticmethod
    def format_header(text: str) -> str:
        """Форматирование заголовка"""
        return f"<b>{text}</b>"

    @staticmethod
    def format_subheader(text: str) -> str:
        """Форматирование подзаголовка"""
        return f"<i>{text}</i>"

    @staticmethod
    def format_text(text: str) -> str:
        """Форматирование обычного текста в курсиве"""
        return f"<i>{text}</i>"

    @staticmethod
    def create_divider() -> str:
        """Создание разделителя"""
        return "―" * 32


# ========== КОНФИГУРАЦИЯ ==========
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(','))) if os.getenv("ADMIN_IDS") else []


# ========== БАЗА ДАННЫХ ==========
class AppleDatabase:
    """База данных PostgreSQL для Railway"""

    def __init__(self):
        self.dsn = os.getenv("DATABASE_URL")
        self.pool = None

    async def init(self):
        """Инициализация подключения и создание таблиц"""
        if not self.dsn:
            logging.error("❌ DATABASE_URL не найден")
            return False

        try:
            self.pool = await asyncpg.create_pool(
                dsn=self.dsn,
                min_size=1,
                max_size=10,
                command_timeout=60
            )
            await self._init_tables()
            logging.info("✅ База данных инициализирована")
            return True
        except Exception as e:
            logging.error(f"❌ Ошибка подключения: {e}")
            return False

    @asynccontextmanager
    async def _get_connection(self):
        """Контекстный менеджер для подключений"""
        async with self.pool.acquire() as connection:
            yield connection

    async def _init_tables(self):
        """Создание таблиц"""
        async with self._get_connection() as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT UNIQUE NOT NULL,
                    username VARCHAR(255),
                    first_name VARCHAR(255),
                    gender VARCHAR(10) DEFAULT 'unknown',
                    search_gender VARCHAR(10) DEFAULT 'any',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    message_count INTEGER DEFAULT 0,
                    session_count INTEGER DEFAULT 0,
                    is_active BOOLEAN DEFAULT TRUE,
                    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id SERIAL PRIMARY KEY,
                    user1_id INTEGER NOT NULL REFERENCES users(id),
                    user2_id INTEGER NOT NULL REFERENCES users(id),
                    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    ended_at TIMESTAMP,
                    message_count INTEGER DEFAULT 0
                )
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS active_connections (
                    telegram_id BIGINT PRIMARY KEY,
                    partner_telegram_id BIGINT NOT NULL,
                    session_id INTEGER NOT NULL REFERENCES sessions(id),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await conn.execute("""
                CREATE TABLE IF NOT EXISTS search_queue (
                    telegram_id BIGINT PRIMARY KEY,
                    gender VARCHAR(10) DEFAULT 'any',
                    search_gender VARCHAR(10) DEFAULT 'any',
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

    async def create_or_update_user(self, telegram_id: int, username: str, first_name: str) -> dict:
        """Создание или обновление пользователя"""
        async with self._get_connection() as conn:
            user = await conn.fetchrow(
                "SELECT * FROM users WHERE telegram_id = $1",
                telegram_id
            )

            if user:
                await conn.execute("""
                    UPDATE users 
                    SET username = $1, first_name = $2, 
                        updated_at = CURRENT_TIMESTAMP, last_seen = CURRENT_TIMESTAMP
                    WHERE telegram_id = $3
                """, username, first_name, telegram_id)
                user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", telegram_id)
            else:
                await conn.execute("""
                    INSERT INTO users (telegram_id, username, first_name) 
                    VALUES ($1, $2, $3)
                """, telegram_id, username, first_name)
                user = await conn.fetchrow("SELECT * FROM users WHERE telegram_id = $1", telegram_id)

            return dict(user) if user else {}

    async def update_user_gender(self, telegram_id: int, gender: str) -> bool:
        """Обновление пола пользователя"""
        async with self._get_connection() as conn:
            await conn.execute("""
                UPDATE users 
                SET gender = $1, updated_at = CURRENT_TIMESTAMP 
                WHERE telegram_id = $2
            """, gender, telegram_id)
            return True

    async def update_user_search_gender(self, telegram_id: int, search_gender: str) -> bool:
        """Обновление предпочитаемого пола для поиска"""
        async with self._get_connection() as conn:
            await conn.execute("""
                UPDATE users 
                SET search_gender = $1, updated_at = CURRENT_TIMESTAMP 
                WHERE telegram_id = $2
            """, search_gender, telegram_id)
            return True

    async def join_search_queue(self, telegram_id: int) -> bool:
        """Добавление в очередь поиска"""
        async with self._get_connection() as conn:
            # Получаем данные пользователя
            user = await conn.fetchrow(
                "SELECT gender, search_gender FROM users WHERE telegram_id = $1",
                telegram_id
            )

            if not user:
                return False

            gender = user['gender']
            search_gender = user['search_gender']

            # Проверяем, не в поиске ли уже
            in_queue = await conn.fetchrow(
                "SELECT * FROM search_queue WHERE telegram_id = $1",
                telegram_id
            )
            if in_queue:
                return False

            # Проверяем, не в активном чате ли
            in_chat = await conn.fetchrow(
                "SELECT * FROM active_connections WHERE telegram_id = $1",
                telegram_id
            )
            if in_chat:
                return False

            # Добавляем в очередь с учетом пола
            await conn.execute("""
                INSERT INTO search_queue (telegram_id, gender, search_gender) 
                VALUES ($1, $2, $3)
            """, telegram_id, gender, search_gender)
            return True

    async def leave_search_queue(self, telegram_id: int) -> bool:
        """Выход из очереди поиска"""
        async with self._get_connection() as conn:
            result = await conn.execute(
                "DELETE FROM search_queue WHERE telegram_id = $1",
                telegram_id
            )
            return "DELETE 1" in result

    async def find_partner(self, telegram_id: int) -> Optional[int]:
        """Поиск партнера для чата с учетом предпочтений по полу"""
        async with self._get_connection() as conn:
            # Получаем данные пользователя
            user_data = await conn.fetchrow("""
                SELECT gender, search_gender FROM users WHERE telegram_id = $1
            """, telegram_id)

            if not user_data:
                return None

            user_gender = user_data['gender']
            user_search_gender = user_data['search_gender']

            # Ищем партнера с учетом предпочтений
            if user_search_gender == 'any':
                # Пользователь ищет любого
                partner = await conn.fetchrow("""
                    SELECT s.telegram_id, u.gender 
                    FROM search_queue s
                    JOIN users u ON s.telegram_id = u.telegram_id
                    WHERE s.telegram_id != $1 
                    AND (s.search_gender = 'any' OR s.search_gender = $2)
                    ORDER BY s.joined_at 
                    LIMIT 1
                """, telegram_id, user_gender)
            else:
                # Пользователь ищет конкретный пол
                partner = await conn.fetchrow("""
                    SELECT s.telegram_id, u.gender 
                    FROM search_queue s
                    JOIN users u ON s.telegram_id = u.telegram_id
                    WHERE s.telegram_id != $1 
                    AND u.gender = $2
                    AND (s.search_gender = 'any' OR s.search_gender = $3)
                    ORDER BY s.joined_at 
                    LIMIT 1
                """, telegram_id, user_search_gender, user_gender)

            if not partner:
                return None

            partner_telegram_id = partner['telegram_id']

            # Получаем ID пользователей из таблицы users
            user = await conn.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
            partner_user = await conn.fetchrow("SELECT id FROM users WHERE telegram_id = $1", partner_telegram_id)

            if not user or not partner_user:
                return None

            user_id = user['id']
            partner_id = partner_user['id']

            # Удаляем обоих из очереди
            await conn.execute("DELETE FROM search_queue WHERE telegram_id IN ($1, $2)",
                               telegram_id, partner_telegram_id)

            # Создаем сессию
            session = await conn.fetchrow("""
                INSERT INTO sessions (user1_id, user2_id) 
                VALUES ($1, $2)
                RETURNING id
            """, user_id, partner_id)
            session_id = session['id']

            # Создаем активные соединения
            await conn.execute("""
                INSERT INTO active_connections (telegram_id, partner_telegram_id, session_id) 
                VALUES ($1, $2, $3)
                ON CONFLICT (telegram_id) DO UPDATE SET
                partner_telegram_id = $2,
                session_id = $3,
                created_at = CURRENT_TIMESTAMP
            """, telegram_id, partner_telegram_id, session_id)

            await conn.execute("""
                INSERT INTO active_connections (telegram_id, partner_telegram_id, session_id) 
                VALUES ($1, $2, $3)
                ON CONFLICT (telegram_id) DO UPDATE SET
                partner_telegram_id = $2,
                session_id = $3,
                created_at = CURRENT_TIMESTAMP
            """, partner_telegram_id, telegram_id, session_id)

            return partner_telegram_id

    async def get_active_partner(self, telegram_id: int) -> Optional[int]:
        """Получение активного партнера"""
        async with self._get_connection() as conn:
            result = await conn.fetchrow("""
                SELECT partner_telegram_id FROM active_connections 
                WHERE telegram_id = $1
            """, telegram_id)
            return result['partner_telegram_id'] if result else None

    async def end_session(self, telegram_id: int) -> Optional[int]:
        """Завершение сессии"""
        async with self._get_connection() as conn:
            result = await conn.fetchrow("""
                SELECT partner_telegram_id, session_id FROM active_connections 
                WHERE telegram_id = $1
            """, telegram_id)

            if not result:
                return None

            partner_telegram_id = result['partner_telegram_id']
            session_id = result['session_id']

            user = await conn.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
            partner = await conn.fetchrow("SELECT id FROM users WHERE telegram_id = $1", partner_telegram_id)

            await conn.execute("DELETE FROM active_connections WHERE telegram_id IN ($1, $2)",
                               telegram_id, partner_telegram_id)

            if session_id:
                await conn.execute("""
                    UPDATE sessions 
                    SET ended_at = CURRENT_TIMESTAMP 
                    WHERE id = $1
                """, session_id)

            if user:
                await conn.execute("""
                    UPDATE users 
                    SET session_count = session_count + 1, 
                        updated_at = CURRENT_TIMESTAMP 
                    WHERE id = $1
                """, user['id'])

            if partner:
                await conn.execute("""
                    UPDATE users 
                    SET session_count = session_count + 1, 
                        updated_at = CURRENT_TIMESTAMP 
                    WHERE id = $1
                """, partner['id'])

            return partner_telegram_id

    async def increment_message_count(self, telegram_id: int):
        """Увеличение счетчика сообщений"""
        async with self._get_connection() as conn:
            await conn.execute("""
                UPDATE users 
                SET message_count = message_count + 1, 
                    updated_at = CURRENT_TIMESTAMP,
                    last_seen = CURRENT_TIMESTAMP 
                WHERE telegram_id = $1
            """, telegram_id)

    async def get_user_stats(self, telegram_id: int) -> dict:
        """Получение статистики пользователя"""
        async with self._get_connection() as conn:
            result = await conn.fetchrow("""
                SELECT 
                    u.*,
                    (SELECT COUNT(*) FROM users) as total_users,
                    (SELECT COUNT(*) FROM search_queue) as searching_users,
                    (SELECT COUNT(*) FROM active_connections) / 2 as active_chats
                FROM users u
                WHERE u.telegram_id = $1
            """, telegram_id)

            return dict(result) if result else {}

    async def get_user_by_telegram_id(self, telegram_id: int) -> Optional[dict]:
        """Получение пользователя по Telegram ID"""
        async with self._get_connection() as conn:
            result = await conn.fetchrow(
                "SELECT * FROM users WHERE telegram_id = $1",
                telegram_id
            )
            return dict(result) if result else None

    async def cleanup_old_searches(self, hours: int = 1):
        """Очистка старых поисков"""
        async with self._get_connection() as conn:
            await conn.execute("""
                DELETE FROM search_queue 
                WHERE joined_at < NOW() - INTERVAL '$1 HOURS'
            """, hours)

    async def get_user_gender(self, telegram_id: int) -> str:
        """Получение пола пользователя"""
        async with self._get_connection() as conn:
            result = await conn.fetchrow(
                "SELECT gender FROM users WHERE telegram_id = $1",
                telegram_id
            )
            return result['gender'] if result else 'unknown'

    async def get_user_search_gender(self, telegram_id: int) -> str:
        """Получение предпочитаемого пола для поиска"""
        async with self._get_connection() as conn:
            result = await conn.fetchrow(
                "SELECT search_gender FROM users WHERE telegram_id = $1",
                telegram_id
            )
            return result['search_gender'] if result else 'any'


# ========== ИНИЦИАЛИЗАЦИЯ ==========
db = AppleDatabase()
design = AppleDesign()
router = Router()


# Состояния
class ChatStates(StatesGroup):
    main = State()
    searching = State()
    chatting = State()
    editing_gender = State()
    editing_search_gender = State()


# ========== КЛАВИАТУРЫ ==========
def create_keyboard_main() -> ReplyKeyboardBuilder:
    """Главная клавиатура"""
    builder = ReplyKeyboardBuilder()

    builder.button(text=f"{design.EMOJI['search']} Найти собеседника")
    builder.button(text=f"{design.EMOJI['chat']} Групповой чат")
    builder.button(text=f"{design.EMOJI['user']} Поиск по полу")
    builder.button(text=f"{design.EMOJI['stats']} Профиль")

    builder.adjust(1, 1, 2)

    return builder.as_markup(
        resize_keyboard=True,
        input_field_placeholder="Выберите действие"
    )


def create_keyboard_chatting() -> ReplyKeyboardBuilder:
    """Клавиатура во время чата"""
    builder = ReplyKeyboardBuilder()
    builder.button(text=f"{design.EMOJI['stop']} Завершить диалог")
    builder.button(text=f"{design.EMOJI['warning']} Пожаловаться")
    builder.adjust(1)
    return builder.as_markup(resize_keyboard=True)


def create_keyboard_searching() -> ReplyKeyboardBuilder:
    """Клавиатура во время поиска"""
    builder = ReplyKeyboardBuilder()
    builder.button(text=f"{design.EMOJI['cancel']} Отменить поиск")
    return builder.as_markup(resize_keyboard=True)


def create_keyboard_profile() -> ReplyKeyboardBuilder:
    """Клавиатура профиля"""
    builder = ReplyKeyboardBuilder()
    builder.button(text=f"{design.EMOJI['edit']} Указать свой пол")
    builder.button(text=f"{design.EMOJI['search']} Указать кого искать")
    builder.button(text=f"{design.EMOJI['stats']} Моя статистика")
    builder.button(text=f"{design.EMOJI['back']} Назад")
    builder.adjust(1, 1, 1, 1)
    return builder.as_markup(resize_keyboard=True)


def create_inline_keyboard_gender() -> InlineKeyboardBuilder:
    """Inline клавиатура для выбора своего пола"""
    builder = InlineKeyboardBuilder()
    builder.button(text=f"{design.EMOJI['male']} Мужчина", callback_data="gender_male")
    builder.button(text=f"{design.EMOJI['female']} Женщина", callback_data="gender_female")
    builder.button(text="Не указывать", callback_data="gender_unknown")
    builder.adjust(1, 1, 1)
    return builder.as_markup()


def create_inline_keyboard_search_gender() -> InlineKeyboardBuilder:
    """Inline клавиатура для выбора кого искать"""
    builder = InlineKeyboardBuilder()
    builder.button(text=f"{design.EMOJI['male']} Мужчин", callback_data="search_gender_male")
    builder.button(text=f"{design.EMOJI['female']} Женщин", callback_data="search_gender_female")
    builder.button(text=f"{design.EMOJI['any']} Любой", callback_data="search_gender_any")
    builder.adjust(1, 1, 1)
    return builder.as_markup()


# ========== СООБЩЕНИЯ ==========
class AppleMessages:
    """Сообщения в стиле Apple"""

    @staticmethod
    def welcome(first_name: str) -> str:
        """Приветственное сообщение"""
        return f"""
{design.format_header(f"{design.EMOJI['welcome']} Добро пожаловать, {first_name}")}

{design.EMOJI['sparkle']} <b>AnonChat</b> — приватное пространство для анонимного общения.

{design.format_subheader("Принципы дизайна:")}
  {design.EMOJI['lock']} Конфиденциальность
  {design.EMOJI['shield']} Безопасность
  {design.EMOJI['connection']} Простота

{design.format_subheader("Перед началом:")}
1. {design.format_text("Перейдите в Профиль")}
2. {design.format_text("Укажите свой пол")}
3. {design.format_text("Выберите кого искать")}
4. {design.format_text("Начните поиск собеседника")}

{design.create_divider()}
{design.format_text("Ваша личность полностью защищена")}
"""

    @staticmethod
    def profile_menu(user_data: dict) -> str:
        """Меню профиля"""
        gender_map = {
            'male': f"{design.EMOJI['male']} Мужчина",
            'female': f"{design.EMOJI['female']} Женщина",
            'unknown': "Не указан",
            'any': f"{design.EMOJI['any']} Любой"
        }

        gender = gender_map.get(user_data.get('gender', 'unknown'), "Не указан")
        search_gender = gender_map.get(user_data.get('search_gender', 'any'), f"{design.EMOJI['any']} Любой")

        return f"""
{design.format_header(f"{design.EMOJI['stats']} Ваш профиль")}

{design.format_subheader("👤 Информация:")}
{design.format_text(f"Имя: {user_data.get('first_name', 'Аноним')}")}
{design.format_text(f"ID: {user_data.get('telegram_id', 'N/A')}")}

{design.format_subheader("⚙️ Настройки поиска:")}
{design.format_text(f"Ваш пол: {gender}")}
{design.format_text(f"Ищу: {search_gender}")}

{design.create_divider()}
{design.format_text("Настройте параметры поиска для лучшего подбора собеседников")}
"""

    @staticmethod
    def need_gender_setup() -> str:
        """Сообщение о необходимости настройки профиля"""
        return f"""
{design.format_header(f"{design.EMOJI['warning']} Настройте профиль")}

{design.format_text("Перед началом поиска необходимо:")}

1. {design.format_text("Указать свой пол")}
2. {design.format_text("Выбрать кого искать")}

{design.format_text("Перейдите в Профиль → Указать свой пол")}
"""

    @staticmethod
    def select_your_gender() -> str:
        """Выберите свой пол"""
        return f"""
{design.format_header(f"{design.EMOJI['user']} Укажите свой пол")}

{design.format_text("Это поможет найти подходящего собеседника")}
"""

    @staticmethod
    def select_search_gender() -> str:
        """Выберите кого искать"""
        return f"""
{design.format_header(f"{design.EMOJI['search']} Кого вы ищете?")}

{design.format_text("Выберите предпочтительный пол собеседника")}
"""

    @staticmethod
    def gender_updated(gender: str) -> str:
        """Пол обновлен"""
        gender_map = {
            'male': f"{design.EMOJI['male']} Мужчина",
            'female': f"{design.EMOJI['female']} Женщина",
            'unknown': "Не указан"
        }
        return f"""
{design.format_header(f"{design.EMOJI['success']} Пол обновлен")}

{design.format_text(f"Теперь ваш пол: {gender_map.get(gender, 'Не указан')}")}
"""

    @staticmethod
    def search_gender_updated(search_gender: str) -> str:
        """Настройки поиска обновлены"""
        gender_map = {
            'male': f"{design.EMOJI['male']} Мужчин",
            'female': f"{design.EMOJI['female']} Женщин",
            'any': f"{design.EMOJI['any']} Любой"
        }
        return f"""
{design.format_header(f"{design.EMOJI['success']} Настройки поиска обновлены")}

{design.format_text(f"Теперь вы ищете: {gender_map.get(search_gender, 'Любой')}")}
"""

    @staticmethod
    def searching(gender: str) -> str:
        """Сообщение о поиске"""
        gender_map = {
            'male': "мужчину",
            'female': "женщину",
            'any': "собеседника"
        }
        search_for = gender_map.get(gender, "собеседника")

        return f"""
{design.format_header(f"{design.EMOJI['search']} Ищем {search_for}...")}

{design.format_text("Идет поиск подходящего собеседника...")}
"""

    @staticmethod
    def found() -> str:
        """Сообщение о найденном собеседнике"""
        return f"""
{design.format_header(f"{design.EMOJI['found']} Собеседник найден!")}

{design.format_text("Можете начинать общение")}
"""

    @staticmethod
    def search_stopped() -> str:
        """Поиск остановлен"""
        return f"""
{design.format_header(f"{design.EMOJI['stop_sign']} Поиск остановлен")}

{design.format_text("Отправьте /next, чтобы начать поиск")}
"""

    @staticmethod
    def chat_stopped() -> str:
        """Диалог остановлен"""
        return f"""
{design.format_header(f"{design.EMOJI['sad']} Диалог остановлен")}

{design.format_text("Отправьте /next, чтобы начать поиск")}
"""

    @staticmethod
    def no_partner() -> str:
        """Нет собеседника"""
        return f"""
{design.format_header(f"{design.EMOJI['confused']} У вас нет собеседника")}

{design.format_text("Отправьте /next, чтобы начать поиск")}
"""

    @staticmethod
    def partner_left() -> str:
        """Сообщение о выходе собеседника"""
        return f"""
{design.format_header(f"{design.EMOJI['warning']} Собеседник вышел")}

{design.format_text("Соединение разорвано")}
"""

    @staticmethod
    def stats(user_data: dict) -> str:
        """Сообщение со статистикой"""
        created_at = user_data.get('created_at', '')[:10] if user_data.get('created_at') else ''
        last_seen = user_data.get('last_seen', '')[:16] if user_data.get('last_seen') else ''

        gender_map = {
            'male': f"{design.EMOJI['male']} Мужчина",
            'female': f"{design.EMOJI['female']} Женщина",
            'unknown': "Не указан",
            'any': f"{design.EMOJI['any']} Любой"
        }

        gender = gender_map.get(user_data.get('gender', 'unknown'), "Не указан")
        search_gender = gender_map.get(user_data.get('search_gender', 'any'), f"{design.EMOJI['any']} Любой")

        return f"""
{design.format_header(f"{design.EMOJI['stats']} Ваша статистика")}

{design.format_subheader("👤 Профиль:")}
{design.format_text(f"Имя: {user_data.get('first_name', 'Аноним')}")}
{design.format_text(f"Пол: {gender}")}
{design.format_text(f"Ищу: {search_gender}")}
{design.format_text(f"ID: {user_data.get('telegram_id', 'N/A')}")}
{design.format_text(f"С нами с: {created_at}")}

{design.format_subheader("📈 Активность:")}
{design.format_text(f"Сообщений: {user_data.get('message_count', 0)}")}
{design.format_text(f"Диалогов: {user_data.get('session_count', 0)}")}
{design.format_text(f"Был онлайн: {last_seen}")}

{design.format_subheader("🌐 Система:")}
{design.format_text(f"Всего пользователей: {user_data.get('total_users', 0)}")}
{design.format_text(f"В поиске: {user_data.get('searching_users', 0)}")}
{design.format_text(f"Активных чатов: {user_data.get('active_chats', 0)}")}

{design.create_divider()}
{design.format_text("Продолжайте в том же духе!")}
"""

    @staticmethod
    def error_no_chat() -> str:
        """Ошибка: нет активного чата"""
        return f"""
{design.format_header(f"{design.EMOJI['error']} Нет активного диалога")}

{design.format_text("Начните поиск собеседника")}
"""


# ========== ОБРАБОТЧИКИ КОМАНД ==========
messages = AppleMessages()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    """Приветствие"""
    user_data = await db.create_or_update_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username or "",
        first_name=message.from_user.first_name or "Пользователь"
    )

    await state.set_state(ChatStates.main)

    await message.answer(
        messages.welcome(message.from_user.first_name),
        reply_markup=create_keyboard_main(),
        parse_mode="HTML"
    )


# ========== ПРОВЕРКА НАСТРОЙКИ ПРОФИЛЯ ==========
async def check_profile_setup(user_id: int) -> bool:
    """Проверка, настроен ли профиль пользователя"""
    user_data = await db.get_user_by_telegram_id(user_id)

    if not user_data:
        return False

    # Проверяем, указан ли пол
    gender = user_data.get('gender', 'unknown')

    return gender != 'unknown'


async def require_profile_setup(message: Message, state: FSMContext) -> bool:
    """Проверка профиля перед поиском"""
    if not await check_profile_setup(message.from_user.id):
        await message.answer(
            messages.need_gender_setup(),
            reply_markup=create_keyboard_main(),
            parse_mode="HTML"
        )
        return False
    return True


# ========== ПОИСК ==========
async def search_handler(message: Message, state: FSMContext):
    """Обработчик поиска"""
    # Проверяем настройку профиля
    if not await require_profile_setup(message, state):
        return

    user = await db.get_user_by_telegram_id(message.from_user.id)
    if not user:
        await cmd_start(message, state)
        return

    partner_id = await db.get_active_partner(message.from_user.id)
    if partner_id:
        await state.set_state(ChatStates.chatting)
        await message.answer(
            messages.found(),
            reply_markup=create_keyboard_chatting(),
            parse_mode="HTML"
        )
        return

    success = await db.join_search_queue(message.from_user.id)

    if not success:
        await message.answer(
            design.format_text("Вы уже находитесь в поиске"),
            parse_mode="HTML"
        )
        return

    await state.set_state(ChatStates.searching)

    # Получаем предпочтения по полу для сообщения
    search_gender = await db.get_user_search_gender(message.from_user.id)

    await message.answer(
        messages.searching(search_gender),
        reply_markup=create_keyboard_searching(),
        parse_mode="HTML"
    )

    await asyncio.sleep(1)
    partner_id = await db.find_partner(message.from_user.id)

    if partner_id:
        partner_data = await db.get_user_by_telegram_id(partner_id)

        if not partner_data:
            await message.answer("❌ Ошибка: данные партнера не найдены")
            await db.leave_search_queue(message.from_user.id)
            await state.set_state(ChatStates.main)
            return

        await state.set_state(ChatStates.chatting)

        await message.answer(
            messages.found(),
            reply_markup=create_keyboard_chatting(),
            parse_mode="HTML"
        )

        try:
            await bot.send_message(
                chat_id=partner_id,
                text=messages.found(),
                reply_markup=create_keyboard_chatting(),
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Ошибка уведомления партнера: {e}")


@router.message(F.text.contains("Найти собеседника"))
@router.message(Command("search"))
@router.message(Command("next"))
async def cmd_search(message: Message, state: FSMContext):
    """Поиск собеседника"""
    await search_handler(message, state)


# ========== ОТМЕНА ПОИСКА ==========
async def cancel_handler(message: Message, state: FSMContext):
    """Обработчик отмены поиска"""
    await db.leave_search_queue(message.from_user.id)
    await state.set_state(ChatStates.main)

    await message.answer(
        messages.search_stopped(),
        reply_markup=create_keyboard_main(),
        parse_mode="HTML"
    )


@router.message(F.text.contains("Отменить поиск"))
@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    """Отмена поиска"""
    await cancel_handler(message, state)


# ========== ОСТАНОВКА ==========
async def stop_handler(message: Message, state: FSMContext):
    """Обработчик остановки"""
    current_state = await state.get_state()
    user_id = message.from_user.id
    partner_id = await db.get_active_partner(user_id)

    if current_state == ChatStates.searching:
        # Остановка поиска
        await db.leave_search_queue(user_id)
        await state.set_state(ChatStates.main)

        await message.answer(
            messages.search_stopped(),
            reply_markup=create_keyboard_main(),
            parse_mode="HTML"
        )

    elif current_state == ChatStates.chatting and partner_id:
        # Завершение диалога
        partner_id = await db.end_session(user_id)

        if partner_id:
            try:
                await bot.send_message(
                    chat_id=partner_id,
                    text=messages.partner_left(),
                    reply_markup=create_keyboard_main(),
                    parse_mode="HTML"
                )
            except Exception as e:
                logging.error(f"Ошибка уведомления партнера: {e}")

        await state.set_state(ChatStates.main)

        await message.answer(
            messages.chat_stopped(),
            reply_markup=create_keyboard_main(),
            parse_mode="HTML"
        )

    else:
        # Нет активного диалога или поиска
        await state.set_state(ChatStates.main)

        await message.answer(
            messages.no_partner(),
            reply_markup=create_keyboard_main(),
            parse_mode="HTML"
        )


@router.message(F.text.contains("Завершить диалог"))
@router.message(Command("stop"))
async def cmd_stop(message: Message, state: FSMContext):
    """Остановка поиска или завершение диалога"""
    await stop_handler(message, state)


# ========== ПРОФИЛЬ ==========
@router.message(F.text.contains("Профиль"))
@router.message(Command("profile"))
async def cmd_profile(message: Message, state: FSMContext):
    """Открытие профиля"""
    user = await db.get_user_by_telegram_id(message.from_user.id)

    if not user:
        await message.answer(
            design.format_text("Сначала зарегистрируйтесь через /start"),
            parse_mode="HTML"
        )
        return

    await message.answer(
        messages.profile_menu(user),
        reply_markup=create_keyboard_profile(),
        parse_mode="HTML"
    )


@router.message(F.text.contains("Указать свой пол"))
async def cmd_set_gender(message: Message, state: FSMContext):
    """Указать свой пол"""
    await state.set_state(ChatStates.editing_gender)
    await message.answer(
        messages.select_your_gender(),
        reply_markup=create_inline_keyboard_gender(),
        parse_mode="HTML"
    )


@router.message(F.text.contains("Указать кого искать"))
async def cmd_set_search_gender(message: Message, state: FSMContext):
    """Указать кого искать"""
    # Сначала проверяем, указан ли свой пол
    if not await check_profile_setup(message.from_user.id):
        await message.answer(
            messages.need_gender_setup(),
            parse_mode="HTML"
        )
        return

    await state.set_state(ChatStates.editing_search_gender)
    await message.answer(
        messages.select_search_gender(),
        reply_markup=create_inline_keyboard_search_gender(),
        parse_mode="HTML"
    )


@router.message(F.text.contains("Моя статистика"))
@router.message(Command("stats"))
async def cmd_stats(message: Message):
    """Статистика"""
    user = await db.get_user_by_telegram_id(message.from_user.id)

    if not user:
        await message.answer(
            design.format_text("Сначала зарегистрируйтесь через /start"),
            parse_mode="HTML"
        )
        return

    stats = await db.get_user_stats(message.from_user.id)

    await message.answer(
        messages.stats(stats),
        parse_mode="HTML",
        reply_markup=create_keyboard_main()
    )


@router.message(F.text.contains("Назад"))
async def cmd_back(message: Message, state: FSMContext):
    """Возврат в главное меню"""
    await state.set_state(ChatStates.main)
    await message.answer(
        design.format_text("Главное меню"),
        reply_markup=create_keyboard_main(),
        parse_mode="HTML"
    )


@router.message(F.text.contains("Поиск по полу"))
async def cmd_search_by_gender(message: Message):
    """Информация о поиске по полу"""
    await message.answer(
        f"""
{design.format_header(f"{design.EMOJI['info']} Поиск по полу")}

{design.format_text("Для использования поиска по полу:")}

1. {design.format_text("Перейдите в Профиль")}
2. {design.format_text("Укажите свой пол")}
3. {design.format_text("Выберите кого искать")}
4. {design.format_text("Начните поиск")}

{design.format_text("Система автоматически найдет собеседника по вашим предпочтениям")}
""",
        parse_mode="HTML",
        reply_markup=create_keyboard_main()
    )


# ========== INLINE КНОПКИ ==========
@router.callback_query(F.data.startswith("gender_"))
async def handle_gender_callback(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора пола"""
    gender = callback.data.split("_")[1]  # male, female, unknown

    await db.update_user_gender(callback.from_user.id, gender)

    await callback.message.edit_text(
        messages.gender_updated(gender),
        parse_mode="HTML"
    )

    await state.set_state(ChatStates.main)
    await callback.answer("Пол успешно обновлен!")


@router.callback_query(F.data.startswith("search_gender_"))
async def handle_search_gender_callback(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора кого искать"""
    search_gender = callback.data.split("_")[2]  # male, female, any

    await db.update_user_search_gender(callback.from_user.id, search_gender)

    await callback.message.edit_text(
        messages.search_gender_updated(search_gender),
        parse_mode="HTML"
    )

    await state.set_state(ChatStates.main)
    await callback.answer("Настройки поиска обновлены!")


# ========== ОБРАБОТКА СООБЩЕНИЙ ЧАТА ==========
@router.message(ChatStates.chatting)
async def handle_chat_message(message: Message, state: FSMContext):
    """Обработка сообщений в чате"""
    partner_id = await db.get_active_partner(message.from_user.id)

    if not partner_id:
        await state.set_state(ChatStates.main)
        await message.answer(
            messages.error_no_chat(),
            reply_markup=create_keyboard_main(),
            parse_mode="HTML"
        )
        return

    await db.increment_message_count(message.from_user.id)

    try:
        if message.text:
            await bot.send_message(chat_id=partner_id, text=message.text)
        elif message.photo:
            await bot.send_photo(
                chat_id=partner_id,
                photo=message.photo[-1].file_id,
                caption=message.caption
            )
        elif message.sticker:
            await bot.send_sticker(chat_id=partner_id, sticker=message.sticker.file_id)
        elif message.voice:
            await bot.send_voice(chat_id=partner_id, voice=message.voice.file_id)
        elif message.video:
            await bot.send_video(
                chat_id=partner_id,
                video=message.video.file_id,
                caption=message.caption
            )
        elif message.document:
            await bot.send_document(
                chat_id=partner_id,
                document=message.document.file_id,
                caption=message.caption
            )
        else:
            await message.answer(
                design.format_text("Этот тип сообщения не поддерживается"),
                parse_mode="HTML"
            )

    except Exception as e:
        logging.error(f"Ошибка пересылки: {e}")
        await db.end_session(message.from_user.id)
        await state.set_state(ChatStates.main)
        await message.answer(
            messages.partner_left(),
            reply_markup=create_keyboard_main(),
            parse_mode="HTML"
        )


@router.message(ChatStates.searching)
async def handle_searching_message(message: Message, state: FSMContext):
    """Сообщение во время поиска"""
    partner_id = await db.get_active_partner(message.from_user.id)

    if partner_id:
        await state.set_state(ChatStates.chatting)
        await message.answer(
            messages.found(),
            reply_markup=create_keyboard_chatting(),
            parse_mode="HTML"
        )
    elif message.text and not ("Отменить поиск" in message.text or message.text.startswith('/')):
        await message.answer(
            design.format_text("Идет поиск собеседника... Для отмены нажмите 'Отменить поиск'"),
            parse_mode="HTML"
        )


# ========== ОБРАБОТКА ОСТАЛЬНЫХ СООБЩЕНИЙ ==========
@router.message()
async def handle_other_messages(message: Message, state: FSMContext):
    """Обработка всех остальных сообщений"""
    current_state = await state.get_state()

    if current_state == ChatStates.main:
        await message.answer(
            design.format_text("Используйте кнопки ниже для навигации"),
            parse_mode="HTML",
            reply_markup=create_keyboard_main()
        )
    elif current_state in [ChatStates.editing_gender, ChatStates.editing_search_gender]:
        await message.answer(
            design.format_text("Пожалуйста, используйте кнопки выше для выбора"),
            parse_mode="HTML"
        )


# ========== ФОНОВЫЕ ЗАДАЧИ ==========
async def background_tasks():
    """Фоновые задачи"""
    while True:
        try:
            await db.cleanup_old_searches()
            await asyncio.sleep(1800)
        except Exception as e:
            logging.error(f"Ошибка в фоновой задаче: {e}")
            await asyncio.sleep(60)


# ========== ЗАПУСК ==========
async def main():
    """Основная функция запуска"""
    global bot

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    if not TOKEN:
        logging.error("❌ BOT_TOKEN не найден")
        return

    db_success = await db.init()
    if not db_success:
        logging.error("❌ Не удалось инициализировать базу данных")
        return

    bot = Bot(
        token=TOKEN,
        default=DefaultBotProperties(
            parse_mode="HTML",
            link_preview_is_disabled=True
        )
    )

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    asyncio.create_task(background_tasks())

    logging.info("🚀 AnonChat запускается...")
    await asyncio.sleep(1)
    logging.info("✅ Система инициализирована")
    logging.info("⚙️  Поиск по полу активирован")
    logging.info("💬 Ожидание подключений...")

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        logging.info("👋 AnonChat завершает работу")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Завершение работы...")
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
