import asyncio
import logging
import os
from typing import Optional, Tuple
from aiogram import Router, F, Bot, Dispatcher, types
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import ReplyKeyboardBuilder
import asyncpg
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.types import CallbackQuery
from datetime import datetime, timedelta, timezone

# ========== ЗАГРУЗКА .env ==========
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

if not TOKEN:
    raise ValueError("❌ BOT_TOKEN не найден в .env файле")
if not DATABASE_URL:
    raise ValueError("❌ DATABASE_URL не найден в .env файле")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# ========== БАЗА ДАННЫХ POSTGRESQL ==========
class Database:
    """PostgreSQL база данных для Railway"""

    def __init__(self):
        self.dsn = DATABASE_URL
        self.pool: Optional[asyncpg.Pool] = None

    async def init(self) -> bool:
        """Инициализация подключения и таблиц"""
        try:
            self.pool = await asyncpg.create_pool(
                dsn=self.dsn,
                min_size=1,
                max_size=10,
                command_timeout=60,
                server_settings={
                    'client_encoding': 'UTF8'
                }
            )
            await self._create_tables()
            logger.info("✅ База данных PostgreSQL подключена")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка подключения к PostgreSQL: {e}")
            return False

    async def _create_tables(self):
        """Создание необходимых таблиц"""
        async with self.pool.acquire() as conn:
            # Пользователи
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT UNIQUE NOT NULL,
                    username VARCHAR(255),
                    first_name VARCHAR(255),
                    gender VARCHAR(10),
                    age INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active BOOLEAN DEFAULT TRUE
                )
            """)

            # Сессии чатов
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    id SERIAL PRIMARY KEY,
                    user1_id INTEGER NOT NULL REFERENCES users(id),
                    user2_id INTEGER NOT NULL REFERENCES users(id),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    ended_at TIMESTAMP,
                    message_count INTEGER DEFAULT 0,
                    UNIQUE(user1_id, user2_id)
                )
            """)

            # Активные соединения
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS active_chats (
                    telegram_id BIGINT PRIMARY KEY,
                    partner_telegram_id BIGINT NOT NULL,
                    session_id INTEGER REFERENCES chat_sessions(id),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Очередь поиска
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS search_queue (
                        telegram_id BIGINT PRIMARY KEY,
                        joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        target_gender VARCHAR(10)  -- NULL = обычный поиск, 'male'/'female' = гендерный поиск
                    )
                """)

            # Оценки собеседников
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS user_ratings (
                    id SERIAL PRIMARY KEY,
                    rater_user_id INTEGER NOT NULL REFERENCES users(id),
                    rated_user_id INTEGER NOT NULL REFERENCES users(id),
                    session_id INTEGER REFERENCES chat_sessions(id),
                    rating INTEGER CHECK (rating IN (-1, 0, 1)), -- -1=👎, 0=нет оценки, 1=👍
                    complaint TEXT,
                    complaint_category VARCHAR(50),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(rater_user_id, rated_user_id, session_id)
                )
            """)

            # Премиум-подписки (покупка за Telegram Stars)
            await conn.execute("""
                            CREATE TABLE IF NOT EXISTS premium (
                                id SERIAL PRIMARY KEY,
                                telegram_id BIGINT UNIQUE NOT NULL,
                                stars_paid INTEGER NOT NULL,  -- сколько заплатил (49, 99, 199)
                                duration_days INTEGER NOT NULL,  -- 1, 7, 30
                                purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                                expires_at TIMESTAMP NOT NULL,
                                is_active BOOLEAN DEFAULT TRUE
                            )
                        """)

            # Групповые чаты (до 3 участников)
            await conn.execute("""
                            CREATE TABLE IF NOT EXISTS group_chats (
                                id SERIAL PRIMARY KEY,
                                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                                ended_at TIMESTAMP,
                                message_count INTEGER DEFAULT 0,
                                is_active BOOLEAN DEFAULT TRUE
                            )
                        """)

            # Участники групповых чатов
            await conn.execute("""
                            CREATE TABLE IF NOT EXISTS group_chat_members (
                                group_id INTEGER REFERENCES group_chats(id) ON DELETE CASCADE,
                                telegram_id BIGINT NOT NULL,
                                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                                PRIMARY KEY (group_id, telegram_id)
                            )
                        """)

            # Очередь для группового поиска
            await conn.execute("""
                            CREATE TABLE IF NOT EXISTS group_search_queue (
                                telegram_id BIGINT PRIMARY KEY,
                                target_gender VARCHAR(10),  -- NULL = случайные, 'male'/'female' = конкретный
                                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                            )
                        """)

            logger.info("✅ Таблицы созданы/проверены")

    @asynccontextmanager
    async def get_connection(self):
        """Контекстный менеджер для подключений"""
        async with self.pool.acquire() as conn:
            yield conn

    async def ensure_user(self, telegram_id: int, username: str, first_name: str):
        """Создать или обновить пользователя"""
        async with self.get_connection() as conn:
            await conn.execute("""
                INSERT INTO users (telegram_id, username, first_name, last_seen)
                VALUES ($1, $2, $3, CURRENT_TIMESTAMP)
                ON CONFLICT (telegram_id) DO UPDATE SET
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    last_seen = EXCLUDED.last_seen,
                    is_active = TRUE
            """, telegram_id, username, first_name)

    async def get_user_profile(self, telegram_id: int) -> Optional[dict]:
        """Получить профиль пользователя"""
        async with self.get_connection() as conn:
            user = await conn.fetchrow("""
                SELECT telegram_id, username, first_name, gender, age 
                FROM users WHERE telegram_id = $1
            """, telegram_id)

            if user:
                return dict(user)
            return None

    async def get_user_gender(self, telegram_id: int) -> Optional[str]:
        """Получить пол пользователя"""
        async with self.get_connection() as conn:
            result = await conn.fetchrow("""
                SELECT gender FROM users WHERE telegram_id = $1
            """, telegram_id)
            return result['gender'] if result else None

    async def update_user_gender(self, telegram_id: int, gender: str):
        """Обновить пол пользователя"""
        async with self.get_connection() as conn:
            await conn.execute("""
                UPDATE users SET gender = $1 
                WHERE telegram_id = $2
            """, gender, telegram_id)

    async def update_user_age(self, telegram_id: int, age: int):
        """Обновить возраст пользователя"""
        async with self.get_connection() as conn:
            await conn.execute("""
                UPDATE users SET age = $1 
                WHERE telegram_id = $2
            """, age, telegram_id)

    async def add_to_search(self, telegram_id: int, target_gender: Optional[str] = None):
        """Добавить в очередь поиска, с опциональным целевым полом"""
        async with self.get_connection() as conn:
            if target_gender in ('male', 'female'):
                await conn.execute("""
                    INSERT INTO search_queue (telegram_id, target_gender)
                    VALUES ($1, $2)
                    ON CONFLICT (telegram_id) DO UPDATE SET
                        joined_at = CURRENT_TIMESTAMP,
                        target_gender = EXCLUDED.target_gender
                """, telegram_id, target_gender)
            else:
                await conn.execute("""
                    INSERT INTO search_queue (telegram_id, target_gender)
                    VALUES ($1, NULL)
                    ON CONFLICT (telegram_id) DO UPDATE SET
                        joined_at = CURRENT_TIMESTAMP,
                        target_gender = NULL
                """, telegram_id)

    async def remove_from_search(self, telegram_id: int):
        """Удалить из очереди поиска"""
        async with self.get_connection() as conn:
            await conn.execute("""
                DELETE FROM search_queue WHERE telegram_id = $1
            """, telegram_id)

    async def remove_from_group_chat(self, telegram_id: int):
        """Удалить пользователя из группового чата"""
        async with self.get_connection() as conn:
            await conn.execute("""
                DELETE FROM group_chat_members
                WHERE telegram_id = $1
            """, telegram_id)

    async def find_partner(self, telegram_id: int) -> Optional[Tuple[int, int]]:
        """Обычный поиск с защитой гендерных предпочтений + логирование"""
        async with self.get_connection() as conn:
            # Мой пол
            my_row = await conn.fetchrow(
                "SELECT gender FROM users WHERE telegram_id = $1",
                telegram_id
            )
            if not my_row or not my_row['gender']:
                logger.warning(f"find_partner: пользователь {telegram_id} без пола")
                return None
            my_gender = my_row['gender']

            logger.info(f"find_partner: пользователь {telegram_id} (пол {my_gender}) начинает обычный поиск")

            partner_id = None
            skipped_id = None

            # Первый кандидат в очереди
            candidate = await conn.fetchrow("""
                SELECT sq.telegram_id, sq.target_gender, u.gender AS partner_gender
                FROM search_queue sq
                JOIN users u ON u.telegram_id = sq.telegram_id
                WHERE sq.telegram_id != $1
                ORDER BY sq.joined_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            """, telegram_id)

            if candidate:
                candidate_id = candidate['telegram_id']
                target_of_partner = candidate['target_gender']
                partner_gender = candidate['partner_gender']

                logger.info(
                    f"find_partner: проверяем кандидата {candidate_id} (пол {partner_gender}, target_gender={target_of_partner})")

                # Защита: если кандидат в гендерном поиске и я НЕ подхожу под его запрос
                if target_of_partner is not None and my_gender != target_of_partner:
                    logger.info(
                        f"find_partner: пропускаем {candidate_id} — он ищет {target_of_partner}, а я {my_gender}")
                    skipped_id = candidate_id
                else:
                    partner_id = candidate_id
                    logger.info(f"find_partner: кандидат {candidate_id} подходит — соединяем")

            # Fallback — если не подошёл или не было кандидата
            if partner_id is None:
                logger.info(f"find_partner: ищем fallback (пропустили: {skipped_id})")
                if skipped_id:
                    next_candidate = await conn.fetchrow("""
                        SELECT sq.telegram_id
                        FROM search_queue sq
                        WHERE sq.telegram_id != $1
                          AND sq.telegram_id != $2
                        ORDER BY sq.joined_at ASC
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                    """, telegram_id, skipped_id)
                else:
                    next_candidate = await conn.fetchrow("""
                        SELECT sq.telegram_id
                        FROM search_queue sq
                        WHERE sq.telegram_id != $1
                        ORDER BY sq.joined_at ASC
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                    """, telegram_id)

                if not next_candidate:
                    logger.info("find_partner: никого не нашли")
                    return None

                partner_id = next_candidate['telegram_id']
                logger.info(f"find_partner: fallback выбран — {partner_id}")

            # === Создание сессии чата ===
            user1 = await conn.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
            user2 = await conn.fetchrow("SELECT id FROM users WHERE telegram_id = $1", partner_id)

            if not user1 or not user2:
                logger.error(f"Не найден пользователь в БД: {telegram_id} или {partner_id}")
                return None

            existing_session = await conn.fetchrow("""
                SELECT id FROM chat_sessions 
                WHERE (user1_id = $1 AND user2_id = $2) 
                   OR (user1_id = $2 AND user2_id = $1)
                  AND ended_at IS NULL
            """, user1['id'], user2['id'])

            if existing_session:
                session_id = existing_session['id']
                await conn.execute("""
                    UPDATE chat_sessions 
                    SET ended_at = NULL, created_at = CURRENT_TIMESTAMP
                    WHERE id = $1
                """, session_id)
            else:
                session = await conn.fetchrow("""
                    INSERT INTO chat_sessions (user1_id, user2_id)
                    VALUES ($1, $2)
                    ON CONFLICT (user1_id, user2_id) DO UPDATE SET
                        ended_at = NULL, created_at = CURRENT_TIMESTAMP
                    RETURNING id
                """, user1['id'], user2['id'])
                if not session:
                    logger.error("Не удалось создать сессию чата")
                    return None
                session_id = session['id']

            await conn.execute("""
                INSERT INTO active_chats (telegram_id, partner_telegram_id, session_id)
                VALUES ($1, $2, $3), ($4, $5, $6)
                ON CONFLICT (telegram_id) DO UPDATE SET
                    partner_telegram_id = EXCLUDED.partner_telegram_id,
                    session_id = EXCLUDED.session_id,
                    created_at = CURRENT_TIMESTAMP
            """, telegram_id, partner_id, session_id, partner_id, telegram_id, session_id)

            await conn.execute("""
                DELETE FROM search_queue WHERE telegram_id IN ($1, $2)
            """, telegram_id, partner_id)

            logger.info(f"find_partner: чат успешно создан {telegram_id} ↔ {partner_id} (session_id={session_id})")
            return partner_id, session_id

    async def find_partner_by_gender(self, telegram_id: int, target_gender: str) -> Optional[Tuple[int, int]]:
        """Гендерный поиск с приоритетом на взаимность + логирование"""
        async with self.get_connection() as conn:
            my_row = await conn.fetchrow(
                "SELECT gender FROM users WHERE telegram_id = $1",
                telegram_id
            )
            if not my_row or not my_row['gender']:
                logger.warning(f"find_partner_by_gender: пользователь {telegram_id} без пола")
                return None
            my_gender = my_row['gender']

            logger.info(f"find_partner_by_gender: {telegram_id} (пол {my_gender}) ищет {target_gender}")

            partner_id = None

            # Взаимный поиск
            mutual = await conn.fetchrow("""
                SELECT sq.telegram_id
                FROM search_queue sq
                JOIN users u ON u.telegram_id = sq.telegram_id
                WHERE sq.telegram_id != $1
                  AND sq.target_gender = $2
                  AND u.gender = $3
                  AND sq.target_gender IS NOT NULL
                ORDER BY sq.joined_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            """, telegram_id, my_gender, target_gender)

            if mutual:
                partner_id = mutual['telegram_id']
                logger.info(f"find_partner_by_gender: найден взаимный партнёр {partner_id}")

            else:
                if my_gender == target_gender:
                    logger.info("find_partner_by_gender: строгий однополый поиск — ждём только взаимных")
                    return None

                fallback = await conn.fetchrow("""
                    SELECT sq.telegram_id
                    FROM search_queue sq
                    JOIN users u ON u.telegram_id = sq.telegram_id
                    WHERE sq.telegram_id != $1
                      AND u.gender = $2
                    ORDER BY sq.joined_at ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                """, telegram_id, target_gender)

                if fallback:
                    partner_id = fallback['telegram_id']
                    logger.info(f"find_partner_by_gender: fallback — любой с полом {target_gender}: {partner_id}")
                else:
                    logger.info("find_partner_by_gender: никого не нашли")
                    return None

            # === Создание сессии (то же самое, что в find_partner) ===
            user1 = await conn.fetchrow("SELECT id FROM users WHERE telegram_id = $1", telegram_id)
            user2 = await conn.fetchrow("SELECT id FROM users WHERE telegram_id = $1", partner_id)

            if not user1 or not user2:
                logger.error(f"Не найден пользователь в БД: {telegram_id} или {partner_id}")
                return None

            existing_session = await conn.fetchrow("""
                SELECT id FROM chat_sessions 
                WHERE (user1_id = $1 AND user2_id = $2) 
                   OR (user1_id = $2 AND user2_id = $1)
                  AND ended_at IS NULL
            """, user1['id'], user2['id'])

            if existing_session:
                session_id = existing_session['id']
                await conn.execute("""
                    UPDATE chat_sessions 
                    SET ended_at = NULL, created_at = CURRENT_TIMESTAMP
                    WHERE id = $1
                """, session_id)
            else:
                session = await conn.fetchrow("""
                    INSERT INTO chat_sessions (user1_id, user2_id)
                    VALUES ($1, $2)
                    ON CONFLICT (user1_id, user2_id) DO UPDATE SET
                        ended_at = NULL, created_at = CURRENT_TIMESTAMP
                    RETURNING id
                """, user1['id'], user2['id'])
                if not session:
                    logger.error("Не удалось создать сессию чата")
                    return None
                session_id = session['id']

            await conn.execute("""
                INSERT INTO active_chats (telegram_id, partner_telegram_id, session_id)
                VALUES ($1, $2, $3), ($4, $5, $6)
                ON CONFLICT (telegram_id) DO UPDATE SET
                    partner_telegram_id = EXCLUDED.partner_telegram_id,
                    session_id = EXCLUDED.session_id,
                    created_at = CURRENT_TIMESTAMP
            """, telegram_id, partner_id, session_id, partner_id, telegram_id, session_id)

            await conn.execute("""
                DELETE FROM search_queue WHERE telegram_id IN ($1, $2)
            """, telegram_id, partner_id)

            logger.info(
                f"find_partner_by_gender: чат успешно создан {telegram_id} ↔ {partner_id} (session_id={session_id})")
            return partner_id, session_id

    async def add_to_group_search(self, telegram_id: int, target_gender: Optional[str] = None):
        """Добавить в очередь группового поиска"""
        async with self.get_connection() as conn:
            await conn.execute("""
                INSERT INTO group_search_queue (telegram_id, target_gender)
                VALUES ($1, $2)
                ON CONFLICT (telegram_id) DO UPDATE SET
                    joined_at = CURRENT_TIMESTAMP,
                    target_gender = EXCLUDED.target_gender
            """, telegram_id, target_gender)

    async def remove_from_group_search(self, telegram_id: int):
        """Удалить из очереди группового поиска"""
        async with self.get_connection() as conn:
            await conn.execute("""
                DELETE FROM group_search_queue WHERE telegram_id = $1
            """, telegram_id)

    async def find_group_partner(self, telegram_id: int, target_gender: Optional[str], bot: Bot) -> Optional[
        Tuple[list, int, bool]]:
        logger.info(f"find_group_partner вызван для {telegram_id} с target_gender={target_gender}")

        async with self.get_connection() as conn:
            async with conn.transaction():
                # Очистка зависших групп
                await conn.execute("""
                    UPDATE group_chats gc
                    SET is_active = FALSE, ended_at = CURRENT_TIMESTAMP
                    WHERE gc.is_active = TRUE
                      AND (SELECT COUNT(*) FROM group_chat_members gcm WHERE gcm.group_id = gc.id) = 1
                """)

                # 1. Уже в группе — сразу возвращаем, если пользователь состоит в активной группе
                existing = await conn.fetchrow("""
                    SELECT gcm.group_id, 
                           COUNT(*) OVER (PARTITION BY gcm.group_id) AS member_count
                    FROM group_chat_members gcm
                    JOIN group_chats gc ON gc.id = gcm.group_id
                    WHERE gcm.telegram_id = $1 AND gc.is_active = TRUE
                    LIMIT 1
                """, telegram_id)

                if existing:
                    group_id = existing['group_id']
                    members = await conn.fetch("SELECT telegram_id FROM group_chat_members WHERE group_id = $1",
                                               group_id)
                    member_ids = [row['telegram_id'] for row in members]

                    logger.info(f"Пользователь {telegram_id} уже в группе {group_id} с {len(member_ids)} участниками")

                    return member_ids, group_id, False

                # 2. Присоединение к неполной группе — только если совместимо с target_gender
                candidate_group = await conn.fetchrow("""
                    SELECT gc.id AS group_id
                    FROM group_chats gc
                    WHERE gc.is_active = TRUE
                      AND (SELECT COUNT(*) FROM group_chat_members gcm WHERE gcm.group_id = gc.id) BETWEEN 1 AND 2
                    ORDER BY gc.created_at ASC
                    LIMIT 1
                    FOR UPDATE OF gc SKIP LOCKED
                """)

                if candidate_group:
                    group_id = candidate_group['group_id']
                    logger.info(f"Найдена неполная группа {group_id}")

                    # Получаем полы участников
                    existing = await conn.fetch("""
                        SELECT u.telegram_id, u.gender
                        FROM group_chat_members gcm
                        JOIN users u ON u.telegram_id = gcm.telegram_id
                        WHERE gcm.group_id = $1
                    """, group_id)

                    genders = [row['gender'] for row in existing if row['gender'] is not None]
                    logger.info(f"Полы в группе {group_id}: {genders}")

                    # Проверка совместимости
                    compatible = True
                    if target_gender:
                        # Гендерный поиск: все существующие должны быть целевым полом
                        if not all(g == target_gender for g in genders):
                            logger.info(
                                f"Группа {group_id} НЕ подходит — существующие полы {genders}, а ищем {target_gender}. Пропускаем.")
                            compatible = False

                    if compatible:
                        # Подходит — присоединяемся
                        before_count = len(existing)

                        await conn.execute(
                            "INSERT INTO group_chat_members (group_id, telegram_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                            group_id, telegram_id)
                        await self.remove_from_group_search(telegram_id)

                        after_count = before_count + 1
                        after_ids = [row['telegram_id'] for row in existing] + [telegram_id]

                        if after_count < 2:
                            await conn.execute("DELETE FROM group_chat_members WHERE group_id = $1", group_id)
                            await conn.execute("UPDATE group_chats SET is_active = FALSE WHERE id = $1", group_id)
                            return None

                        if before_count == 2 and after_count == 3:
                            await bot.send_message(telegram_id,
                                                   f"👥 Вы присоединились к групповому чату!\n\nУчастников: {after_count}\n\n/leave - Покинуть групповой чат",
                                                   parse_mode="HTML")
                            for old in [r['telegram_id'] for r in existing]:
                                await bot.send_message(old,
                                                       f"👤 Новый участник присоединился к чату!\n\nТеперь в чате {after_count} участников")

                        return after_ids, group_id, True

                # 3. Создание новой группы — с поддержкой постепенного заполнения
                logger.info(f"Создаём новую группу для {telegram_id} с target_gender={target_gender}")

                my_gender_row = await conn.fetchrow("SELECT gender FROM users WHERE telegram_id = $1", telegram_id)
                my_gender = my_gender_row['gender'] if my_gender_row else None
                if not my_gender:
                    logger.warning(f"Пользователь {telegram_id} без пола — не можем создать группу")
                    return None
                logger.info(f"Мой пол: {my_gender}")

                base_query = """
                    SELECT gsq.telegram_id, u.gender, gsq.target_gender
                    FROM group_search_queue gsq
                    JOIN users u ON u.telegram_id = gsq.telegram_id
                    WHERE gsq.telegram_id != $1
                """
                base_params = [telegram_id]

                partners = []

                if target_gender:
                    # === ГЕНДЕРНЫЙ ПОИСК ===
                    # Приоритет: случайные нужного пола (чтобы не смешиваться с другими гендерными искателями)
                    # Этап 1: ищем случайных нужного пола
                    random_gender_query = base_query + " AND u.gender = $2 AND gsq.target_gender IS NULL ORDER BY gsq.joined_at ASC LIMIT 2 FOR UPDATE SKIP LOCKED"
                    random_params = [telegram_id, target_gender]
                    random_partners = await conn.fetch(random_gender_query, *random_params)

                    if random_partners:
                        logger.info(f"Гендерный поиск: найдены случайные нужного пола ({len(random_partners)})")
                        partners = random_partners
                    else:
                        # Этап 2: случайных нет — берём ОДНОГО, кто ищет мой пол (взаимность)
                        mutual_gender_query = base_query + " AND u.gender = $2 AND gsq.target_gender = $3 ORDER BY gsq.joined_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED"
                        mutual_params = [telegram_id, target_gender, my_gender]
                        partners = await conn.fetch(mutual_gender_query, *mutual_params)
                        logger.info(f"Гендерный поиск: случайных нет, найден 1 взаимный (ищущий {my_gender})")

                    if not partners:
                        logger.info("Гендерный поиск: никого не найдено — ждём")
                else:
                    # === СЛУЧАЙНЫЙ ПОИСК (остаётся как было) ===
                    mutual_query = base_query + " AND gsq.target_gender = $2 ORDER BY gsq.joined_at ASC LIMIT 1 FOR UPDATE SKIP LOCKED"
                    mutual_params = [telegram_id, my_gender]
                    mutual_partners = await conn.fetch(mutual_query, *mutual_params)

                    if mutual_partners:
                        logger.info(f"Случайный поиск: найден 1 взаимный партнёр")
                        partners = mutual_partners
                    else:
                        random_query = base_query + " AND gsq.target_gender IS NULL ORDER BY gsq.joined_at ASC LIMIT 2 FOR UPDATE SKIP LOCKED"
                        partners = await conn.fetch(random_query, *base_params)
                        logger.info(f"Случайный поиск: берём случайных ({len(partners)})")

                logger.info(
                    f"Найдено подходящих партнёров: {[(p['telegram_id'], p['gender'], p['target_gender']) for p in partners]}"
                )

                if len(partners) == 0:
                    logger.info("Нет подходящих партнёров — ждём первого")
                    return None

                # Создаём группу
                partner_ids = [row['telegram_id'] for row in partners]
                all_members = [telegram_id] + partner_ids

                new_group = await conn.fetchrow("INSERT INTO group_chats DEFAULT VALUES RETURNING id")
                group_id = new_group['id']

                for member in all_members:
                    await conn.execute(
                        "INSERT INTO group_chat_members (group_id, telegram_id) VALUES ($1, $2)",
                        group_id, member
                    )

                await conn.execute("DELETE FROM group_search_queue WHERE telegram_id = ANY($1)", all_members)

                logger.info(f"Создана группа {group_id} с участниками {all_members} (ожидаем до 3)")

                return all_members, group_id, False

    async def add_to_group_chat(self, group_id: int, telegram_id: int) -> bool:
        """Добавить третьего участника в существующий групповой чат"""
        async with self.get_connection() as conn:
            # Проверяем, есть ли уже 3 участника
            count = await conn.fetchrow("""
                SELECT COUNT(*) as cnt FROM group_chat_members WHERE group_id = $1
            """, group_id)
            if count['cnt'] >= 3:
                return False

            # Добавляем нового
            await conn.execute("""
                INSERT INTO group_chat_members (group_id, telegram_id)
                VALUES ($1, $2)
                ON CONFLICT DO NOTHING
            """, group_id, telegram_id)

            # Удаляем из очереди
            await self.remove_from_group_search(telegram_id)

            return True

    async def get_group_id(self, telegram_id: int) -> Optional[int]:
        async with self.get_connection() as conn:
            try:
                row = await conn.fetchrow("""
                    SELECT gcm.group_id 
                    FROM group_chat_members gcm
                    JOIN group_chats gc ON gc.id = gcm.group_id
                    WHERE gcm.telegram_id = $1 
                    AND gc.is_active = TRUE
                    LIMIT 1
                """, telegram_id)

                if row:
                    logger.info(f"get_group_id: найдена группа {row['group_id']} для пользователя {telegram_id}")
                    return row['group_id']
                else:
                    logger.info(f"get_group_id: активная группа не найдена для пользователя {telegram_id}")
                    return None

            except Exception as e:
                logger.error(f"Ошибка в get_group_id для {telegram_id}: {e}")
                return None

    async def get_group_members(self, telegram_id: int) -> Optional[list]:
        """Получить список участников группы по одному ID"""
        async with self.get_connection() as conn:
            try:
                # Находим активную группу пользователя
                group_row = await conn.fetchrow("""
                    SELECT gcm.group_id 
                    FROM group_chat_members gcm
                    JOIN group_chats gc ON gc.id = gcm.group_id
                    WHERE gcm.telegram_id = $1 
                    AND gc.is_active = TRUE
                    LIMIT 1
                """, telegram_id)

                if not group_row:
                    return None

                group_id = group_row['group_id']

                # Находим ВСЕХ участников этой группы
                members = await conn.fetch("""
                    SELECT telegram_id FROM group_chat_members
                    WHERE group_id = $1
                """, group_id)

                logger.info(f"get_group_members: группа {group_id}, участники: {[m['telegram_id'] for m in members]}")
                return [m['telegram_id'] for m in members]

            except Exception as e:
                logger.error(f"Ошибка в get_group_members для {telegram_id}: {e}")
                return None

    async def get_chat_recipients(telegram_id: int) -> list:
        """Получить всех получателей сообщения (для 1-на-1 или группы)"""
        # Сначала проверяем групповой чат
        members = await db.get_group_members(telegram_id)
        if members and len(members) > 1:
            return [m for m in members if m != telegram_id]

        # Если не группа — обычный 1-на-1
        partner = await db.get_partner(telegram_id)
        if partner:
            return [partner]

        return []

    async def end_group_chat(self, telegram_id: int):
        """Завершить групповой чат"""
        async with self.get_connection() as conn:
            members = await self.get_group_members(telegram_id)
            if not members:
                return

            # Удаляем всех из членов
            await conn.execute("""
                DELETE FROM group_chat_members WHERE telegram_id = ANY($1)
            """, members)

    async def get_partner(self, telegram_id: int) -> Optional[int]:
        """Получить ID партнера"""
        async with self.get_connection() as conn:
            result = await conn.fetchrow("""
                SELECT partner_telegram_id FROM active_chats 
                WHERE telegram_id = $1
            """, telegram_id)
            return result['partner_telegram_id'] if result else None

    async def get_session(self, telegram_id: int) -> Optional[int]:
        """Получить ID сессии"""
        async with self.get_connection() as conn:
            result = await conn.fetchrow("""
                SELECT session_id FROM active_chats 
                WHERE telegram_id = $1
            """, telegram_id)
            return result['session_id'] if result else None

    async def end_chat(self, telegram_id: int) -> Optional[Tuple[int, int]]:
        """Завершить чат"""
        async with self.get_connection() as conn:
            # Получаем информацию о чате
            chat_info = await conn.fetchrow("""
                SELECT partner_telegram_id, session_id 
                FROM active_chats 
                WHERE telegram_id = $1
            """, telegram_id)

            if not chat_info:
                return None

            partner_id = chat_info['partner_telegram_id']
            session_id = chat_info['session_id']

            # Удаляем активные соединения
            await conn.execute("""
                DELETE FROM active_chats 
                WHERE telegram_id IN ($1, $2)
            """, telegram_id, partner_id)

            # Проверяем, есть ли другие активные соединения с этой сессией
            remaining_active = await conn.fetchrow("""
                SELECT COUNT(*) as count FROM active_chats 
                WHERE session_id = $1
            """, session_id)

            # Если нет других активных соединений с этой сессией, помечаем как завершенную
            if remaining_active and remaining_active['count'] == 0:
                await conn.execute("""
                    UPDATE chat_sessions 
                    SET ended_at = CURRENT_TIMESTAMP 
                    WHERE id = $1 AND ended_at IS NULL
                """, session_id)

            return partner_id, session_id

    async def add_rating(self, rater_telegram_id: int, rated_telegram_id: int,
                         rating: int, session_id: int = None):
        """Добавить оценку собеседнику"""
        async with self.get_connection() as conn:
            # Получаем user_id обоих пользователей
            rater_user = await conn.fetchrow(
                "SELECT id FROM users WHERE telegram_id = $1",
                rater_telegram_id
            )
            rated_user = await conn.fetchrow(
                "SELECT id FROM users WHERE telegram_id = $1",
                rated_telegram_id
            )

            if not rater_user or not rated_user:
                return False

            # Если session_id не указан, пытаемся найти последнюю сессию
            if not session_id:
                session = await conn.fetchrow("""
                    SELECT cs.id FROM chat_sessions cs
                    JOIN users u1 ON cs.user1_id = u1.id
                    JOIN users u2 ON cs.user2_id = u2.id
                    WHERE (u1.telegram_id = $1 AND u2.telegram_id = $2)
                       OR (u1.telegram_id = $2 AND u2.telegram_id = $1)
                    ORDER BY cs.created_at DESC
                    LIMIT 1
                """, rater_telegram_id, rated_telegram_id)

                if session:
                    session_id = session['id']

            await conn.execute("""
                INSERT INTO user_ratings 
                (rater_user_id, rated_user_id, session_id, rating)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (rater_user_id, rated_user_id, session_id) 
                DO UPDATE SET 
                    rating = EXCLUDED.rating,
                    created_at = CURRENT_TIMESTAMP
            """, rater_user['id'], rated_user['id'], session_id, rating)

            return True

    async def add_complaint(self, reporter_telegram_id: int, reported_telegram_id: int,
                            complaint: str, category: str = None, session_id: int = None):
        """Добавить жалобу на собеседника"""
        async with self.get_connection() as conn:
            # Получаем user_id обоих пользователей
            reporter_user = await conn.fetchrow(
                "SELECT id FROM users WHERE telegram_id = $1",
                reporter_telegram_id
            )
            reported_user = await conn.fetchrow(
                "SELECT id FROM users WHERE telegram_id = $1",
                reported_telegram_id
            )

            if not reporter_user or not reported_user:
                return False

            # Если session_id не указан, пытаемся найти последнюю сессию
            if not session_id:
                session = await conn.fetchrow("""
                    SELECT cs.id FROM chat_sessions cs
                    JOIN users u1 ON cs.user1_id = u1.id
                    JOIN users u2 ON cs.user2_id = u2.id
                    WHERE (u1.telegram_id = $1 AND u2.telegram_id = $2)
                       OR (u1.telegram_id = $2 AND u2.telegram_id = $1)
                    ORDER BY cs.created_at DESC
                    LIMIT 1
                """, reporter_telegram_id, reported_telegram_id)

                if session:
                    session_id = session['id']

            await conn.execute("""
                INSERT INTO user_ratings 
                (rater_user_id, rated_user_id, session_id, complaint, complaint_category)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (rater_user_id, rated_user_id, session_id) 
                DO UPDATE SET 
                    complaint = EXCLUDED.complaint,
                    complaint_category = EXCLUDED.complaint_category,
                    created_at = CURRENT_TIMESTAMP
            """, reporter_user['id'], reported_user['id'], session_id, complaint, category)

            return True

    async def get_user_rating_stats(self, telegram_id: int) -> dict:
        """Получить статистику оценок пользователя"""
        async with self.get_connection() as conn:
            user = await conn.fetchrow(
                "SELECT id FROM users WHERE telegram_id = $1",
                telegram_id
            )

            if not user:
                return {"likes": 0, "dislikes": 0, "complaints": 0}

            stats = await conn.fetchrow("""
                SELECT 
                    COUNT(CASE WHEN rating = 1 THEN 1 END) as likes,
                    COUNT(CASE WHEN rating = -1 THEN 1 END) as dislikes,
                    COUNT(CASE WHEN complaint IS NOT NULL THEN 1 END) as complaints
                FROM user_ratings 
                WHERE rated_user_id = $1
            """, user['id'])

            return {
                "likes": stats['likes'] or 0,
                "dislikes": stats['dislikes'] or 0,
                "complaints": stats['complaints'] or 0
            }

    async def has_active_premium(self, telegram_id: int) -> bool:
        async with self.get_connection() as conn:
            result = await conn.fetchrow("""
                SELECT 1 FROM premium
                WHERE telegram_id = $1
                  AND is_active = TRUE
                  AND expires_at > CURRENT_TIMESTAMP
                LIMIT 1
            """, telegram_id)
            return result is not None

    async def buy_premium(self, telegram_id: int, stars_paid: int) -> Tuple[bool, str]:
        """Купить/выдать премиум с СТАКИНГОМ (добавление дней к текущему сроку)"""
        async with self.get_connection() as conn:
            # Определяем длительность
            if stars_paid == 49:
                duration_days = 1
            elif stars_paid == 99:
                duration_days = 7
            elif stars_paid == 199:
                duration_days = 30
            else:
                return False, "Неверная сумма"

            now_utc = datetime.now(timezone.utc)

            # Получаем текущую запись
            current = await conn.fetchrow("""
                SELECT expires_at FROM premium
                WHERE telegram_id = $1 AND is_active = TRUE
            """, telegram_id)

            if current:
                # Приводим expires_at из базы к aware UTC для сравнения
                current_expires = current['expires_at']
                if current_expires.tzinfo is None:
                    current_expires = current_expires.replace(tzinfo=timezone.utc)
                else:
                    current_expires = current_expires.astimezone(timezone.utc)

                if current_expires > now_utc:
                    # Стакаем — добавляем дни к текущей дате окончания
                    new_expires_at = current_expires + timedelta(days=duration_days)
                    message = f"Премиум продлён на {duration_days} дней"
                else:
                    # Премиум истёк — начинаем заново
                    new_expires_at = now_utc + timedelta(days=duration_days)
                    message = f"Премиум активирован на {duration_days} дней"
            else:
                new_expires_at = now_utc + timedelta(days=duration_days)
                message = f"Премиум активирован на {duration_days} дней"

            # Перед записью в базу убираем tzinfo — делаем naive UTC
            new_expires_naive = new_expires_at.replace(tzinfo=None)

            # Записываем в базу
            await conn.execute("""
                INSERT INTO premium (telegram_id, stars_paid, duration_days, expires_at)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (telegram_id) DO UPDATE SET
                    stars_paid = EXCLUDED.stars_paid,
                    duration_days = EXCLUDED.duration_days,
                    purchased_at = CURRENT_TIMESTAMP,
                    expires_at = EXCLUDED.expires_at,
                    is_active = TRUE
            """, telegram_id, stars_paid, duration_days, new_expires_naive)

            return True, message

    async def get_premium_info(self, telegram_id: int) -> Optional[dict]:
        async with self.get_connection() as conn:
            row = await conn.fetchrow("""
                SELECT stars_paid, duration_days, purchased_at, expires_at
                FROM premium
                WHERE telegram_id = $1 AND is_active = TRUE
                ORDER BY purchased_at DESC
                LIMIT 1
            """, telegram_id)
            if row:
                return dict(row)
            return None

    async def get_premium_remaining_time(self, telegram_id: int) -> Optional[str]:
        async with self.get_connection() as conn:
            row = await conn.fetchrow("""
                SELECT expires_at FROM premium
                WHERE telegram_id = $1 AND is_active = TRUE
                ORDER BY purchased_at DESC
                LIMIT 1
            """, telegram_id)

            if not row:
                return None

            expires_at = row['expires_at']
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            else:
                expires_at = expires_at.astimezone(timezone.utc)

            now = datetime.now(timezone.utc)
            remaining = expires_at - now

            if remaining.total_seconds() <= 0:
                return None

            days = remaining.days
            hours = remaining.seconds // 3600
            minutes = (remaining.seconds % 3600) // 60

            parts = []
            if days > 0:
                parts.append(f"{days} д.")
            if hours > 0:
                parts.append(f"{hours} ч.")
            if minutes > 0 and days == 0 and hours == 0:
                parts.append(f"{minutes} мин.")

            return " ".join(parts) if parts else "менее минуты"


# ========== ИНИЦИАЛИЗАЦИЯ БАЗЫ И РОУТЕРА ==========
db = Database()
router = Router(name="anonymous_chat")


# ========== СОСТОЯНИЯ FSM ==========
class ChatState(StatesGroup):
    idle = State()
    searching = State()
    chatting = State()
    rating = State()


class ProfileState(StatesGroup):
    main = State()
    gender = State()
    age = State()


# Добавить в StatesGroup (можно добавить к существующим ChatState или создать новый)
class SearchByGenderState(StatesGroup):
    selecting_gender = State()  # Выбор пола для поиска
    searching = State()         # В процессе поиска


class GroupSearchState(StatesGroup):
    selecting_mode = State()  # выбор режима: случайные / найти девушек / найти парней
    searching = State()       # в процессе поиска группы


# ========== КЛАВИАТУРЫ ==========
def get_main_keyboard():
    """Главная клавиатура — Поиск по полу и Профиль в третьем ряду"""
    builder = ReplyKeyboardBuilder()

    # Первый ряд — 1 кнопка
    builder.button(text="🔍 Найти собеседника")

    # Второй ряд — 1 кнопка (центрировано)
    builder.button(text="👥 Групповой поиск")

    # Третий ряд — 2 кнопки
    builder.button(text="🍓 Поиск по полу")
    builder.button(text="⚙️ Профиль")

    # Расположение: 1 в первом, 1 во втором, 2 в третьем
    builder.adjust(1, 1, 2)

    return builder.as_markup(
        resize_keyboard=True,
        input_field_placeholder="Выберите действие..."
    )


def get_profile_keyboard():
    """Клавиатура в меню профиля"""
    builder = ReplyKeyboardBuilder()

    builder.button(text="🚻 Пол")
    builder.button(text="🔞 Возраст")
    builder.button(text="← Назад")

    builder.adjust(2, 1)

    return builder.as_markup(
        resize_keyboard=True,
        input_field_placeholder="Выберите параметр..."
    )


def get_gender_keyboard():
    """Клавиатура выбора пола"""
    builder = ReplyKeyboardBuilder()

    builder.button(text="👨 Парень")
    builder.button(text="👩 Девушка")
    builder.button(text="← Назад")

    builder.adjust(2, 1)

    return builder.as_markup(
        resize_keyboard=True,
        input_field_placeholder="Выберите пол..."
    )


def get_age_keyboard():
    """Клавиатура для возврата из возраста"""
    builder = ReplyKeyboardBuilder()

    builder.button(text="← Назад")

    return builder.as_markup(
        resize_keyboard=True,
        input_field_placeholder="Введите возраст 16-99..."
    )


def get_rating_inline_keyboard():
    """Inline-клавиатура для оценки собеседника"""
    keyboard = [
        [
            InlineKeyboardButton(text="👍", callback_data="rating_like"),
            InlineKeyboardButton(text="👎", callback_data="rating_dislike")
        ],
        [
            InlineKeyboardButton(text="⚠️ Пожаловаться →", callback_data="rating_complaint")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_complaint_categories_inline_keyboard():
    """Inline-клавиатура категорий жалоб с кнопкой Назад"""
    keyboard = [
        [
            InlineKeyboardButton(text="🚫 Оскорбления", callback_data="complaint_insults")
        ],
        [
            InlineKeyboardButton(text="📵 Контент 18+", callback_data="complaint_adult")
        ],
        [
            InlineKeyboardButton(text="💳 Мошенничество", callback_data="complaint_fraud")
        ],
        [
            InlineKeyboardButton(text="📢 Спам", callback_data="complaint_spam")
        ],
        [
            InlineKeyboardButton(text="👤 Выдача за другого", callback_data="complaint_impersonation")
        ],
        [
            InlineKeyboardButton(text="👥 Другое", callback_data="complaint_other")
        ],
        [
            InlineKeyboardButton(text="← Назад", callback_data="complaint_back")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_premium_inline_keyboard():
    """Инлайн-клавиатура для покупки премиума (без кнопки 'Назад')"""
    keyboard = [
        [
            InlineKeyboardButton(text="⭐ 49 Stars — 1 день", callback_data="buy_premium_49")
        ],
        [
            InlineKeyboardButton(text="⭐ 99 Stars — 7 дней", callback_data="buy_premium_99")
        ],
        [
            InlineKeyboardButton(text="⭐ 199 Stars — 30 дней", callback_data="buy_premium_199")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


# ========== КОМАНДА СТАРТ ==========
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    """Обработка команды /start"""
    await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.first_name or ""
    )

    await state.set_state(ChatState.idle)

    # Получаем имя пользователя
    user_name = message.from_user.first_name or message.from_user.username or "аноним"

    welcome_text = (
        f"👋 <b>Добро пожаловать, {user_name}!</b>\n\n"
        "✨ <b>Возможности:</b>\n"
        "├ 🎯 Найти собеседника\n"
        "├ 👥 Групповой поиск\n"
        "├ 🔍 Поиск по полу\n"
        "└ ⚙️ Профиль\n\n"
        "<i>Нажмите кнопку ниже, чтобы начать!</i>"
    )

    await message.answer(
        welcome_text,
        reply_markup=get_main_keyboard(),
        parse_mode="HTML"
    )


# ========== КНОПКА "НАЙТИ СОБЕСЕДНИКА" ==========
@router.message(F.text == "🔍 Найти собеседника")
async def find_chat_button(message: Message, state: FSMContext):
    """Обработка нажатия кнопки поиска"""
    await cmd_search(message, state)


# ========== КНОПКА "ПОИСК ПО ПОЛУ" ==========
@router.message(F.text == "🍓 Поиск по полу")
async def search_by_gender_button(message: Message, state: FSMContext):
    """Обработка нажатия кнопки поиска по полу"""

    # 1. Проверяем, указан ли пол
    user_profile = await db.get_user_profile(message.from_user.id)
    if not user_profile or not user_profile.get('gender'):
        await message.answer(
            "⚠️ <b>Пожалуйста, сначала укажите ваш пол!</b>\n\n"
            "Перейдите в ⚙️ Профиль → 🚻 Пол и выберите ваш пол.",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
        return

    # 2. Проверяем премиум
    has_premium = await db.has_active_premium(message.from_user.id)
    if not has_premium:
        await message.answer(
            "🍓 <b>Поиск по полу</b> — эксклюзивная премиум-функция!\n\n"
            "Чтобы найти именно девушку или парня — активируйте премиум:\n\n"
            "💎 <b>Выберите подписку:</b>",
            parse_mode="HTML",
            reply_markup=get_premium_inline_keyboard()
        )
        return

    # 3. Премиум есть — показываем обычные кнопки выбора пола (reply)
    keyboard = ReplyKeyboardBuilder()
    keyboard.button(text="👩 Найти девушку")
    keyboard.button(text="👨 Найти парня")
    keyboard.button(text="← Назад")
    keyboard.adjust(2, 1)

    await message.answer(
        "Выберите пол собеседника для поиска:",
        parse_mode="HTML",
        reply_markup=keyboard.as_markup(resize_keyboard=True)
    )


@router.message(F.text == "👥 Групповой поиск")
async def group_search_menu(message: Message, state: FSMContext):
    """Меню выбора режима группового поиска — с премиум-отметкой"""
    user_profile = await db.get_user_profile(message.from_user.id)
    if not user_profile or not user_profile.get('gender'):
        await message.answer(
            "⚠️ <b>Пожалуйста, сначала укажите ваш пол!</b>\n\n"
            "Перейдите в ⚙️ Профиль → 🚻 Пол и выберите ваш пол.",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
        return

    keyboard = ReplyKeyboardBuilder()
    keyboard.button(text="🎲 Случайные собеседники")
    keyboard.button(text="🙋‍♀️ Найти девушек")
    keyboard.button(text="🙋‍♂️ Найти парней")
    keyboard.button(text="← Назад")
    keyboard.adjust(1, 2, 1)

    await message.answer(
        "👥 <b>Групповой поиск</b>\n\n"
        "🎲 <b>Случайные собеседники</b>\n"
        "└ <i>3 случайных пользователя</i>\n\n"
        "🙋‍♀️ <b>Найти девушек</b>\n"
        "└ Групповой чат с 2 девушками\n"
        "   💎 <i>Требуется премиум</i>\n\n"
        "🙋‍♂️ <b>Найти парней</b>\n"
        "└ Групповой чат с 2 парнями\n"
        "   💎 <i>Требуется премиум</i>\n\n"
        "🚀 Выберите тип поиска:",
        parse_mode="HTML",
        reply_markup=keyboard.as_markup(resize_keyboard=True)
    )
    await state.set_state(GroupSearchState.selecting_mode)


@router.message(GroupSearchState.selecting_mode)
async def start_group_search(message: Message, state: FSMContext):
    if message.text == "← Назад":
        await state.clear()
        await message.answer("Главное меню:", reply_markup=get_main_keyboard())
        return

    user_profile = await db.get_user_profile(message.from_user.id)
    user_gender = user_profile['gender']

    # Случайный поиск — доступен всем
    if message.text == "🎲 Случайные собеседники":
        target_gender = None
        search_text = "собеседников"

    # Гендерные варианты
    elif message.text in ["🙋‍♀️ Найти девушек", "🙋‍♂️ Найти парней"]:
        # === СНАЧАЛА ПРОВЕРЯЕМ ПРЕМИУМ ===
        if not await db.has_active_premium(message.from_user.id):
            await message.answer(
                "🍓 <b>Поиск по полу в групповом чате</b> — эксклюзивная премиум-функция!\n\n"
                "Чтобы создать группу именно с девушками или парнями — активируйте премиум:\n\n"
                "💎 <b>Выберите подписку:</b>",
                parse_mode="HTML",
                reply_markup=get_premium_inline_keyboard()
            )
            return

        # === ТОЛЬКО ЕСЛИ ПРЕМИУМ ЕСТЬ — ПРОВЕРЯЕМ ПОЛ ===
        if message.text == "🙋‍♀️ Найти девушек":
            if user_gender != "male":
                await message.answer("❌ Эта опция доступна только парням.")
                return
            target_gender = "female"
            search_text = "девушек"

        elif message.text == "🙋‍♂️ Найти парней":
            if user_gender != "female":
                await message.answer("❌ Эта опция доступна только девушкам.")
                return
            target_gender = "male"
            search_text = "парней"

    else:
        await message.answer("Выберите вариант из меню.")
        return

    # Если дошли сюда — всё ок: либо случайный, либо премиум + правильный пол
    await db.add_to_group_search(message.from_user.id, target_gender)

    result = await db.find_group_partner(message.from_user.id, target_gender, message.bot)

    if result:
        members, group_id, is_joining = result
        member_count = len(members)
        initiator_id = message.from_user.id

        for member in members:
            try:
                if is_joining and member == initiator_id:
                    continue
                text = f"👥 Групповой чат создан!\n\nУчастников: {member_count}\n\n/leave - Покинуть групповой чат"
                await message.bot.send_message(member, text, parse_mode="HTML")

                key = StorageKey(bot_id=message.bot.id, chat_id=member, user_id=member)
                member_state = FSMContext(storage=state.storage, key=key)
                await member_state.set_state(ChatState.chatting)
            except Exception as e:
                logger.error(f"Ошибка обработки участника {member}: {e}")

        await state.set_state(ChatState.chatting)
    else:
        await message.answer(
            f"🔍 Ищем {search_text}...\n\n"
            "/leave — остановить поиск",
            parse_mode="HTML"
        )
        await state.set_state(GroupSearchState.searching)


@router.callback_query(F.data.startswith("buy_premium_"))
async def process_buy_premium_callback(callback: CallbackQuery, bot: Bot):
    """Обработка покупки премиума"""
    stars_str = callback.data.split("_")[-1]
    stars = int(stars_str)

    # Определяем длительность для заголовка и описания
    if stars == 199:
        duration = "1 месяц"
    elif stars == 99:
        duration = "7 дней"
    else:
        duration = "1 день"

    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=f"Премиум доступ · {duration}",
        description=(
            "Полный доступ\n"
            "ко всем премиум-функциям\n"
            f"на {duration}."
        ),
        payload=str(stars),
        provider_token="",
        currency="XTR",
        prices=[types.LabeledPrice(label="Премиум", amount=stars)],
    )

    await callback.answer()


# ========== ПРОФИЛЬ ==========
@router.message(F.text == "⚙️ Профиль")
async def profile_menu(message: Message, state: FSMContext):  # ← добавил state
    """Отображение профиля пользователя"""
    user_profile = await db.get_user_profile(message.from_user.id)
    if not user_profile:
        await message.answer("Ошибка загрузки профиля.")
        return

    telegram_id = message.from_user.id
    gender = user_profile.get('gender')
    age = user_profile.get('age')

    gender_text = "Парень" if gender == "male" else "Девушка" if gender == "female" else "Не указан"
    age_text = age if age else "Не указан"

    # Статистика репутации
    stats = await db.get_user_rating_stats(telegram_id)
    likes = stats['likes']
    dislikes = stats['dislikes']
    complaints = stats['complaints']

    # Оставшееся время премиума
    remaining_time = await db.get_premium_remaining_time(telegram_id)
    has_premium = remaining_time is not None

    # Блок статуса аккаунта
    if has_premium:
        premium_block = (
            "💎 <b>Статус аккаунта:</b>\n"
            "└ ✅ <b>Премиум-аккаунт</b>\n"
            f"⏰ Осталось: <b>{remaining_time}</b>"
        )
    else:
        premium_block = (
            "💎 <b>Статус аккаунта:</b>\n"
            "└ ❌ Обычный аккаунт"
        )

    # Блок репутации
    reputation_block = (
        "⭐️ <b>Репутация:</b>\n"
        f"├ 👍 Лайки: <b>{likes}</b>\n"
        f"├ 👎 Дизлайки: <b>{dislikes}</b>\n"
        f"└ ⚠️ Нарушения: <b>{complaints}</b>"
    )

    profile_text = (
        f"🆔 <code>{telegram_id}</code>\n\n"
        f"📊 <b>Основная информация:</b>\n"
        f"├ 🚻 Пол: <b>{gender_text}</b>\n"
        f"└ 🔞 Возраст: <b>{age_text}</b>\n\n"
        f"{premium_block}\n\n"
        f"{reputation_block}"
    )

    await message.answer(
        profile_text,
        parse_mode="HTML",
        reply_markup=get_profile_keyboard()
    )

    # Теперь state доступен — устанавливаем состояние профиля
    await state.set_state(ProfileState.main)


# ========== КНОПКИ ПРОФИЛЯ ==========
@router.message(F.text == "🚻 Пол", ProfileState.main)
async def profile_gender(message: Message, state: FSMContext):
    """Выбор пола в профиле"""
    await message.answer("Выберите ваш пол:", reply_markup=get_gender_keyboard())
    await state.set_state(ProfileState.gender)


@router.message(F.text == "🔞 Возраст", ProfileState.main)
async def profile_age(message: Message, state: FSMContext):
    """Установка возраста"""
    await message.answer("Введите ваш возраст (16-99):", reply_markup=get_age_keyboard())
    await state.set_state(ProfileState.age)


@router.message(F.text == "← Назад")
async def profile_back(message: Message, state: FSMContext):
    """Возврат в главное меню"""
    current_state = await state.get_state()

    if current_state in [ProfileState.main, ProfileState.gender, ProfileState.age]:
        await state.set_state(ChatState.idle)
        await message.answer("Возврат в главное меню", reply_markup=get_main_keyboard())
    else:
        await message.answer("Возврат в главное меню", reply_markup=get_main_keyboard())


# ========== ВЫБОР ПОЛА ==========
@router.message(F.text.in_(["👨 Парень", "👩 Девушка"]), ProfileState.gender)
async def set_gender(message: Message, state: FSMContext):
    """Установить пол пользователя"""
    gender_map = {
        "👨 Парень": "male",
        "👩 Девушка": "female"
    }

    gender = gender_map[message.text]
    await db.update_user_gender(message.from_user.id, gender)

    gender_display = "Парень" if gender == "male" else "Девушка"
    await message.answer(f"✅ Пол установлен: {gender_display}", reply_markup=get_profile_keyboard())
    await state.set_state(ProfileState.main)


# ========== УСТАНОВКА ВОЗРАСТА ==========
@router.message(ProfileState.age)
async def set_age(message: Message, state: FSMContext):
    """Установить возраст пользователя"""
    if message.text == "← Назад":
        await message.answer("Профиль:", reply_markup=get_profile_keyboard())
        await state.set_state(ProfileState.main)
        return

    try:
        age = int(message.text)
        if 16 <= age <= 99:
            await db.update_user_age(message.from_user.id, age)
            await message.answer(f"✅ Возраст установлен: {age}", reply_markup=get_profile_keyboard())
            await state.set_state(ProfileState.main)
        else:
            await message.answer("❌ Возраст должен быть от 16 до 99 лет")
    except ValueError:
        await message.answer("❌ Пожалуйста, введите число от 16 до 99")


@router.message(Command("search"))
@router.message(Command("next"))
@router.message(F.text == "🔍 Найти собеседника")
async def cmd_search(message: Message, state: FSMContext):
    """Начать поиск собеседника"""

    # Проверяем, указан ли пол у пользователя
    user_profile = await db.get_user_profile(message.from_user.id)

    if not user_profile or not user_profile.get('gender'):
        await message.answer(
            "⚠️ <b>Пожалуйста, сначала укажите ваш пол!</b>\n\n"
            "Перейдите в ⚙️ Профиль → 🚻 Пол и выберите ваш пол.",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
        return

    await db.ensure_user(
        message.from_user.id,
        message.from_user.username or "",
        message.from_user.first_name or ""
    )

    current_state = await state.get_state()

    if current_state == ChatState.searching:
        await message.answer(
            "🔍 <i>Ищем собеседника...</i>\n\n"
            "<i>/stop — остановить поиск</i>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
        return

    # Завершаем текущий диалог, если он есть (/next во время чата)
    if current_state == ChatState.chatting:
        partner_info = await db.end_chat(message.from_user.id)

        # СООБЩЕНИЕ ОТПРАВИТЕЛЮ КОМАНДЫ /next
        await message.answer(
            "<i>Диалог остановлен</i> 😔\n\n"
            "<i>Начинаю поиск нового собеседника...</i>",
            parse_mode="HTML",
            reply_markup=get_rating_inline_keyboard()  # ИЗМЕНЕНИЕ: inline-кнопки
        )

        if partner_info:
            partner_id, session_id = partner_info
            try:
                # Сбрасываем состояние партнёру
                partner_key = StorageKey(
                    bot_id=message.bot.id,
                    chat_id=partner_id,
                    user_id=partner_id
                )
                partner_context = FSMContext(storage=state.storage, key=partner_key)
                await partner_context.set_state(ChatState.idle)

                await message.bot.send_message(
                    partner_id,
                    "<i>Диалог остановлен</i> 😔\n\n"
                    "<i>Отправьте /search, чтобы начать поиск</i>",
                    parse_mode="HTML",
                    reply_markup=get_rating_inline_keyboard()  # ИЗМЕНЕНИЕ: inline-кнопки
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления партнёра при /next: {e}")

        await state.set_state(ChatState.idle)

    # Очищаем возможные остатки в БД
    if await db.get_partner(message.from_user.id):
        await db.end_chat(message.from_user.id)

    # Начинаем поиск
    await db.add_to_search(message.from_user.id)
    await state.set_state(ChatState.searching)

    partner_data = await db.find_partner(message.from_user.id)

    if partner_data:
        partner_id, _ = partner_data

        # Получаем профиль партнёра один раз
        partner_profile = await db.get_user_profile(partner_id)

        # === Сообщение инициатору поиска ===
        initiator_has_premium = await db.has_active_premium(message.from_user.id)

        if initiator_has_premium and partner_profile:
            gender_text = "Парень" if partner_profile['gender'] == "male" else "Девушка"
            age_text = partner_profile['age'] if partner_profile['age'] else "Не указан"

            initiator_text = (
                f"<b>Собеседник найден!</b>\n\n"
                f"<i>Пол: {gender_text}</i>\n"
                f"<i>Возраст: {age_text}</i>\n\n"
                f"<i>/next — искать следующего</i>\n"
                f"<i>/stop — закончить диалог</i>"
            )
        else:
            initiator_text = (
                "<b>Собеседник найден!</b>\n\n"
                "<i>/next — искать следующего</i>\n"
                "<i>/stop — закончить диалог</i>"
            )

        await message.answer(
            initiator_text,
            parse_mode="HTML",
            reply_markup=None
        )
        await state.set_state(ChatState.chatting)

        # === Сообщение партнёру ===
        try:
            partner_has_premium = await db.has_active_premium(partner_id)

            if partner_has_premium and partner_profile:
                # Для партнёра показываем профиль ИНИЦИАТОРА
                initiator_profile = await db.get_user_profile(message.from_user.id)
                gender_text = "Парень" if initiator_profile['gender'] == "male" else "Девушка"
                age_text = initiator_profile['age'] if initiator_profile['age'] else "Не указан"

                partner_text = (
                    f"<b>Собеседник найден!</b>\n\n"
                    f"<i>Пол: {gender_text}</i>\n"
                    f"<i>Возраст: {age_text}</i>\n\n"
                    f"<i>/next — искать следующего</i>\n"
                    f"<i>/stop — закончить диалог</i>"
                )
            else:
                partner_text = (
                    "<b>Собеседник найден!</b>\n\n"
                    "<i>/next — искать следующего</i>\n"
                    "<i>/stop — закончить диалог</i>"
                )

            await message.bot.send_message(
                partner_id,
                partner_text,
                parse_mode="HTML",
                reply_markup=None
            )

            # Устанавливаем состояние партнёру
            partner_key = StorageKey(
                bot_id=message.bot.id,
                chat_id=partner_id,
                user_id=partner_id
            )
            partner_context = FSMContext(storage=state.storage, key=partner_key)
            await partner_context.set_state(ChatState.chatting)

            logger.info(f"Чат успешно начат: {message.from_user.id} ↔ {partner_id}")

        except Exception as e:
            logger.error(f"Ошибка подключения партнёра {partner_id}: {e}")
            await db.end_chat(message.from_user.id)
            await state.set_state(ChatState.idle)
            await message.answer(
                "<i>Ошибка соединения.</i> 😔\n"
                "<i>Поиск отменён. Попробуйте снова.</i>",
                parse_mode="HTML",
                reply_markup=get_main_keyboard()
            )
            return
    else:
        await message.answer(
            "🔍 <i>Ищем собеседника...</i>\n\n"
            "<i>/stop — остановить поиск</i>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )


@router.message(Command("stop"))
async def cmd_stop(message: Message, state: FSMContext):
    """Остановить поиск или диалог"""
    current_state = await state.get_state()

    if current_state == ChatState.searching:
        await db.remove_from_search(message.from_user.id)
        await state.set_state(ChatState.idle)
        await message.answer(
            "<i>Поиск остановлен</i> ⛔️\n\n"
            "<i>Отправьте /search, чтобы начать поиск</i>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
        return

    elif current_state == ChatState.chatting:
        partner_info = await db.end_chat(message.from_user.id)

        if partner_info:
            partner_id, _ = partner_info
            try:
                # Сбрасываем состояние партнёру
                partner_key = StorageKey(
                    bot_id=message.bot.id,
                    chat_id=partner_id,
                    user_id=partner_id
                )
                partner_context = FSMContext(storage=state.storage, key=partner_key)
                await partner_context.set_state(ChatState.idle)

                await message.bot.send_message(
                    partner_id,
                    "<i>Диалог остановлен</i> 😔\n\n"
                    "<i>Отправьте /search, чтобы начать поиск</i>",
                    parse_mode="HTML",
                    reply_markup=get_rating_inline_keyboard()  # ИЗМЕНЕНИЕ: inline-кнопки
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления партнёра при /stop: {e}")

        await state.set_state(ChatState.idle)
        await message.answer(
            "<i>Диалог остановлен</i> 😔\n\n"
            "<i>Отправьте /search, чтобы начать поиск</i>",
            parse_mode="HTML",
            reply_markup=get_rating_inline_keyboard()  # ИЗМЕНЕНИЕ: inline-кнопки
        )
        return

    else:
        await message.answer(
            "<i>У вас нет собеседника</i> 😐\n\n"
            "<i>Отправьте /search, чтобы начать</i>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )


@router.message(Command("leave"))
async def cmd_leave(message: Message, state: FSMContext):
    logger.info(f"/leave от {message.from_user.id}")
    logger.info(f"Состояние FSM: {await state.get_state()}")

    # Проверяем наличие активной группы в БД
    group_members = await db.get_group_members(message.from_user.id)
    logger.info(f"get_group_members вернул: {group_members}")

    group_id = await db.get_group_id(message.from_user.id)
    logger.info(f"get_group_id вернул: {group_id}")

    # Проверяем активного партнера для 1-на-1 чата
    partner = await db.get_partner(message.from_user.id)
    logger.info(f"get_partner вернул: {partner}")

    current_state = await state.get_state()

    if current_state in [ChatState.searching.state, GroupSearchState.searching.state]:
        await db.remove_from_search(message.from_user.id)
        await db.remove_from_group_search(message.from_user.id)
        await state.clear()
        await message.answer(
            "🔍 Поиск остановлен.\n\n"
            "Вы вернулись в главное меню.",
            reply_markup=get_main_keyboard()
        )
        return

    # Обработка группового чата
    if group_members and len(group_members) > 1:
        logger.info(f"Обработка группового чата для {message.from_user.id}")
        leaver_id = message.from_user.id
        remaining_members = [m for m in group_members if m != leaver_id]
        remaining_count = len(remaining_members)

        logger.info(f"Оставшиеся участники: {remaining_members} (количество: {remaining_count})")

        # Уведомляем остальных участников
        if remaining_count == 1:
            last_member = remaining_members[0]
            try:
                await message.bot.send_message(
                    last_member,
                    "👥 Групповой чат завершен\n\n"
                    "Все участники покинули чат",
                    reply_markup=get_main_keyboard()
                )
                # Сбрасываем состояние для последнего участника
                key = StorageKey(bot_id=message.bot.id, chat_id=last_member, user_id=last_member)
                member_state = FSMContext(storage=state.storage, key=key)
                await member_state.set_state(ChatState.idle)
                logger.info(f"Уведомление отправлено последнему участнику {last_member}")
            except Exception as e:
                logger.error(f"Ошибка уведомления последнего участника {last_member}: {e}")
        elif remaining_count > 1:
            text = f"👤 Участник покинул чат\n\nВ групповом чате осталось {remaining_count} участников"
            for member in remaining_members:
                try:
                    await message.bot.send_message(member, text)
                except Exception as e:
                    logger.error(f"Ошибка уведомления участника {member}: {e}")

        # Удаляем пользователя из группы
        async with db.get_connection() as conn:
            await conn.execute("""
                DELETE FROM group_chat_members 
                WHERE telegram_id = $1
            """, leaver_id)
            logger.info(f"Пользователь {leaver_id} удален из group_chat_members")

            # Если группа почти пуста, полностью очищаем ее
            if remaining_count <= 1:
                await conn.execute("""
                    DELETE FROM group_chat_members 
                    WHERE group_id = $1
                """, group_id)
                await conn.execute("""
                    UPDATE group_chats 
                    SET is_active = FALSE, ended_at = CURRENT_TIMESTAMP 
                    WHERE id = $1
                """, group_id)
                logger.info(f"Группа {group_id} полностью очищена и деактивирована")

        await message.answer(
            "👥 Вы покинули групповой чат\n\n"
            "Вернитесь в главное меню для нового поиска",
            reply_markup=get_main_keyboard()
        )
        await state.set_state(ChatState.idle)
        return

    # Обработка 1-на-1 чата
    elif partner:
        logger.info(f"Обработка 1-на-1 чата для {message.from_user.id}")
        await db.end_chat(message.from_user.id)
        await state.set_state(ChatState.idle)
        await message.answer(
            "Диалог завершён 😐\n\n"
            "Отправьте /search, чтобы начать новый поиск",
            reply_markup=get_main_keyboard()
        )
        return

    # Если ни группа, ни 1-на-1 чат не найдены
    else:
        await message.answer(
            "Вы не в чате и не в поиске.\n\n"
            "Используйте меню для начала.",
            reply_markup=get_main_keyboard()
        )


@router.message(F.text.in_(["👩 Найти девушку", "👨 Найти парня"]))
async def start_gender_search(message: Message, state: FSMContext):
    """Начать поиск по конкретному полу"""

    # Определяем целевой пол по тексту кнопки
    gender_map = {
        "👩 Найти девушку": "female",
        "👨 Найти парня": "male"
    }

    target_gender = gender_map[message.text]

    # Получаем профиль текущего пользователя
    user_profile = await db.get_user_profile(message.from_user.id)

    if not user_profile or not user_profile.get('gender'):
        await message.answer(
            "⚠️ <b>Пожалуйста, сначала укажите ваш пол!</b>\n\n"
            "Перейдите в ⚙️ Профиль → 🚻 Пол и выберите ваш пол.",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
        return

    current_user_gender = user_profile.get('gender')

    # Завершаем текущий диалог, если он есть
    current_state = await state.get_state()
    if current_state == ChatState.chatting:
        partner_info = await db.end_chat(message.from_user.id)
        if partner_info:
            partner_id, _ = partner_info
            try:
                partner_key = StorageKey(
                    bot_id=message.bot.id,
                    chat_id=partner_id,
                    user_id=partner_id
                )
                partner_context = FSMContext(storage=state.storage, key=partner_key)
                await partner_context.set_state(ChatState.idle)

                await message.bot.send_message(
                    partner_id,
                    "<i>Диалог остановлен</i> 😔\n\n"
                    "<i>Отправьте /search, чтобы начать поиск</i>",
                    parse_mode="HTML",
                    reply_markup=get_rating_inline_keyboard()
                )
            except Exception as e:
                logger.error(f"Ошибка уведомления партнёра: {e}")

    # Очищаем возможные остатки в БД
    if await db.get_partner(message.from_user.id):
        await db.end_chat(message.from_user.id)

    # Добавляем в общую очередь поиска с указанием целевого пола
    await db.add_to_search(message.from_user.id, target_gender=target_gender)

    # Сохраняем целевой пол в состоянии (если нужно для других целей)
    await state.update_data(target_gender=target_gender)

    # Начинаем поиск по полу
    partner_data = await db.find_partner_by_gender(message.from_user.id, target_gender)

    if partner_data:
        partner_id, _ = partner_data

        # Первый пользователь (инициатор)
        await message.answer(
            f"<b>Собеседник найден!</b>\n\n"
            f"<i>/next — искать следующего</i>\n"
            f"<i>/stop — закончить диалог</i>",
            parse_mode="HTML",
            reply_markup=None
        )
        await state.set_state(ChatState.chatting)

        # Второй пользователь
        try:
            await message.bot.send_message(
                partner_id,
                "<b>Собеседник найден!</b>\n\n"
                "<i>/next — искать следующего</i>\n"
                "<i>/stop — закончить диалог</i>",
                parse_mode="HTML",
                reply_markup=None
            )

            partner_key = StorageKey(
                bot_id=message.bot.id,
                chat_id=partner_id,
                user_id=partner_id
            )
            partner_context = FSMContext(storage=state.storage, key=partner_key)
            await partner_context.set_state(ChatState.chatting)

            logger.info(f"Гендерный чат начат: {message.from_user.id} ↔ {partner_id}")

        except Exception as e:
            logger.error(f"Ошибка подключения партнёра {partner_id}: {e}")
            await db.end_chat(message.from_user.id)
            await state.set_state(ChatState.idle)
            await message.answer(
                "<i>Ошибка соединения.</i> 😔\n"
                "<i>Поиск отменён. Попробуйте снова.</i>",
                parse_mode="HTML",
                reply_markup=get_main_keyboard()
            )
            return
    else:
        # Простое сообщение о поиске без лишних деталей
        gender_text = "девушку" if target_gender == 'female' else "парня"
        await message.answer(
            f"🔍 <i>Ищем {gender_text}...</i>\n\n"
            f"<i>/stop — остановить поиск</i>",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )


@router.message(Command("givepremium"))
async def cmd_give_premium(message: Message):
    """Команда /givepremium <user_id> — выдать премиум на 7 дней (только админ)"""
    if message.from_user.id != ADMIN_ID:
        await message.answer("Доступ запрещён.")
        return

    text = message.text.strip()
    parts = text.split()
    if len(parts) != 2:
        await message.answer(
            "Использование:\n"
            "/givepremium <user_id>\n\n"
            "Пример:\n"
            "/givepremium 7529123320"
        )
        return

    try:
        target_id = int(parts[1])
    except ValueError:
        await message.answer("Неверный ID пользователя.")
        return

    if target_id == ADMIN_ID:
        await message.answer("У вас уже есть всё.")
        return

    # Выдаём премиум на 7 дней
    success, _ = await db.buy_premium(target_id, stars_paid=99)

    if success:
        await message.answer(
            "Премиум активирован.\n\n"
            f"Пользователь: <code>{target_id}</code>\n"
            "Длительность: 7 дней.\n\n"
            "Поиск по полу теперь доступен.",
            parse_mode="HTML"
        )

        # Уведомление пользователю — в чистом стиле Apple
        try:
            await message.bot.send_message(
                target_id,
                "Премиум активирован.\n\n"
                "Полный доступ\n"
                "ко всем функциям\n"
                "на 7 дней.\n\n"
                "Поиск по полу открыт.",
                parse_mode="HTML"
            )
        except Exception:
            await message.answer("Премиум выдан, но пользователь не получит уведомление (не запускал бота).")
    else:
        await message.answer("Не удалось активировать премиум.")


@router.callback_query(F.data.startswith("rating_"))
async def handle_rating_callback(callback: CallbackQuery, state: FSMContext):
    """Обработка inline-кнопок оценки"""

    action = callback.data

    if action == "rating_like":
        # Сохраняем информацию о том, какой партнер оценивается
        await handle_rating_selection(callback, state, 1)

    elif action == "rating_dislike":
        # Сохраняем информацию о том, какой партнер оценивается
        await handle_rating_selection(callback, state, -1)

    elif action == "rating_complaint":
        # Показываем клавиатуру с категориями жалоб
        await callback.message.edit_reply_markup(
            reply_markup=get_complaint_categories_inline_keyboard()
        )
        await callback.answer("Выберите категорию жалобы")


async def handle_rating_selection(callback: CallbackQuery, state: FSMContext, rating_value: int):
    """Общая обработка оценки (лайк/дизлайк)"""
    # Получаем последнюю сессию для оценки
    user_data = await state.get_data()
    partner_id = user_data.get('rating_partner_id')
    session_id = user_data.get('rating_session_id')

    # Если нет сохраненных данных, пытаемся найти последнего партнера
    if not partner_id:
        partner_id, session_id = await find_last_partner(callback.from_user.id)

    if partner_id:
        await db.add_rating(callback.from_user.id, partner_id, rating_value, session_id)
        rating_text = "👍 Спасибо за оценку!" if rating_value == 1 else "👎 Спасибо за оценку!"
        await callback.answer(rating_text, show_alert=False)
    else:
        rating_text = "👍 Спасибо!" if rating_value == 1 else "👎 Спасибо!"
        await callback.answer(rating_text, show_alert=False)

    # Удаляем inline-кнопки после выбора (только для лайков/дизлайков)
    await callback.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("complaint_"))
async def handle_complaint_callback(callback: CallbackQuery, state: FSMContext):
    """Обработка категорий жалоб"""

    # Обработка кнопки "Назад"
    if callback.data == "complaint_back":
        await callback.message.edit_reply_markup(
            reply_markup=get_rating_inline_keyboard()
        )
        await callback.answer("Возврат к оценке", show_alert=False)
        return

    category_map = {
        "complaint_insults": "🚫 Оскорбления",
        "complaint_adult": "📵 Контент 18+",
        "complaint_fraud": "💳 Мошенничество",
        "complaint_spam": "📢 Спам",
        "complaint_impersonation": "👤 Выдача за другого",
        "complaint_other": "👥 Другое"
    }

    category = callback.data
    category_text = category_map.get(category, "Другое")

    # Получаем последнюю сессию для жалобы
    user_data = await state.get_data()
    partner_id = user_data.get('rating_partner_id')
    session_id = user_data.get('rating_session_id')

    if not partner_id:
        partner_id, session_id = await find_last_partner(callback.from_user.id)

    if partner_id:
        await db.add_complaint(
            callback.from_user.id,
            partner_id,
            f"Категория: {category_text}",
            category.replace("complaint_", ""),
            session_id
        )
        # ИЗМЕНЕНИЕ: show_alert=False для такого же вида как "Спасибо за оценку"
        await callback.answer(f"Жалоба отправлена: {category_text}", show_alert=False)
    else:
        await callback.answer("Спасибо за обратную связь!", show_alert=False)

    # Удаляем inline-кнопки после выбора категории жалобы
    await callback.message.edit_reply_markup(reply_markup=None)


async def find_last_partner(telegram_id: int):
    """Найти последнего партнера пользователя"""
    async with db.get_connection() as conn:
        user = await conn.fetchrow(
            "SELECT id FROM users WHERE telegram_id = $1",
            telegram_id
        )

        if user:
            last_session = await conn.fetchrow("""
                SELECT cs.id, 
                       CASE 
                           WHEN cs.user1_id = $1 THEN u2.telegram_id
                           ELSE u1.telegram_id
                       END as partner_id
                FROM chat_sessions cs
                JOIN users u1 ON cs.user1_id = u1.id
                JOIN users u2 ON cs.user2_id = u2.id
                WHERE (cs.user1_id = $1 OR cs.user2_id = $1)
                AND cs.ended_at IS NOT NULL
                ORDER BY cs.ended_at DESC
                LIMIT 1
            """, user['id'])

            if last_session:
                return last_session['partner_id'], last_session['id']

    return None, None


# ========== ПЕРЕСЫЛКА СООБЩЕНИЙ В ГРУППОВОМ ЧАТЕ ==========
@router.message(ChatState.chatting)
async def group_chat_forward(message: Message, state: FSMContext):
    """Пересылает все сообщения в групповом чате всем участникам группы (кроме отправителя)"""
    # Проверяем, является ли пользователь участником группового чата
    group_members = await db.get_group_members(message.from_user.id)

    if not group_members or len(group_members) <= 1:
        # Не в групповом чате или один — ничего не делаем (сообщения обрабатываются другими хендлерами)
        return

    sender_id = message.from_user.id
    recipients = [m for m in group_members if m != sender_id]  # все кроме отправителя

    if not recipients:
        return

    # Определяем тип сообщения и пересылаем соответствующим способом
    try:
        if message.text:
            # Текстовое сообщение
            for recipient in recipients:
                await message.bot.send_message(recipient, message.text)

        elif message.photo:
            # Фото (берём лучшее качество)
            photo = message.photo[-1]
            for recipient in recipients:
                await message.bot.send_photo(recipient, photo.file_id, caption=message.caption)

        elif message.video:
            for recipient in recipients:
                await message.bot.send_video(
                    recipient,
                    message.video.file_id,
                    caption=message.caption,
                    duration=message.video.duration,
                    width=message.video.width,
                    height=message.video.height
                )

        elif message.video_note:
            for recipient in recipients:
                await message.bot.send_video_note(recipient, message.video_note.file_id)

        elif message.voice:
            for recipient in recipients:
                await message.bot.send_voice(recipient, message.voice.file_id, caption=message.caption)

        elif message.audio:
            for recipient in recipients:
                await message.bot.send_audio(
                    recipient,
                    message.audio.file_id,
                    caption=message.caption,
                    duration=message.audio.duration,
                    performer=message.audio.performer,
                    title=message.audio.title
                )

        elif message.document:
            for recipient in recipients:
                await message.bot.send_document(recipient, message.document.file_id, caption=message.caption)

        elif message.sticker:
            for recipient in recipients:
                await message.bot.send_sticker(recipient, message.sticker.file_id)

        elif message.animation:
            for recipient in recipients:
                await message.bot.send_animation(recipient, message.animation.file_id, caption=message.caption)

        elif message.location:
            for recipient in recipients:
                await message.bot.send_location(recipient, message.location.latitude, message.location.longitude)

        elif message.contact:
            for recipient in recipients:
                await message.bot.send_contact(
                    recipient,
                    phone_number=message.contact.phone_number,
                    first_name=message.contact.first_name,
                    last_name=message.contact.last_name
                )

        # Добавь другие типы по необходимости (poll, dice и т.д.)

    except Exception as e:
        logger.error(f"Ошибка пересылки сообщения в групповом чате от {sender_id}: {e}")
        # Если ошибка (например, пользователь заблокировал бота) — можно завершить чат для всех, но пока просто логируем


# ========== ПЕРЕСЫЛКА СООБЩЕНИЙ ==========
@router.message(F.text, ChatState.chatting)
async def forward_message(message: Message, state: FSMContext):
    """Пересылать текстовые сообщения между собеседниками"""
    # Проверяем, действительно ли пользователь в состоянии chatting
    current_state = await state.get_state()

    # Если состояние не chatting, но есть активный партнер в базе,
    # восстанавливаем состояние
    partner_id = await db.get_partner(message.from_user.id)

    if not partner_id:
        # Нет активного партнера, возвращаем в главное меню
        await state.set_state(ChatState.idle)
        await message.answer(
            "Диалог завершен 😐\n\nОтправьте /search, чтобы начать новый поиск",
            reply_markup=get_main_keyboard()
        )
        return

    # Если состояние не chatting, но партнер есть, восстанавливаем состояние
    if current_state != ChatState.chatting:
        await state.set_state(ChatState.chatting)

    # Отправляем сообщение партнеру
    try:
        await message.bot.send_message(partner_id, message.text)

        # Обновляем счетчик сообщений в сессии
        session_id = await db.get_session(message.from_user.id)
        if session_id:
            async with db.get_connection() as conn:
                await conn.execute("""
                    UPDATE chat_sessions 
                    SET message_count = message_count + 1 
                    WHERE id = $1
                """, session_id)

    except Exception as e:
        logger.error(f"Ошибка пересылки сообщения: {e}")

        # Если не удалось отправить, возможно, партнер отключился
        await db.end_chat(message.from_user.id)
        await state.set_state(ChatState.idle)

        await message.answer(
            "❌ Не удалось отправить сообщение. Собеседник отключился.\n\n"
            "Отправьте /search, чтобы начать новый поиск",
            reply_markup=get_main_keyboard()
        )


# ========== ПЕРЕСЫЛКА МЕДИА ==========
@router.message(ChatState.chatting)
async def forward_all_media(message: Message, state: FSMContext):
    """Пересылать все виды медиа и другие типы сообщений"""
    # Пропускаем текстовые сообщения (они обрабатываются выше)
    if message.text:
        return

    partner_id = await db.get_partner(message.from_user.id)

    if not partner_id:
        await state.set_state(ChatState.idle)
        await message.answer(
            "Диалог завершен 😐\n\nОтправьте /search, чтобы начать новый поиск",
            reply_markup=get_main_keyboard()
        )
        return

    try:
        # Пересылаем в зависимости от типа контента

        # ФОТО (может быть несколько фото в альбоме)
        if message.photo:
            if len(message.photo) > 0:
                # Берем фото самого высокого качества (последний в массиве)
                photo = message.photo[-1]
                await message.bot.send_photo(
                    chat_id=partner_id,
                    photo=photo.file_id,
                    caption=message.caption
                )

        # ВИДЕО
        elif message.video:
            await message.bot.send_video(
                chat_id=partner_id,
                video=message.video.file_id,
                caption=message.caption,
                duration=message.video.duration,
                width=message.video.width,
                height=message.video.height
            )

        # ВИДЕОЗАПИСЬ (Video Note - кружочки)
        elif message.video_note:
            await message.bot.send_video_note(
                chat_id=partner_id,
                video_note=message.video_note.file_id,
                duration=message.video_note.duration,
                length=message.video_note.length
            )

        # СТИКЕРЫ
        elif message.sticker:
            await message.bot.send_sticker(
                chat_id=partner_id,
                sticker=message.sticker.file_id
            )

        # ГОЛОСОВЫЕ СООБЩЕНИЯ
        elif message.voice:
            await message.bot.send_voice(
                chat_id=partner_id,
                voice=message.voice.file_id,
                caption=message.caption,
                duration=message.voice.duration
            )

        # АУДИО ФАЙЛЫ
        elif message.audio:
            await message.bot.send_audio(
                chat_id=partner_id,
                audio=message.audio.file_id,
                caption=message.caption,
                duration=message.audio.duration,
                performer=message.audio.performer,
                title=message.audio.title
            )

        # ДОКУМЕНТЫ
        elif message.document:
            await message.bot.send_document(
                chat_id=partner_id,
                document=message.document.file_id,
                caption=message.caption
            )

        # АНИМАЦИИ (GIF)
        elif message.animation:
            await message.bot.send_animation(
                chat_id=partner_id,
                animation=message.animation.file_id,
                caption=message.caption,
                duration=message.animation.duration,
                width=message.animation.width,
                height=message.animation.height
            )

        # КОНТАКТЫ
        elif message.contact:
            await message.bot.send_contact(
                chat_id=partner_id,
                phone_number=message.contact.phone_number,
                first_name=message.contact.first_name,
                last_name=message.contact.last_name
            )

        # ГЕОЛОКАЦИЯ
        elif message.location:
            await message.bot.send_location(
                chat_id=partner_id,
                latitude=message.location.latitude,
                longitude=message.location.longitude
            )

        # ОПОВЕЩЕНИЕ О НАБОРЕ ТЕКСТА (просто игнорируем)
        elif message.chat_shared or message.users_shared or message.write_access_allowed:
            # Эти типы сообщений не пересылаем
            return

        # Если тип сообщения не поддерживается
        else:
            await message.answer(
                "❌ Этот тип сообщения не поддерживается для пересылки",
                reply_markup=get_main_keyboard()
            )
            return

        # Обновляем счетчик сообщений в сессии
        session_id = await db.get_session(message.from_user.id)
        if session_id:
            async with db.get_connection() as conn:
                await conn.execute("""
                    UPDATE chat_sessions 
                    SET message_count = message_count + 1 
                    WHERE id = $1
                """, session_id)

        logger.info(f"Медиа сообщение переслано от {message.from_user.id} к {partner_id}")

    except Exception as e:
        logger.error(f"Ошибка пересылки медиа: {e}")

        # Проверяем конкретные ошибки
        error_message = str(e).lower()

        if "forbidden" in error_message or "blocked" in error_message:
            # Пользователь заблокировал бота
            await db.end_chat(message.from_user.id)
            await state.set_state(ChatState.idle)
            await message.answer(
                "❌ Собеседник заблокировал бота. Диалог завершен.\n\n"
                "Отправьте /search, чтобы начать новый поиск",
                reply_markup=get_main_keyboard()
            )
        elif "file is too big" in error_message:
            # Файл слишком большой
            await message.answer(
                "❌ Файл слишком большой для отправки. Максимальный размер файла - 50 МБ",
                reply_markup=get_main_keyboard()
            )
        else:
            # Общая ошибка
            await message.answer(
                "❌ Не удалось отправить это сообщение. Попробуйте другой формат или текст.",
                reply_markup=get_main_keyboard()
            )


# ========== ПРОВЕРКА АКТИВНОГО ЧАТА ПРИ ЛЮБОМ СООБЩЕНИИ ==========
@router.message()
async def check_active_chat(message: Message, state: FSMContext):
    """Проверяем активный чат при любом сообщении"""
    # Пропускаем команды
    if message.text and message.text.startswith('/'):
        return

    # Проверяем состояние
    current_state = await state.get_state()

    # Если состояние chatting, но партнера нет - исправляем
    if current_state == ChatState.chatting:
        partner_id = await db.get_partner(message.from_user.id)
        if not partner_id:
            await state.set_state(ChatState.idle)
            await message.answer(
                "Диалог завершен 😐\n\nОтправьте /search, чтобы начать новый поиск",
                reply_markup=get_main_keyboard()
            )
    # Если состояние не установлено или idle, но есть активный партнер
    elif current_state in [None, ChatState.idle.state]:
        partner_id = await db.get_partner(message.from_user.id)
        if partner_id:
            # Тихо восстанавливаем состояние
            await state.set_state(ChatState.chatting)
            # И ПЕРЕСЫЛАЕМ ПЕРВОЕ СООБЩЕНИЕ!
            try:
                await message.bot.send_message(partner_id, message.text)
            except Exception as e:
                logger.error(f"Ошибка пересылки сообщения при восстановлении: {e}")
                await db.end_chat(message.from_user.id)
                await state.set_state(ChatState.idle)
                await message.answer(
                    "❌ Не удалось отправить сообщение. Собеседник отключился.\n\n"
                    "Отправьте /search, чтобы начать новый поиск",
                    reply_markup=get_main_keyboard()
                )


@router.pre_checkout_query()
async def pre_checkout_query(pre_checkout_q: types.PreCheckoutQuery):
    """Подтверждение платежа"""
    await pre_checkout_q.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message):
    """Успешная оплата премиум — красивое подтверждение"""
    payload = message.successful_payment.invoice_payload
    stars_paid = int(payload)  # сумма из payload

    success, text = await db.buy_premium(message.from_user.id, stars_paid)

    if success:
        # Определяем длительность для красивого сообщения
        if stars_paid == 199:
            duration = "1 месяц"
        elif stars_paid == 99:
            duration = "7 дней"
        else:
            duration = "1 день"

        await message.answer(
            f"✨ <b>Премиум активирован</b>\n\n"
            f"Полный доступ ко всем функциям\n"
            f"на {duration}.\n\n"
            f"🍓 Поиск по полу теперь доступен.\n"
            f"Наслаждайтесь общением ❤️",
            parse_mode="HTML",
            reply_markup=get_main_keyboard()
        )
    else:
        await message.answer(
            f"❌ Произошла ошибка при активации.\n"
            f"Попробуйте позже или напишите в поддержку.",
            reply_markup=get_main_keyboard()
        )


# ========== ЗАПУСК БОТА ==========
async def main():
    """Главная функция запуска бота"""
    # Инициализация базы данных
    logger.info("🔄 Инициализация базы данных...")
    db_success = await db.init()
    if not db_success:
        logger.error("❌ Не удалось подключиться к базе данных")
        return

    # Создание бота и диспетчера
    bot = Bot(token=TOKEN)
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # Регистрация роутера
    dp.include_router(router)

    logger.info("✅ Бот запускается...")

    # Запуск бота
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("❌ Бот остановлен")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}")
