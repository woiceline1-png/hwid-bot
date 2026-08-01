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
                hwid TEXT UNIQUE,
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
        # Anti-spam: skip jika sudah pernah balas
        try:
            if await bot_replied_to_command(ctx):
                return None
        except Exception:
            pass
        kwargs.setdefault('reference', ctx.message)
        kwargs.setdefault('fail_on_not_exists', False)
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

    # Diam untuk duplikat & command tidak dikenal
    if isinstance(error, AlreadyProcessed):
        return
    if isinstance(error, commands.CommandNotFound):
        return

    # Tangani error umum → kirim pesan ke user
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

    # Error lain → log + tampilkan ke user
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
            "`!extendhwid 1515667018961915966 60`"
        ),
        inline=False
    )
    await ctx.send(embed=embed)


@bot.command(name='checkhwid')
@commands.has_permissions(administrator=True)
async def check_hwid(ctx, user_input: str = None):
    """
    Cek HWID user.
    Usage:
      !checkhwid                          → cek diri sendiri
      !checkhwid @user                    → cek via mention
      !checkhwid 1515667018961915966      → cek via Discord ID
    """
    if user_input is None:
        user = ctx.author
    else:
        user, err = await resolve_user(ctx, user_input)
        if err:
            await ctx.send(err)
            return

    display_name = get_display_name(user)

    async with aiosqlite.connect(DATABASE_FILE) as db:
        cursor = await db.execute(
            'SELECT hwid, verified, verified_at, expiry_date FROM users WHERE discord_id = ?',
            (user.id,)
        )
        row = await cursor.fetchone()
        if row:
            hwid, verified, verified_at, expiry_date = row
            now_wib = get_wib_time()
            expired = False
            if expiry_date:
                try:
                    if now_wib > datetime.fromisoformat(expiry_date):
                        expired = True
                except:
                    pass

            color = discord.Color.green() if (verified and not expired) else discord.Color.red()
            embed = discord.Embed(title=f"HWID Info for {display_name}", color=color)
            embed.add_field(name="HWID", value=f"`{hwid}`", inline=False)
            embed.add_field(name="Verified", value="✅ Yes" if verified else "❌ No", inline=True)
            embed.add_field(name="Verified At", value=verified_at or "Never", inline=True)

            exp_display = "Not set"
            if expiry_date:
                try:
                    exp_display = datetime.fromisoformat(expiry_date).strftime('%Y-%m-%d %H:%M WIB')
                except:
                    pass

            embed.add_field(name="Expiry Date", value=exp_display, inline=False)
            embed.add_field(name="Status", value="⏰ EXPIRED" if expired else "🟢 Active", inline=False)
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"❌ No HWID registered for {display_name}")


