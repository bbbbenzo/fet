import asyncio
import logging
import sqlite3
import os
import time
from datetime import datetime
from typing import Optional, Dict
from contextlib import contextmanager

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

# ========== НАСТРОЙКИ APPLE-СТИЛЯ ==========
load_dotenv()


class AppleDesign:
    """Дизайн в стиле Apple"""

    # Эмодзи и символы
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
        "notification": "🔔"
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
    def format_list_item(emoji: str, text: str) -> str:
        """Форматирование элемента списка"""
        return f"  {emoji} {text}"

    @staticmethod
    def create_divider() -> str:
        """Создание разделителя"""
        return "―" * 32


TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = list(map(int, os.getenv("ADMIN_IDS", "").split(','))) if os.getenv("ADMIN_IDS") else []


# ========== БАЗА ДАННЫХ ==========
class AppleDatabase:
    """База данных в стиле Apple — минималистичная и эффективная"""

    def __init__(self, db_name="anonchat.db"):
        self.db_name = db_name
        self._init_database()

    @contextmanager
    def _connection(self):
        """Элегантное управление подключениями"""
        conn = sqlite3.connect(self.db_name)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    def _init_database(self):
        """Инициализация структуры базы данных"""
        with self._connection() as conn:
            # Пользователи
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    message_count INTEGER DEFAULT 0,
                    session_count INTEGER DEFAULT 0,
                    is_active BOOLEAN DEFAULT 1,
                    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Сессии чатов
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user1_id INTEGER NOT NULL,
                    user2_id INTEGER NOT NULL,
                    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    ended_at TIMESTAMP,
                    message_count INTEGER DEFAULT 0,
                    FOREIGN KEY (user1_id) REFERENCES users(id),
                    FOREIGN KEY (user2_id) REFERENCES users(id)
                )
            """)

            # Активные соединения (используем telegram_id для быстрого доступа)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS active_connections (
                    telegram_id INTEGER PRIMARY KEY,
                    partner_telegram_id INTEGER NOT NULL,
                    session_id INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Поисковые очереди
            conn.execute("""
                CREATE TABLE IF NOT EXISTS search_queue (
                    telegram_id INTEGER PRIMARY KEY,
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

    def create_or_update_user(self, telegram_id: int, username: str, first_name: str) -> dict:
        """Создание или обновление пользователя"""
        with self._connection() as conn:
            # Проверяем существование
            cursor = conn.execute(
                "SELECT * FROM users WHERE telegram_id = ?",
                (telegram_id,)
            )
            user = cursor.fetchone()

            if user:
                # Обновляем
                conn.execute("""
                    UPDATE users 
                    SET username = ?, first_name = ?, updated_at = CURRENT_TIMESTAMP, last_seen = CURRENT_TIMESTAMP
                    WHERE telegram_id = ?
                """, (username, first_name, telegram_id))
                cursor = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
                return dict(cursor.fetchone())
            else:
                # Создаем нового
                cursor = conn.execute("""
                    INSERT INTO users (telegram_id, username, first_name) 
                    VALUES (?, ?, ?)
                """, (telegram_id, username, first_name))
                cursor = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
                return dict(cursor.fetchone())

    def join_search_queue(self, telegram_id: int) -> bool:
        """Добавление в очередь поиска"""
        with self._connection() as conn:
            # Проверяем, не в поиске ли уже
            cursor = conn.execute(
                "SELECT * FROM search_queue WHERE telegram_id = ?",
                (telegram_id,)
            )
            if cursor.fetchone():
                return False

            # Проверяем, не в активном чате ли
            cursor = conn.execute(
                "SELECT * FROM active_connections WHERE telegram_id = ?",
                (telegram_id,)
            )
            if cursor.fetchone():
                return False

            # Добавляем в очередь
            conn.execute(
                "INSERT INTO search_queue (telegram_id) VALUES (?)",
                (telegram_id,)
            )
            return True

    def leave_search_queue(self, telegram_id: int) -> bool:
        """Выход из очереди поиска"""
        with self._connection() as conn:
            cursor = conn.execute(
                "DELETE FROM search_queue WHERE telegram_id = ?",
                (telegram_id,)
            )
            return cursor.rowcount > 0

    def find_partner(self, telegram_id: int) -> Optional[int]:
        """Поиск партнера для чата"""
        with self._connection() as conn:
            # Ищем другого пользователя в очереди
            cursor = conn.execute("""
                SELECT telegram_id FROM search_queue 
                WHERE telegram_id != ? 
                ORDER BY joined_at 
                LIMIT 1
            """, (telegram_id,))

            partner = cursor.fetchone()
            if not partner:
                return None

            partner_telegram_id = partner['telegram_id']

            # Получаем ID пользователей из таблицы users
            cursor = conn.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
            user = cursor.fetchone()
            if not user:
                return None

            cursor = conn.execute("SELECT id FROM users WHERE telegram_id = ?", (partner_telegram_id,))
            partner_user = cursor.fetchone()
            if not partner_user:
                return None

            user_id = user['id']
            partner_id = partner_user['id']

            # Удаляем обоих из очереди
            conn.execute("DELETE FROM search_queue WHERE telegram_id IN (?, ?)",
                         (telegram_id, partner_telegram_id))

            # Создаем сессию
            cursor = conn.execute("""
                INSERT INTO sessions (user1_id, user2_id) 
                VALUES (?, ?)
            """, (user_id, partner_id))
            session_id = cursor.lastrowid

            # Создаем активные соединения
            conn.execute("""
                INSERT OR REPLACE INTO active_connections (telegram_id, partner_telegram_id, session_id) 
                VALUES (?, ?, ?)
            """, (telegram_id, partner_telegram_id, session_id))

            conn.execute("""
                INSERT OR REPLACE INTO active_connections (telegram_id, partner_telegram_id, session_id) 
                VALUES (?, ?, ?)
            """, (partner_telegram_id, telegram_id, session_id))

            return partner_telegram_id

    def get_active_partner(self, telegram_id: int) -> Optional[int]:
        """Получение активного партнера"""
        with self._connection() as conn:
            cursor = conn.execute("""
                SELECT partner_telegram_id FROM active_connections 
                WHERE telegram_id = ?
            """, (telegram_id,))
            result = cursor.fetchone()
            return result['partner_telegram_id'] if result else None

    def end_session(self, telegram_id: int) -> Optional[int]:
        """Завершение сессии"""
        with self._connection() as conn:
            # Получаем партнера и сессию
            cursor = conn.execute("""
                SELECT partner_telegram_id, session_id FROM active_connections 
                WHERE telegram_id = ?
            """, (telegram_id,))

            result = cursor.fetchone()
            if not result:
                return None

            partner_telegram_id = result['partner_telegram_id']
            session_id = result['session_id']

            # Получаем ID пользователей
            cursor = conn.execute("SELECT id FROM users WHERE telegram_id = ?", (telegram_id,))
            user = cursor.fetchone()
            if user:
                user_id = user['id']

            cursor = conn.execute("SELECT id FROM users WHERE telegram_id = ?", (partner_telegram_id,))
            partner = cursor.fetchone()
            if partner:
                partner_id = partner['id']

            # Удаляем соединения
            conn.execute("DELETE FROM active_connections WHERE telegram_id IN (?, ?)",
                         (telegram_id, partner_telegram_id))

            # Обновляем сессию
            if session_id:
                conn.execute("""
                    UPDATE sessions 
                    SET ended_at = CURRENT_TIMESTAMP 
                    WHERE id = ?
                """, (session_id,))

            # Обновляем статистику пользователей
            if user:
                conn.execute("""
                    UPDATE users 
                    SET session_count = session_count + 1, 
                        updated_at = CURRENT_TIMESTAMP 
                    WHERE id = ?
                """, (user_id,))

            if partner:
                conn.execute("""
                    UPDATE users 
                    SET session_count = session_count + 1, 
                        updated_at = CURRENT_TIMESTAMP 
                    WHERE id = ?
                """, (partner_id,))

            return partner_telegram_id

    def increment_message_count(self, telegram_id: int):
        """Увеличение счетчика сообщений"""
        with self._connection() as conn:
            conn.execute("""
                UPDATE users 
                SET message_count = message_count + 1, 
                    updated_at = CURRENT_TIMESTAMP,
                    last_seen = CURRENT_TIMESTAMP 
                WHERE telegram_id = ?
            """, (telegram_id,))

    def get_user_stats(self, telegram_id: int) -> dict:
        """Получение статистики пользователя"""
        with self._connection() as conn:
            cursor = conn.execute("""
                SELECT 
                    u.*,
                    (SELECT COUNT(*) FROM users) as total_users,
                    (SELECT COUNT(*) FROM search_queue) as searching_users,
                    (SELECT COUNT(*) FROM active_connections) / 2 as active_chats
                FROM users u
                WHERE u.telegram_id = ?
            """, (telegram_id,))

            result = cursor.fetchone()
            if result:
                return dict(result)
            return {}

    def get_user_by_telegram_id(self, telegram_id: int) -> Optional[dict]:
        """Получение пользователя по Telegram ID"""
        with self._connection() as conn:
            cursor = conn.execute(
                "SELECT * FROM users WHERE telegram_id = ?",
                (telegram_id,)
            )
            result = cursor.fetchone()
            return dict(result) if result else None

    def cleanup_old_searches(self, hours: int = 1):
        """Очистка старых поисков"""
        with self._connection() as conn:
            conn.execute("""
                DELETE FROM search_queue 
                WHERE joined_at < datetime('now', ?)
            """, (f'-{hours} hours',))


# ========== ИНИЦИАЛИЗАЦИЯ ==========
db = AppleDatabase()
design = AppleDesign()
router = Router()


# Состояния
class ChatStates(StatesGroup):
    main = State()
    searching = State()
    chatting = State()


# ========== КЛАВИАТУРЫ В СТИЛЕ APPLE ==========
def create_keyboard_main() -> ReplyKeyboardBuilder:
    """Главная клавиатура — чистая и минималистичная"""
    builder = ReplyKeyboardBuilder()

    # Первая большая кнопка - Найти собеседника
    builder.button(text=f"{design.EMOJI['search']} Найти собеседника")

    # Вторая большая кнопка - Групповой чат
    builder.button(text=f"{design.EMOJI['chat']} Групповой чат")

    # Третий ряд: Поиск по полу и Профиль
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


# ========== СООБЩЕНИЯ В СТИЛЕ APPLE ==========
class AppleMessages:
    """Сообщения в стиле Apple — чистые, информативные, элегантные"""

    @staticmethod
    def welcome(first_name: str) -> str:
        """Приветственное сообщение"""
        return f"""
{design.format_header(f"{design.EMOJI['welcome']} Добро пожаловать, {first_name}")}

{design.EMOJI['sparkle']} <b>AnonChat</b> — приватное пространство для анонимного общения.

{design.format_subheader("Принципы дизайна:")}
{design.format_list_item(design.EMOJI['lock'], "Конфиденциальность")}
{design.format_list_item(design.EMOJI['shield'], "Безопасность")}
{design.format_list_item(design.EMOJI['connection'], "Простота")}

{design.format_subheader("Как это работает:")}
1. {design.format_list_item(design.EMOJI['search'], "Найдите собеседника")}
2. {design.format_list_item(design.EMOJI['chat'], "Общайтесь анонимно")}
3. {design.format_list_item(design.EMOJI['stop'], "Завершите когда захотите")}

{design.create_divider()}
{design.EMOJI['bot']} <i>Ваша личность полностью защищена</i>
"""

    @staticmethod
    def searching() -> str:
        """Сообщение о поиске"""
        return f"""
{design.format_header(f"{design.EMOJI['search']} Ищем собеседника...")}

/stop - остановить поиск
"""

    @staticmethod
    def found() -> str:
        """Сообщение о найденном собеседнике"""
        return f"""
{design.format_header(f"{design.EMOJI['found']} Собеседник найден!")}

/next - искать следующего
/stop — закончить диалог
"""

    @staticmethod
    def stopped() -> str:
        """Сообщение о завершении диалога"""
        return f"""
{design.format_header(f"{design.EMOJI['stop']} Диалог остановлен 😔")}

Отправьте /next, чтобы начать поиск
"""

    @staticmethod
    def partner_left() -> str:
        """Сообщение о выходе собеседника"""
        return f"""
{design.format_header(f"{design.EMOJI['warning']} Собеседник вышел")}

{design.EMOJI['connection']} <i>Соединение разорвано</i>

{design.create_divider()}
{design.EMOJI['search']} <i>Начните новый поиск когда будете готовы</i>
"""

    @staticmethod
    def stats(user_data: dict) -> str:
        """Сообщение со статистикой"""
        created_at = user_data.get('created_at', '')
        if created_at and len(created_at) > 10:
            created_at = created_at[:10]

        last_seen = user_data.get('last_seen', '')
        if last_seen and len(last_seen) > 16:
            last_seen = last_seen[:16]

        return f"""
{design.format_header(f"{design.EMOJI['stats']} Ваша статистика")}

{design.format_subheader("👤 Профиль:")}
{design.format_list_item(design.EMOJI['user'], f"Имя: {user_data.get('first_name', 'Аноним')}")}
{design.format_list_item("🆔", f"ID: {user_data.get('telegram_id', 'N/A')}")}
{design.format_list_item("📅", f"С нами с: {created_at}")}

{design.format_subheader("📈 Активность:")}
{design.format_list_item("✉", f"Сообщений: {user_data.get('message_count', 0)}")}
{design.format_list_item("💬", f"Диалогов: {user_data.get('session_count', 0)}")}
{design.format_list_item("⏱", f"Был онлайн: {last_seen}")}

{design.format_subheader("🌐 Система:")}
{design.format_list_item("👥", f"Всего пользователей: {user_data.get('total_users', 0)}")}
{design.format_list_item("🔍", f"В поиске: {user_data.get('searching_users', 0)}")}
{design.format_list_item("💭", f"Активных чатов: {user_data.get('active_chats', 0)}")}

{design.create_divider()}
{design.EMOJI['rocket']} <i>Продолжайте в том же духе!</i>
"""

    @staticmethod
    def privacy() -> str:
        """Сообщение о приватности"""
        return f"""
{design.format_header(f"{design.EMOJI['lock']} Наша философия приватности")}

{design.EMOJI['shield']} <b>Ваша анонимность — наш приоритет</b>

{design.format_subheader("Мы не храним:")}
• Ваши личные данные
• Содержание сообщений
• Метаданные переписки
• Геолокацию

{design.format_subheader("Мы защищаем:")}
• Вашу личность
• Конфиденциальность общения
• Свободу самовыражения
• Право на анонимность

{design.create_divider()}
{design.EMOJI['sparkle']} <i>Общайтесь свободно. Оставайтесь анонимными.</i>
"""

    @staticmethod
    def error_no_chat() -> str:
        """Ошибка: нет активного чата"""
        return f"""
{design.format_header(f"{design.EMOJI['error']} Нет активного диалога")}

{design.EMOJI['search']} <i>Начните поиск собеседника</i>
"""

    @staticmethod
    def search_cancelled() -> str:
        """Поиск отменен"""
        return f"""
{design.format_header(f"{design.EMOJI['cancel']} Поиск отменен")}

{design.EMOJI['search']} <i>Вы можете начать поиск в любой момент</i>
"""


# ========== ОБРАБОТЧИКИ КОМАНД ==========
messages = AppleMessages()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    """Приветствие — элегантное и информативное"""
    # Регистрируем пользователя
    user_data = db.create_or_update_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username or "",
        first_name=message.from_user.first_name or "Пользователь"
    )

    await state.set_state(ChatStates.main)

    # Отправляем приветствие с задержкой для эффекта
    await message.answer("⏳ <i>Загружаем интерфейс...</i>", parse_mode="HTML")
    await asyncio.sleep(0.5)

    await message.answer(
        messages.welcome(message.from_user.first_name),
        reply_markup=create_keyboard_main(),
        parse_mode="HTML"
    )


