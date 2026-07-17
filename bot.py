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
        # Migration: tambah kolom expiry_date jika belum ada
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
            now_utc = datetime.utcnow()
            expired = False
            if expiry_date:
                try:
                    exp_dt = datetime.fromisoformat(expiry_date)
                    if now_utc > exp_dt:
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
            embed.add_field(
                name="Expiry Date",
                value=expiry_date or "Not set",
                inline=False
            )
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
        # Cek apakah user ini SUDAH diverifikasi (status verified = 1)
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
            return # <- Bot akan berhenti di sini, TIDAK mengirim DM lagi

        # Cek apakah HWID dipakai user lain
        cursor = await db.execute(
            'SELECT discord_id FROM users WHERE hwid = ? AND discord_id != ?',
            (hwid, member.id)
        )
        existing = await cursor.fetchone()
        if existing:
            await ctx.send(f"❌ HWID `{hwid}` already used by another user!")
            return

        expiry_date = datetime.utcnow() + timedelta(days=expiry_days)
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
                f"⏰ Expired pada: `{expiry_date.strftime('%Y-%m-%d %H:%M UTC')}`\n"
                f"⏳ Durasi: **{expiry_days} hari**"
            )
        except discord.Forbidden:
            await ctx.send("⚠️ Gagal mengirim DM. Pastikan user mengizinkan DM dari server ini.")

        await ctx.send(
            f"✅ HWID `{hwid}` verified for {member.display_name}!\n"
            f"⏰ Expiry: **{expiry_days} days** "
            f"({expiry_date.strftime('%Y-%m-%d %H:%M UTC')})"
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
        if current_expiry_str:
            try:
                current_expiry = datetime.fromisoformat(current_expiry_str)
                # Jika sudah expired, hitung dari now
                if current_expiry < datetime.utcnow():
                    current_expiry = datetime.utcnow()
            except:
                current_expiry = datetime.utcnow()
        else:
            current_expiry = datetime.utcnow()

        new_expiry = current_expiry + timedelta(days=additional_days)
        await db.execute(
            'UPDATE users SET expiry_date = ?, verified = 1 WHERE discord_id = ?',
            (new_expiry.isoformat(), member.id)
        )
        await db.commit()

        await ctx.send(
            f"✅ Extended {member.display_name}'s expiry by **{additional_days} days**!\n"
            f"🆕 New expiry: `{new_expiry.strftime('%Y-%m-%d %H:%M UTC')}`"
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

        now_utc = datetime.utcnow()
        embed = discord.Embed(
            title=f"Verified Users ({len(rows)})",
            color=discord.Color.green()
        )
        for row in rows[:10]:
            discord_id, username, hwid, verified_at, expiry_date = row
            expired = False
            if expiry_date:
                try:
                    if now_utc > datetime.fromisoformat(expiry_date):
                        expired = True
                except:
                    pass
            status = "⏰ EXPIRED" if expired else "🟢 Active"
            embed.add_field(
                name=f"<@{discord_id}>",
                value=f"HWID: `{hwid[:8]}...`\nVerified: {verified_at}\nExpiry: {expiry_date or 'N/A'}\nStatus: {status}",
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
            now_utc = datetime.utcnow()
            expired = False
            if expiry_date:
                try:
                    if now_utc > datetime.fromisoformat(expiry_date):
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
            embed.add_field(name="Expiry Date", value=expiry_date or "Not set", inline=False)
            embed.add_field(
                name="Status",
                value="⏰ EXPIRED" if expired else "🟢 Active",
                inline=False
            )
            await ctx.send(embed=embed)
        else:
            await ctx.send("❌ No HWID registered for you yet. Contact admin to verify.")

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
            if datetime.utcnow() > expiry_dt:
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
                # Cek expiry
                expiry_str = row[2]
                if expiry_str:
                    try:
                        if datetime.utcnow() > datetime.fromisoformat(expiry_str):
                            return None  # expired
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
