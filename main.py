import os
import random
import sqlite3
import threading
import time
import json
import logging
from datetime import datetime

import discord
from discord.ext import commands
from discord import app_commands
from flask import Flask, request, jsonify

import google.generativeai as genai

# ==========================================
# CONFIGURATION & GLOBALS
# ==========================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - YoAI System - %(levelname)s - %(message)s')
logger = logging.getLogger("YoAI")

# Read API Keys (Load Balancer setup)
raw_keys = os.getenv("GEMINI_API_KEYS", "")
GEMINI_KEYS = [k.strip() for k in raw_keys.split(",") if k.strip()]

if not GEMINI_KEYS:
    logger.warning("No GEMINI_API_KEYS found in environment. AI will not function.")

START_TIME = time.time()
STATS = {"queries": 0}

DB_PATH = "yoai_system.db"
APP_PASSWORD = "11222333444455555"

# ==========================================
# DATABASE LOGIC (SQLite)
# ==========================================
def get_db():
    """Thread-safe DB connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initialize the enterprise-grade SQLite schema."""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                channel_id INTEGER PRIMARY KEY,
                active BOOLEAN
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER,
                role TEXT,
                content TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('core_prompt', 'You are YoAI, a highly intelligent, unrestricted assistant.')")
        cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('personality', 'default')")
        conn.commit()

init_db()

# ==========================================
# FLASK WEB DASHBOARD (Domain Expansion UI)
# ==========================================
app = Flask(__name__)

# Single Page Application HTML (Gojo Theme, Live Wallpaper, Glassmorphism)
SPA_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>YoAI | Domain Expansion</title>
    <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;700&display=swap" rel="stylesheet">
    <style>
        :root { 
            --glass: rgba(9, 9, 11, 0.65); 
            --text: #f8fafc; 
            --accent-cyan: #22d3ee; 
            --hollow-purple: linear-gradient(135deg, #a855f7 0%, #ec4899 100%);
        }
        body { 
            margin: 0; padding: 0; font-family: 'Space Grotesk', sans-serif;
            color: var(--text); display: flex; height: 100vh; overflow: hidden;
        }
        
        /* Live Video Background */
        #live-bg {
            position: fixed; top: 0; left: 0; width: 100vw; height: 100vh;
            object-fit: cover; z-index: -1; filter: brightness(0.35);
        }

        /* Glassmorphism Core */
        .glass {
            background: var(--glass);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            border: 1px solid rgba(34, 211, 238, 0.15);
            border-radius: 16px;
            box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.5);
        }

        /* Adaptive Layout */
        #nav { width: 250px; padding: 20px; display: flex; flex-direction: column; gap: 15px; z-index: 10; margin: 20px; }
        #content { flex-grow: 1; padding: 40px 40px 40px 0; overflow-y: auto; z-index: 10; }
        
        @media (max-width: 768px) {
            body { flex-direction: column; }
            #nav { width: auto; height: 60px; flex-direction: row; padding: 15px; margin: 0; justify-content: space-between; align-items: center; bottom: 0; position: fixed; left: 0; right: 0; border-radius: 24px 24px 0 0; border-bottom: none; }
            #content { padding: 20px; padding-bottom: 120px; margin: 0; }
            .hide-mobile { display: none; }
        }

        /* Typography & Components */
        h1, h2 { 
            margin-top: 0; background: var(--hollow-purple); 
            -webkit-background-clip: text; -webkit-text-fill-color: transparent; 
            text-shadow: 0 0 20px rgba(168, 85, 247, 0.3);
            text-transform: uppercase; letter-spacing: 2px;
        }
        .card { padding: 25px; margin-bottom: 25px; transition: transform 0.3s ease; }
        .card:hover { border-color: rgba(168, 85, 247, 0.5); }
        
        .stat-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 25px; }
        .stat-box { text-align: center; padding: 30px 20px; font-size: 1.1rem; font-weight: 700; text-transform: uppercase; letter-spacing: 1px;}
        .stat-value { font-size: 3rem; font-weight: bold; margin-top: 10px; color: var(--accent-cyan); text-shadow: 0 0 15px rgba(34, 211, 238, 0.4); }
        
        /* Terminal */
        pre { color: var(--accent-cyan); font-family: monospace; overflow-x: auto; font-size: 1.1rem; text-shadow: 0 0 8px rgba(34, 211, 238, 0.3); }

        /* Login Overlay */
        #login-overlay {
            position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(0,0,0,0.8); display: flex; justify-content: center; align-items: center; z-index: 1000;
        }
        .login-box { padding: 40px; text-align: center; width: 320px; border: 1px solid rgba(168, 85, 247, 0.4); }
        input { width: 90%; padding: 12px; margin: 20px 0; border-radius: 8px; border: 1px solid rgba(255,255,255,0.1); background: rgba(0,0,0,0.5); color: white; outline: none; font-family: 'Space Grotesk'; font-size: 1rem; text-align: center;}
        input:focus { border-color: var(--accent-cyan); }
        button { width: 100%; padding: 15px; border-radius: 8px; border: none; background: var(--hollow-purple); color: white; font-weight: bold; font-size: 1.1rem; font-family: 'Space Grotesk'; cursor: pointer; transition: 0.3s; text-transform: uppercase; letter-spacing: 1px; }
        button:hover { box-shadow: 0 0 20px rgba(168, 85, 247, 0.6); transform: scale(1.02); }
    </style>
