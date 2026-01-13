import logging
import os
import asyncio
import aiosqlite
import sqlite3
from aiogram import Bot, Dispatcher, types, F, html
from aiogram.enums import ParseMode, ContentType
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup,
    KeyboardButton, ReplyKeyboardRemove, ForceReply, InputMediaPhoto
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from datetime import datetime, timedelta
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from typing import Optional, Tuple, Dict, Any, List, Set
from aiogram.dispatcher.middlewares.base import BaseMiddleware
import re
import base64
from io import BytesIO
import hashlib
import hmac
import secrets

from aiohttp import web
import sys
import time
from collections import defaultdict
from contextlib import suppress

# --- Koyeb Specific: Prevent Multiple Instances with PID check ---
if os.environ.get('BOT_IS_RUNNING'):
    # Check if it's the same process ID to avoid false positives
    current_pid = str(os.getpid())
    stored_pid = os.environ.get('BOT_PID')
    if stored_pid != current_pid:
        logging.critical(f"Bot restart detected. Old PID: {stored_pid}, New PID: {current_pid}")
    else:
        logging.critical("Bot is already running. Exiting...")
        sys.exit(1)

os.environ['BOT_IS_RUNNING'] = '1'
os.environ['BOT_PID'] = str(os.getpid())

# --- Constants ---
CATEGORIES = [
    "Relationship", "Family", "School", "Friendship",
    "Religion", "Mental", "Addiction", "Harassment", "Crush", "Health", "Trauma", "Sexual Assault",
    "Other"
]
POINTS_PER_CONFESSION = 1
POINTS_PER_LIKE_RECEIVED = int(os.getenv("POINTS_PER_LIKE", "1"))
POINTS_PER_DISLIKE_RECEIVED = int(os.getenv("POINTS_PER_DISLIKE", "-1"))
MAX_CATEGORIES = 3  # Maximum categories allowed per confession
MAX_PHOTO_SIZE_MB = 5  # Maximum photo size in MB

# Add rate limiting
RATE_LIMIT_SECONDS = 30  # Minimum time between confessions
user_last_action = defaultdict(float)

# --- Koyeb Specific: Load environment variables ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")  # Changed from BOT_TOKENS to BOT_TOKEN for Koyeb convention
ADMIN_IDS_STR = os.getenv("ADMIN_IDS")  # Comma-separated admin IDs
CHANNEL_ID = os.getenv("CHANNEL_ID")
PAGE_SIZE = int(os.getenv("PAGE_SIZE", "15"))  # Number of items per page for pagination

# --- Koyeb Specific: Database Configuration ---
# Koyeb provides DATABASE_URL for PostgreSQL, but we'll use SQLite for simplicity
# For production on Koyeb, consider using PostgreSQL
# --- Database Configuration ---
DATABASE_URL = os.getenv("DATABASE_URL")

# Use PostgreSQL if DATABASE_URL is provided (Koyeb)
# Otherwise use SQLite (local development)
if DATABASE_URL and ('postgres://' in DATABASE_URL or 'postgresql://' in DATABASE_URL):
    USE_POSTGRESQL = True
    logging.info("Using PostgreSQL database (Koyeb)")
    
    # Import asyncpg for PostgreSQL
    import asyncpg
    
    # Global connection pool
    db_pool = None
    
    async def create_db_pool():
        """Create PostgreSQL connection pool"""
        global db_pool
        try:
            # Remove 'postgresql://' if present, asyncpg needs 'postgres://'
            connection_string = DATABASE_URL.replace('postgresql://', 'postgres://')
            
            db_pool = await asyncpg.create_pool(
                connection_string,
                min_size=1,
                max_size=10,
                command_timeout=60
            )
            logging.info("✅ PostgreSQL connection pool created")
            return db_pool
        except Exception as e:
            logging.error(f"Failed to create PostgreSQL connection: {e}")
            raise
    
else:
    # Fallback to SQLite for local development
    USE_POSTGRESQL = False
    logging.info("Using SQLite database (local development)")
    
    DATABASE_PATH = "confessions.db"
    db = None
    
    async def create_db_pool():
        """Create SQLite connection"""
        global db
        db = await aiosqlite.connect(DATABASE_PATH)
        db.row_factory = aiosqlite.Row
        logging.info(f"SQLite database connection created at {DATABASE_PATH}")
        return db

# Helper functions that work with both databases
async def execute_query(query: str, *params):
    """Execute query and return results"""
    if USE_POSTGRESQL:
        async with db_pool.acquire() as conn:
            # Convert SQLite ? placeholders to PostgreSQL $1, $2, etc.
            converted_query = query
            param_count = len(params)
            for i in range(param_count, 0, -1):
                converted_query = converted_query.replace('?', f'${i}', 1)
            return await conn.fetch(converted_query, *params)
    else:
        async with db.execute(query, params) as cursor:
            return await cursor.fetchall()

async def fetch_one(query: str, *params):
    """Fetch single row"""
    if USE_POSTGRESQL:
        async with db_pool.acquire() as conn:
            # Convert placeholders
            converted_query = query
            param_count = len(params)
            for i in range(param_count, 0, -1):
                converted_query = converted_query.replace('?', f'${i}', 1)
            row = await conn.fetchrow(converted_query, *params)
            return row
    else:
        async with db.execute(query, params) as cursor:
            return await cursor.fetchone()

async def execute_update(query: str, *params):
    """Execute INSERT/UPDATE/DELETE query"""
    if USE_POSTGRESQL:
        async with db_pool.acquire() as conn:
            # Convert placeholders
            converted_query = query
            param_count = len(params)
            for i in range(param_count, 0, -1):
                converted_query = converted_query.replace('?', f'${i}', 1)
            result = await conn.execute(converted_query, *params)
            return result
    else:
        async with db.execute(query, params) as cursor:
            await db.commit()
            return cursor

HTTP_PORT = int(os.getenv("PORT", "8080"))  # Koyeb uses PORT environment variable

# Parse admin IDs
ADMIN_IDS: Set[int] = set()
if ADMIN_IDS_STR:
    for admin_id_str in ADMIN_IDS_STR.split(','):
        try:
            ADMIN_IDS.add(int(admin_id_str.strip()))
        except ValueError:
            logging.error(f"Invalid admin ID in ADMIN_IDS: {admin_id_str}")

if not ADMIN_IDS:
    logging.warning("No admin IDs configured. Add ADMIN_IDS to .env file")

# Validate essential environment variables
if not BOT_TOKEN:
    logging.critical("BOT_TOKEN environment variable is required. Please set it in Koyeb environment variables.")
    sys.exit(1)

if not CHANNEL_ID:
    logging.warning("CHANNEL_ID not set. Channel posting will be disabled.")
    CHANNEL_ID = None

# --- Koyeb Specific: Setup logging with proper format ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)  # Log to stdout for Koyeb
    ]
)
logger = logging.getLogger(__name__)

# Bot and Dispatcher
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher(storage=MemoryStorage())

# Bot info
bot_info = None

# --- FSM States ---
class ConfessionForm(StatesGroup):
    selecting_categories = State()
    waiting_for_text = State()
    waiting_for_photo = State()

class CommentForm(StatesGroup):
    waiting_for_comment = State()
    waiting_for_reply = State()

class ContactAdminForm(StatesGroup):
    waiting_for_message = State()

class AdminActions(StatesGroup):
    waiting_for_rejection_reason = State()

class UserProfileForm(StatesGroup):
    waiting_for_profile_name = State()

class ChatForm(StatesGroup):
    chatting = State()

class ContactRequestForm(StatesGroup):
    waiting_for_contact_message = State()

class BlockForm(StatesGroup):
    waiting_for_block_duration = State()
    waiting_for_block_reason = State()

# --- Database (SQLite Version) ---
db = None

# --- Koyeb Specific: Create database connection function ---
async def create_db_pool():
    try:
        # Connect to SQLite database
        db = await aiosqlite.connect(DATABASE_PATH)
        db.row_factory = aiosqlite.Row
        logger.info(f"SQLite database connection created successfully at {DATABASE_PATH}")
        return db
    except Exception as e:
        logger.error(f"Failed to create database connection: {e}")
        raise

# --- Helper Functions for Profile Links ---
def encode_user_id(user_id: int) -> str:
    """Encode user ID to a short, non-reversible string"""
    # Create a simple hash-based encoding
    salt = "profile_salt_v1"  # Keep this constant
    data = f"{user_id}{salt}".encode()
    # Use SHA256 and take first 12 characters
    hash_obj = hashlib.sha256(data).hexdigest()[:12]
    return hash_obj

async def get_encoded_profile_link(user_id: int) -> str:
    """Get encoded profile link for a user"""
    encoded = encode_user_id(user_id)
    logger.info(f"Encoding user {user_id} to {encoded}")
    
    if not bot_info or not hasattr(bot_info, 'username'):
        logger.warning(f"bot_info not available when encoding profile link for user {user_id}")
        # Fallback
        bot_username = BOT_TOKEN.split(':')[0] if BOT_TOKEN else "unknown_bot"
        return f"https://t.me/{bot_username}?start=profile_{encoded}"
    
    link = f"https://t.me/{bot_info.username}?start=profile_{encoded}"
    logger.info(f"Generated profile link for user {user_id}: {link}")
    return link

async def get_user_id_from_encoded(encoded_id: str) -> Optional[int]:
    """Get user ID from encoded string by checking database"""
    if not db:
        return None
    
    try:
        # Get all user IDs and find one that matches the encoding
        async with db.execute("SELECT user_id FROM user_status") as cursor:
            users = await cursor.fetchall()
        
        for user_row in users:
            user_id = user_row['user_id']
            if encode_user_id(user_id) == encoded_id:
                return user_id
    except Exception as e:
        logger.error(f"Error decoding user ID: {e}")
    
    return None

async def setup():
    global db, bot_info
    db = await create_db_pool()
    bot_info = await bot.get_me()
    logger.info(f"Bot started: @{bot_info.username}")

    # --- Confessions Table Schema (SQLite) ---
    await db.execute("""
        CREATE TABLE IF NOT EXISTS confessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            message_id INTEGER,
            photo_file_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            rejection_reason TEXT,
            categories TEXT
        );
    """)
    logger.info("Checked/Created 'confessions' table.")

    # --- Comments Table Schema (SQLite) ---
    await db.execute("""
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            confession_id INTEGER,
            user_id INTEGER NOT NULL,
            text TEXT,
            sticker_file_id TEXT,
            animation_file_id TEXT,
            parent_comment_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (confession_id) REFERENCES confessions(id) ON DELETE CASCADE,
            FOREIGN KEY (parent_comment_id) REFERENCES comments(id) ON DELETE SET NULL
        );
    """)
    logger.info("Checked/Created 'comments' table.")

    # --- Reactions Table (SQLite) ---
    await db.execute("""
         CREATE TABLE IF NOT EXISTS reactions (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             comment_id INTEGER,
             user_id INTEGER NOT NULL,
             reaction_type TEXT NOT NULL,
             created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
             UNIQUE(comment_id, user_id),
             FOREIGN KEY (comment_id) REFERENCES comments(id) ON DELETE CASCADE
         );
    """)
    logger.info("Checked/Created 'reactions' table.")

    # --- Contact Requests Table (SQLite) ---
    await db.execute("""
        CREATE TABLE IF NOT EXISTS contact_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            confession_id INTEGER NOT NULL,
            comment_id INTEGER NOT NULL,
            requester_user_id INTEGER NOT NULL,
            requested_user_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (comment_id, requester_user_id),
            FOREIGN KEY (confession_id) REFERENCES confessions(id) ON DELETE CASCADE,
            FOREIGN KEY (comment_id) REFERENCES comments(id) ON DELETE CASCADE
        );
    """)
    logger.info("Checked/Created 'contact_requests' table.")

    # --- User Points Table (SQLite) ---
    await db.execute("""
        CREATE TABLE IF NOT EXISTS user_points (
            user_id INTEGER PRIMARY KEY,
            points INTEGER NOT NULL DEFAULT 0
        );
    """)
    logger.info("Checked/Created 'user_points' table.")

    # --- Reports Table (SQLite) ---
    await db.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            comment_id INTEGER NOT NULL,
            reporter_user_id INTEGER NOT NULL,
            reported_user_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (comment_id, reporter_user_id),
            FOREIGN KEY (comment_id) REFERENCES comments(id) ON DELETE CASCADE
        );
    """)
    logger.info("Checked/Created 'reports' table.")

    # --- Deletion Requests Table (SQLite) ---
    await db.execute("""
        CREATE TABLE IF NOT EXISTS deletion_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            confession_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TIMESTAMP,
            UNIQUE (confession_id, user_id),
            FOREIGN KEY (confession_id) REFERENCES confessions(id) ON DELETE CASCADE
        );
    """)
    logger.info("Checked/Created 'deletion_requests' table.")

    # --- User Status Table (SQLite) ---
    await db.execute("""
        CREATE TABLE IF NOT EXISTS user_status (
            user_id INTEGER PRIMARY KEY,
            has_accepted_rules INTEGER NOT NULL DEFAULT 0,
            is_blocked INTEGER NOT NULL DEFAULT 0,
            blocked_until TIMESTAMP,
            block_reason TEXT,
            profile_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    logger.info("Checked/Created 'user_status' table.")

    # --- Active Chats Table (SQLite) ---
    await db.execute("""
        CREATE TABLE IF NOT EXISTS active_chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user1_id INTEGER NOT NULL,
            user2_id INTEGER NOT NULL,
            started_by INTEGER NOT NULL,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_message_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active INTEGER DEFAULT 1,
            FOREIGN KEY (user1_id) REFERENCES user_status(user_id),
            FOREIGN KEY (user2_id) REFERENCES user_status(user_id)
        );
    """)
    logger.info("Checked/Created 'active_chats' table.")

    # --- Chat Messages Table (SQLite) ---
    await db.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            sender_id INTEGER NOT NULL,
            message_text TEXT,
            sticker_file_id TEXT,
            animation_file_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (chat_id) REFERENCES active_chats(id) ON DELETE CASCADE,
            FOREIGN KEY (sender_id) REFERENCES user_status(user_id)
        );
    """)
    logger.info("Checked/Created 'chat_messages' table.")

    await db.commit()
    logger.info("Database tables setup complete.")

# --- Helper Functions ---
async def is_admin(user_id: int) -> bool:
    """Check if user is admin"""
    return user_id in ADMIN_IDS

async def get_profile_name(user_id: int) -> str:
    """Get user's profile name or return Anonymous"""
    conn = db
    async with conn.execute("SELECT profile_name FROM user_status WHERE user_id = ?", (user_id,)) as cursor:
        row = await cursor.fetchone()
        if row and row['profile_name']:
            return row['profile_name']
    return "Anonymous"

async def update_profile_name(user_id: int, profile_name: str):
    """Update user's profile name"""
    conn = db
    await conn.execute("""
        INSERT OR REPLACE INTO user_status (user_id, profile_name, last_seen) 
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET 
        profile_name = excluded.profile_name,
        last_seen = CURRENT_TIMESTAMP
    """, (user_id, profile_name))
    await conn.commit()

def create_category_keyboard(selected_categories: List[str] = None):
    if selected_categories is None:
        selected_categories = []
    builder = InlineKeyboardBuilder()
    for category in CATEGORIES:
        prefix = "✅ " if category in selected_categories else ""
        builder.button(text=f"{prefix}{category}", callback_data=f"category_{category}")
    builder.adjust(2)
    if 1 <= len(selected_categories) <= MAX_CATEGORIES:
         builder.row(InlineKeyboardButton(text=f"➡️ Done Selecting ({len(selected_categories)}/{MAX_CATEGORIES})", callback_data="category_done"))
    elif len(selected_categories) > MAX_CATEGORIES:
         builder.row(InlineKeyboardButton(text=f"⚠️ Too Many ({len(selected_categories)}/{MAX_CATEGORIES}) - Click to Confirm", callback_data="category_done"))
    builder.row(InlineKeyboardButton(text="❌ Cancel Selection", callback_data="category_cancel"))
    return builder.as_markup()

async def get_comment_reactions(comment_id: int) -> Tuple[int, int]:
    likes, dislikes = 0, 0
    conn = db
    async with conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN reaction_type = 'like' THEN 1 ELSE 0 END), 0) AS likes, COALESCE(SUM(CASE WHEN reaction_type = 'dislike' THEN 1 ELSE 0 END), 0) AS dislikes FROM reactions WHERE comment_id = ?", (comment_id,)) as cursor:
        row = await cursor.fetchone()
        if row:
            likes, dislikes = row['likes'], row['dislikes']
    return likes, dislikes

async def get_user_points(user_id: int) -> int:
    conn = db
    async with conn.execute("SELECT points FROM user_points WHERE user_id = ?", (user_id,)) as cursor:
        row = await cursor.fetchone()
        return row['points'] if row else 0

async def update_user_points(conn, user_id: int, delta: int):
    if delta == 0: return
    await conn.execute("INSERT OR REPLACE INTO user_points (user_id, points) VALUES (?, COALESCE((SELECT points FROM user_points WHERE user_id = ?), 0) + ?)", 
                      (user_id, user_id, delta))
    logger.debug(f"Updated points for user {user_id} by {delta}")