@bot.command(name='verifyhwid')
@commands.has_permissions(administrator=True)
async def verify_hwid(ctx, user_input: str, hwid: str, expiry_days: int = 30):
    """
    Verify HWID user.
    Usage:
      !verifyhwid @user HWID123              → verify via mention (30 hari)
      !verifyhwid 1515667018961915966 HWID123 60  → verify via Discord ID (60 hari)
    """
    if expiry_days < 1 or expiry_days > 9999:
        await ctx.send("❌ Expiry days must be between **1 and 9999**!")
        return

    user, err = await resolve_user(ctx, user_input)
    if err:
        await ctx.send(err)
        return

    display_name = get_display_name(user)

    async with aiosqlite.connect(DATABASE_FILE) as db:
        # CEK ANTI DOBEL
        cursor = await db.execute(
            'SELECT verified, hwid FROM users WHERE discord_id = ?',
            (user.id,)
        )
        row = await cursor.fetchone()

        if row and row[0] == 1:
            if row[1] == hwid:
                msg = f"ℹ️ {display_name} sudah terverifikasi."
            else:
                msg = f"ℹ️ {display_name} sudah terverifikasi dengan HWID lain. Gunakan !unverifyhwid untuk ganti."
            await ctx.send(msg)
            return

        # Cek HWID bentrok
        cursor = await db.execute(
            'SELECT discord_id FROM users WHERE hwid = ? AND discord_id != ?',
            (hwid, user.id)
        )
        if await cursor.fetchone():
            await ctx.send(f"❌ HWID `{hwid}` already used by another user!")
            return

        expiry_date = get_wib_time() + timedelta(days=expiry_days)
        await db.execute('''
            INSERT INTO users (discord_id, username, hwid, verified, expiry_date)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                hwid = excluded.hwid,
                verified = 1,
                verified_at = CURRENT_TIMESTAMP,
                expiry_date = excluded.expiry_date
        ''', (user.id, str(user), hwid, expiry_date.isoformat()))
        await db.commit()

        # Kirim DM (Cuma 1x) — walau user tidak di server
        try:
            await user.send(
                f"✅ HWID Anda `{hwid}` telah diverifikasi!\n"
                f"⏰ Expired pada: `{expiry_date.strftime('%Y-%m-%d %H:%M WIB')}`\n"
                f"⏳ Durasi: **{expiry_days} hari**"
            )
        except discord.Forbidden:
            pass
        except discord.HTTPException:
            pass

        await ctx.send(
            f"✅ HWID `{hwid}` verified for {display_name}!\n"
            f"⏰ Expiry: **{expiry_days} days** "
            f"({expiry_date.strftime('%Y-%m-%d %H:%M WIB')})"
        )


@bot.command(name='extendhwid')
@commands.has_permissions(administrator=True)
async def extend_hwid(ctx, user_input: str, additional_days: int):
    """
    Perpanjang expiry HWID user.
    Usage:
      !extendhwid @user 30
      !extendhwid 1515667018961915966 30
    """
    if additional_days < 1 or additional_days > 9999:
        await ctx.send("❌ Days must be between 1 and 9999!")
        return

    user, err = await resolve_user(ctx, user_input)
    if err:
        await ctx.send(err)
        return

    display_name = get_display_name(user)

    async with aiosqlite.connect(DATABASE_FILE) as db:
        cursor = await db.execute(
            'SELECT expiry_date FROM users WHERE discord_id = ?',
            (user.id,)
        )
        row = await cursor.fetchone()
        if not row:
            await ctx.send(f"❌ No HWID registered for {display_name}")
            return

        current_expiry = get_wib_time()
        if row[0]:
            try:
                exp_dt = datetime.fromisoformat(row[0])
                current_expiry = exp_dt if exp_dt > get_wib_time() else get_wib_time()
            except:
                pass

        new_expiry = current_expiry + timedelta(days=additional_days)
        await db.execute(
            'UPDATE users SET expiry_date = ?, verified = 1 WHERE discord_id = ?',
            (new_expiry.isoformat(), user.id)
        )
        await db.commit()
        await ctx.send(
            f"✅ Extended {display_name}'s expiry by **{additional_days} days**!\n"
            f"🆕 New expiry: `{new_expiry.strftime('%Y-%m-%d %H:%M WIB')}`"
        )


@bot.command(name='unverifyhwid')
@commands.has_permissions(administrator=True)
async def unverify_hwid(ctx, user_input: str):
    """
    Unverify HWID user.
    Usage:
      !unverifyhwid @user
      !unverifyhwid 1515667018961915966
    """
    user, err = await resolve_user(ctx, user_input)
    if err:
        await ctx.send(err)
        return

    display_name = get_display_name(user)

    async with aiosqlite.connect(DATABASE_FILE) as db:
        await db.execute(
            'UPDATE users SET verified = 0 WHERE discord_id = ?',
            (user.id,)
        )
        await db.commit()
    await ctx.send(f"✅ HWID unverified for {display_name}!")