@router.message(F.text.contains("Найти собеседника"))
async def cmd_search_button(message: Message, state: FSMContext):
    """Поиск по кнопке"""
    await search_handler(message, state)


@router.message(Command("search"))
async def cmd_search_command(message: Message, state: FSMContext):
    """Поиск по команде"""
    await search_handler(message, state)


async def search_handler(message: Message, state: FSMContext):
    """Общий обработчик поиска"""
    user = db.get_user_by_telegram_id(message.from_user.id)
    if not user:
        await cmd_start(message, state)
        return

    # Проверяем активный чат
    partner_id = db.get_active_partner(message.from_user.id)
    if partner_id:
        await state.set_state(ChatStates.chatting)
        await message.answer(
            messages.found(),
            reply_markup=create_keyboard_chatting(),
            parse_mode="HTML"
        )
        return

    # Входим в очередь поиска
    success = db.join_search_queue(message.from_user.id)

    if not success:
        await message.answer(
            "ℹ️ <i>Вы уже находитесь в поиске</i>",
            parse_mode="HTML"
        )
        return

    await state.set_state(ChatStates.searching)

    # Только одно сообщение о начале поиска
    await message.answer(
        messages.searching(),
        reply_markup=create_keyboard_searching(),
        parse_mode="HTML"
    )

    # Пытаемся найти собеседника сразу и с небольшой задержкой
    await asyncio.sleep(1)
    partner_id = db.find_partner(message.from_user.id)

    if partner_id:
        # Получаем данные партнера
        partner_data = db.get_user_by_telegram_id(partner_id)

        if not partner_data:
            await message.answer("❌ Ошибка: данные партнера не найдены")
            db.leave_search_queue(message.from_user.id)
            await state.set_state(ChatStates.main)
            return

        await state.set_state(ChatStates.chatting)

        # Уведомляем обоих пользователей
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
    else:
        # Если не нашли партнера сразу, продолжаем поиск в фоне
        # Не отправляем дополнительных сообщений
        pass