async def build_comment_keyboard(comment_id: int, commenter_user_id: int, viewer_user_id: int, confession_owner_id: int, is_admin: bool = False):
    likes, dislikes = await get_comment_reactions(comment_id)
    builder = InlineKeyboardBuilder()
    
    # Disable like/dislike for user's own comments
    if commenter_user_id != viewer_user_id:
        builder.button(text=f"👍 {likes}", callback_data=f"react_like_{comment_id}")
        builder.button(text=f"👎 {dislikes}", callback_data=f"react_dislike_{comment_id}")
    else:
        # Show disabled buttons for own comments
        builder.button(text=f"👍 {likes}", callback_data="noop")
        builder.button(text=f"👎 {dislikes}", callback_data="noop")
    
    builder.button(text="↪️ Reply", callback_data=f"reply_{comment_id}")
    
    builder.adjust(3)  # Only 3 buttons per row now
    
    return builder.as_markup()

async def safe_send_message(user_id: int, text: str, **kwargs) -> Optional[types.Message]:
    try:
        sent_message = await bot.send_message(user_id, text, **kwargs)
        return sent_message
    except (TelegramForbiddenError, TelegramBadRequest) as e:
        if "bot was blocked" in str(e) or "user is deactivated" in str(e) or "chat not found" in str(e):
            logger.warning(f"Could not send message to user {user_id}: Blocked/deactivated. {e}")
        else:
            logger.warning(f"Telegram API error sending to {user_id}: {e}")
    except TelegramRetryAfter as e:
        logger.warning(f"Flood control for {user_id}. Retrying after {e.retry_after}s")
        await asyncio.sleep(e.retry_after)
        return await safe_send_message(user_id, text, **kwargs)
    except Exception as e:
        logger.error(f"Unexpected error sending message to user {user_id}: {e}", exc_info=True)
    return None

async def update_channel_post_button(confession_id: int):
    global bot_info
    await asyncio.sleep(0.1)
    if not bot_info or not CHANNEL_ID:
        logger.error(f"No bot info or CHANNEL_ID for {confession_id} button update.")
        return
    
    conn = db
    async with conn.execute("SELECT message_id FROM confessions WHERE id = ? AND status = 'approved'", (confession_id,)) as cursor:
        conf_data = await cursor.fetchone()
    async with conn.execute("SELECT COUNT(*) FROM comments WHERE confession_id = ?", (confession_id,)) as cursor:
        count_row = await cursor.fetchone()
        count = count_row[0] if count_row else 0
    
    if not conf_data or not conf_data['message_id']: 
        logger.debug(f"No approved conf/msg_id for {confession_id} button.")
        return
    
    ch_msg_id = conf_data['message_id']
    link = f"https://t.me/{bot_info.username}?start=view_{confession_id}"
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"💬 View / Add Comments ({count})", url=link)]])
    
    try: 
        await bot.edit_message_reply_markup(chat_id=CHANNEL_ID, message_id=ch_msg_id, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower(): 
            logger.info(f"Button for {confession_id} already updated ({count}).")
        elif "message to edit not found" in str(e).lower(): 
            logger.warning(f"Msg {ch_msg_id} not found in {CHANNEL_ID} (conf {confession_id}). Maybe deleted?")
        else: 
            logger.error(f"Failed edit channel post {ch_msg_id} for conf {confession_id}: {e}")
    except Exception as e: 
        logger.error(f"Unexpected err updating btn for conf {confession_id}: {e}", exc_info=True)

async def get_comment_sequence_number(conn, comment_id: int, confession_id: int) -> Optional[int]:
    """Fetches the sequential number of a specific comment within its confession."""
    query = """
        WITH ranked_comments AS (
            SELECT id, ROW_NUMBER() OVER (ORDER BY created_at ASC) as rn
            FROM comments
            WHERE confession_id = ?
        )
        SELECT rn FROM ranked_comments WHERE id = ?;
    """
    try:
        async with conn.execute(query, (confession_id, comment_id)) as cursor:
            row = await cursor.fetchone()
            return row['rn'] if row else None
    except Exception as e:
        logger.error(f"Could not fetch sequence number for comment {comment_id}: {e}")
        return None

async def show_comments_for_confession(user_id: int, confession_id: int, message_to_edit: Optional[types.Message] = None, page: int = 1):
    conn = db
    async with conn.execute("SELECT status, user_id FROM confessions WHERE id = ?", (confession_id,)) as cursor:
        conf_data = await cursor.fetchone()
    
    if not conf_data or conf_data['status'] != 'approved':
        err_txt = f"Confession #{confession_id} not found or not approved."
        if message_to_edit: 
            await message_to_edit.edit_text(err_txt, reply_markup=None)
        else: 
            await safe_send_message(user_id, err_txt)
        return
    
    confession_owner_id = conf_data['user_id']
    async with conn.execute("SELECT COUNT(*) FROM comments WHERE confession_id = ?", (confession_id,)) as cursor:
        total_row = await cursor.fetchone()
        total_count = total_row[0] if total_row else 0
    
    if total_count == 0:
        msg_text = "<i>No comments yet. Be the first!</i>"
        if message_to_edit: 
            await message_to_edit.edit_text(msg_text, reply_markup=None)
        else: 
            await safe_send_message(user_id, msg_text)
        nav = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="➕ Add Comment", callback_data=f"add_{confession_id}")]])
        await safe_send_message(user_id, "You can add your own comment below:", reply_markup=nav)
        return

    total_pages = (total_count + PAGE_SIZE - 1) // PAGE_SIZE
    page = max(1, min(page, total_pages))
    offset = (page - 1) * PAGE_SIZE
    
    async with conn.execute("""
        SELECT c.id, c.user_id, c.text, c.sticker_file_id, c.animation_file_id, c.parent_comment_id, c.created_at, 
               COALESCE(up.points, 0) as user_points, us.profile_name
        FROM comments c 
        LEFT JOIN user_points up ON c.user_id = up.user_id 
        LEFT JOIN user_status us ON c.user_id = us.user_id
        WHERE c.confession_id = ? 
        ORDER BY c.created_at ASC LIMIT ? OFFSET ?
    """, (confession_id, PAGE_SIZE, offset)) as cursor:
        comments_raw = await cursor.fetchall()

    db_id_to_message_id: Dict[int, int] = {}
    is_admin_user = await is_admin(user_id)

    if not comments_raw:
        await safe_send_message(user_id, f"<i>No comments on page {page}.</i>")
    else:
        for i, c_data in enumerate(comments_raw):
            seq_num, db_id, commenter_uid = offset + i + 1, c_data['id'], c_data['user_id']
            
            # Get profile name or Anonymous
            profile_name = c_data['profile_name'] if c_data['profile_name'] is not None else "Anonymous"
            aura_points = c_data['user_points'] if c_data['user_points'] is not None else 0
            
            # Build display tag
            tag_parts = []
            if commenter_uid == confession_owner_id:
                tag_parts.append("👑 Author")
            if commenter_uid == user_id:
                tag_parts.append("👤 You")
            
            tag_str = f" ({', '.join(tag_parts)})" if tag_parts else ""
            
            # Get encoded profile link
            encoded_profile_link = await get_encoded_profile_link(commenter_uid)
            display_name = f"<a href='{encoded_profile_link}'>{profile_name}</a> 🏅{aura_points}{tag_str}"
            
            admin_info = f" [UID: <code>{commenter_uid}</code>]" if is_admin_user else ""

            reply_to_msg_id = None
            text_reply_prefix = ""
            parent_db_id = c_data['parent_comment_id'] if c_data['parent_comment_id'] is not None else None
            
            if parent_db_id:
                if parent_db_id in db_id_to_message_id:
                    reply_to_msg_id = db_id_to_message_id[parent_db_id]
                else: 
                    parent_seq_num = await get_comment_sequence_number(conn, parent_db_id, confession_id)
                    if parent_seq_num:
                        text_reply_prefix = f"↪️ <i>Replying to comment #{parent_seq_num}...</i>\n"
                    else:
                        text_reply_prefix = "↪️ <i>Replying to another comment...</i>\n"

            keyboard = await build_comment_keyboard(db_id, commenter_uid, user_id, confession_owner_id, is_admin_user)
            
            sent_message = None
            try:
                if c_data['sticker_file_id']:
                    sent_message = await bot.send_sticker(user_id, sticker=c_data['sticker_file_id'], reply_to_message_id=reply_to_msg_id)
                    await bot.send_message(user_id, f"{text_reply_prefix}{display_name}{admin_info}", reply_markup=keyboard)
                elif c_data['animation_file_id']:
                    sent_message = await bot.send_animation(user_id, animation=c_data['animation_file_id'], reply_to_message_id=reply_to_msg_id)
                    await bot.send_message(user_id, f"{text_reply_prefix}{display_name}{admin_info}", reply_markup=keyboard)
                elif c_data['text']:
                    full_text = f"{text_reply_prefix}💬 {html.quote(c_data['text'])}\n\n{display_name}{admin_info}"
                    sent_message = await bot.send_message(user_id, full_text, reply_markup=keyboard, disable_web_page_preview=True, reply_to_message_id=reply_to_msg_id)
                
                if sent_message:
                    db_id_to_message_id[db_id] = sent_message.message_id

            except Exception as e:
                logger.warning(f"Could not send comment #{seq_num} to {user_id}: {e}")
                await safe_send_message(user_id, f"⚠️ Error displaying comment #{seq_num}.")
            await asyncio.sleep(0.1)

    # Navigation buttons
    nav_row = []
    if page > 1: 
        nav_row.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"comments_page_{confession_id}_{page-1}"))
    if total_pages > 1: 
        nav_row.append(InlineKeyboardButton(text=f"Page {page}/{total_pages}", callback_data="noop"))
    if page < total_pages: 
        nav_row.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"comments_page_{confession_id}_{page+1}"))
    
    nav_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        nav_row,
        [InlineKeyboardButton(text="➕ Add Comment", callback_data=f"add_{confession_id}")]
    ])
    
    end_txt = f"--- Showing comments {offset+1} to {min(offset+PAGE_SIZE, total_count)} of {total_count} for Confession #{confession_id} ---"
    await safe_send_message(user_id, end_txt, reply_markup=nav_keyboard)

async def check_rate_limit(user_id: int) -> bool:
    """Check if user is rate limited"""
    current_time = time.time()
    if current_time - user_last_action[user_id] < RATE_LIMIT_SECONDS:
        return False
    user_last_action[user_id] = current_time
    return True

# --- Middleware ---
class BlockUserMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: types.TelegramObject, data: Dict[str, Any]) -> Any:
        user = data.get('event_from_user')
        if not user:
            return await handler(event, data)

        user_id = user.id
        
        # Allow /start and /help commands even for blocked users
        if isinstance(event, types.Message) and event.text:
            if event.text.startswith('/start') or event.text.startswith('/help'):
                return await handler(event, data)
        
        # Admins cannot be blocked
        if await is_admin(user_id):
            return await handler(event, data)

        conn = db
        async with conn.execute("SELECT is_blocked, blocked_until, block_reason FROM user_status WHERE user_id = ?", (user_id,)) as cursor:
            status = await cursor.fetchone()
        
        if status and status['is_blocked']:
            now = datetime.now()
            # Handle datetime conversion safely
            blocked_until = None
            if status['blocked_until']:
                try:
                    if isinstance(status['blocked_until'], str):
                        blocked_until = datetime.fromisoformat(status['blocked_until'].replace('Z', '+00:00'))
                    else:
                        blocked_until = status['blocked_until']
                except:
                    blocked_until = None
            
            if blocked_until and blocked_until < now:
                # Unblock expired temporary blocks
                await conn.execute("UPDATE user_status SET is_blocked = 0, blocked_until = NULL, block_reason = NULL WHERE user_id = ?", (user_id,))
                await conn.commit()
                return await handler(event, data)
            else:
                expiry_info = f"until {blocked_until.strftime('%Y-%m-%d %H:%M %Z')}" if blocked_until else "permanently"
                reason_info = f"\nReason: <i>{html.quote(status['block_reason'])}</i>" if status['block_reason'] else ""
                
                block_message = f"❌ <b>You are blocked from using this bot {expiry_info}.</b>{reason_info}\n\nContact admins if you believe this is a mistake."

                if isinstance(event, types.CallbackQuery):
                    await event.answer(f"You are blocked {expiry_info}.", show_alert=True)
                elif isinstance(event, types.Message):
                    with suppress(Exception):
                        await event.answer(block_message)
                return  # Stop processing the event

        return await handler(event, data)

# --- Profile Management Handlers ---
def create_profile_pagination_keyboard(base_callback: str, current_page: int, total_pages: int):
    builder = InlineKeyboardBuilder()
    row = []
    if current_page > 1:
        row.append(InlineKeyboardButton(text="⬅️ Prev", callback_data=f"{base_callback}_{current_page - 1}"))
    if total_pages > 1:
        row.append(InlineKeyboardButton(text=f"Page {current_page}/{total_pages}", callback_data="noop"))
    if current_page < total_pages:
        row.append(InlineKeyboardButton(text="Next ➡️", callback_data=f"{base_callback}_{current_page + 1}"))
    if row:
        builder.row(*row)
    builder.row(InlineKeyboardButton(text="⬅️ Back to Profile", callback_data="profile_main"))
    return builder.as_markup()

@dp.message(Command("profile"))
async def user_profile(message: types.Message):
    user_id = message.from_user.id
    
    # Check if user has accepted rules first
    conn = db
    async with conn.execute("SELECT has_accepted_rules FROM user_status WHERE user_id = ?", (user_id,)) as cursor:
        has_accepted_row = await cursor.fetchone()
        has_accepted = has_accepted_row['has_accepted_rules'] if has_accepted_row else 0
    
    if not has_accepted:
        await message.answer("⚠️ Please use /start first to accept the rules.")
        return
    
    points = await get_user_points(user_id)
    profile_name = await get_profile_name(user_id)
    
    profile_text = f"👤 <b>Your Profile</b>\n\n"
    profile_text += f"🏅 <b>Aura Points:</b> {points}\n"
    profile_text += f"👁️ <b>Display Name:</b> {profile_name}\n\n"
    profile_text += "<b>What would you like to do?</b>"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Change Display Name", callback_data="change_profile_name")],
        [InlineKeyboardButton(text="📜 My Confessions", callback_data="profile_confessions_1")],
        [InlineKeyboardButton(text="💬 My Comments", callback_data="profile_comments_1")],
        [InlineKeyboardButton(text="💬 My Active Chats", callback_data="my_active_chats")],
        [InlineKeyboardButton(text="📨 Pending Contact Requests", callback_data="pending_contact_requests")]
    ])
    
    await message.answer(profile_text, reply_markup=keyboard)

@dp.callback_query(F.data == "profile_main")
async def back_to_profile(callback_query: types.CallbackQuery):
    """Go back to main profile menu"""
    user_id = callback_query.from_user.id
    points = await get_user_points(user_id)
    profile_name = await get_profile_name(user_id)
    
    profile_text = f"👤 <b>Your Profile</b>\n\n"
    profile_text += f"🏅 <b>Aura Points:</b> {points}\n"
    profile_text += f"👁️ <b>Display Name:</b> {profile_name}\n\n"
    profile_text += "<b>What would you like to do?</b>"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Change Display Name", callback_data="change_profile_name")],
        [InlineKeyboardButton(text="📜 My Confessions", callback_data="profile_confessions_1")],
        [InlineKeyboardButton(text="💬 My Comments", callback_data="profile_comments_1")],
        [InlineKeyboardButton(text="💬 My Active Chats", callback_data="my_active_chats")],
        [InlineKeyboardButton(text="📨 Pending Contact Requests", callback_data="pending_contact_requests")]
    ])
    
    await callback_query.message.edit_text(profile_text, reply_markup=keyboard)
    await callback_query.answer()

@dp.callback_query(F.data == "change_profile_name")
async def change_profile_name_start(callback_query: types.CallbackQuery, state: FSMContext):
    await state.set_state(UserProfileForm.waiting_for_profile_name)
    await callback_query.message.answer("Please enter your new display name (max 32 characters):")
    await callback_query.answer()

@dp.message(UserProfileForm.waiting_for_profile_name, F.text)
async def receive_profile_name(message: types.Message, state: FSMContext):
    profile_name = message.text.strip()
    
    if len(profile_name) > 32:
        await message.answer("Profile name too long. Maximum 32 characters. Please try again:")
        return
    
    if not re.match(r'^[a-zA-Z0-9_ ]+$', profile_name):
        await message.answer("Profile name can only contain letters, numbers, spaces and underscores. Please try again:")
        return
    
    await update_profile_name(message.from_user.id, profile_name)
    await message.answer(f"✅ Your display name has been updated to: <b>{html.quote(profile_name)}</b>")
    await state.clear()

