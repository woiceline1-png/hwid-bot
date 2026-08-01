import discord
from discord.ext import commands, tasks
import aiosqlite
import asyncio
import os
import sqlite3
import sys
import threading
import traceback
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS

# ===== KONFIGURASI =====
TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    raise ValueError("DISCORD_TOKEN tidak ditemukan di environment variable!")

DATABASE_FILE = "hwid.db"


class AlreadyProcessed(commands.CommandError):
    pass


_processed_message_ids = set()
_command_locks = {}


def acquire_single_instance_lock():
    lock_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bot.lock')
    lock_file = open(lock_path, 'w')
    try:
        if sys.platform == 'win32':
            import msvcrt
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise RuntimeError('Bot sudah jalan di instance lain! Matikan duplikat dulu.')
    return lock_file


def get_wib_time():
    return datetime.utcnow() + timedelta(hours=7)


# ===== DATABASE =====
async def init_db():
    async with aiosqlite.connect(DATABASE_FILE) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                discord_id INTEGER PRIMARY KEY,
                username TEXT,
                hwid TEXT,
                verified INTEGER DEFAULT 0,
                verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expiry_date TIMESTAMP
            )
        ''')
        try:
            await db.execute('ALTER TABLE users ADD COLUMN expiry_date TIMESTAMP')
        except:
            pass
        await db.execute('''
            CREATE TABLE IF NOT EXISTS command_dedup (
                message_id INTEGER PRIMARY KEY,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db.execute(
            "DELETE FROM command_dedup WHERE processed_at < datetime('now', '-7 days')"
        )
        await db.execute('''
            CREATE TABLE IF NOT EXISTS bot_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        await db.commit()

        # ===== MIGRASI: Hapus UNIQUE constraint pada hwid =====
        # Cek apakah kolom hwid masih punya UNIQUE constraint
        cursor = await db.execute("PRAGMA table_info(users)")
        columns = await cursor.fetchall()
        cursor = await db.execute("PRAGMA index_list(users)")
        indexes = await cursor.fetchall()
        
        for idx in indexes:
            idx_name = idx[1]
            if idx_name and 'hwid' in idx_name.lower():
                cursor2 = await db.execute(f"PRAGMA index_info('{idx_name}')")
                idx_cols = await cursor2.fetchall()
                if idx_cols and idx_cols[0][2] == 'hwid':
                    print(f"🔄 Migrasi: Menghapus UNIQUE constraint '{idx_name}' pada kolom hwid...")
                    # Rebuild tabel tanpa UNIQUE pada hwid
                    await db.execute('''
                        CREATE TABLE IF NOT EXISTS users_new (
                            discord_id INTEGER PRIMARY KEY,
                            username TEXT,
                            hwid TEXT,
                            verified INTEGER DEFAULT 0,
                            verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            expiry_date TIMESTAMP
                        )
                    ''')
                    await db.execute('''
                        INSERT OR IGNORE INTO users_new 
                        (discord_id, username, hwid, verified, verified_at, expiry_date)
                        SELECT discord_id, username, hwid, verified, verified_at, expiry_date 
                        FROM users
                    ''')
                    await db.execute('DROP TABLE users')
                    await db.execute('ALTER TABLE users_new RENAME TO users')
                    print("✅ Migrasi selesai: UNIQUE constraint pada hwid sudah dihapus.")
                    break

        await db.commit()


async def get_saved_voice_channel_id():
    async with aiosqlite.connect(DATABASE_FILE) as db:
        cursor = await db.execute(
            "SELECT value FROM bot_settings WHERE key = 'voice_channel_id'"
        )
        row = await cursor.fetchone()
        return int(row[0]) if row else None


async def save_voice_channel_id(channel_id: int):
    async with aiosqlite.connect(DATABASE_FILE) as db:
        await db.execute('''
            INSERT INTO bot_settings (key, value) VALUES ('voice_channel_id', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        ''', (str(channel_id),))
        await db.commit()


async def clear_saved_voice_channel_id():
    async with aiosqlite.connect(DATABASE_FILE) as db:
        await db.execute(
            "DELETE FROM bot_settings WHERE key = 'voice_channel_id'"
        )
        await db.commit()


voice_reconnect_lock = asyncio.Lock()


async def reconnect_voice_channel():
    channel_id = await get_saved_voice_channel_id()
    if not channel_id:
        return False

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            print(f'❌ Voice channel {channel_id} tidak ditemukan')
            return False

    if not isinstance(channel, discord.VoiceChannel):
        return False

    if any(vc.channel and vc.channel.id == channel_id for vc in bot.voice_clients):
        return True

    for vc in bot.voice_clients:
        await vc.disconnect(force=True)

    try:
        await channel.connect(reconnect=True, self_deaf=True)
        print(f'🔊 Bot reconnect ke voice: {channel.name}')
        return True
    except Exception as e:
        print(f'❌ Gagal reconnect voice: {e}')
        return False


async def claim_command_message(message_id: int) -> bool:
    if message_id in _processed_message_ids:
        return False

    async with aiosqlite.connect(DATABASE_FILE) as db:
        try:
            await db.execute('BEGIN IMMEDIATE')
            await db.execute(
                'INSERT INTO command_dedup (message_id) VALUES (?)',
                (message_id,)
            )
            await db.commit()
            _processed_message_ids.add(message_id)
            if len(_processed_message_ids) > 2000:
                _processed_message_ids.clear()
            return True
        except sqlite3.IntegrityError:
            await db.rollback()
            return False


async def bot_replied_to_command(ctx) -> bool:
    try:
        async for msg in ctx.channel.history(limit=25):
            if msg.author.id != bot.user.id:
                continue
            ref = msg.reference
            if ref and ref.message_id == ctx.message.id:
                return True
    except discord.HTTPException:
        pass
    return False


# ====================================================================
# ===== HELPER: RESOLVE USER DARI MENTION ATAU RAW DISCORD ID ========
# ====================================================================
async def resolve_user(ctx, user_input: str):
    """
    Resolve user dari:
      - Mention : @user  →  <@123456> atau <@!123456>
      - Raw ID  : 1515667018961915966

    Return: (user_object, error_message)
    """
    if not user_input:
        return None, "❌ User tidak boleh kosong!"

    user_id = None

    # Case 1: Format mention <@123456> atau <@!123456>
    if user_input.startswith('<@') and user_input.endswith('>'):
        clean = user_input[2:-1].lstrip('!')
        try:
            user_id = int(clean)
        except ValueError:
            pass
    else:
        # Case 2: Raw Discord ID (angka saja)
        try:
            user_id = int(user_input.strip())
        except ValueError:
            pass

    if not user_id:
        return None, (
            "❌ Format user tidak valid!\n"
            "Gunakan **mention** (`@user`) atau **Discord ID** (angka).\n"
            "Contoh: `!verifyhwid 1515667018961915966 HWID123 30`"
        )

    # Coba cari sebagai Member dulu (jika ada di guild)
    if ctx.guild:
        try:
            member = ctx.guild.get_member(user_id)
            if member:
                return member, None
            member = await ctx.guild.fetch_member(user_id)
            if member:
                return member, None
        except discord.NotFound:
            pass
        except discord.Forbidden:
            pass
        except Exception:
            pass

    # Jika tidak ada di guild, fetch sebagai User via API
    try:
        user = await bot.fetch_user(user_id)
        return user, None
    except discord.NotFound:
        return None, "❌ User tidak ditemukan di Discord! Cek kembali Discord ID-nya."
    except discord.HTTPException as e:
        return None, f"❌ Gagal mengambil data user: {str(e)}"


async def safe_send_dm(user, content: str) -> bool:
    """Kirim DM ke user dengan aman. Return True jika berhasil, False jika gagal."""
    try:
        if user.dm_channel is None:
            await user.create_dm()
        await user.send(content)
        return True
    except discord.Forbidden:
        print(f"⚠️ Tidak bisa kirim DM ke {getattr(user, 'display_name', user)} — DM ditutup.")
        return False
    except discord.HTTPException as e:
        print(f"⚠️ Gagal kirim DM ke {getattr(user, 'display_name', user)} — {e}")
        return False
    except Exception as e:
        print(f"⚠️ Error saat kirim DM ke {getattr(user, 'display_name', user)} — {e}")
        return False


def get_display_name(user) -> str:
    """Ambil display name dari Member atau User dengan aman."""
    return getattr(user, 'display_name', None) or str(user)


# ===== DISCORD BOT =====
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
bot = commands.Bot(command_prefix='!', intents=intents)


@tasks.loop(seconds=30)
async def keep_voice_connected():
    channel_id = await get_saved_voice_channel_id()
    if not channel_id:
        return
    connected = any(
        vc.channel and vc.channel.id == channel_id
        for vc in bot.voice_clients
    )
    if not connected:
        async with voice_reconnect_lock:
            await reconnect_voice_channel()


@keep_voice_connected.before_loop
async def before_keep_voice_connected():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    await init_db()
    print(f'✅ Bot ready! Logged in as {bot.user}')
    if not keep_voice_connected.is_running():
        keep_voice_connected.start()
    await reconnect_voice_channel()


@bot.event
async def on_voice_state_update(member, before, after):
    if member.id != bot.user.id:
        return
    channel_id = await get_saved_voice_channel_id()
    if not channel_id:
        return
    if after.channel is None or after.channel.id != channel_id:
        await asyncio.sleep(3)
        async with voice_reconnect_lock:
            await reconnect_voice_channel()


@bot.before_invoke
async def prevent_duplicate_commands(ctx):
    if not await claim_command_message(ctx.message.id):
        raise AlreadyProcessed()

    lock = _command_locks.setdefault(ctx.message.id, asyncio.Lock())
    await lock.acquire()
    ctx._dedup_lock = lock

    original_send = ctx.send

    async def guarded_send(*args, **kwargs):
        try:
            if await bot_replied_to_command(ctx):
                return None
        except Exception:
            pass

        reference = kwargs.pop('reference', None)
        if reference is not None and 'reference' not in kwargs:
            try:
                kwargs['reference'] = discord.MessageReference.from_message(
                    reference, fail_on_not_exists=False
                )
            except Exception:
                kwargs['reference'] = reference

        try:
            return await original_send(*args, **kwargs)
        except discord.HTTPException as e:
            print(f"❌ Gagal kirim pesan: {e}")
            return None

    ctx.send = guarded_send


@bot.after_invoke
async def release_command_lock(ctx):
    lock = getattr(ctx, '_dedup_lock', None)
    if lock and lock.locked():
        lock.release()
    _command_locks.pop(ctx.message.id, None)


@bot.event
async def on_command_error(ctx, error):
    lock = getattr(ctx, '_dedup_lock', None)
    if lock and lock.locked():
        lock.release()
    _command_locks.pop(ctx.message.id, None)

    if isinstance(error, AlreadyProcessed):
        return
    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Kamu tidak punya izin **Administrator** untuk pakai command ini!")
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Parameter kurang: `{error.param.name}`\n💡 Cek format command!")
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send(f"❌ Parameter tidak valid: {str(error)}")
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.send("❌ Kamu tidak bisa pakai command ini!")
        return

    print(f"❌ ERROR di command {ctx.command}: {type(error).__name__}: {error}")
    traceback.print_exc()
    try:
        await ctx.send(f"❌ Terjadi error: `{type(error).__name__}: {str(error)}`")
    except Exception:
        pass


# ====================================================================
# ===== COMMANDS =====================================================
# ====================================================================

@bot.command(name='ping')
async def ping(ctx):
    """Test apakah bot merespon."""
    latency_ms = round(bot.latency * 1000)
    await ctx.send(f"🏓 Pong! Latency: **{latency_ms}ms**\n🤖 Bot online dan merespon!")


@bot.command(name='helpext')
@commands.has_permissions(administrator=True)
async def help_ext(ctx):
    """Tampilkan daftar command lengkap."""
    embed = discord.Embed(
        title="📖 Daftar Command Bot",
        description="**User bisa pakai mention `@user` ATAU Discord ID `1515667018961915966`**",
        color=discord.Color.blue()
    )
    embed.add_field(
        name="🔧 Admin Commands",
        value=(
            "`!checkhwid [@user|ID]` — Cek HWID user\n"
            "`!verifyhwid <@user|ID> <hwid> [days=30]` — Verify HWID\n"
            "`!extendhwid <@user|ID> <days>` — Perpanjang expiry\n"
            "`!unverifyhwid <@user|ID>` — Unverify HWID\n"
            "`!listhwid` — List user verified\n"
            "`!cleardm <@user|ID>` — Hapus DM bot\n"
            "`!joinvoice [channel_id]` — Bot join voice\n"
            "`!leavevoice` — Bot keluar voice\n"
            "`!helpext` — Tampilkan help ini"
        ),
        inline=False
    )
    embed.add_field(
        name="👤 User Commands",
        value=(
            "`!myhwid` — Cek HWID sendiri\n"
            "`!ping` — Test bot online"
        ),
        inline=False
    )
    embed.add_field(
        name="💡 Contoh Penggunaan",
        value=(
            "`!verifyhwid 1515667018961915966 944D3FDCA8CFD61B0A006D49FC32765A 999`\n"
            "`!verifyhwid @username HWID123 30`\n"
            "`!extendhwid 1515667018961915966 60`\n"
            "`!cleardm 1515667018961915966`"
        ),
        inline=False
    )
    await ctx.send(embed=embed)


@bot.command(name='checkhwid')
@commands.has_permissions(administrator=True)
async def check_hwid(ctx, user_input: str = None):
    """Cek HWID user (mention atau Discord ID)."""
    if user_input is None:
        user = ctx.author
    else:
        user, err = await resolve_user(ctx, user_input)
        if err:
            await ctx.send
