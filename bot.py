import discord
from discord.ext import commands
import aiosqlite
import asyncio
import os
import threading
import json
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify
from flask_cors import CORS

# ===== KONFIGURASI =====
TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    raise ValueError("DISCORD_TOKEN tidak ditemukan di environment variable!")

DATABASE_FILE = "hwid.db"
API_PORT = int(os.environ.get("PORT", 5000))

# Helper untuk Waktu Indonesia Barat (WIB / UTC+7)
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
        await db.commit()

# ===== DISCORD BOT =====
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    await init_db()
    print(f'✅ Bot ready! Logged in as {bot.user}')

@bot.command(name='checkhwid')
@commands.has_permissions(administrator=True)
async def check_hwid(ctx, member: discord.Member = None):
    if member is None:
        member = ctx.author
    async with aiosqlite.connect(DATABASE_FILE) as db:
        cursor = await db.execute(
            'SELECT hwid, verified, verified_at, expiry_date FROM users WHERE discord_id = ?',
            (member.id,)
        )
        row = await cursor.fetchone()
        if row:
            hwid, verified, verified_at, expiry_date = row
            now_wib = get_wib_time()
            expired = False
            if expiry_date:
                try:
                    exp_dt = datetime.fromisoformat(expiry_date)
                    if now_wib > exp_dt:
                        expired = True
                except:
                    pass

            color = discord.Color.green() if (verified and not expired) else discord.Color.red()
            embed = discord.Embed(
                title=f"HWID Info for {member.display_name}",
                color=color
            )
            embed.add_field(name="HWID", value=f"`{hwid}`", inline=False)
            embed.add_field(
                name="Verified",
                value="✅ Yes" if verified else "❌ No",
                inline=True
            )
            embed.add_field(name="Verified At", value=verified_at or "Never", inline=True)
            
            exp_display = "Not set"
            if expiry_date:
                try:
                    exp_display = datetime.fromisoformat(expiry_date).strftime('%Y-%m-%d %H:%M WIB')
                except:
                    exp_display = expiry_date
            
            embed.add_field(name="Expiry Date", value=exp_display, inline=False)
            embed.add_field(
                name="Status",
                value="⏰ EXPIRED" if expired else "🟢 Active",
                inline=False
            )
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"❌ No HWID registered for {member.display_name}")

@bot.command(name='verifyhwid')
@commands.has_permissions(administrator=True)
async def verify_hwid(ctx, member: discord.Member, hwid: str, expiry_days: int = 30):
    """
    Format: !verifyhwid @user @hwid @expiry_days (1-9999)
    """
    if expiry_days < 1 or expiry_days > 9999:
        await ctx.send("❌ Expiry days must be between **1 and 9999**!")
        return

    async with aiosqlite.connect(DATABASE_FILE) as db:
        # === CEK ANTI DOBEL DM MAKSIMAL ===
        cursor = await db.execute(
            'SELECT verified, hwid FROM users WHERE discord_id = ?',
            (member.id,)
        )
        row = await cursor.fetchone()
        
        if row and row[0] == 1:
            if row[1] == hwid:
                await ctx.send(f"ℹ️ {member.display_name} sudah terverifikasi dengan HWID tersebut. Tidak ada perubahan.")
            else:
                await ctx.send(f"ℹ️ {member.display_name} sudah terverifikasi dengan HWID berbeda. Gunakan `!unverifyhwid` dulu jika ingin mengganti HWID.")
            return

        cursor = await db.execute(
            'SELECT discord_id FROM users WHERE hwid = ? AND discord_id != ?',
            (hwid, member.id)
        )
        existing = await cursor.fetchone()
        if existing:
            await ctx.send(f"❌ HWID `{hwid}` already used by another user!")
            return

        # Hitung waktu WIB
        expiry_date = get_wib_time() + timedelta(days=expiry_days)
        expiry_str = expiry_date.isoformat()

        await db.execute('''
            INSERT INTO users (discord_id, username, hwid, verified, expiry_date)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                hwid = excluded.hwid,
                verified = 1,
                verified_at = CURRENT_TIMESTAMP,
                expiry_date = excluded.expiry_date
        ''', (member.id, str(member), hwid, expiry_str))
        await db.commit()

        try:
            await member.send(
                f"✅ HWID Anda `{hwid}` telah diverifikasi!\n"
                f"⏰ Expired pada: `{expiry_date.strftime('%Y-%m-%d %H:%M WIB')}`\n"
                f"⏳ Durasi: **{expiry_days} hari**"
            )
        except discord.Forbidden:
            await ctx.send("⚠️ Gagal mengirim DM. Pastikan user mengizinkan DM dari server ini.")

        await ctx.send(
            f"✅ HWID `{hwid}` verified for {member.display_name}!\n"
            f"⏰ Expiry: **{expiry_days} days** "
            f"({expiry_date.strftime('%Y-%m-%d %H:%M WIB')})"
        )