@dp.callback_query(F.data.startswith("profile_confessions_"))
async def show_user_confessions(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    try:
        page = int(callback_query.data.split("_")[-1])
    except ValueError:
        page = 1
    
    conn = db
    async with conn.execute("SELECT COUNT(*) FROM confessions WHERE user_id = ?", (user_id,)) as cursor:
        total_row = await cursor.fetchone()
        total_count = total_row[0] if total_row else 0
    
    if total_count == 0:
        await callback_query.message.edit_text(
            "📭 <b>Your Confessions</b>\n\n"
            "You haven't submitted any confessions yet.\n\n"
            "Use /confess to submit your first confession!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Back to Profile", callback_data="profile_main")]
            ])
        )
        await callback_query.answer()
        return
    
    total_pages = (total_count + 5 - 1) // 5
    page = max(1, min(page, total_pages))
    offset = (page - 1) * 5
    
    async with conn.execute("""
        SELECT id, text, status, created_at 
        FROM confessions 
        WHERE user_id = ? 
        ORDER BY created_at DESC 
        LIMIT 5 OFFSET ?
    """, (user_id, offset)) as cursor:
        confessions = await cursor.fetchall()
    
    response_text = f"<b>📜 Your Confessions (Page {page}/{total_pages})</b>\n\n"
    builder = InlineKeyboardBuilder()
    
    for conf in confessions:
        snippet = html.quote(conf['text'][:60]) + ('...' if len(conf['text']) > 60 else '')
        status_emoji = {"approved": "✅", "pending": "⏳", "rejected": "❌", "deleted": "🗑️"}.get(conf['status'], "❓")
        response_text += f"<b>ID:</b> #{conf['id']} ({status_emoji} {conf['status'].capitalize()})\n"
        response_text += f"<i>\"{snippet}\"</i>\n"
        response_text += f"<i>Submitted: {conf['created_at'][:10]}</i>\n\n"
        
        if conf['status'] in ['approved', 'pending']:
            builder.row(InlineKeyboardButton(
                text=f"🗑️ Request Deletion for #{conf['id']}", 
                callback_data=f"req_del_conf_{conf['id']}"
            ))
    
    nav_keyboard = create_profile_pagination_keyboard("profile_confessions", page, total_pages)
    final_markup = builder.attach(InlineKeyboardBuilder.from_markup(nav_keyboard)).as_markup()
    
    await callback_query.message.edit_text(response_text, reply_markup=final_markup)
    await callback_query.answer()

@dp.callback_query(F.data.startswith("profile_comments_"))
async def show_user_comments(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    try:
        page = int(callback_query.data.split("_")[-1])
    except ValueError:
        page = 1
    
    conn = db
    async with conn.execute("SELECT COUNT(*) FROM comments WHERE user_id = ?", (user_id,)) as cursor:
        total_row = await cursor.fetchone()
        total_count = total_row[0] if total_row else 0
    
    if total_count == 0:
        await callback_query.message.edit_text(
            "💬 <b>Your Comments</b>\n\n"
            "You haven't made any comments yet.\n\n"
            "Browse confessions and add comments to participate!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Back to Profile", callback_data="profile_main")]
            ])
        )
        await callback_query.answer()
        return
    
    total_pages = (total_count + 5 - 1) // 5
    page = max(1, min(page, total_pages))
    offset = (page - 1) * 5
    
    async with conn.execute("""
        SELECT c.id, c.text, c.sticker_file_id, c.animation_file_id, c.confession_id, c.created_at,
               conf.text as confession_text
        FROM comments c
        LEFT JOIN confessions conf ON c.confession_id = conf.id
        WHERE c.user_id = ?
        ORDER BY c.created_at DESC 
        LIMIT 5 OFFSET ?
    """, (user_id, offset)) as cursor:
        comments = await cursor.fetchall()
    
    response_text = f"<b>💬 Your Comments (Page {page}/{total_pages})</b>\n\n"
    
    for comm in comments:
        if comm['text']: 
            snippet = "💬 " + html.quote(comm['text'][:60]) + ('...' if len(comm['text']) > 60 else '')
        elif comm['sticker_file_id']: 
            snippet = "[Sticker]"
        elif comm['animation_file_id']: 
            snippet = "[GIF]"
        else: 
            snippet = "[Unknown Content]"
        
        # Get confession snippet
        conf_snippet = html.quote(comm['confession_text'][:40]) + ('...' if len(comm['confession_text']) > 40 else '') if comm['confession_text'] else "Unknown"
        
        link = f"https://t.me/{bot_info.username}?start=view_{comm['confession_id']}"
        response_text += f"<b>On Confession:</b> <a href='{link}'>#{comm['confession_id']}</a>\n"
        response_text += f"<i>\"{conf_snippet}\"</i>\n"
        response_text += f"<b>Your comment:</b> {snippet}\n"
        response_text += f"<i>Posted: {comm['created_at'][:10]}</i>\n\n"
    
    nav_keyboard = create_profile_pagination_keyboard("profile_comments", page, total_pages)
    await callback_query.message.edit_text(response_text, reply_markup=nav_keyboard, disable_web_page_preview=True)
    await callback_query.answer()

@dp.callback_query(F.data.startswith("req_del_conf_"))
async def request_deletion_prompt(callback_query: types.CallbackQuery):
    conf_id = int(callback_query.data.split("_")[-1])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Yes, Request Deletion", callback_data=f"confirm_del_conf_{conf_id}")],
        [InlineKeyboardButton(text="❌ No, Cancel", callback_data="profile_main")]
    ])
    
    await callback_query.message.edit_text(
        f"Are you sure you want to request the deletion of Confession #{conf_id}?\n\n"
        f"<i>This will notify admins for review. If approved, your confession will be permanently deleted.</i>",
        reply_markup=keyboard
    )
    await callback_query.answer()

@dp.callback_query(F.data.startswith("confirm_del_conf_"))
async def confirm_deletion_request(callback_query: types.CallbackQuery):
    conf_id = int(callback_query.data.split("_")[-1])
    user_id = callback_query.from_user.id
    
    conn = db
    try:
        async with conn.execute("SELECT user_id, text, status FROM confessions WHERE id = ?", (conf_id,)) as cursor:
            conf_data = await cursor.fetchone()
        
        if not conf_data or conf_data['user_id'] != user_id:
            await callback_query.answer("This is not your confession.", show_alert=True)
            return
        
        if conf_data['status'] not in ['approved', 'pending']:
            await callback_query.answer(f"This confession cannot be deleted (status: {conf_data['status']}).", show_alert=True)
            return
        
        # Check if request already exists
        async with conn.execute("SELECT id FROM deletion_requests WHERE confession_id = ? AND user_id = ?", (conf_id, user_id)) as cursor:
            existing_req = await cursor.fetchone()
        
        if existing_req:
            await callback_query.answer("You have already requested deletion for this confession.", show_alert=True)
            return
        
        # Create deletion request
        await conn.execute(
            """INSERT INTO deletion_requests (confession_id, user_id, status) VALUES (?, ?, 'pending')""", 
            (conf_id, user_id)
        )
        await conn.commit()
        
        # Notify admins
        snippet = html.quote(conf_data['text'][:200])
        profile_name = await get_profile_name(user_id)
        
        admin_text = (
            f"🗑️ <b>New Deletion Request</b>\n\n"
            f"<b>User:</b> {profile_name}\n"
            f"<b>User ID:</b> <code>{user_id}</code>\n"
            f"<b>Confession ID:</b> <code>{conf_id}</code>\n"
            f"<b>Status:</b> {conf_data['status'].capitalize()}\n\n"
            f"<b>Content Snippet:</b>\n<i>\"{snippet}...\"</i>"
        )
        
        admin_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Approve Deletion", callback_data=f"admin_approve_delete_{conf_id}")],
            [InlineKeyboardButton(text="❌ Reject Deletion", callback_data=f"admin_reject_delete_{conf_id}")]
        ])
        
        # Notify all admins
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, admin_text, reply_markup=admin_keyboard)
            except Exception as e:
                logger.warning(f"Could not notify admin {admin_id}: {e}")
        
        await callback_query.answer("✅ Deletion request sent. An admin will review it shortly.", show_alert=True)
        
        # Go back to confessions list
        callback_query.data = "profile_confessions_1"
        await show_user_confessions(callback_query)
        
    except Exception as e:
        logger.error(f"Error processing deletion request for conf {conf_id} by user {user_id}: {e}")
        await callback_query.answer("An error occurred while sending your request.", show_alert=True)

@dp.callback_query(F.data == "my_active_chats")
async def show_active_chats(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    conn = db
    
    async with conn.execute("""
        SELECT ac.id, 
               CASE WHEN ac.user1_id = ? THEN ac.user2_id ELSE ac.user1_id END as other_user_id,
               us.profile_name as other_user_name,
               ac.last_message_at,
               COUNT(cm.id) as message_count
        FROM active_chats ac
        LEFT JOIN user_status us ON (CASE WHEN ac.user1_id = ? THEN ac.user2_id ELSE ac.user1_id END) = us.user_id
        LEFT JOIN chat_messages cm ON ac.id = cm.chat_id
        WHERE (ac.user1_id = ? OR ac.user2_id = ?) AND ac.is_active = 1
        GROUP BY ac.id, other_user_id, other_user_name, ac.last_message_at
        ORDER BY ac.last_message_at DESC
    """, (user_id, user_id, user_id, user_id)) as cursor:
        chats = await cursor.fetchall()
    
    if not chats:
        await callback_query.message.edit_text(
            "💬 <b>Your Active Chats</b>\n\n"
            "You have no active chats.\n\n"
            "<i>Start a chat by approving a contact request or requesting to contact a commenter.</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Back to Profile", callback_data="profile_main")]
            ])
        )
        await callback_query.answer()
        return
    
    response_text = "💬 <b>Your Active Chats</b>\n\n"
    keyboard = InlineKeyboardBuilder()
    
    for chat in chats:
        other_user_name = chat['other_user_name'] or "Anonymous"
        message_count = chat['message_count'] or 0
        last_msg_time = chat['last_message_at'][:19] if chat['last_message_at'] else "No messages"
        
        response_text += f"👤 <b>{other_user_name}</b>\n"
        response_text += f"   Messages: {message_count}\n"
        response_text += f"   Last activity: {last_msg_time}\n\n"
        
        keyboard.button(
            text=f"💬 Chat with {other_user_name[:15]}",
            callback_data=f"view_chat_{chat['id']}"
        )
    
    keyboard.button(text="⬅️ Back to Profile", callback_data="profile_main")
    keyboard.adjust(1)
    
    await callback_query.message.edit_text(response_text, reply_markup=keyboard.as_markup())
    await callback_query.answer()

@dp.callback_query(F.data.startswith("view_chat_"))
async def view_chat_messages(callback_query: types.CallbackQuery, state: FSMContext):
    chat_id = int(callback_query.data.split("_")[-1])
    user_id = callback_query.from_user.id
    
    conn = db
    # Verify user is part of this chat
    async with conn.execute("""
        SELECT ac.id, 
               CASE WHEN ac.user1_id = ? THEN ac.user2_id ELSE ac.user1_id END as other_user_id,
               us.profile_name as other_user_name
        FROM active_chats ac
        LEFT JOIN user_status us ON (CASE WHEN ac.user1_id = ? THEN ac.user2_id ELSE ac.user1_id END) = us.user_id
        WHERE ac.id = ? AND (ac.user1_id = ? OR ac.user2_id = ?) AND ac.is_active = 1
    """, (user_id, user_id, chat_id, user_id, user_id)) as cursor:
        chat_data = await cursor.fetchone()
    
    if not chat_data:
        await callback_query.answer("Chat not found or no longer active.", show_alert=True)
        return
    
    other_user_id = chat_data['other_user_id']
    other_user_name = chat_data['other_user_name'] or "Anonymous"
    
    # Set state for chatting
    await state.set_state(ChatForm.chatting)
    await state.update_data(chat_id=chat_id, other_user_id=other_user_id)
    
    # Get last 10 messages
    async with conn.execute("""
        SELECT cm.*, us.profile_name as sender_name
        FROM chat_messages cm
        LEFT JOIN user_status us ON cm.sender_id = us.user_id
        WHERE cm.chat_id = ?
        ORDER BY cm.created_at DESC
        LIMIT 10
    """, (chat_id,)) as cursor:
        messages = await cursor.fetchall()
    
    response_text = f"💬 <b>Chat with {other_user_name}</b>\n\n"
    
    if not messages:
        response_text += "<i>No messages yet. Start the conversation!</i>\n\n"
    else:
        # Show messages in chronological order
        for msg in reversed(messages):
            sender_name = msg['sender_name'] or ("You" if msg['sender_id'] == user_id else "Anonymous")
            time_str = msg['created_at'][11:16] if msg['created_at'] else ""
            
            if msg['message_text']:
                response_text += f"<b>{sender_name}</b> ({time_str}):\n"
                response_text += f"{html.quote(msg['message_text'])}\n\n"
            elif msg['sticker_file_id']:
                response_text += f"<b>{sender_name}</b> ({time_str}): [Sticker]\n\n"
            elif msg['animation_file_id']:
                response_text += f"<b>{sender_name}</b> ({time_str}): [GIF]\n\n"
    
    response_text += "<i>Send a message below to continue this conversation.</i>\n\n"
    response_text += "<i>Type /endchat to disconnect.</i>"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚫 Disconnect Chat", callback_data=f"disconnect_chat_{chat_id}")],
        [InlineKeyboardButton(text="⬅️ Back to Chats", callback_data="my_active_chats")]
    ])
    
    await callback_query.message.edit_text(response_text, reply_markup=keyboard)
    await callback_query.answer()

# Chat message handler - don't forward commands
@dp.message(ChatForm.chatting)
async def handle_chat_message(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    state_data = await state.get_data()
    chat_id = state_data.get('chat_id')
    other_user_id = state_data.get('other_user_id')
    
    if not chat_id:
        await state.clear()
        return
    
    # Check if message is a command
    if message.text and message.text.startswith('/'):
        # Handle commands locally without forwarding
        if message.text.startswith('/endchat'):
            await end_chat_command(message, state)
        elif message.text.startswith('/cancel'):
            await cancel_command(message, state)
        elif message.text.startswith('/start'):
            await start(message, state)
        elif message.text.startswith('/help'):
            await help_command(message)
        elif message.text.startswith('/profile'):
            await user_profile(message)
        elif message.text.startswith('/rules'):
            await rules_command(message)
        elif message.text.startswith('/privacy'):
            await privacy_command(message)
        else:
            await message.answer("Commands are not forwarded in chats. Use them outside of chat mode.")
        return
    
    conn = db
    try:
        # Save message
        if message.text:
            await conn.execute("""
                INSERT INTO chat_messages (chat_id, sender_id, message_text)
                VALUES (?, ?, ?)
            """, (chat_id, user_id, message.text))
        elif message.sticker:
            await conn.execute("""
                INSERT INTO chat_messages (chat_id, sender_id, sticker_file_id)
                VALUES (?, ?, ?)
            """, (chat_id, user_id, message.sticker.file_id))
        elif message.animation:
            await conn.execute("""
                INSERT INTO chat_messages (chat_id, sender_id, animation_file_id)
                VALUES (?, ?, ?)
            """, (chat_id, user_id, message.animation.file_id))
        else:
            await message.answer("Only text, stickers, and GIFs are supported in chats.")
            return
        
        # Update last message time
        await conn.execute("""
            UPDATE active_chats 
            SET last_message_at = CURRENT_TIMESTAMP 
            WHERE id = ?
        """, (chat_id,))
        
        await conn.commit()
        
        # Forward message to other user
        try:
            if message.text:
                await bot.send_message(
                    other_user_id,
                    f"💬 <b>New message in chat:</b>\n\n{html.quote(message.text)}",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="💬 Go to Chat", callback_data=f"view_chat_{chat_id}")]
                    ])
                )
            elif message.sticker:
                await bot.send_sticker(other_user_id, sticker=message.sticker.file_id)
            elif message.animation:
                await bot.send_animation(other_user_id, animation=message.animation.file_id)
        except Exception as e:
            logger.warning(f"Could not forward message to user {other_user_id}: {e}")
        
        await message.answer("✅ Message sent!")
        
    except Exception as e:
        logger.error(f"Error handling chat message: {e}")
        await message.answer("❌ Error sending message.")

@dp.callback_query(F.data.startswith("disconnect_chat_"))
async def disconnect_chat(callback_query: types.CallbackQuery, state: FSMContext):
    chat_id = int(callback_query.data.split("_")[-1])
    user_id = callback_query.from_user.id
    
    conn = db
    try:
        # Get other user info
        async with conn.execute("""
            SELECT user1_id, user2_id FROM active_chats 
            WHERE id = ? AND (user1_id = ? OR user2_id = ?) AND is_active = 1
        """, (chat_id, user_id, user_id)) as cursor:
            chat_data = await cursor.fetchone()
        
        if not chat_data:
            await callback_query.answer("Chat not found.", show_alert=True)
            return
        
        other_user_id = chat_data['user1_id'] if chat_data['user2_id'] == user_id else chat_data['user2_id']
        
        # Deactivate chat
        await conn.execute("UPDATE active_chats SET is_active = 0 WHERE id = ?", (chat_id,))
        await conn.commit()
        
        # Clear state if in chat
        current_state = await state.get_state()
        if current_state == ChatForm.chatting:
            await state.clear()
        
        # Notify other user
        try:
            await bot.send_message(
                other_user_id,
                f"⚠️ <b>Chat disconnected</b>\n\n"
                f"The other user has ended the chat."
            )
        except Exception as e:
            logger.warning(f"Could not notify user {other_user_id} about chat disconnect: {e}")
        
        await callback_query.message.edit_text(
            "✅ Chat disconnected.\n\n"
            "You can start a new chat by requesting contact from another user's profile.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Back to Profile", callback_data="profile_main")]
            ])
        )
        await callback_query.answer()
        
    except Exception as e:
        logger.error(f"Error disconnecting chat {chat_id}: {e}")
        await callback_query.answer("Error disconnecting chat.", show_alert=True)

