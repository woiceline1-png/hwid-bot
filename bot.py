import discord
from discord.ext import commands
import aiosqlite
import asyncio
import os
from flask import Flask, request, jsonify
from flask_cors import CORS
import threading
import json
import sys

# ===== KONFIGURASI =====
TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN:
    print("❌ DISCORD_TOKEN tidak ditemukan di environment variable!")
    sys.exit(1)

DATABASE_FILE = "hwid.db"
API_PORT = int(os.environ.get("PORT", 8080))

# ===== DATABASE =====
async def init_db():
    try:
        async with aiosqlite.connect(DATABASE_FILE) as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    discord_id INTEGER PRIMARY KEY,
                    username TEXT,
                    hwid TEXT UNIQUE,
                    verified INTEGER DEFAULT 0,
                    verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            await db.commit()
            print("✅ Database initialized")
    except Exception as e:
        print(f"❌ Database error: {e}")

# ===== DISCORD BOT =====
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    await init_db()
    print(f'✅ Bot ready! Logged in as {bot.user}')
    print(f'✅ Bot is in {len(bot.guilds)} guilds')

@bot.command(name='checkhwid')
@commands.has_permissions(administrator=True)
async def check_hwid(ctx, member: discord.Member = None):
    if member is None:
        member = ctx.author
    async with aiosqlite.connect(DATABASE_FILE) as db:
        cursor = await db.execute('SELECT hwid, verified, verified_at FROM users WHERE discord_id = ?', (member.id,))
        row = await cursor.fetchone()
        if row:
            hwid, verified, verified_at = row
            embed = discord.Embed(
                title=f"HWID Info for {member.display_name}",
                color=discord.Color.green() if verified else discord.Color.red()
            )
            embed.add_field(name="HWID", value=f"`{hwid}`", inline=False)
            embed.add_field(name="Verified", value="✅ Yes" if verified else "❌ No", inline=True)
            embed.add_field(name="Verified At", value=verified_at or "Never", inline=True)
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"❌ No HWID registered for {member.display_name}")

@bot.command(name='verifyhwid')
@commands.has_permissions(administrator=True)
async def verify_hwid(ctx, member: discord.Member, hwid: str):
    async with aiosqlite.connect(DATABASE_FILE) as db:
        cursor = await db.execute('SELECT discord_id FROM users WHERE hwid = ? AND discord_id != ?', (hwid, member.id))
        existing = await cursor.fetchone()
        if existing:
            await ctx.send(f"❌ HWID `{hwid}` already used by another user!")
            return
        await db.execute('''
            INSERT INTO users (discord_id, username, hwid, verified)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(discord_id) DO UPDATE SET
                hwid = excluded.hwid,
                verified = 1,
                verified_at = CURRENT_TIMESTAMP
        ''', (member.id, str(member), hwid))
        await db.commit()
        try:
            await member.send(f"✅ HWID Anda `{hwid}` telah diverifikasi! Anda sekarang bisa menggunakan aplikasi.")
        except discord.Forbidden:
            await ctx.send("⚠️ Gagal mengirim DM. Pastikan user mengizinkan DM dari server ini.")
        await ctx.send(f"✅ HWID `{hwid}` verified for {member.display_name}!")

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
        cursor = await db.execute('SELECT discord_id, username, hwid, verified_at FROM users WHERE verified = 1')
        rows = await cursor.fetchall()
        if not rows:
            await ctx.send("No verified users found.")
            return
        embed = discord.Embed(title=f"Verified Users ({len(rows)})", color=discord.Color.green())
        for row in rows[:10]:
            embed.add_field(
                name=f"<@{row[0]}>",
                value=f"HWID: `{row[2][:8]}...`\nVerified: {row[3]}",
                inline=False
            )
        await ctx.send(embed=embed)

@bot.command(name='myhwid')
async def my_hwid(ctx):
    async with aiosqlite.connect(DATABASE_FILE) as db:
        cursor = await db.execute('SELECT hwid, verified, verified_at FROM users WHERE discord_id = ?', (ctx.author.id,))
        row = await cursor.fetchone()
        if row:
            hwid, verified, verified_at = row
            embed = discord.Embed(
                title=f"Your HWID Status",
                color=discord.Color.green() if verified else discord.Color.red()
            )
            embed.add_field(name="HWID", value=f"`{hwid}`", inline=False)
            embed.add_field(name="Verified", value="✅ Yes" if verified else "❌ No", inline=True)
            embed.add_field(name="Verified At", value=verified_at or "Never", inline=True)
            await ctx.send(embed=embed)
        else:
            await ctx.send("❌ No HWID registered for you yet. Contact admin to verify.")

# ===== FLASK API =====
app = Flask(__name__)
CORS(app)

@app.route('/')
def home():
    return "✅ Bot is running on Railway!"

@app.route('/verify', methods=['GET'])
def verify_hwid_api():
    hwid = request.args.get('hwid')
    if not hwid:
        return jsonify({"error": "Missing HWID"}), 400
    async def check_db():
        async with aiosqlite.connect(DATABASE_FILE) as db:
            cursor = await db.execute('SELECT verified FROM users WHERE hwid = ?', (hwid,))
            row = await cursor.fetchone()
            return row is not None and row[0] == 1
    verified = asyncio.run(check_db())
    return jsonify({"verified": verified, "hwid": hwid})

@app.route('/getuser', methods=['GET'])
def get_user_from_hwid():
    hwid = request.args.get('hwid')
    if not hwid:
        return jsonify({"error": "Missing HWID"}), 400
    async def get_user():
        async with aiosqlite.connect(DATABASE_FILE) as db:
            cursor = await db.execute(
                'SELECT discord_id, username FROM users WHERE hwid = ? AND verified = 1',
                (hwid,)
            )
            row = await cursor.fetchone()
            if row:
                return {"discord_id": row[0], "username": row[1]}
            return None
    user = asyncio.run(get_user())
    if user:
        return jsonify(user)
    else:
        return jsonify({"error": "User not found or not verified"}), 404

def run_api():
    port = int(os.environ.get("PORT", 8080))
    print(f"🌐 Starting Flask API on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

# ===== RUN =====
if __name__ == "__main__":
    print("🚀 Starting HWID Bot...")
    api_thread = threading.Thread(target=run_api, daemon=True)
    api_thread.start()
    print("✅ API thread started")
    try:
        bot.run(TOKEN)
    except Exception as e:
        print(f"❌ Bot error: {e}")
        sys.exit(1)