@router.message(F.text.contains("Отменить поиск"))
async def cmd_cancel_button(message: Message, state: FSMContext):
    """Отмена поиска по кнопке"""
    await cancel_handler(message, state)


@router.message(Command("cancel"))
async def cmd_cancel_command(message: Message, state: FSMContext):
    """Отмена поиска по команде"""
    await cancel_handler(message, state)


async def cancel_handler(message: Message, state: FSMContext):
    """Общий обработчик отмены поиска"""
    db.leave_search_queue(message.from_user.id)

    await state.set_state(ChatStates.main)

    await message.answer(
        messages.search_cancelled(),
        reply_markup=create_keyboard_main(),
        parse_mode="HTML"
    )


@router.message(F.text.contains("Завершить диалог"))
async def cmd_stop_button(message: Message, state: FSMContext):
    """Завершение диалога по кнопке"""
    await stop_handler(message, state)


@router.message(Command("stop"))
async def cmd_stop_command(message: Message, state: FSMContext):
    """Завершение диалога по команде"""
    await stop_handler(message, state)


@router.message(Command("next"))
async def cmd_next(message: Message, state: FSMContext):
    """Поиск следующего собеседника"""
    # Завершаем текущую сессию
    partner_id = db.end_session(message.from_user.id)

    if partner_id:
        # Уведомляем текущего партнера
        try:
            await bot.send_message(
                chat_id=partner_id,
                text=messages.partner_left(),
                reply_markup=create_keyboard_main(),
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Ошибка уведомления партнера: {e}")

    # Начинаем новый поиск без дополнительных сообщений
    await search_handler(message, state)


async def stop_handler(message: Message, state: FSMContext):
    """Общий обработчик завершения диалога"""
    user = db.get_user_by_telegram_id(message.from_user.id)

    if not user:
        await cmd_start(message, state)
        return

    partner_id = db.end_session(message.from_user.id)

    if partner_id:
        # Уведомляем партнера
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

    # Анимация завершения
    await message.answer("🔄 <i>Завершаем сессию...</i>", parse_mode="HTML")
    await asyncio.sleep(0.5)

    await message.answer(
        messages.stopped(),
        reply_markup=create_keyboard_main(),
        parse_mode="HTML"
    )


@router.message(F.text.contains("Профиль"))
async def cmd_profile_button(message: Message):
    """Профиль по кнопке"""
    await stats_handler(message)


@router.message(Command("stats"))
async def cmd_stats_command(message: Message):
    """Статистика по команде"""
    await stats_handler(message)


async def stats_handler(message: Message):
    """Общий обработчик статистики"""
    user = db.get_user_by_telegram_id(message.from_user.id)

    if not user:
        await message.answer(
            "ℹ️ <i>Сначала зарегистрируйтесь через /start</i>",
            parse_mode="HTML"
        )
        return

    stats = db.get_user_stats(message.from_user.id)

    await message.answer(
        messages.stats(stats),
        parse_mode="HTML",
        reply_markup=create_keyboard_main()
    )


@router.message(F.text.contains("Приватность"))
async def cmd_privacy(message: Message):
    """Информация о приватности"""
    await message.answer(
        messages.privacy(),
        parse_mode="HTML",
        reply_markup=create_keyboard_main()
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    """Админ-панель — минималистичная и функциональная"""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer(
            "🔒 <i>Эта функция доступна только администраторам</i>",
            parse_mode="HTML"
        )
        return

    user = db.get_user_by_telegram_id(message.from_user.id)
    if not user:
        await message.answer("Сначала запустите бота командой /start")
        return

    stats = db.get_user_stats(message.from_user.id)

    admin_text = f"""
{design.format_header("🛠 Панель администратора")}

{design.format_subheader("Системная информация:")}
{design.format_list_item("👥", f"Всего пользователей: {stats.get('total_users', 0)}")}
{design.format_list_item("🔍", f"В поиске: {stats.get('searching_users', 0)}")}
{design.format_list_item("💭", f"Активных чатов: {stats.get('active_chats', 0)}")}
{design.format_list_item("⏱", f"Время работы: {time.strftime('%H:%M:%S')}")}

{design.create_divider()}
{design.EMOJI['shield']} <i>Система работает стабильно</i>
"""

    await message.answer(admin_text, parse_mode="HTML")


# ========== ОБРАБОТКА СООБЩЕНИЙ ЧАТА ==========
@router.message(ChatStates.chatting)
async def handle_chat_message(message: Message, state: FSMContext):
    """Обработка сообщений в чате — плавная пересылка"""
    # Получаем партнера
    partner_id = db.get_active_partner(message.from_user.id)

    if not partner_id:
        # Если партнера нет, переводим в главное состояние
        await state.set_state(ChatStates.main)
        await message.answer(
            messages.error_no_chat(),
            reply_markup=create_keyboard_main(),
            parse_mode="HTML"
        )
        return

    # Увеличиваем счетчик сообщений
    db.increment_message_count(message.from_user.id)

    try:
        # Пересылаем сообщение партнеру
        if message.text:
            await bot.send_message(
                chat_id=partner_id,
                text=message.text
            )
        elif message.photo:
            await bot.send_photo(
                chat_id=partner_id,
                photo=message.photo[-1].file_id,
                caption=message.caption
            )
        elif message.sticker:
            await bot.send_sticker(
                chat_id=partner_id,
                sticker=message.sticker.file_id
            )
        elif message.voice:
            await bot.send_voice(
                chat_id=partner_id,
                voice=message.voice.file_id
            )
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
                "ℹ️ <i>Этот тип сообщения не поддерживается</i>",
                parse_mode="HTML"
            )
            return

    except Exception as e:
        logging.error(f"Ошибка пересылки: {e}")
        await message.answer(
            "⚠️ <i>Не удалось доставить сообщение. Возможно, собеседник покинул чат.</i>",
            parse_mode="HTML"
        )
        # Если ошибка доставки, завершаем сессию
        db.end_session(message.from_user.id)
        await state.set_state(ChatStates.main)
        await message.answer(
            messages.partner_left(),
            reply_markup=create_keyboard_main(),
            parse_mode="HTML"
        )


@router.message(ChatStates.searching)
async def handle_searching_message(message: Message, state: FSMContext):
    """Сообщение во время поиска"""
    # Проверяем, не нашли ли мы уже партнера
    partner_id = db.get_active_partner(message.from_user.id)
    if partner_id:
        # Если партнер найден, переводим в состояние чата
        await state.set_state(ChatStates.chatting)
        # Отправляем уведомление только если это команда или специальное сообщение
        if message.text and ("Завершить диалог" in message.text or "Пожаловаться" in message.text):
            await message.answer(
                messages.found(),
                reply_markup=create_keyboard_chatting(),
                parse_mode="HTML"
            )
        else:
            # Для обычных сообщений - переходим в обработку чата
            await handle_chat_message(message, state)
    else:
        # Проверяем, не команда ли это управления поиском
        if message.text and ("Отменить поиск" in message.text or message.text.startswith('/')):
            # Пропускаем обычные команды
            return
        # Если все еще в поиске, показываем сообщение ожидания
        await message.answer(
            "⏳ <i>Все еще ищем идеального собеседника...</i>\n"
            "Пожалуйста, подождите. Для отмены поиска нажмите 'Отменить поиск'",
            parse_mode="HTML"
        )


@router.message()
async def handle_other_messages(message: Message, state: FSMContext):
    """Обработка всех остальных сообщений"""
    current_state = await state.get_state()

    if current_state == ChatStates.main:
        await message.answer(
            "💡 <i>Используйте кнопки ниже для навигации</i>\n\n"
            "Или нажмите /start для просмотра возможностей",
            parse_mode="HTML",
            reply_markup=create_keyboard_main()
        )
    else:
        await message.answer(
            "ℹ️ <i>Это сообщение не может быть обработано в текущем режиме</i>",
            parse_mode="HTML"
        )


# ========== ФОНОВЫЕ ЗАДАЧИ ==========
async def background_tasks():
    """Фоновые задачи для обслуживания системы"""
    while True:
        try:
            # Очистка старых поисков каждые 30 минут
            db.cleanup_old_searches()
            await asyncio.sleep(1800)  # 30 минут
        except Exception as e:
            logging.error(f"Ошибка в фоновой задаче: {e}")
            await asyncio.sleep(60)


# ========== ОСНОВНАЯ ФУНКЦИЯ ==========
async def main():
    """Запуск бота — элегантная инициализация"""
    global bot

    # Настройка логирования в стиле Apple
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    if not TOKEN:
        logging.error("❌ BOT_TOKEN не найден")
        return

    # Инициализация бота
    bot = Bot(
        token=TOKEN,
        default=DefaultBotProperties(
            parse_mode="HTML",
            link_preview_is_disabled=True
        )
    )

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # Запускаем фоновые задачи
    asyncio.create_task(background_tasks())

    # Элегантный запуск
    logging.info("🚀 AnonChat запускается...")
    await asyncio.sleep(1)
    logging.info("✅ Система инициализирована")
    logging.info("🔒 Приватность активирована")
    logging.info("💬 Ожидание подключений...")

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        logging.info("👋 AnonChat завершает работу")


if __name__ == "__main__":
    # Запуск с обработкой ошибок
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Завершение работы...")
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