# --- FIXED: View Profile Handler ---
@dp.callback_query(F.data.startswith("view_profile_"))
async def view_user_profile(callback_query: types.CallbackQuery):
    """View another user's profile - FIXED VERSION"""
    try:
        # Get encoded user ID from callback data
        parts = callback_query.data.split("_")
        if len(parts) < 3:
            await callback_query.answer("Invalid profile link.", show_alert=True)
            return
            
        encoded_user_id = "_".join(parts[2:])  # Get all remaining parts as encoded ID
        viewer_user_id = callback_query.from_user.id
        
        # Decode the user ID
        target_user_id = await get_user_id_from_encoded(encoded_user_id)
        
        if not target_user_id:
            await callback_query.answer("User not found or link expired.", show_alert=True)
            return
            
    except Exception as e:
        logger.error(f"Error parsing profile callback data: {e}")
        await callback_query.answer("Invalid profile link.", show_alert=True)
        return
    
    if target_user_id == viewer_user_id:
        # Redirect to own profile
        await callback_query.answer("Redirecting to your profile...")
        await user_profile(callback_query.message)
        return
    
    conn = db
    # Get target user info
    async with conn.execute("""
        SELECT us.profile_name, up.points, us.user_id
        FROM user_status us
        LEFT JOIN user_points up ON us.user_id = up.user_id
        WHERE us.user_id = ?
    """, (target_user_id,)) as cursor:
        user_data = await cursor.fetchone()
    
    if not user_data:
        await callback_query.answer("User not found.", show_alert=True)
        return
    
    profile_name = user_data['profile_name'] or "Anonymous"
    points = user_data['points'] or 0
    
    # Check if there's already an active chat
    async with conn.execute("""
        SELECT id FROM active_chats 
        WHERE ((user1_id = ? AND user2_id = ?) OR (user1_id = ? AND user2_id = ?)) 
        AND is_active = 1
    """, (viewer_user_id, target_user_id, target_user_id, viewer_user_id)) as cursor:
        existing_chat = await cursor.fetchone()
    
    # Check if there's a pending contact request FROM viewer TO target
    async with conn.execute("""
        SELECT id, status FROM contact_requests 
        WHERE requester_user_id = ? AND requested_user_id = ? 
        AND status = 'pending'
    """, (viewer_user_id, target_user_id)) as cursor:
        pending_request = await cursor.fetchone()
    
    # Check if there's an approved contact request in either direction
    async with conn.execute("""
        SELECT id FROM contact_requests 
        WHERE ((requester_user_id = ? AND requested_user_id = ?) 
        OR (requester_user_id = ? AND requested_user_id = ?))
        AND status = 'approved'
    """, (viewer_user_id, target_user_id, target_user_id, viewer_user_id)) as cursor:
        approved_request = await cursor.fetchone()
    
    profile_text = f"👤 <b>User Profile</b>\n\n"
    profile_text += f"📛 <b>Display Name:</b> {profile_name}\n"
    profile_text += f"🏅 <b>Aura Points:</b> {points}\n\n"
    
    keyboard = InlineKeyboardBuilder()
    
    if existing_chat:
        profile_text += "<i>You have an active chat with this user.</i>"
        keyboard.button(text="💬 Go to Chat", callback_data=f"view_chat_{existing_chat['id']}")
    elif pending_request:
        profile_text += "<i>You have a pending contact request with this user.</i>"
        keyboard.button(text="⏳ Request Pending", callback_data="noop")
    elif approved_request:
        profile_text += "<i>Contact request approved. You can start chatting!</i>"
        # Start a new chat
        keyboard.button(text="💬 Start Chat", callback_data=f"start_chat_{target_user_id}")
    else:
        profile_text += "<i>You can request to chat with this user.</i>"
        keyboard.button(text="🤝 Request Contact", callback_data=f"req_contact_{target_user_id}")
    
    keyboard.button(text="⬅️ Back", callback_data="noop")
    keyboard.adjust(1)
    
    await callback_query.message.edit_text(profile_text, reply_markup=keyboard.as_markup())
    await callback_query.answer()

# --- FIXED: Get User ID Command ---
@dp.message(Command("id"))
async def get_user_id(message: types.Message):
    """Get user ID (for admins only) - FIXED VERSION"""
    user_id = message.from_user.id
    
    if not await is_admin(user_id):
        await message.answer("This command is for admins only.")
        return
    
    # Check if message is a reply
    if message.reply_to_message:
        target_id = message.reply_to_message.from_user.id
        target_name = message.reply_to_message.from_user.full_name
        target_username = message.reply_to_message.from_user.username
        
        # Get user profile info from database
        conn = db
        profile_name = await get_profile_name(target_id)
        points = await get_user_points(target_id)
        
        response_text = f"👤 <b>User Information</b>\n\n"
        response_text += f"<b>User ID:</b> <code>{target_id}</code>\n"
        response_text += f"<b>Name:</b> {target_name}\n"
        
        if target_username:
            response_text += f"<b>Username:</b> @{target_username}\n"
        
        response_text += f"<b>Profile Name:</b> {profile_name}\n"
        response_text += f"<b>Aura Points:</b> {points}\n"
        
        # Check if user is blocked
        async with conn.execute("SELECT is_blocked, blocked_until FROM user_status WHERE user_id = ?", (target_id,)) as cursor:
            status = await cursor.fetchone()
            if status and status['is_blocked']:
                if status['blocked_until']:
                    response_text += f"<b>Status:</b> ⏸️ Blocked until {status['blocked_until'][:19]}\n"
                else:
                    response_text += f"<b>Status:</b> 🚫 Permanently blocked\n"
            else:
                response_text += f"<b>Status:</b> ✅ Active\n"
        
        await message.answer(response_text)
    else:
        # Check if there's an argument (user ID) in the command
        command_args = message.text.split()
        
        if len(command_args) > 1:
            # Try to parse the user ID from arguments
            try:
                target_id = int(command_args[1])
                
                # Get user info from database
                conn = db
                
                # Try to get user info from Telegram API first
                try:
                    target_user = await bot.get_chat(target_id)
                    target_name = target_user.full_name
                    target_username = target_user.username
                except:
                    target_name = "Unknown"
                    target_username = None
                
                # Get user profile info from database
                profile_name = await get_profile_name(target_id)
                points = await get_user_points(target_id)
                
                response_text = f"👤 <b>User Information</b>\n\n"
                response_text += f"<b>User ID:</b> <code>{target_id}</code>\n"
                response_text += f"<b>Name:</b> {target_name}\n"
                
                if target_username:
                    response_text += f"<b>Username:</b> @{target_username}\n"
                
                response_text += f"<b>Profile Name:</b> {profile_name}\n"
                response_text += f"<b>Aura Points:</b> {points}\n"
                
                # Check if user is blocked
                async with conn.execute("SELECT is_blocked, blocked_until FROM user_status WHERE user_id = ?", (target_id,)) as cursor:
                    status = await cursor.fetchone()
                    if status and status['is_blocked']:
                        if status['blocked_until']:
                            response_text += f"<b>Status:</b> ⏸️ Blocked until {status['blocked_until'][:19]}\n"
                        else:
                            response_text += f"<b>Status:</b> 🚫 Permanently blocked\n"
                    else:
                        response_text += f"<b>Status:</b> ✅ Active\n"
                
                await message.answer(response_text)
                return
                
            except ValueError:
                await message.answer("Invalid user ID. Please provide a numeric user ID.")
                return
            except Exception as e:
                logger.error(f"Error getting user info for ID {command_args[1]}: {e}")
                await message.answer(f"Error getting user info: {e}")
                return
        else:
            # Show own ID
            profile_name = await get_profile_name(user_id)
            points = await get_user_points(user_id)
            
            response_text = f"👤 <b>Your Information</b>\n\n"
            response_text += f"<b>Your ID:</b> <code>{user_id}</code>\n"
            response_text += f"<b>Profile Name:</b> {profile_name}\n"
            response_text += f"<b>Aura Points:</b> {points}\n"
            
            await message.answer(response_text)

async def show_user_profile_directly(message: types.Message, target_user_id: int):
    """Show user profile directly (for deep links)"""
    viewer_user_id = message.from_user.id
    
    if target_user_id == viewer_user_id:
        # Redirect to own profile
        await message.answer("Redirecting to your profile...")
        await user_profile(message)
        return
    
    conn = db
    # Get target user info
    async with conn.execute("""
        SELECT us.profile_name, up.points, us.user_id
        FROM user_status us
        LEFT JOIN user_points up ON us.user_id = up.user_id
        WHERE us.user_id = ?
    """, (target_user_id,)) as cursor:
        user_data = await cursor.fetchone()
    
    if not user_data:
        await message.answer("User not found.")
        return
    
    profile_name = user_data['profile_name'] or "Anonymous"
    points = user_data['points'] or 0
    
    # Check if there's already an active chat
    async with conn.execute("""
        SELECT id FROM active_chats 
        WHERE ((user1_id = ? AND user2_id = ?) OR (user1_id = ? AND user2_id = ?)) 
        AND is_active = 1
    """, (viewer_user_id, target_user_id, target_user_id, viewer_user_id)) as cursor:
        existing_chat = await cursor.fetchone()
    
    # Check if there's a pending contact request FROM viewer TO target
    async with conn.execute("""
        SELECT id, status FROM contact_requests 
        WHERE requester_user_id = ? AND requested_user_id = ? 
        AND status = 'pending'
    """, (viewer_user_id, target_user_id)) as cursor:
        pending_request = await cursor.fetchone()
    
    # Check if there's an approved contact request in either direction
    async with conn.execute("""
        SELECT id FROM contact_requests 
        WHERE ((requester_user_id = ? AND requested_user_id = ?) 
        OR (requester_user_id = ? AND requested_user_id = ?))
        AND status = 'approved'
    """, (viewer_user_id, target_user_id, target_user_id, viewer_user_id)) as cursor:
        approved_request = await cursor.fetchone()
    
    profile_text = f"👤 <b>User Profile</b>\n\n"
    profile_text += f"📛 <b>Display Name:</b> {profile_name}\n"
    profile_text += f"🏅 <b>Aura Points:</b> {points}\n\n"
    
    keyboard = InlineKeyboardBuilder()
    
    if existing_chat:
        profile_text += "<i>You have an active chat with this user.</i>"
        keyboard.button(text="💬 Go to Chat", callback_data=f"view_chat_{existing_chat['id']}")
    elif pending_request:
        profile_text += "<i>You have a pending contact request with this user.</i>"
        keyboard.button(text="⏳ Request Pending", callback_data="noop")
    elif approved_request:
        profile_text += "<i>Contact request approved. You can start chatting!</i>"
        # Start a new chat
        keyboard.button(text="💬 Start Chat", callback_data=f"start_chat_{target_user_id}")
    else:
        profile_text += "<i>You can request to chat with this user.</i>"
        keyboard.button(text="🤝 Request Contact", callback_data=f"req_contact_{target_user_id}")
    
    keyboard.button(text="⬅️ Back", callback_data="profile_main")
    keyboard.adjust(1)
    
    await message.answer(profile_text, reply_markup=keyboard.as_markup())

@dp.callback_query(F.data.startswith("req_contact_"))
async def request_contact_start(callback_query: types.CallbackQuery, state: FSMContext):
    """Start contact request process"""
    try:
        target_user_id = int(callback_query.data.split("_")[-1])
        requester_user_id = callback_query.from_user.id
    except ValueError:
        await callback_query.answer("Invalid user ID.", show_alert=True)
        return
    
    if target_user_id == requester_user_id:
        await callback_query.answer("You cannot request contact with yourself.", show_alert=True)
        return
    
    conn = db
    # Check if request already exists
    async with conn.execute("""
        SELECT id FROM contact_requests 
        WHERE requester_user_id = ? AND requested_user_id = ?
    """, (requester_user_id, target_user_id)) as cursor:
        existing_request = await cursor.fetchone()
    
    if existing_request:
        await callback_query.answer("You already have a pending request with this user.", show_alert=True)
        return
    
    # Check if there's already an active chat
    async with conn.execute("""
        SELECT id FROM active_chats 
        WHERE ((user1_id = ? AND user2_id = ?) OR (user1_id = ? AND user2_id = ?)) 
        AND is_active = 1
    """, (requester_user_id, target_user_id, target_user_id, requester_user_id)) as cursor:
        existing_chat = await cursor.fetchone()
    
    if existing_chat:
        await callback_query.answer("You already have an active chat with this user.", show_alert=True)
        return
    
    await state.set_state(ContactRequestForm.waiting_for_contact_message)
    await state.update_data(
        target_user_id=target_user_id,
        original_message_id=callback_query.message.message_id
    )
    
    await callback_query.message.answer(
        "🤝 <b>Contact Request</b>\n\n"
        "Please write a message to introduce yourself (optional):\n\n"
        "<i>This message will be sent to the user along with your contact request.</i>\n\n"
        "<i>Type /skip to send without a message, or /cancel to abort.</i>"
    )
    await callback_query.answer()

@dp.message(ContactRequestForm.waiting_for_contact_message, F.text)
async def receive_contact_message(message: types.Message, state: FSMContext):
    """Process contact request message - FIXED VERSION"""
    user_id = message.from_user.id
    state_data = await state.get_data()
    target_user_id = state_data.get('target_user_id')
    original_message_id = state_data.get('original_message_id')
    
    if not target_user_id:
        await message.answer("Error: No target user selected.")
        await state.clear()
        return
    
    message_text = message.text.strip()
    
    if message_text.lower() == "/cancel":
        await message.answer("Contact request cancelled.", reply_markup=ReplyKeyboardRemove())
        await state.clear()
        return
    
    if message_text.lower() == "/skip":
        message_text = ""
    
    conn = db
    try:
        # Get a valid confession where both users have interacted
        async with conn.execute("""
            SELECT DISTINCT c.id 
            FROM confessions c 
            JOIN comments com ON c.id = com.confession_id 
            WHERE (c.user_id = ? AND com.user_id = ?) 
               OR (c.user_id = ? AND com.user_id = ?)
            LIMIT 1
        """, (user_id, target_user_id, target_user_id, user_id)) as cursor:
            confession_data = await cursor.fetchone()
        
        confession_id = confession_data['id'] if confession_data else 0
        
        # Get a comment ID where they interacted
        comment_id = 0
        if confession_id > 0:
            async with conn.execute("""
                SELECT id FROM comments 
                WHERE confession_id = ? AND (user_id = ? OR user_id = ?)
                LIMIT 1
            """, (confession_id, user_id, target_user_id)) as cursor:
                comment_data = await cursor.fetchone()
            
            comment_id = comment_data['id'] if comment_data else 0
        
        # Create contact request
        await conn.execute("""
            INSERT INTO contact_requests 
            (confession_id, comment_id, requester_user_id, requested_user_id, status, message) 
            VALUES (?, ?, ?, ?, 'pending', ?)
        """, (confession_id, comment_id, user_id, target_user_id, message_text))
        
        await conn.commit()
        
        # Get requester profile name
        requester_profile = await get_profile_name(user_id)
        
        # Notify target user
        try:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Approve", callback_data=f"approve_contact_{user_id}")],
                [InlineKeyboardButton(text="❌ Reject", callback_data=f"reject_contact_{user_id}")]
            ])
            
            notification_text = f"🤝 <b>New Contact Request</b>\n\n"
            notification_text += f"<b>From:</b> {requester_profile}\n"
            notification_text += f"<b>User ID:</b> <code>{user_id}</code>\n\n"
            
            if message_text:
                notification_text += f"<b>Message:</b>\n{html.quote(message_text)}\n\n"
            
            notification_text += "<i>You can approve or reject this contact request.</i>"
            
            await bot.send_message(target_user_id, notification_text, reply_markup=keyboard)
        except Exception as e:
            logger.warning(f"Could not notify user {target_user_id} about contact request: {e}")
        
        await message.answer(
            f"✅ Contact request sent to user!\n\n"
            f"<i>They will be notified and can approve or reject your request.</i>"
        )
        
        # Update original message if possible
        try:
            await bot.edit_message_text(
                chat_id=user_id,
                message_id=original_message_id,
                text="✅ Contact request sent!",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Back to Profile", callback_data="profile_main")]
                ])
            )
        except:
            pass
        
    except Exception as e:
        logger.error(f"Error creating contact request: {e}")
        await message.answer("❌ Error sending contact request. Please try again.")
    
    finally:
        await state.clear()