@bot.command(name='extendhwid')
@commands.has_permissions(administrator=True)
async def extend_hwid(ctx, member: discord.Member, additional_days: int):
    """Tambah hari ke expiry user yang sudah ada."""
    if additional_days < 1 or additional_days > 9999:
        await ctx.send("❌ Days must be between 1 and 9999!")
        return

    async with aiosqlite.connect(DATABASE_FILE) as db:
        cursor = await db.execute(
            'SELECT expiry_date, verified FROM users WHERE discord_id = ?',
            (member.id,)
        )
        row = await cursor.fetchone()
        if not row:
            await ctx.send(f"❌ No HWID registered for {member.display_name}")
            return

        current_expiry_str, verified = row
        now_wib = get_wib_time()
        
        if current_expiry_str:
            try:
                current_expiry = datetime.fromisoformat(current_expiry_str)
                if current_expiry < now_wib:
                    current_expiry = now_wib
            except:
                current_expiry = now_wib
        else:
            current_expiry = now_wib

        new_expiry = current_expiry + timedelta(days=additional_days)
        await db.execute(
            'UPDATE users SET expiry_date = ?, verified = 1 WHERE discord_id = ?',
            (new_expiry.isoformat(), member.id)
        )
        await db.commit()

        await ctx.send(
            f"✅ Extended {member.display_name}'s expiry by **{additional_days} days**!\n"
            f"🆕 New expiry: `{new_expiry.strftime('%Y-%m-%d %H:%M WIB')}`"
        )

@bot.command(name='unverifyhwid')
@commands.has_permissions(administrator=True)
async def unverify_hwid(ctx, member: discord.Member):
    async with aiosqlite.connect(DATABASE_FILE) as db:
        await db.execute('UPDATE users SET verified = 0 WHERE discord_id = ?', (member.id,))
        await db.commit()
        await ctx.send(f"✅ HWID unverified for {member.display_name}!")

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
        embed = discord.Embed(
            title=f"Verified Users ({len(rows)})",
            color=discord.Color.green()
        )
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
                value=f"HWID: `{hwid[:8]}...`\nVerified: {verified_at}\nExpiry: {exp_display}\nStatus: {status}",
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
            embed = discord.Embed(
                title=f"Your HWID Status",
                color=color
            )
            embed.add_field(name="HWID", value=f"`{hwid}`", inline=False)
            embed.add_field(
                name="Verified",
                value="✅ Yes" if verified else "❌ No",
                inline=True
            )
            embed.add_field(name="Verified At", value=verified_at or "Never", inline=True)
            
            exp_display = "Not set"
            if expiry_date:
                try:
                    exp_display = datetime.fromisoformat(expiry_date).strftime('%Y-%m-%d %H:%M WIB')
                except:
                    exp_display = expiry_date
                    
            embed.add_field(name="Expiry Date", value=exp_display, inline=False)
            embed.add_field(
                name="Status",
                value="⏰ EXPIRED" if expired else "🟢 Active",
                inline=False
            )
            await ctx.send(embed=embed)
        else:
            await ctx.send("❌ No HWID registered for you yet. Contact admin to verify.")

@bot.command(name='cleardm')
@commands.has_permissions(administrator=True)
async def clear_dm(ctx, member: discord.Member):
    """Menghapus semua pesan bot di DM seorang user (maks 100 pesan terakhir)."""
    dm_channel = member.dm_channel
    if dm_channel is None:
        # Jika bot belum pernah DM user ini, buat channel DM-nya
        dm_channel = await member.create_dm()
    
    await ctx.send(f"🧹 Sedang membersihkan DM bot dengan {member.display_name}...")
    
    deleted_count = 0
    try:
        # Ambil 100 pesan terakhir di DM
        async for message in dm_channel.history(limit=100):
            # Hanya hapus pesan yang dikirim oleh bot
            if message.author == bot.user:
                try:
                    await message.delete()
                    deleted_count += 1
                    # Delay sedikit agar tidak kena rate-limit Discord (5 detik per 5 delete)
                    await asyncio.sleep(1)
                except discord.Forbidden:
                    await ctx.send("❌ Bot tidak punya izin menghapus pesan di DM tersebut.")
                    break
                except discord.HTTPException:
                    pass # Abaikan jika ada error kecil
                
        await ctx.send(f"✅ Berhasil menghapus **{deleted_count}** pesan bot di DM {member.display_name}.")
    except discord.Forbidden:
        await ctx.send(f"❌ Gagal mengakses DM {member.display_name}. Bot tidak bisa membuka DM-nya.")
    except Exception as e:
        await ctx.send(f"❌ Terjadi error: {str(e)}")

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
    now_wib = get_wib_time()
    
    if expiry_date_str:
        try:
            expiry_dt = datetime.fromisoformat(expiry_date_str)
            expiry_iso = expiry_dt.isoformat()
            if now_wib > expiry_dt:
                expired = True
                verified = False
        except Exception:
            pass

    return jsonify({
        "verified": verified,
        "hwid": hwid,
        "expiry_date": expiry_iso,
        "expired": expired
    })

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
                expiry_str = row[2]
                if expiry_str:
                    try:
                        if get_wib_time() > datetime.fromisoformat(expiry_str):
                            return None
                    except:
                        pass
                return {"discord_id": row[0], "username": row[1], "expiry_date": expiry_str}
            return None

    user = asyncio.run(get_user())
    if user:
        return jsonify(user)
    else:
        return jsonify({"error": "User not found, not verified, or expired"}), 404

def run_api():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

# ===== RUN =====
if __name__ == "__main__":
    api_thread = threading.Thread(target=run_api, daemon=True)
    api_thread.start()
    bot.run(TOKEN)