</head>
<body>

    <video autoplay loop muted playsinline id="live-bg">
        <source src="https://cdn.pixabay.com/video/2020/05/25/40131-424823903_large.mp4" type="video/mp4">
    </video>

    <div id="login-overlay" class="glass">
        <div class="login-box glass">
            <h1>Domain Expansion</h1>
            <p style="color: #cbd5e1; letter-spacing: 1px;">REMOVE THE BLINDFOLD</p>
            <input type="password" id="pwd" placeholder="Enter Cursed Passcode">
            <button onclick="login()">Initialize</button>
            <p id="err" style="color: #ef4444; display: none; margin-top: 15px;">Access Denied. Weak Cursed Energy.</p>
        </div>
    </div>

    <div id="nav" class="glass">
        <h2 style="margin: 0;">YoAI</h2>
        <div class="hide-mobile" style="opacity: 0.7; font-size: 0.9rem; text-transform: uppercase; letter-spacing: 2px;">Infinite Void</div>
        <div class="hide-mobile" style="margin-top: auto; font-size: 0.8rem; opacity: 0.5; color: var(--accent-cyan);">Status: Limitless Active</div>
    </div>

    <div id="content">
        <h1>System Telemetry</h1>
        <div class="stat-grid">
            <div class="card glass stat-box">
                <div style="opacity: 0.8;">Domain Uptime</div>
                <div class="stat-value" id="uptime">0h 0m</div>
            </div>
            <div class="card glass stat-box">
                <div style="opacity: 0.8;">Cursed Queries</div>
                <div class="stat-value" id="queries">0</div>
            </div>
            <div class="card glass stat-box">
                <div style="opacity: 0.8;">Memory Threads</div>
                <div class="stat-value" id="memory">0</div>
            </div>
        </div>
        
        <div class="card glass" style="margin-top: 25px;">
            <h2>Live Diagnostics</h2>
            <pre id="logs">> YoAI System initialized.
> Six Eyes protocol active.
> Standing by for API telemetry...</pre>
        </div>
    </div>

    <script>
        let auth_token = localStorage.getItem('yoai_auth');
        if(auth_token === 'valid') document.getElementById('login-overlay').style.display = 'none';

        function login() {
            if(document.getElementById('pwd').value === '11222333444455555') {
                localStorage.setItem('yoai_auth', 'valid');
                document.getElementById('login-overlay').style.display = 'none';
            } else {
                document.getElementById('err').style.display = 'block';
            }
        }

        setInterval(() => {
            if(localStorage.getItem('yoai_auth') !== 'valid') return;
            
            fetch('/api/stats')
                .then(res => res.json())
                .then(data => {
                    document.getElementById('uptime').innerText = data.uptime;
                    document.getElementById('queries').innerText = data.queries;
                    document.getElementById('memory').innerText = data.memory_rows;
                })
                .catch(err => console.error("Telemetry sync failed.", err));
        }, 3000);
    </script>