@dp.callback_query(F.data.startswith("approve_contact_"))
async def approve_contact_request(callback_query: types.CallbackQuery):
    """Approve a contact request"""
    try:
        requester_user_id = int(callback_query.data.split("_")[-1])
        approver_user_id = callback_query.from_user.id
    except ValueError:
        await callback_query.answer("Invalid request.", show_alert=True)
        return
    
    conn = db
    try:
        # Update contact request status
        await conn.execute("""
            UPDATE contact_requests 
            SET status = 'approved' 
            WHERE requester_user_id = ? AND requested_user_id = ? AND status = 'pending'
        """, (requester_user_id, approver_user_id))
        
        # Create active chat
        await conn.execute("""
            INSERT INTO active_chats (user1_id, user2_id, started_by)
            VALUES (?, ?, ?)
        """, (requester_user_id, approver_user_id, requester_user_id))
        
        await conn.commit()
        
        # Get profile names
        requester_profile = await get_profile_name(requester_user_id)
        approver_profile = await get_profile_name(approver_user_id)
        
        # Notify requester
        try:
            # Get chat ID
            async with conn.execute("""
                SELECT id FROM active_chats 
                WHERE user1_id = ? AND user2_id = ? 
                ORDER BY id DESC LIMIT 1
            """, (requester_user_id, approver_user_id)) as cursor:
                chat_data = await cursor.fetchone()
            
            if chat_data:
                chat_id = chat_data['id']
                await bot.send_message(
                    requester_user_id,
                    f"✅ <b>Contact Request Approved!</b>\n\n"
                    f"Your contact request to {approver_profile} has been approved!\n\n"
                    f"<i>You can now start chatting with them.</i>",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="💬 Start Chat", callback_data=f"view_chat_{chat_id}")]
                    ])
                )
        except Exception as e:
            logger.warning(f"Could not notify requester {requester_user_id}: {e}")
        
        # Update approver's message
        await callback_query.message.edit_text(
            f"✅ <b>Contact Request Approved</b>\n\n"
            f"You have approved the contact request from {requester_profile}.\n\n"
            f"<i>A new chat has been created. You can find it in your active chats.</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💬 Go to Chats", callback_data="my_active_chats")]
            ])
        )
        await callback_query.answer("Contact request approved!")
        
    except Exception as e:
        logger.error(f"Error approving contact request: {e}")
        await callback_query.answer("Error approving request.", show_alert=True)

@dp.callback_query(F.data.startswith("reject_contact_"))
async def reject_contact_request(callback_query: types.CallbackQuery):
    """Reject a contact request"""
    try:
        requester_user_id = int(callback_query.data.split("_")[-1])
        rejecter_user_id = callback_query.from_user.id
    except ValueError:
        await callback_query.answer("Invalid request.", show_alert=True)
        return
    
    conn = db
    try:
        # Update contact request status
        await conn.execute("""
            UPDATE contact_requests 
            SET status = 'rejected' 
            WHERE requester_user_id = ? AND requested_user_id = ? AND status = 'pending'
        """, (requester_user_id, rejecter_user_id))
        
        await conn.commit()
        
        # Get profile names
        requester_profile = await get_profile_name(requester_user_id)
        
        # Notify requester
        try:
            await bot.send_message(
                requester_user_id,
                f"❌ <b>Contact Request Rejected</b>\n\n"
                f"Your contact request to {await get_profile_name(rejecter_user_id)} has been rejected.\n\n"
                f"<i>You can request contact with other users.</i>"
            )
        except Exception as e:
            logger.warning(f"Could not notify requester {requester_user_id}: {e}")
        
        # Update rejecter's message
        await callback_query.message.edit_text(
            f"❌ <b>Contact Request Rejected</b>\n\n"
            f"You have rejected the contact request from {requester_profile}.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Back", callback_data="pending_contact_requests")]
            ])
        )
        await callback_query.answer("Contact request rejected!")
        
    except Exception as e:
        logger.error(f"Error rejecting contact request: {e}")
        await callback_query.answer("Error rejecting request.", show_alert=True)

@dp.callback_query(F.data == "pending_contact_requests")
async def show_pending_contact_requests(callback_query: types.CallbackQuery):
    """Show pending contact requests for the user"""
    user_id = callback_query.from_user.id
    conn = db
    
    async with conn.execute("""
        SELECT cr.id, cr.requester_user_id, cr.message, cr.created_at, us.profile_name
        FROM contact_requests cr
        LEFT JOIN user_status us ON cr.requester_user_id = us.user_id
        WHERE cr.requested_user_id = ? AND cr.status = 'pending'
        ORDER BY cr.created_at DESC
    """, (user_id,)) as cursor:
        requests = await cursor.fetchall()
    
    if not requests:
        await callback_query.message.edit_text(
            "📨 <b>Pending Contact Requests</b>\n\n"
            "You have no pending contact requests.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Back to Profile", callback_data="profile_main")]
            ])
        )
        await callback_query.answer()
        return
    
    response_text = "📨 <b>Pending Contact Requests</b>\n\n"
    keyboard = InlineKeyboardBuilder()
    
    for req in requests:
        profile_name = req['profile_name'] or "Anonymous"
        message = req['message'] or "No message"
        time_str = req['created_at'][:19] if req['created_at'] else ""
        
        response_text += f"👤 <b>{profile_name}</b>\n"
        response_text += f"<i>Message:</i> {html.quote(message[:50])}{'...' if len(message) > 50 else ''}\n"
        response_text += f"<i>Time:</i> {time_str}\n\n"
        
        keyboard.row(
            InlineKeyboardButton(text=f"✅ Approve {profile_name[:10]}...", callback_data=f"approve_contact_{req['requester_user_id']}"),
            InlineKeyboardButton(text=f"❌ Reject", callback_data=f"reject_contact_{req['requester_user_id']}")
        )
    
    keyboard.row(InlineKeyboardButton(text="⬅️ Back to Profile", callback_data="profile_main"))
    
    await callback_query.message.edit_text(response_text, reply_markup=keyboard.as_markup())
    await callback_query.answer()

@dp.callback_query(F.data.startswith("start_chat_"))
async def start_chat_from_profile(callback_query: types.CallbackQuery, state: FSMContext):
    """Start a chat from user profile (after approved contact request)"""
    try:
        target_user_id = int(callback_query.data.split("_")[-1])
        user_id = callback_query.from_user.id
    except ValueError:
        await callback_query.answer("Invalid user ID.", show_alert=True)
        return
    
    conn = db
    try:
        # Check if chat already exists
        async with conn.execute("""
            SELECT id FROM active_chats 
            WHERE ((user1_id = ? AND user2_id = ?) OR (user1_id = ? AND user2_id = ?)) 
            AND is_active = 1
        """, (user_id, target_user_id, target_user_id, user_id)) as cursor:
            existing_chat = await cursor.fetchone()
        
        if existing_chat:
            # Go to existing chat
            await callback_query.answer("Redirecting to existing chat...")
            callback_query.data = f"view_chat_{existing_chat['id']}"
            await view_chat_messages(callback_query, state)
            return
        
        # Create new chat
        await conn.execute("""
            INSERT INTO active_chats (user1_id, user2_id, started_by)
            VALUES (?, ?, ?)
        """, (user_id, target_user_id, user_id))
        
        await conn.commit()
        
        # Get chat ID
        async with conn.execute("""
            SELECT id FROM active_chats 
            WHERE user1_id = ? AND user2_id = ? 
            ORDER BY id DESC LIMIT 1
        """, (user_id, target_user_id)) as cursor:
            chat_data = await cursor.fetchone()
        
        if chat_data:
            chat_id = chat_data['id']
            target_profile = await get_profile_name(target_user_id)
            
            # Notify target user
            try:
                await bot.send_message(
                    target_user_id,
                    f"💬 <b>New Chat Started</b>\n\n"
                    f"{await get_profile_name(user_id)} has started a chat with you!",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="💬 Go to Chat", callback_data=f"view_chat_{chat_id}")]
                    ])
                )
            except Exception as e:
                logger.warning(f"Could not notify user {target_user_id} about new chat: {e}")
            
            await callback_query.answer("Chat started!")
            
            # Redirect to chat view
            callback_query.data = f"view_chat_{chat_id}"
            await view_chat_messages(callback_query, state)
            
    except Exception as e:
        logger.error(f"Error starting chat: {e}")
        await callback_query.answer("Error starting chat.", show_alert=True)

# --- COMMENT HANDLERS ---

@dp.callback_query(F.data.startswith("add_"))
async def add_comment_start(callback_query: types.CallbackQuery, state: FSMContext):
    """Start adding a comment to a confession"""
    try:
        confession_id = int(callback_query.data.split("_")[1])
    except (ValueError, IndexError):
        await callback_query.answer("Invalid confession ID.", show_alert=True)
        return
    
    # Check if confession exists and is approved
    conn = db
    async with conn.execute("SELECT status FROM confessions WHERE id = ?", (confession_id,)) as cursor:
        conf_data = await cursor.fetchone()
    
    if not conf_data or conf_data['status'] != 'approved':
        await callback_query.answer("This confession is not available for comments.", show_alert=True)
        return
    
    await state.update_data(confession_id=confession_id, parent_comment_id=None)
    await state.set_state(CommentForm.waiting_for_comment)
    
    await callback_query.message.answer(
        "💬 <b>Add a Comment</b>\n\n"
        "Send your comment text, sticker, or GIF.\n"
        "Comments are anonymous (shown with your display name).\n\n"
        "<i>Type /cancel to abort.</i>"
    )
    await callback_query.answer()

@dp.callback_query(F.data.startswith("reply_"))
async def reply_comment_start(callback_query: types.CallbackQuery, state: FSMContext):
    """Start replying to a comment"""
    try:
        comment_id = int(callback_query.data.split("_")[1])
    except (ValueError, IndexError):
        await callback_query.answer("Invalid comment ID.", show_alert=True)
        return
    
    # Get confession ID from the comment
    conn = db
    async with conn.execute("SELECT confession_id FROM comments WHERE id = ?", (comment_id,)) as cursor:
        comment_data = await cursor.fetchone()
    
    if not comment_data:
        await callback_query.answer("Comment not found.", show_alert=True)
        return
    
    confession_id = comment_data['confession_id']
    
    # Check if confession is approved
    async with conn.execute("SELECT status FROM confessions WHERE id = ?", (confession_id,)) as cursor:
        conf_data = await cursor.fetchone()
    
    if not conf_data or conf_data['status'] != 'approved':
        await callback_query.answer("This confession is not available for comments.", show_alert=True)
        return
    
    await state.update_data(confession_id=confession_id, parent_comment_id=comment_id)
    await state.set_state(CommentForm.waiting_for_comment)
    
    await callback_query.message.answer(
        "↪️ <b>Reply to Comment</b>\n\n"
        "Send your reply text, sticker, or GIF.\n"
        "Replies are anonymous (shown with your display name).\n\n"
        "<i>Type /cancel to abort.</i>"
    )
    await callback_query.answer()

@dp.message(CommentForm.waiting_for_comment, F.text)
async def receive_comment_text(message: types.Message, state: FSMContext):
    """Receive text comment"""
    if message.text.startswith('/'):
        return
    
    await process_comment(message, state, text=message.text, sticker_file_id=None, animation_file_id=None)

@dp.message(CommentForm.waiting_for_comment, F.sticker)
async def receive_comment_sticker(message: types.Message, state: FSMContext):
    """Receive sticker comment"""
    await process_comment(message, state, text=None, sticker_file_id=message.sticker.file_id, animation_file_id=None)

@dp.message(CommentForm.waiting_for_comment, F.animation)
async def receive_comment_gif(message: types.Message, state: FSMContext):
    """Receive GIF comment"""
    await process_comment(message, state, text=None, sticker_file_id=None, animation_file_id=message.animation.file_id)