@bot.command(name='listhwid')
@commands.has_permissions(administrator=True)
async def list_hwid(ctx):
    async with aiosqlite.connect(DATABASE_FILE) as db:
        cursor = await db.execute(
            'SELECT discord_id, username, hwid, verified_at, expiry_date FROM users WHERE verified = 1'
        )
        rows = await cursor.fetchall()
        if not rows:
            await ctx.send("No verified users found.")
            return
        now_wib = get_wib_time()
        embed = discord.Embed(title=f"Verified Users ({len(rows)})", color=discord.Color.green())
        for row in rows[:10]:
            discord_id, username, hwid, verified_at, expiry_date = row
            expired = False
            exp_display = "N/A"
            if expiry_date:
                try:
                    exp_dt = datetime.fromisoformat(expiry_date)
                    if now_wib > exp_dt:
                        expired = True
                    exp_display = exp_dt.strftime('%Y-%m-%d %H:%M WIB')
                except:
                    pass
            status = "⏰ EXPIRED" if expired else "🟢 Active"
            embed.add_field(
                name=f"<@{discord_id}>",
                value=f"HWID: `{hwid[:8]}...`\nExpiry: {exp_display}\nStatus: {status}",
                inline=False
            )
        await ctx.send(embed=embed)


@bot.command(name='myhwid')
async def my_hwid(ctx):
    async with aiosqlite.connect(DATABASE_FILE) as db:
        cursor = await db.execute(
            'SELECT hwid, verified, verified_at, expiry_date FROM users WHERE discord_id = ?',
            (ctx.author.id,)
        )
        row = await cursor.fetchone()
        if row:
            hwid, verified, verified_at, expiry_date = row
            now_wib = get_wib_time()
            expired = False
            if expiry_date:
                try:
                    if now_wib > datetime.fromisoformat(expiry_date):
                        expired = True
                except:
                    pass
            color = discord.Color.green() if (verified and not expired) else discord.Color.red()
            embed = discord.Embed(title="Your HWID Status", color=color)
            embed.add_field(name="HWID", value=f"`{hwid}`", inline=False)
            embed.add_field(name="Verified", value="✅ Yes" if verified else "❌ No", inline=True)
            exp_display = "Not set"
            if expiry_date:
                try:
                    exp_display = datetime.fromisoformat(expiry_date).strftime('%Y-%m-%d %H:%M WIB')
                except:
                    pass
            embed.add_field(name="Expiry Date", value=exp_display, inline=False)
            embed.add_field(name="Status", value="⏰ EXPIRED" if expired else "🟢 Active", inline=False)
            await ctx.send(embed=embed)
        else:
            await ctx.send("❌ No HWID registered for you yet. Contact admin to verify.")


@bot.command(name='cleardm')
@commands.has_permissions(administrator=True)
async def clear_dm(ctx, user_input: str):
    """
    Hapus pesan bot di DM user.
    Usage:
      !cleardm @user
      !cleardm 1515667018961915966
    """
    user, err = await resolve_user(ctx, user_input)
    if err:
        await ctx.send(err)
        return

    display_name = get_display_name(user)
    dm_channel = user.dm_channel if user.dm_channel else await user.create_dm()
    await ctx.send(f"🧹 Sedang membersihkan DM bot dengan {display_name}...")
    deleted_count = 0
    try:
        async for message in dm_channel.history(limit=100):
            if message.author == bot.user:
                try:
                    await message.delete()
                    deleted_count += 1
                    await asyncio.sleep(1)
                except:
                    pass
        await ctx.send(f"✅ Berhasil menghapus **{deleted_count}** pesan bot di DM {display_name}.")
    except:
        await ctx.send(f"❌ Gagal mengakses DM {display_name}.")