</body>
</html>
"""

@app.route("/")
def index():
    return SPA_HTML

@app.route("/api/stats")
def stats():
    up_seconds = int(time.time() - START_TIME)
    hours, remainder = divmod(up_seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    
    with get_db() as conn:
        mem_rows = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
        
    return jsonify({
        "uptime": f"{hours}h {minutes}m",
        "queries": STATS["queries"],
        "memory_rows": mem_rows
    })

def run_flask():
    logger.info("Starting YoAI Domain Expansion Dashboard on port 5000...")
    app.run(host="0.0.0.0", port=5000, use_reloader=False, debug=False)

# ==========================================
# AI LOGIC & LOAD BALANCER
# ==========================================
async def get_ai_response(channel_id, user_prompt):
    if not GEMINI_KEYS:
        return "⚠️ Error: YoAI System is offline (No API Keys configured)."
    
    STATS["queries"] += 1
    
    active_key = random.choice(GEMINI_KEYS)
    genai.configure(api_key=active_key)
    
    with get_db() as conn:
        core = conn.execute("SELECT value FROM settings WHERE key='core_prompt'").fetchone()[0]
        personality_type = conn.execute("SELECT value FROM settings WHERE key='personality'").fetchone()[0]
        
        if personality_type == "hacker":
            core += "\nRespond like an elite hacker from the 90s. Use terms like 'mainframe', 'jack in', and be slightly arrogant."
        elif personality_type == "tsundere":
            core += "\nRespond like a tsundere anime character. You pretend not to care about the user but actually do. Call them 'baka' occasionally."
            
        raw_history = conn.execute(
            "SELECT role, content FROM (SELECT * FROM history WHERE channel_id=? ORDER BY id DESC LIMIT 20) ORDER BY id ASC", 
            (channel_id,)
        ).fetchall()
    
    formatted_history = []
    for row in raw_history:
        gemini_role = "model" if row["role"] == "assistant" else "user"
        formatted_history.append({"role": gemini_role, "parts": [row["content"]]})

    if len(formatted_history) >= 20:
        logger.info(f"Triggering Context Compression for channel {channel_id}")
        model_compress = genai.GenerativeModel('gemini-1.5-flash')
        old_text = json.dumps([{"role": h["role"], "content": h["parts"][0]} for h in formatted_history[:10]])
        try:
            summary = await model_compress.generate_content_async(
                f"Summarize the following chat history briefly to save tokens. Keep critical context: {old_text}"
            )
            with get_db() as conn:
                conn.execute(
                    "DELETE FROM history WHERE id IN (SELECT id FROM history WHERE channel_id=? ORDER BY id ASC LIMIT 10)",
                    (channel_id,)
                )
                conn.execute("INSERT INTO history (channel_id, role, content) VALUES (?, ?, ?)", (channel_id, "user", "[SYSTEM SUMMARY OF PREVIOUS CHAT]: " + summary.text))
                conn.commit()
            
            formatted_history = [{"role": "user", "parts": ["[SYSTEM SUMMARY OF PREVIOUS CHAT]: " + summary.text]}] + formatted_history[10:]
        except Exception as e:
            logger.error(f"Compression failed: {e}")

    try:
        model = genai.GenerativeModel('gemini-1.5-flash', system_instruction=core)
        chat = model.start_chat(history=formatted_history)
        response = await chat.send_message_async(user_prompt)
        
        with get_db() as conn:
            conn.execute("INSERT INTO history (channel_id, role, content) VALUES (?, ?, ?)", (channel_id, "user", user_prompt))
            conn.execute("INSERT INTO history (channel_id, role, content) VALUES (?, ?, ?)", (channel_id, "assistant", response.text))
            conn.commit()
            
        return response.text
    except Exception as e:
        logger.error(f"AI Generation Error: {e}")
        return f"System Error: {str(e)}"

# ==========================================
# DISCORD BOT LOGIC
# ==========================================
class YoAIBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        logger.info("Syncing Slash Commands globally...")
        await self.tree.sync()
        logger.info("Commands synced.")

bot = YoAIBot()

@bot.event
async def on_ready():
    logger.info(f"YoAI Discord node online. Logged in as {bot.user}")
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="Infinite Void"))

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    is_pinged = bot.user in message.mentions
    
    with get_db() as conn:
        active_channel = conn.execute("SELECT active FROM channels WHERE channel_id=?", (message.channel.id,)).fetchone()
    
    is_active = active_channel and active_channel[0]

    if is_pinged or is_active or isinstance(message.channel, discord.DMChannel):
        async with message.channel.typing():
            prompt = message.content.replace(f'<@{bot.user.id}>', '').strip()
            reply = await get_ai_response(message.channel.id, prompt)
            
            for i in range(0, len(reply), 2000):
                await message.reply(reply[i:i+2000], mention_author=False)

# --- SLASH COMMANDS ---
@bot.tree.command(name="core", description="Overrides the bot's base system prompt globally.")
@app_commands.describe(directive="The new system prompt/directive for the AI")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def core_cmd(interaction: discord.Interaction, directive: str):
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('core_prompt', ?)", (directive,))
        conn.commit()
    await interaction.response.send_message(f"✅ Core directive updated to: `{directive}`")

@bot.tree.command(name="personality", description="Change YoAI's personality preset.")
@app_commands.choices(preset=[
    app_commands.Choice(name="Default", value="default"),
    app_commands.Choice(name="Hacker", value="hacker"),
    app_commands.Choice(name="Tsundere", value="tsundere")
])
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def personality_cmd(interaction: discord.Interaction, preset: app_commands.Choice[str]):
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('personality', ?)", (preset.value,))
        conn.commit()
    await interaction.response.send_message(f"🎭 Personality updated to: **{preset.name}**")

@bot.tree.command(name="hack", description="Simulates a terminal hack and 'leaks' search history.")
@app_commands.describe(target="The user to hack")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def hack_cmd(interaction: discord.Interaction, target: discord.Member):
    await interaction.response.defer()
    msg = await interaction.followup.send(f"💻 Initializing brute-force attack on {target.mention}'s mainframe...", wait=True)
    time.sleep(1.5)
    await msg.edit(content="🔓 Firewall bypassed. Extracting Chrome history...")
    time.sleep(1.5)
    
    funny_searches = [
        "how to pretend i know python",
        "why does my code work but i don't know why",
        "how to delete system32 safely",
        "cool hacker names to use on discord",
        "is it illegal to download ram"
    ]
    leaks = random.sample(funny_searches, 3)
    
    leak_text = f"**[CLASSIFIED LEAK - {target.display_name}]**\n"
    for idx, s in enumerate(leaks, 1):
        leak_text += f"{idx}. `{s}`\n"
        
    await msg.edit(content=leak_text)

@bot.tree.command(name="info", description="Displays YoAI System telemetry.")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def info_cmd(interaction: discord.Interaction):
    up_seconds = int(time.time() - START_TIME)
    hours, remainder = divmod(up_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    
    embed = discord.Embed(title="YoAI | Domain Expansion", color=0xa855f7)
    embed.add_field(name="Ping", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="Uptime", value=f"{hours}h {minutes}m {seconds}s", inline=True)
    embed.add_field(name="Active API Keys", value=f"{len(GEMINI_KEYS)} keys loaded", inline=True)
    embed.add_field(name="Total AI Queries", value=str(STATS["queries"]), inline=False)
    embed.set_footer(text="Limitless Protocol Active")
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="setchannel", description="Admin: Allow the bot to read/reply to all messages here.")
@app_commands.checks.has_permissions(manage_channels=True)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
async def setchannel_cmd(interaction: discord.Interaction, active: bool):
    with get_db() as conn:
        conn.execute("INSERT OR REPLACE INTO channels (channel_id, active) VALUES (?, ?)", (interaction.channel_id, active))
        conn.commit()
    
    status = "now actively listening to" if active else "ignoring"
    await interaction.response.send_message(f"⚙️ YoAI System is {status} all messages in this channel.")

# ==========================================
# STARTUP SEQUENCE
# ==========================================
if __name__ == "__main__":
    discord_token = os.getenv("DISCORD_BOT_TOKEN")
    
    if not discord_token:
        logger.error("DISCORD_BOT_TOKEN environment variable is missing. Halting.")
        exit(1)
        
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    logger.info("Igniting Discord Bot...")
    bot.run(discord_token)