async def process_comment(message: types.Message, state: FSMContext, text: Optional[str] = None, 
                         sticker_file_id: Optional[str] = None, animation_file_id: Optional[str] = None):
    """Process and save comment"""
    user_id = message.from_user.id
    state_data = await state.get_data()
    confession_id = state_data.get('confession_id')
    parent_comment_id = state_data.get('parent_comment_id')
    
    if not confession_id:
        await message.answer("Error: No confession selected. Please try again.")
        await state.clear()
        return
    
    # Validate content
    if not text and not sticker_file_id and not animation_file_id:
        await message.answer("Please send text, sticker, or GIF.")
        return
    
    if text and len(text) > 2000:
        await message.answer("Comment too long (max 2000 characters).")
        return
    
    try:
        conn = db
        # Insert comment
        await conn.execute("""
            INSERT INTO comments (confession_id, user_id, text, sticker_file_id, animation_file_id, parent_comment_id)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (confession_id, user_id, text, sticker_file_id, animation_file_id, parent_comment_id))
        
        # Update user points for commenting
        await update_user_points(conn, user_id, 1)
        await conn.commit()
        
        # Get user's profile name
        profile_name = await get_profile_name(user_id)
        
        # Notify confession owner if it's not their own comment
        async with conn.execute("SELECT user_id FROM confessions WHERE id = ?", (confession_id,)) as cursor:
            conf_owner = await cursor.fetchone()
        
        if conf_owner and conf_owner['user_id'] != user_id:
            await safe_send_message(
                conf_owner['user_id'],
                f"💬 <b>New comment on your confession #{confession_id}</b>\n\n"
                f"<i>Someone commented on your confession.</i>\n"
                f"Use this link to view: https://t.me/{bot_info.username}?start=view_{confession_id}"
            )
        
        # Update channel button with new comment count
        await update_channel_post_button(confession_id)
        
        await message.answer(
            f"✅ <b>Comment posted!</b>\n\n"
            f"Posted as: <b>{profile_name}</b>\n"
            f"On confession: <b>#{confession_id}</b>\n\n"
            f"<i>View all comments: https://t.me/{bot_info.username}?start=view_{confession_id}</i>"
        )
        
        logger.info(f"Comment added to confession #{confession_id} by user {user_id}")
        
    except Exception as e:
        logger.error(f"Error adding comment to confession #{confession_id}: {e}", exc_info=True)
        await message.answer("❌ Error posting comment. Please try again.")
    
    finally:
        await state.clear()

@dp.callback_query(F.data.startswith("react_like_"))
async def react_like(callback_query: types.CallbackQuery):
    """Handle like reaction"""
    await handle_reaction(callback_query, "like")

@dp.callback_query(F.data.startswith("react_dislike_"))
async def react_dislike(callback_query: types.CallbackQuery):
    """Handle dislike reaction"""
    await handle_reaction(callback_query, "dislike")

async def handle_reaction(callback_query: types.CallbackQuery, reaction_type: str):
    """Process reaction to a comment"""
    try:
        comment_id = int(callback_query.data.split("_")[-1])
        user_id = callback_query.from_user.id
    except (ValueError, IndexError):
        await callback_query.answer("Invalid reaction.", show_alert=True)
        return
    
    conn = db
    try:
        # Get comment owner first
        async with conn.execute("SELECT user_id FROM comments WHERE id = ?", (comment_id,)) as cursor:
            comment_data = await cursor.fetchone()
        
        if not comment_data:
            await callback_query.answer("Comment not found.", show_alert=True)
            return
        
        comment_owner_id = comment_data['user_id']
        
        # Check if user is trying to react to their own comment
        if comment_owner_id == user_id:
            await callback_query.answer("You cannot react to your own comment.", show_alert=True)
            return
        
        # Check if user already reacted
        async with conn.execute(
            "SELECT reaction_type FROM reactions WHERE comment_id = ? AND user_id = ?",
            (comment_id, user_id)
        ) as cursor:
            existing_reaction = await cursor.fetchone()
        
        if existing_reaction:
            if existing_reaction['reaction_type'] == reaction_type:
                # Remove reaction if clicking same button
                await conn.execute(
                    "DELETE FROM reactions WHERE comment_id = ? AND user_id = ?",
                    (comment_id, user_id)
                )
                # Remove points from comment owner
                points_change = POINTS_PER_DISLIKE_RECEIVED if reaction_type == "dislike" else POINTS_PER_LIKE_RECEIVED
                await update_user_points(conn, comment_owner_id, -points_change)
            else:
                # Change reaction type
                # First remove old points
                old_points = POINTS_PER_DISLIKE_RECEIVED if existing_reaction['reaction_type'] == "dislike" else POINTS_PER_LIKE_RECEIVED
                await update_user_points(conn, comment_owner_id, -old_points)
                
                # Update reaction
                await conn.execute("""
                    UPDATE reactions SET reaction_type = ? WHERE comment_id = ? AND user_id = ?
                """, (reaction_type, comment_id, user_id))
                
                # Add new points
                new_points = POINTS_PER_DISLIKE_RECEIVED if reaction_type == "dislike" else POINTS_PER_LIKE_RECEIVED
                await update_user_points(conn, comment_owner_id, new_points)
        else:
            # Add new reaction
            await conn.execute("""
                INSERT INTO reactions (comment_id, user_id, reaction_type) VALUES (?, ?, ?)
            """, (comment_id, user_id, reaction_type))
            
            # Add points to comment owner
            points_change = POINTS_PER_DISLIKE_RECEIVED if reaction_type == "dislike" else POINTS_PER_LIKE_RECEIVED
            await update_user_points(conn, comment_owner_id, points_change)
        
        await conn.commit()
        
        # Update the message with new reaction counts
        likes, dislikes = await get_comment_reactions(comment_id)
        
        # Get message to edit
        message = callback_query.message
        if message:
            try:
                # Extract current keyboard and update reaction counts
                keyboard = message.reply_markup
                if keyboard:
                    inline_keyboard = keyboard.inline_keyboard
                    new_inline_keyboard = []
                    
                    for row in inline_keyboard:
                        new_row = []
                        for button in row:
                            text = button.text
                            callback_data = button.callback_data
                            
                            if callback_data == f"react_like_{comment_id}":
                                text = f"👍 {likes}"
                            elif callback_data == f"react_dislike_{comment_id}":
                                text = f"👎 {dislikes}"
                            
                            new_row.append(InlineKeyboardButton(text=text, callback_data=callback_data))
                        new_inline_keyboard.append(new_row)
                    
                    new_keyboard = InlineKeyboardMarkup(inline_keyboard=new_inline_keyboard)
                    
                    # Try to edit the message
                    try:
                        await message.edit_reply_markup(reply_markup=new_keyboard)
                    except TelegramBadRequest as e:
                        if "message is not modified" not in str(e):
                            raise
                
                reaction_emoji = "👍" if reaction_type == "like" else "👎"
                action = "added" if not existing_reaction else "changed to" if existing_reaction['reaction_type'] != reaction_type else "removed"
                await callback_query.answer(f"{reaction_emoji} reaction {action}!")
                
            except Exception as e:
                logger.error(f"Error updating reaction UI for comment {comment_id}: {e}")
                await callback_query.answer(f"Reaction {reaction_type}d!")
        
    except Exception as e:
        logger.error(f"Error processing reaction for comment {comment_id}: {e}", exc_info=True)
        await callback_query.answer("Error processing reaction.", show_alert=True)

@dp.callback_query(F.data.startswith("browse_"))
async def browse_comments(callback_query: types.CallbackQuery):
    """Browse comments for a confession"""
    try:
        confession_id = int(callback_query.data.split("_")[1])
    except (ValueError, IndexError):
        await callback_query.answer("Invalid confession ID.", show_alert=True)
        return
    
    await show_comments_for_confession(callback_query.from_user.id, confession_id)
    await callback_query.answer()

@dp.callback_query(F.data.startswith("comments_page_"))
async def browse_comments_page(callback_query: types.CallbackQuery):
    """Browse comments pagination"""
    try:
        _, _, confession_id_str, page_str = callback_query.data.split("_")
        confession_id = int(confession_id_str)
        page = int(page_str)
    except (ValueError, IndexError):
        await callback_query.answer("Invalid page.", show_alert=True)
        return
    
    await show_comments_for_confession(callback_query.from_user.id, confession_id, callback_query.message, page)
    await callback_query.answer()

@dp.callback_query(F.data == "noop")
async def noop_handler(callback_query: types.CallbackQuery):
    """Handle no-operation callbacks"""
    await callback_query.answer()

# --- Confession Submission Flow with Photo Support ---
@dp.message(Command("confess"), StateFilter(None))
async def start_confession(message: types.Message, state: FSMContext):
    # Check if user has accepted rules first
    user_id = message.from_user.id
    conn = db
    async with conn.execute("SELECT has_accepted_rules FROM user_status WHERE user_id = ?", (user_id,)) as cursor:
        has_accepted_row = await cursor.fetchone()
        has_accepted = has_accepted_row['has_accepted_rules'] if has_accepted_row else 0
    
    if not has_accepted:
        await message.answer("⚠️ Please use /start first to accept the rules before submitting confessions.")
        return
    
    await state.clear()
    await state.update_data(selected_categories=[])
    await message.answer(
        f"📝 <b>Confession Submission</b>\n\n"
        f"Please choose 1 to {MAX_CATEGORIES} categories. Click 'Done Selecting' when finished.\n\n"
        f"<i>After selecting categories, you can submit text-only or text with a photo.</i>",
        reply_markup=create_category_keyboard([])
    )
    await state.set_state(ConfessionForm.selecting_categories)

@dp.callback_query(StateFilter(ConfessionForm.selecting_categories), F.data.startswith("category_"))
async def handle_category_selection(callback_query: types.CallbackQuery, state: FSMContext):
    action = callback_query.data.split("_", 1)[1]
    user_data = await state.get_data()
    selected_categories: List[str] = user_data.get("selected_categories", [])
    
    if action == "cancel":
        await state.clear()
        await callback_query.message.edit_text("Confession submission cancelled.", reply_markup=None)
        await callback_query.answer()
        return
    
    if action == "done":
        if not selected_categories:
            await callback_query.answer("Please select at least 1 category.", show_alert=True)
            return
        if len(selected_categories) > MAX_CATEGORIES:
            await callback_query.answer(f"Too many categories (max {MAX_CATEGORIES}).", show_alert=True)
            return
        
        await state.set_state(ConfessionForm.waiting_for_text)
        category_tags = " ".join([f"#{html.quote(cat)}" for cat in selected_categories])
        
        await callback_query.message.edit_text(
            f"✅ <b>Categories selected:</b> {category_tags}\n\n"
            f"📝 <b>Now, send your confession:</b>\n\n"
            f"• Text only: Send your confession text (min 10 chars, max 3900 chars)\n"
            f"• Text with photo: Send a photo with caption (photo max {MAX_PHOTO_SIZE_MB}MB)\n\n"
            f"<i>Type /cancel to abort.</i>"
        )
        await callback_query.answer()
        return
    
    category = action
    if category in CATEGORIES:
        if category in selected_categories:
            selected_categories.remove(category)
        elif len(selected_categories) < MAX_CATEGORIES:
            selected_categories.append(category)
        else:
            await callback_query.answer(f"You can only select up to {MAX_CATEGORIES} categories.", show_alert=True)
            return
        
        await state.update_data(selected_categories=selected_categories)
        await callback_query.message.edit_reply_markup(reply_markup=create_category_keyboard(selected_categories))
        await callback_query.answer(f"'{category}' {'selected' if category in selected_categories else 'deselected'}.")

# Handle text-only confession with rate limiting
@dp.message(ConfessionForm.waiting_for_text, F.text)
async def receive_text_confession(message: types.Message, state: FSMContext):
    # Check if it's a command
    if message.text.startswith('/'):
        return
    
    # Check rate limit
    if not await check_rate_limit(message.from_user.id):
        await message.answer(f"⏳ Please wait {RATE_LIMIT_SECONDS} seconds between submissions.")
        return
    
    await process_confession(message, state, text=message.text, photo_file_id=None)

# Handle photo with caption confession with accurate size check
@dp.message(ConfessionForm.waiting_for_text, F.photo)
async def receive_photo_confession(message: types.Message, state: FSMContext):
    # Check rate limit
    if not await check_rate_limit(message.from_user.id):
        await message.answer(f"⏳ Please wait {RATE_LIMIT_SECONDS} seconds between submissions.")
        return
    
    # Get actual file size
    photo_file_id = message.photo[-1].file_id
    text = message.caption or ""
    
    if not text.strip():
        await message.answer("❌ Please add a caption to your photo. The caption is your confession text.")
        return
    
    # Check file size (approximate but better)
    file_size_mb = message.photo[-1].file_size / (1024 * 1024) if message.photo[-1].file_size else 0
    
    if file_size_mb > MAX_PHOTO_SIZE_MB:
        await message.answer(f"❌ Photo is too large ({file_size_mb:.1f}MB). Maximum size is {MAX_PHOTO_SIZE_MB}MB.")
        return
    
    await process_confession(message, state, text=text, photo_file_id=photo_file_id)

async def process_confession(message: types.Message, state: FSMContext, text: str, photo_file_id: Optional[str] = None):
    user_id = message.from_user.id
    state_data = await state.get_data()
    selected_categories: List[str] = state_data.get("selected_categories", [])
    
    if not selected_categories:
        await message.answer("⚠️ Error: Category info lost. Please start again with /confess.")
        await state.clear()
        return
    
    if len(text) < 10:
        await message.answer("❌ Confession too short (minimum 10 characters).")
        return
    
    if len(text) > 3900:
        await message.answer(f"❌ Confession too long (maximum 3900 characters).")
        return
    
    try:
        conn = db
        async with conn.execute("""
            INSERT INTO confessions (text, user_id, categories, status, photo_file_id) 
            VALUES (?, ?, ?, 'pending', ?)
        """, (text, user_id, ",".join(selected_categories), photo_file_id)) as cursor:
            conf_id = cursor.lastrowid
        
        if not conf_id:
            raise Exception("Failed to get confession ID")
        
        await update_user_points(conn, user_id, POINTS_PER_CONFESSION)
        await conn.commit()
        
        # Prepare confession preview for admin
        category_tags = " ".join([f"#{html.quote(cat)}" for cat in selected_categories])
        
        if photo_file_id:
            # Send photo with caption to admin
            admin_caption = (
                f"🖼️ <b>New Photo Confession Review</b>\n"
                f"<b>ID:</b> {conf_id}\n"
                f"<b>Categories:</b> {category_tags}\n"
                f"<b>User ID:</b> <code>{user_id}</code>\n\n"
                f"<b>Caption:</b>\n{html.quote(text)}"
            )
            
            # Create admin keyboard
            admin_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Approve", callback_data=f"approve_{conf_id}"),
                 InlineKeyboardButton(text="❌ Reject", callback_data=f"reject_{conf_id}")]
            ])
            
            # Send to all admins
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_photo(
                        chat_id=admin_id,
                        photo=photo_file_id,
                        caption=admin_caption,
                        reply_markup=admin_keyboard
                    )
                except Exception as e:
                    logger.warning(f"Could not send photo confession to admin {admin_id}: {e}")
        else:
            # Text-only confession
            admin_msg_text = (
                f"📝 <b>New Confession Review</b>\n"
                f"<b>ID:</b> {conf_id}\n"
                f"<b>Categories:</b> {category_tags}\n"
                f"<b>User ID:</b> <code>{user_id}</code>\n\n"
                f"<b>Text:</b>\n{html.quote(text)}"
            )
            
            admin_keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Approve", callback_data=f"approve_{conf_id}"),
                 InlineKeyboardButton(text="❌ Reject", callback_data=f"reject_{conf_id}")]
            ])
            
            # Send to all admins
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_message(admin_id, admin_msg_text, reply_markup=admin_keyboard)
                except Exception as e:
                    logger.warning(f"Could not send text confession to admin {admin_id}: {e}")
        
        # Notify user
        if photo_file_id:
            await message.answer(
                f"✅ <b>Your photo confession has been submitted!</b>\n\n"
                f"<b>Confession ID:</b> #{conf_id}\n"
                f"<b>Categories:</b> {category_tags}\n\n"
                f"<i>An admin will review it shortly. You'll be notified when it's approved.</i>"
            )
        else:
            await message.answer(
                f"✅ <b>Your confession has been submitted!</b>\n\n"
                f"<b>Confession ID:</b> #{conf_id}\n"
                f"<b>Categories:</b> {category_tags}\n\n"
                f"<i>An admin will review it shortly. You'll be notified when it's approved.</i>"
            )
        
        logger.info(f"Confession #{conf_id} (Photo: {bool(photo_file_id)}) submitted by User ID {user_id}")
        
    except Exception as e:
        logger.error(f"Error processing confession from {user_id}: {e}", exc_info=True)
        await message.answer("❌ An internal error occurred. Please try again.")
    
    finally:
        await state.clear()

# --- Admin Action Handlers ---
@dp.callback_query(lambda c: c.data.startswith(("approve_", "reject_")) and len(c.data.split("_")) == 2 and c.data.split("_")[1].isdigit())
async def admin_action(callback_query: types.CallbackQuery, state: FSMContext):
    if not await is_admin(callback_query.from_user.id):
        await callback_query.answer("Unauthorized.", show_alert=True)
        return
    
    action, conf_id_str = callback_query.data.split("_", 1)
    conf_id = int(conf_id_str)
    
    conn = db
    async with conn.execute("SELECT id, text, user_id, categories, status, photo_file_id FROM confessions WHERE id = ?", (conf_id,)) as cursor:
        conf = await cursor.fetchone()
    
    if not conf:
        await callback_query.answer("Confession not found.", show_alert=True)
        return
    
    if conf['status'] != 'pending':
        await callback_query.answer(f"Already '{conf['status']}'.", show_alert=True)
        return

    if action == "approve":
        try:
            link = f"https://t.me/{bot_info.username}?start=view_{conf['id']}"
            categories = conf['categories'].split(",") if conf['categories'] else []
            category_tags = " ".join([f"#{html.quote(cat)}" for cat in categories])
            
            if conf['photo_file_id']:
                # Post photo confession to channel
                channel_caption = f"<b>Confession #{conf['id']}</b>\n\n{html.quote(conf['text'])}\n\n{category_tags}"
                channel_kbd = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 View / Add Comments (0)", url=link)]])
                
                msg = await bot.send_photo(
                    chat_id=CHANNEL_ID,
                    photo=conf['photo_file_id'],
                    caption=channel_caption,
                    reply_markup=channel_kbd
                )
            else:
                # Post text confession to channel
                channel_post_text = f"<b>Confession #{conf['id']}</b>\n\n{html.quote(conf['text'])}\n\n{category_tags}"
                channel_kbd = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💬 View / Add Comments (0)", url=link)]])
                
                msg = await bot.send_message(CHANNEL_ID, channel_post_text, reply_markup=channel_kbd)
            
            # Update database
            await conn.execute("UPDATE confessions SET status = 'approved', message_id = ? WHERE id = ?", (msg.message_id, conf_id))
            await conn.commit()
            
            # Notify user
            await safe_send_message(conf['user_id'], f"✅ Your confession (#{conf_id}) has been approved and posted!")
            
            # Update admin message
            try:
                await callback_query.message.edit_text(callback_query.message.html_text + "\n\n-- ✅ Approved --", reply_markup=None)
            except:
                pass
            
            await callback_query.answer(f"Confession #{conf_id} approved.")
            
        except Exception as e:
            logger.error(f"Error approving Confession {conf_id}: {e}", exc_info=True)
            await callback_query.answer(f"Error: {e}", show_alert=True)
    
    elif action == "reject":
        await state.update_data(
            rejecting_conf_id=conf_id,
            original_admin_text=callback_query.message.html_text,
            admin_review_message_id=callback_query.message.message_id
        )
        await state.set_state(AdminActions.waiting_for_rejection_reason)
        
        reason_keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="/skip")],
                [KeyboardButton(text="/cancel")]
            ],
            resize_keyboard=True,
            one_time_keyboard=True
        )
        
        await callback_query.answer("❓ Provide rejection reason")
        await bot.send_message(
            callback_query.from_user.id,
            f"Reason for rejecting Confession #{conf_id}?\nUse /skip or /cancel.",
            reply_markup=reason_keyboard
        )

@dp.message(AdminActions.waiting_for_rejection_reason, F.text)
async def receive_rejection_reason(message: types.Message, state: FSMContext):
    """Process confession rejection with reason"""
    if not await is_admin(message.from_user.id):
        await state.clear()
        return
    
    state_data = await state.get_data()
    conf_id = state_data.get('rejecting_conf_id')
    admin_message_id = state_data.get('admin_review_message_id')
    
    if not conf_id:
        await message.answer("Error: No confession selected.")
        await state.clear()
        return
    
    reason = message.text.strip()
    
    if reason.lower() == "/cancel":
        await message.answer("Rejection cancelled.", reply_markup=ReplyKeyboardRemove())
        await state.clear()
        return
    
    if reason.lower() == "/skip":
        reason = "No reason provided"
    
    conn = db
    try:
        # Update confession with rejection
        await conn.execute("""
            UPDATE confessions 
            SET status = 'rejected', rejection_reason = ? 
            WHERE id = ?
        """, (reason, conf_id))
        
        await conn.commit()
        
        # Notify user
        async with conn.execute("SELECT user_id FROM confessions WHERE id = ?", (conf_id,)) as cursor:
            conf_data = await cursor.fetchone()
        
        if conf_data:
            await safe_send_message(
                conf_data['user_id'],
                f"❌ <b>Your confession #{conf_id} was rejected.</b>\n\n"
                f"<b>Reason:</b> {html.quote(reason)}\n\n"
                f"<i>You can submit a new confession with /confess</i>"
            )
        
        # Update the admin's message
        if admin_message_id:
            try:
                await bot.edit_message_text(
                    chat_id=message.from_user.id,
                    message_id=admin_message_id,
                    text=f"❌ <b>Confession #{conf_id} rejected.</b>\nReason: {html.quote(reason)}",
                    reply_markup=None
                )
            except:
                pass
        
        await message.answer(f"✅ Confession #{conf_id} rejected.", reply_markup=ReplyKeyboardRemove())
        
    except Exception as e:
        logger.error(f"Error rejecting confession {conf_id}: {e}", exc_info=True)
        await message.answer(f"❌ Error rejecting confession: {e}", reply_markup=ReplyKeyboardRemove())
    
    finally:
        await state.clear()

# --- Broadcast Command for Admin ---
@dp.message(Command("broadcast"))
async def broadcast_command(message: types.Message, state: FSMContext):
    """Admin broadcast command to send message to all users"""
    user_id = message.from_user.id
    
    if not await is_admin(user_id):
        await message.answer("This command is for admins only.")
        return
    
    # Check if message is a reply
    if not message.reply_to_message:
        await message.answer("Please reply to a message to broadcast it.\n\nExample:\n1. Send your broadcast message\n2. Reply to it with /broadcast")
        return
    
    # Get the message to broadcast
    broadcast_msg = message.reply_to_message
    
    # Confirmation
    confirm_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Yes, Broadcast", callback_data="confirm_broadcast"),
         InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_broadcast")]
    ])
    
    # Create preview text based on message type
    preview_text = ""
    if broadcast_msg.text:
        preview_text = broadcast_msg.text[:200] + ('...' if len(broadcast_msg.text) > 200 else '')
    elif broadcast_msg.caption:
        preview_text = f"[Photo with caption]: {broadcast_msg.caption[:200]}..."
    elif broadcast_msg.photo:
        preview_text = "[Photo message]"
    elif broadcast_msg.video:
        preview_text = "[Video message]"
    elif broadcast_msg.document:
        preview_text = "[Document message]"
    else:
        preview_text = "[Media message]"
    
    await message.answer(
        "⚠️ <b>Confirm Broadcast</b>\n\n"
        f"Are you sure you want to broadcast this message to all users?\n\n"
        f"<i>Message preview:</i>\n"
        f"{preview_text}",
        reply_markup=confirm_keyboard
    )
    
    # Store the message info in state
    await state.update_data(
        broadcast_message_id=broadcast_msg.message_id,
        broadcast_chat_id=broadcast_msg.chat.id,
        broadcast_message_type="reply"
    )

@dp.callback_query(F.data == "confirm_broadcast")
async def confirm_broadcast(callback_query: types.CallbackQuery, state: FSMContext):
    """Confirm and execute broadcast"""
    user_id = callback_query.from_user.id
    
    if not await is_admin(user_id):
        await callback_query.answer("Unauthorized.", show_alert=True)
        return
    
    state_data = await state.get_data()
    message_id = state_data.get('broadcast_message_id')
    chat_id = state_data.get('broadcast_chat_id')
    
    if not message_id or not chat_id:
        await callback_query.answer("Error: Message data not found.", show_alert=True)
        await state.clear()
        return
    
    # Get all user IDs from user_status table
    conn = db
    try:
        async with conn.execute("SELECT DISTINCT user_id FROM user_status WHERE has_accepted_rules = 1 AND is_blocked = 0") as cursor:
            user_rows = await cursor.fetchall()
        
        total_users = len(user_rows)
        successful = 0
        failed = 0
        
        # Send progress message
        progress_msg = await callback_query.message.answer(f"📤 Broadcasting...\n\nSuccess: 0/{total_users}\nFailed: 0/{total_users}")
        
        # Forward the message to all users
        for i, row in enumerate(user_rows):
            target_user_id = row['user_id']
            
            try:
                # Skip if it's an admin (optional)
                if target_user_id in ADMIN_IDS:
                    successful += 1
                    continue
                
                # Forward the message using copy_message which preserves all media types
                await bot.copy_message(
                    chat_id=target_user_id,
                    from_chat_id=chat_id,
                    message_id=message_id
                )
                successful += 1
                
                # Update progress every 10 users
                if i % 10 == 0:
                    try:
                        await progress_msg.edit_text(
                            f"📤 Broadcasting...\n\n"
                            f"Success: {successful}/{total_users}\n"
                            f"Failed: {failed}/{total_users}\n"
                            f"Progress: {((i+1)/total_users*100):.1f}%"
                        )
                    except:
                        pass
                
                # Small delay to avoid rate limits
                await asyncio.sleep(0.05)
                
            except Exception as e:
                logger.warning(f"Failed to send broadcast to user {target_user_id}: {e}")
                failed += 1
        
        # Final report
        await progress_msg.edit_text(
            f"✅ <b>Broadcast Complete!</b>\n\n"
            f"📊 <b>Statistics:</b>\n"
            f"• Total users: {total_users}\n"
            f"• Successful: {successful}\n"
            f"• Failed: {failed}\n"
            f"• Success rate: {(successful/total_users*100 if total_users > 0 else 0):.1f}%"
        )
        
        # Also notify the admin who initiated
        await callback_query.message.edit_text(
            f"✅ Broadcast completed successfully!\n\n"
            f"Sent to {successful} users out of {total_users}.",
            reply_markup=None
        )
        
    except Exception as e:
        logger.error(f"Error during broadcast: {e}", exc_info=True)
        await callback_query.message.edit_text(f"❌ Error during broadcast: {e}")
    
    finally:
        await state.clear()
        await callback_query.answer()

@dp.callback_query(F.data == "cancel_broadcast")
async def cancel_broadcast(callback_query: types.CallbackQuery, state: FSMContext):
    """Cancel broadcast operation"""
    await callback_query.message.edit_text("❌ Broadcast cancelled.")
    await state.clear()
    await callback_query.answer()

# --- Missing Admin Commands Implementation ---

@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    """Admin panel command"""
    user_id = message.from_user.id
    
    if not await is_admin(user_id):
        await message.answer("This command is for admins only.")
        return
    
    admin_text = (
        "👑 <b>Admin Panel</b>\n\n"
        "<b>Available Commands:</b>\n"
        "📊 /stats - Show bot statistics\n"
        "🆔 /id - Get user ID\n"
        "⚠️ /warn - Warn a user\n"
        "⏸️ /block - Temporarily block a user\n"
        "🚫 /pblock - Permanently block a user\n"
        "✅ /unblock - Unblock a user\n"
        "📢 /broadcast - Broadcast message\n\n"
        "<i>Reply to a user's message with most commands.</i>"
    )
    
    await message.answer(admin_text)

@dp.message(Command("stats"))
async def show_stats(message: types.Message):
    """Show bot statistics"""
    user_id = message.from_user.id
    
    if not await is_admin(user_id):
        await message.answer("This command is for admins only.")
        return
    
    conn = db
    try:
        # Get various stats
        async with conn.execute("SELECT COUNT(*) FROM confessions") as cursor:
            total_confessions = (await cursor.fetchone())[0]
        
        async with conn.execute("SELECT COUNT(*) FROM confessions WHERE status = 'approved'") as cursor:
            approved_confessions = (await cursor.fetchone())[0]
        
        async with conn.execute("SELECT COUNT(*) FROM confessions WHERE status = 'pending'") as cursor:
            pending_confessions = (await cursor.fetchone())[0]
        
        async with conn.execute("SELECT COUNT(*) FROM comments") as cursor:
            total_comments = (await cursor.fetchone())[0]
        
        async with conn.execute("SELECT COUNT(*) FROM user_status WHERE has_accepted_rules = 1") as cursor:
            total_users = (await cursor.fetchone())[0]
        
        async with conn.execute("SELECT COUNT(*) FROM user_status WHERE is_blocked = 1") as cursor:
            blocked_users = (await cursor.fetchone())[0]
        
        async with conn.execute("SELECT COUNT(*) FROM active_chats WHERE is_active = 1") as cursor:
            active_chats = (await cursor.fetchone())[0]
        
        # Get recent activity
        today = datetime.now().strftime('%Y-%m-%d')
        async with conn.execute("SELECT COUNT(*) FROM confessions WHERE DATE(created_at) = ?", (today,)) as cursor:
            confessions_today = (await cursor.fetchone())[0]
        
        async with conn.execute("SELECT COUNT(*) FROM comments WHERE DATE(created_at) = ?", (today,)) as cursor:
            comments_today = (await cursor.fetchone())[0]
        
        stats_text = (
            f"📊 <b>Bot Statistics</b>\n\n"
            f"<b>Users:</b>\n"
            f"• Total users: {total_users}\n"
            f"• Blocked users: {blocked_users}\n\n"
            f"<b>Confessions:</b>\n"
            f"• Total: {total_confessions}\n"
            f"• Approved: {approved_confessions}\n"
            f"• Pending: {pending_confessions}\n"
            f"• Today: {confessions_today}\n\n"
            f"<b>Comments:</b>\n"
            f"• Total: {total_comments}\n"
            f"• Today: {comments_today}\n\n"
            f"<b>Chats:</b>\n"
            f"• Active chats: {active_chats}\n\n"
            f"<i>Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M')}</i>"
        )
        
        await message.answer(stats_text)
        
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        await message.answer("❌ Error retrieving statistics.")

@dp.message(Command("warn"))
async def warn_user(message: types.Message):
    """Warn a user"""
    user_id = message.from_user.id
    
    if not await is_admin(user_id):
        await message.answer("This command is for admins only.")
        return
    
    if not message.reply_to_message:
        await message.answer("Please reply to a user's message to warn them.")
        return
    
    target_user_id = message.reply_to_message.from_user.id
    target_name = message.reply_to_message.from_user.full_name
    
    # Extract warning reason from command arguments
    reason = message.text.split(maxsplit=1)[1] if len(message.text.split()) > 1 else "No reason provided"
    
    # Send warning to user
    try:
        await bot.send_message(
            target_user_id,
            f"⚠️ <b>Warning from Admin</b>\n\n"
            f"You have received a warning for violating the rules.\n\n"
            f"<b>Reason:</b> {html.quote(reason)}\n\n"
            f"<i>Continued violations may result in being blocked.</i>"
        )
        await message.answer(f"✅ Warning sent to {target_name} (ID: {target_user_id})")
    except Exception as e:
        logger.error(f"Error warning user {target_user_id}: {e}")
        await message.answer(f"❌ Could not send warning to user. They may have blocked the bot.")

@dp.message(Command("block"))
async def block_user_start(message: types.Message, state: FSMContext):
    """Start temporary block process"""
    user_id = message.from_user.id
    
    if not await is_admin(user_id):
        await message.answer("This command is for admins only.")
        return
    
    if not message.reply_to_message:
        await message.answer("Please reply to a user's message to block them.")
        return
    
    target_user_id = message.reply_to_message.from_user.id
    target_name = message.reply_to_message.from_user.full_name
    
    if target_user_id in ADMIN_IDS:
        await message.answer("❌ Cannot block another admin.")
        return
    
    await state.update_data(
        target_user_id=target_user_id,
        target_name=target_name,
        permanent=False
    )
    await state.set_state(BlockForm.waiting_for_block_duration)
    
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="1 hour"), KeyboardButton(text="1 day")],
            [KeyboardButton(text="3 days"), KeyboardButton(text="1 week")],
            [KeyboardButton(text="/cancel")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    
    await message.answer(
        f"⏰ <b>Temporary Block</b>\n\n"
        f"Blocking user: {target_name} (ID: {target_user_id})\n\n"
        f"Select block duration or send custom duration (e.g., '2 hours', '5 days'):",
        reply_markup=keyboard
    )

@dp.message(Command("pblock"))
async def pblock_user_start(message: types.Message, state: FSMContext):
    """Start permanent block process"""
    user_id = message.from_user.id
    
    if not await is_admin(user_id):
        await message.answer("This command is for admins only.")
        return
    
    if not message.reply_to_message:
        await message.answer("Please reply to a user's message to block them.")
        return
    
    target_user_id = message.reply_to_message.from_user.id
    target_name = message.reply_to_message.from_user.full_name
    
    if target_user_id in ADMIN_IDS:
        await message.answer("❌ Cannot block another admin.")
        return
    
    await state.update_data(
        target_user_id=target_user_id,
        target_name=target_name,
        permanent=True
    )
    await state.set_state(BlockForm.waiting_for_block_reason)
    
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Spamming"), KeyboardButton(text="Harassment")],
            [KeyboardButton(text="Inappropriate content"), KeyboardButton(text="Other")],
            [KeyboardButton(text="/skip"), KeyboardButton(text="/cancel")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    
    await message.answer(
        f"🚫 <b>Permanent Block</b>\n\n"
        f"Blocking user: {target_name} (ID: {target_user_id})\n\n"
        f"Select block reason or type custom reason:",
        reply_markup=keyboard
    )

@dp.message(BlockForm.waiting_for_block_duration, F.text)
async def receive_block_duration(message: types.Message, state: FSMContext):
    """Receive block duration"""
    if message.text == "/cancel":
        await message.answer("Block cancelled.", reply_markup=ReplyKeyboardRemove())
        await state.clear()
        return
    
    state_data = await state.get_data()
    target_user_id = state_data.get('target_user_id')
    target_name = state_data.get('target_name')
    
    duration_text = message.text.strip().lower()
    
    # Parse duration
    duration_map = {
        "1 hour": timedelta(hours=1),
        "1 day": timedelta(days=1),
        "3 days": timedelta(days=3),
        "1 week": timedelta(weeks=1)
    }
    
    if duration_text in duration_map:
        duration = duration_map[duration_text]
    else:
        # Try to parse custom duration
        try:
            if "hour" in duration_text:
                hours = int(''.join(filter(str.isdigit, duration_text)))
                duration = timedelta(hours=hours)
            elif "day" in duration_text:
                days = int(''.join(filter(str.isdigit, duration_text)))
                duration = timedelta(days=days)
            elif "week" in duration_text:
                weeks = int(''.join(filter(str.isdigit, duration_text)))
                duration = timedelta(weeks=weeks)
            else:
                await message.answer("Invalid duration format. Please use like '2 hours' or '3 days'.")
                return
        except:
            await message.answer("Invalid duration format. Please use like '2 hours' or '3 days'.")
            return
    
    await state.update_data(block_duration=duration)
    await state.set_state(BlockForm.waiting_for_block_reason)
    
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Spamming"), KeyboardButton(text="Harassment")],
            [KeyboardButton(text="Inappropriate content"), KeyboardButton(text="Other")],
            [KeyboardButton(text="/skip"), KeyboardButton(text="/cancel")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    
    await message.answer(
        f"⏰ Duration: {duration_text}\n\n"
        f"Now select or type the block reason:",
        reply_markup=keyboard
    )

@dp.message(BlockForm.waiting_for_block_reason, F.text)
async def receive_block_reason(message: types.Message, state: FSMContext):
    """Receive block reason and execute block"""
    if message.text == "/cancel":
        await message.answer("Block cancelled.", reply_markup=ReplyKeyboardRemove())
        await state.clear()
        return
    
    state_data = await state.get_data()
    target_user_id = state_data.get('target_user_id')
    target_name = state_data.get('target_name')
    permanent = state_data.get('permanent', False)
    block_duration = state_data.get('block_duration')
    
    reason = message.text.strip()
    if reason == "/skip":
        reason = "No reason provided"
    
    conn = db
    try:
        if permanent:
            # Permanent block
            blocked_until = None
            await conn.execute("""
                UPDATE user_status 
                SET is_blocked = 1, blocked_until = NULL, block_reason = ?
                WHERE user_id = ?
            """, (reason, target_user_id))
        else:
            # Temporary block
            blocked_until = datetime.now() + block_duration
            await conn.execute("""
                UPDATE user_status 
                SET is_blocked = 1, blocked_until = ?, block_reason = ?
                WHERE user_id = ?
            """, (blocked_until.isoformat(), reason, target_user_id))
        
        await conn.commit()
        
        # Notify user
        try:
            if permanent:
                block_message = (
                    f"🚫 <b>You have been permanently blocked from using this bot.</b>\n\n"
                    f"<b>Reason:</b> {html.quote(reason)}\n\n"
                    f"<i>Contact admins if you believe this is a mistake.</i>"
                )
            else:
                expiry = blocked_until.strftime('%Y-%m-%d %H:%M %Z')
                block_message = (
                    f"⏸️ <b>You have been temporarily blocked from using this bot.</b>\n\n"
                    f"<b>Reason:</b> {html.quote(reason)}\n"
                    f"<b>Block expires:</b> {expiry}\n\n"
                    f"<i>Contact admins if you believe this is a mistake.</i>"
                )
            
            await bot.send_message(target_user_id, block_message)
        except Exception as e:
            logger.warning(f"Could not notify blocked user {target_user_id}: {e}")
        
        # Notify admin
        if permanent:
            await message.answer(
                f"✅ User {target_name} (ID: {target_user_id}) permanently blocked.\n"
                f"Reason: {reason}",
                reply_markup=ReplyKeyboardRemove()
            )
        else:
            expiry = blocked_until.strftime('%Y-%m-%d %H:%M')
            await message.answer(
                f"✅ User {target_name} (ID: {target_user_id}) blocked until {expiry}.\n"
                f"Reason: {reason}",
                reply_markup=ReplyKeyboardRemove()
            )
        
        logger.info(f"User {target_user_id} blocked by admin {message.from_user.id}")
        
    except Exception as e:
        logger.error(f"Error blocking user {target_user_id}: {e}")
        await message.answer(f"❌ Error blocking user: {e}", reply_markup=ReplyKeyboardRemove())
    
    finally:
        await state.clear()

@dp.message(Command("unblock"))
async def unblock_user(message: types.Message):
    """Unblock a user"""
    user_id = message.from_user.id
    
    if not await is_admin(user_id):
        await message.answer("This command is for admins only.")
        return
    
    if not message.reply_to_message:
        await message.answer("Please reply to a user's message to unblock them.")
        return
    
    target_user_id = message.reply_to_message.from_user.id
    target_name = message.reply_to_message.from_user.full_name
    
    conn = db
    try:
        # Check if user is actually blocked
        async with conn.execute("SELECT is_blocked FROM user_status WHERE user_id = ?", (target_user_id,)) as cursor:
            status = await cursor.fetchone()
        
        if not status or not status['is_blocked']:
            await message.answer(f"User {target_name} is not blocked.")
            return
        
        # Unblock user
        await conn.execute("""
            UPDATE user_status 
            SET is_blocked = 0, blocked_until = NULL, block_reason = NULL
            WHERE user_id = ?
        """, (target_user_id,))
        
        await conn.commit()
        
        # Notify user
        try:
            await bot.send_message(
                target_user_id,
                "✅ <b>Your block has been removed.</b>\n\n"
                "<i>You can now use the bot again.</i>"
            )
        except Exception as e:
            logger.warning(f"Could not notify unblocked user {target_user_id}: {e}")
        
        await message.answer(f"✅ User {target_name} (ID: {target_user_id}) has been unblocked.")
        
        logger.info(f"User {target_user_id} unblocked by admin {message.from_user.id}")
        
    except Exception as e:
        logger.error(f"Error unblocking user {target_user_id}: {e}")
        await message.answer(f"❌ Error unblocking user: {e}")

# --- MISSING COMMAND HANDLERS ---

@dp.message(Command("help"))
async def help_command(message: types.Message):
    """Show help information"""
    help_text = (
        "🤖 <b>Confession Bot Help</b>\n\n"
        
        "<b>Main Commands:</b>\n"
        "📝 /confess - Submit an anonymous confession (text or photo)\n"
        "👤 /profile - View and manage your profile\n"
        "📜 /rules - View the bot's rules and regulations\n"
        "🔒 /privacy - View privacy information\n"
        "❌ /cancel - Cancel current action\n"
        "💬 /endchat - End current chat\n\n"
        
        "<b>Profile Features:</b>\n"
        "• View your aura points\n"
        "• Change your display name\n"
        "• See your confessions and comments\n"
        "• Manage active chats\n"
        "• Request contact with other users (click on their name in comments)\n\n"
        
        "<b>How it works:</b>\n"
        "1. Use /start to accept rules\n"
        "2. Submit confessions with /confess (text or photo)\n"
        "3. Admins review and approve\n"
        "4. View approved confessions via links\n"
        "5. Comment on others' confessions\n"
        "6. Click on user names in comments to view their profiles\n"
        "7. Request contact from user profiles\n"
        "8. Earn points for participation\n\n"
        
        "<i>Your display name is shown with your comments. Use /profile to change it.</i>"
    )
    await message.answer(help_text)

@dp.message(Command("rules"))
async def rules_command(message: types.Message):
    """Show rules"""
    rules_text = (
        "<b>📜 Bot Rules & Regulations</b>\n\n"
        "<b>To keep the community safe, respectful, and meaningful, please follow these guidelines when using the bot:</b>\n\n"
        "1.  <b>Stay Relevant:</b> This space is mainly for sharing confessions, experiences, and thoughts.\n\n"
        "2.  <b>Respectful Communication:</b> Sensitive topics (political, religious, cultural, etc.) are allowed but must be discussed with respect.\n\n"
        "3.  <b>No Harmful Content:</b> You may mention names, but at your own risk.\n\n"
        "4.  <b>Names & Responsibility:</b> Do not share personal identifying information about yourself or others.\n\n"
        "5.  <b>Anonymity & Privacy:</b> Don't reveal private details of others without consent.\n\n"
        "6.  <b>Constructive Environment:</b> Keep confessions genuine. Avoid spam, trolling, or repeated submissions.\n\n"
        "<i>Use this space to connect, share, and learn, not to spread misinformation or cause unnecessary drama.</i>"
    )
    await message.answer(rules_text)

@dp.message(Command("privacy"))
async def privacy_command(message: types.Message):
    """Show privacy information"""
    privacy_text = (
        "🔒 <b>Privacy Information</b>\n\n"
        
        "<b>What we store:</b>\n"
        "• Your Telegram User ID\n"
        "• Confessions you submit (anonymous)\n"
        "• Comments you make (with display name)\n"
        "• Your display name preference\n"
        "• Your aura points\n\n"
        
        "<b>What we don't store:</b>\n"
        "• Your phone number\n"
        "• Your profile photo\n"
        "• Your private chats with the bot\n"
        "• Personal identifying information\n\n"
        
        "<b>Your data is:</b>\n"
        "• Anonymous to other users\n"
        "• Only visible to admins when needed\n"
        "• Deletable upon request\n\n"
        
        "<i>By using this bot, you agree to these privacy terms.</i>"
    )
    await message.answer(privacy_text)

@dp.message(Command("cancel"))
async def cancel_command(message: types.Message, state: FSMContext):
    """Cancel any ongoing operation"""
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("No active operation to cancel.")
        return
    
    await state.clear()
    await message.answer("❌ Operation cancelled.", reply_markup=ReplyKeyboardRemove())

@dp.message(Command("endchat"))
async def end_chat_command(message: types.Message, state: FSMContext):
    """End current chat"""
    current_state = await state.get_state()
    if current_state != ChatForm.chatting:
        await message.answer("You are not in a chat.")
        return
    
    state_data = await state.get_data()
    chat_id = state_data.get('chat_id')
    
    if chat_id:
        await state.clear()
        await message.answer(
            "✅ Chat ended.\n\n"
            "You can start a new chat by requesting contact from another user's profile.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Back to Profile", callback_data="profile_main")]
            ])
        )
    else:
        await state.clear()
        await message.answer("Chat ended.")

# --- Start Command ---
@dp.message(Command("start"))
async def start(message: types.Message, state: FSMContext, command: Optional[CommandObject] = None):
    await state.clear()
    user_id = message.from_user.id

    # Check if user has accepted rules
    conn = db
    async with conn.execute("SELECT has_accepted_rules FROM user_status WHERE user_id = ?", (user_id,)) as cursor:
        has_accepted_row = await cursor.fetchone()
        has_accepted = has_accepted_row['has_accepted_rules'] if has_accepted_row else 0

    if not has_accepted:
        rules_text = (
            "<b>📜 Bot Rules & Regulations</b>\n\n"
            "<b>To keep the community safe, respectful, and meaningful, please follow these guidelines when using the bot:</b>\n\n"
            "1.  <b>Stay Relevant:</b> This space is mainly for sharing confessions, experiences, and thoughts.\n\n"
            "2.  <b>Respectful Communication:</b> Sensitive topics (political, religious, cultural, etc.) are allowed but must be discussed with respect.\n\n"
            "3.  <b>No Harmful Content:</b> You may mention names, but at your own risk.\n\n"
            "4.  <b>Names & Responsibility:</b> Do not share personal identifying information about yourself or others.\n\n"
            "5.  <b>Anonymity & Privacy:</b> Don't reveal private details of others without consent.\n\n"
            "6.  <b>Constructive Environment:</b> Keep confessions genuine. Avoid spam, trolling, or repeated submissions.\n\n"
            "<i>Use this space to connect, share, and learn, not to spread misinformation or cause unnecessary drama.</i>"
        )
        accept_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ I Accept the Rules", callback_data="accept_rules")]
        ])
        await message.answer(rules_text, reply_markup=accept_keyboard)
        return

    deep_link_args = command.args if command else None
    if deep_link_args:
        if deep_link_args.startswith("view_"):
            try:
                conf_id = int(deep_link_args.split("_", 1)[1])
                logger.info(f"User {user_id} started via deep link for conf {conf_id}")
                conn = db
                async with conn.execute("""
                    SELECT c.text, c.categories, c.status, c.user_id, c.photo_file_id, COUNT(com.id) as comment_count 
                    FROM confessions c LEFT JOIN comments com ON c.id = com.confession_id
                    WHERE c.id = ? GROUP BY c.id
                """, (conf_id,)) as cursor:
                    conf_data = await cursor.fetchall()
                
                if not conf_data or conf_data[0]['status'] != 'approved':
                    await message.answer(f"Confession #{conf_id} not found or not approved.")
                    return
                
                conf_data = conf_data[0]
                comm_count = conf_data['comment_count']
                categories = conf_data['categories'] or ""
                category_tags = " ".join([f"#{html.quote(cat)}" for cat in categories.split(",")]) if categories else "#Unknown"
                
                # Check if confession has photo
                if conf_data['photo_file_id']:
                    caption = f"<b>Confession #{conf_id}</b>\n\n{html.quote(conf_data['text'])}\n\n{category_tags}\n---"
                    builder = InlineKeyboardBuilder()
                    builder.button(text="➕ Add Comment", callback_data=f"add_{conf_id}")
                    builder.button(text=f"💬 Browse Comments ({comm_count})", callback_data=f"browse_{conf_id}")
                    builder.adjust(1, 1)
                    
                    await bot.send_photo(
                        chat_id=user_id,
                        photo=conf_data['photo_file_id'],
                        caption=caption,
                        reply_markup=builder.as_markup()
                    )
                else:
                    txt = f"<b>Confession #{conf_id}</b>\n\n{html.quote(conf_data['text'])}\n\n{category_tags}\n---"
                    builder = InlineKeyboardBuilder()
                    builder.button(text="➕ Add Comment", callback_data=f"add_{conf_id}")
                    builder.button(text=f"💬 Browse Comments ({comm_count})", callback_data=f"browse_{conf_id}")
                    builder.adjust(1, 1)
                    await message.answer(txt, reply_markup=builder.as_markup())
                    
            except (ValueError, IndexError):
                await message.answer("Invalid link.")
            except Exception as e:
                logger.error(f"Err handling deep link '{deep_link_args}': {e}", exc_info=True)
                await message.answer("Error processing link.")
        elif deep_link_args.startswith("profile_"):
            try:
                # Get encoded user ID from the deep link
                encoded_user_id = deep_link_args.split("_", 1)[1]
                logger.info(f"Processing profile deep link: encoded_id={encoded_user_id}")
                
                # Decode to get actual user ID
                target_user_id = await get_user_id_from_encoded(encoded_user_id)
                
                if not target_user_id:
                    logger.error(f"Could not decode user ID from encoded: {encoded_user_id}")
                    await message.answer("Profile not found or link expired.")
                    return
                
                logger.info(f"Decoded user ID: {target_user_id}")
                
                # Show profile directly
                await show_user_profile_directly(message, target_user_id)
                
            except (ValueError, IndexError) as e:
                logger.error(f"Error parsing profile deep link: {e}")
                await message.answer("Invalid profile link.")
            except Exception as e:
                logger.error(f"Error handling profile deep link: {e}")
                await message.answer("Error processing profile link.")
    
    else:
        # Show welcome message with profile info
        profile_name = await get_profile_name(user_id)
        points = await get_user_points(user_id)
        
        welcome_text = (
            f"👋 Welcome back, <b>{profile_name}</b>!\n\n"
            f"🏅 <b>Your Aura:</b> {points}\n\n"
            "<b>Available Commands:</b>\n"
            "🔹 /confess - Submit anonymous confession (text or photo)\n"
            "🔹 /profile - View and manage your profile\n"
            "🔹 /help - Show all commands\n"
            "🔹 /rules - View bot rules\n"
            "🔹 /privacy - Privacy information\n"
            "🔹 /cancel - Cancel current action\n"
            "🔹 /endchat - End current chat\n\n"
            "<i>Your display name is shown with your comments. Click on names in comments to view profiles.</i>"
        )
        
        await message.answer(welcome_text, reply_markup=ReplyKeyboardRemove())

# --- Accept Rules Handler ---
@dp.callback_query(F.data == "accept_rules")
async def handle_accept_rules(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    conn = db
    await conn.execute(
        """INSERT OR REPLACE INTO user_status (user_id, has_accepted_rules) VALUES (?, 1)""",
        (user_id,)
    )
    await conn.commit()
    
    await callback_query.message.edit_text(
        "✅ <b>Rules Accepted!</b>\n\n"
        "Welcome to the confession bot!\n\n"
        "<b>Next Steps:</b>\n"
        "1. Set your display name using /profile\n"
        "2. Submit confessions with /confess (text or photo)\n"
        "3. Comment on others' confessions\n"
        "4. Click on names in comments to view profiles and request contact\n\n"
        "<i>Your display name will be shown with your comments. You can change it anytime in your profile.</i>",
        reply_markup=None
    )
    await callback_query.answer("Rules accepted!")

# --- Koyeb Specific: Main Execution with HTTP Server ---
async def main():
    try:
        # Clear webhook at the start
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            logger.info("Webhook cleared successfully")
        except Exception as e:
            logger.info(f"Webhook clear result: {e}")
        
        # Startup logging
        import socket
        hostname = socket.gethostname()
        pid = os.getpid()
        logger.info(f"🚀 Bot starting on host: {hostname}, PID: {pid}")
        logger.info(f"🔧 Environment: PORT={HTTP_PORT}, Admins: {len(ADMIN_IDS)}")
        logger.info(f"📁 Database path: {DATABASE_PATH}")
        
        await setup()
        if not db or not bot_info:
            logger.critical("FATAL: Database or bot info missing after setup. Cannot start.")
            return

        # Register middleware
        dp.message.middleware(BlockUserMiddleware())
        dp.callback_query.middleware(BlockUserMiddleware())

        # Set commands for regular users
        user_commands = [
            types.BotCommand(command="start", description="Start/View confession"),
            types.BotCommand(command="confess", description="Submit anonymous confession (text or photo)"),
            types.BotCommand(command="profile", description="View and manage your profile"),
            types.BotCommand(command="help", description="Show help and commands"),
            types.BotCommand(command="rules", description="View the bot's rules"),
            types.BotCommand(command="privacy", description="View privacy information"),
            types.BotCommand(command="cancel", description="Cancel current action"),
            types.BotCommand(command="endchat", description="End current chat"),
        ]
        
        # Set admin commands for admin users
        admin_commands = user_commands + [
            types.BotCommand(command="admin", description="Admin panel"),
            types.BotCommand(command="id", description="Get user info"),
            types.BotCommand(command="warn", description="Warn a user"),
            types.BotCommand(command="block", description="Temporarily block a user"),
            types.BotCommand(command="pblock", description="Permanently block a user"),
            types.BotCommand(command="unblock", description="Unblock a user"),
            types.BotCommand(command="stats", description="Show bot statistics"),
            types.BotCommand(command="broadcast", description="Broadcast message to all users"),
        ]
        
        # Set commands for all users
        await bot.set_my_commands(user_commands)
        
        # Set admin commands for admin users
        for admin_id in ADMIN_IDS:
            try:
                await bot.set_my_commands(
                    admin_commands, 
                    scope=types.BotCommandScopeChat(chat_id=admin_id)
                )
            except Exception as e:
                logger.warning(f"Could not set admin commands for {admin_id}: {e}")

        logger.info("Starting bot...")
        
        # --- Koyeb Specific: Start HTTP server for health checks ---
        async def health_check_handler(request):
            """Handle health check requests"""
            try:
                # Check if bot is responsive
                await bot.get_me()
                return web.Response(text="Bot is running", status=200)
            except Exception as e:
                logger.error(f"Health check failed: {e}")
                return web.Response(text=f"Bot error: {e}", status=500)
        
        async def start_http_server():
            """Start HTTP server for Koyeb health checks"""
            try:
                app = web.Application()
                app.router.add_get('/', health_check_handler)
                app.router.add_get('/health', health_check_handler)
                
                runner = web.AppRunner(app)
                await runner.setup()
                site = web.TCPSite(runner, '0.0.0.0', HTTP_PORT)
                await site.start()
                logger.info(f"✅ HTTP server started on port {HTTP_PORT}")
                
                # Keep server running
                while True:
                    await asyncio.sleep(3600)
            except Exception as e:
                logger.error(f"Failed to start HTTP server: {e}")
                raise
        
        # Start HTTP server in background
        asyncio.create_task(start_http_server())
        
        # Log all registered handlers for debugging
        logger.info(f"Registered message handlers: {len(dp.message.handlers)}")
        logger.info(f"Registered callback handlers: {len(dp.callback_query.handlers)}")
        
        # Start polling with error handling for Koyeb
        logger.info("🚀 Starting bot polling...")
        await dp.start_polling(
            bot, 
            skip_updates=True, 
            allowed_updates=dp.resolve_used_update_types(),
            handle_signals=False  # Important for Koyeb to handle signals properly
        )
        
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        # Sleep before exit to allow logs to be written
        await asyncio.sleep(2)
        raise
    finally:
        logger.info("Shutting down...")
        if bot and bot.session:
            await bot.session.close()
        if db:
            await db.close()
        logger.info("Bot stopped.")

if __name__ == "__main__":
    # Simple startup for Koyeb
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.critical(f"Unhandled exception: {e}")