# ===== VOICE COMMANDS =====
@bot.command(name='joinvoice')
@commands.has_permissions(administrator=True)
async def join_voice(ctx, channel_id: int = None):
    """Bot masuk ke voice channel."""
    if ctx.voice_client:
        await ctx.send("ℹ️ Bot sudah berada di voice channel. Gunakan `!leavevoice` dulu.")
        return

    voice_channel = None
    if channel_id:
        voice_channel = bot.get_channel(channel_id)
        if not voice_channel or not isinstance(voice_channel, discord.VoiceChannel):
            await ctx.send("❌ Channel ID tidak valid atau itu bukan Voice Channel!")
            return
    else:
        if ctx.author.voice:
            voice_channel = ctx.author.voice.channel
        else:
            await ctx.send("❌ Kamu tidak di voice channel, atau berikan ID Voice Channel! Format: `!joinvoice <channel_id>`")
            return

    try:
        await voice_channel.connect(reconnect=True, self_deaf=True)
        await save_voice_channel_id(voice_channel.id)
        await ctx.send(f"✅ Bot berhasil join ke **{voice_channel.name}**!\n🔄 Auto-reconnect aktif jika bot disconnect.")
    except discord.Forbidden:
        await ctx.send("❌ Bot tidak punya izin untuk join ke voice channel tersebut.")
    except Exception as e:
        await ctx.send(f"❌ Terjadi error saat join: {str(e)}")


@bot.command(name='leavevoice')
@commands.has_permissions(administrator=True)
async def leave_voice(ctx):
    """Bot keluar dari voice channel dan matikan auto-reconnect."""
    await clear_saved_voice_channel_id()
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("✅ Bot telah keluar dari voice channel. Auto-reconnect dimatikan.")
    else:
        for vc in bot.voice_clients:
            await vc.disconnect(force=True)
        await ctx.send("✅ Auto-reconnect dimatikan.")


# ===== FLASK API =====
app = Flask(__name__)
CORS(app)


@app.route('/verify', methods=['GET'])
def verify_hwid_api():
    hwid = request.args.get('hwid')
    if not hwid:
        return jsonify({"error": "Missing HWID"}), 400

    async def check_db():
        async with aiosqlite.connect(DATABASE_FILE) as db:
            cursor = await db.execute(
                'SELECT verified, expiry_date FROM users WHERE hwid = ?',
                (hwid,)
            )
            row = await cursor.fetchone()
            if row is None:
                return False, None
            return row[0] == 1, row[1]

    verified, expiry_date_str = asyncio.run(check_db())
    expired = False
    expiry_iso = None
    if expiry_date_str:
        try:
            expiry_dt = datetime.fromisoformat(expiry_date_str)
            expiry_iso = expiry_dt.isoformat()
            if get_wib_time() > expiry_dt:
                expired = True
                verified = False
        except:
            pass
    return jsonify({"verified": verified, "hwid": hwid, "expiry_date": expiry_iso, "expired": expired})


@app.route('/getuser', methods=['GET'])
def get_user_from_hwid():
    hwid = request.args.get('hwid')
    if not hwid:
        return jsonify({"error": "Missing HWID"}), 400

    async def get_user():
        async with aiosqlite.connect(DATABASE_FILE) as db:
            cursor = await db.execute(
                'SELECT discord_id, username, expiry_date FROM users WHERE hwid = ? AND verified = 1',
                (hwid,)
            )
            row = await cursor.fetchone()
            if row:
                if row[2]:
                    try:
                        if get_wib_time() > datetime.fromisoformat(row[2]):
                            return None
                    except:
                        pass
                return {"discord_id": row[0], "username": row[1], "expiry_date": row[2]}
            return None

    user = asyncio.run(get_user())
    if user:
        return jsonify(user)
    else:
        return jsonify({"error": "User not found, not verified, or expired"}), 404


def run_api():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)), debug=False, use_reloader=False)


if __name__ == "__main__":
    _bot_lock = acquire_single_instance_lock()
    api_thread = threading.Thread(target=run_api, daemon=True)
    api_thread.start()
    bot.run(TOKEN)
