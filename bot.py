import discord
from discord import app_commands
from discord.ext import commands
import google.generativeai as genai
from flask import Flask, request, session, jsonify, render_template_string
import threading
import sqlite3
import os
import random
import time
import asyncio
import datetime
import json
import secrets
from typing import List, Dict, Any, Optional

# -------------------- Configuration & Globals --------------------
START_TIME = time.time()
TOTAL_QUERIES = 0
DB_LOCK = threading.Lock()
DB_PATH = "yoai.db"

# Load Gemini API keys from environment variable (comma-separated)
GEMINI_KEYS = os.environ.get("GEMINI_API_KEYS", "").split(",")
if not GEMINI_KEYS or GEMINI_KEYS == [""]:
    raise ValueError("GEMINI_API_KEYS environment variable not set or empty")

# Flask secret key (random per run, but can be overridden via env for production)
FLASK_SECRET = os.environ.get("FLASK_SECRET", secrets.token_hex(32))

# Render provides PORT env var
PORT = int(os.environ.get("PORT", 5000))

# -------------------- Token Retrieval (handle multiple possible env var names) --------------------
def get_discord_token() -> str:
    """Retrieve Discord token from environment variables, checking common names."""
    token = os.environ.get("DISCORD_BOT_TOKEN") or os.environ.get("DISCORD_TOKEN")
    if not token:
        raise ValueError(
            "Discord token not found. Please set DISCORD_BOT_TOKEN or DISCORD_TOKEN environment variable."
        )
    return token

# -------------------- Database Setup --------------------
def init_db():
    """Create all necessary tables if they don't exist."""
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        # Global configuration (key-value)
        c.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
        # User personality presets
        c.execute("CREATE TABLE IF NOT EXISTS user_personality (user_id INTEGER PRIMARY KEY, preset TEXT)")
        # Allowed channels per guild for auto-reply
        c.execute("CREATE TABLE IF NOT EXISTS allowed_channels (guild_id INTEGER, channel_id INTEGER, PRIMARY KEY (guild_id, channel_id))")
        # Message history per channel (for context compression)
        c.execute("""CREATE TABLE IF NOT EXISTS message_history (
            channel_id INTEGER, message_id INTEGER PRIMARY KEY, author_id INTEGER,
            content TEXT, timestamp INTEGER
        )""")
        # Insert default system prompt if not present
        c.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('system_prompt', 'You are YoAI, a helpful AI assistant.')")
        conn.commit()
        conn.close()

init_db()

# -------------------- Gemini Load Balancer with Full Key Fallback --------------------
class GeminiKeyManager:
    def __init__(self, keys: List[str]):
        self.keys = keys
    
    def count(self) -> int:
        return len(self.keys)
    
    def generate_with_fallback(self, model_name: str, contents: Any, system_instruction: Optional[str] = None, max_retries: Optional[int] = None) -> str:
        """
        Attempt to generate content using random keys, falling back through all available keys.
        By default, tries all keys sequentially until one succeeds.
        Returns the generated text or raises exception if all keys fail.
        """
        if max_retries is None:
            max_retries = len(self.keys)  # Try all keys
        
        # Shuffle keys to randomize order
        shuffled_keys = random.sample(self.keys, len(self.keys))
        last_error = None
        
        for attempt, key in enumerate(shuffled_keys[:max_retries]):
            try:
                genai.configure(api_key=key)
                model = genai.GenerativeModel(model_name, system_instruction=system_instruction)
                response = model.generate_content(contents)
                return response.text
            except Exception as e:
                last_error = e
                print(f"Key {key[:8]}... failed (attempt {attempt+1}/{max_retries}): {e}")
                continue
        
        # If we exhausted all retries, raise the last error
        raise last_error or Exception("All Gemini keys failed")

key_manager = GeminiKeyManager(GEMINI_KEYS)

# -------------------- Helper Functions --------------------
def get_system_prompt() -> str:
    """Retrieve the global system prompt from database."""
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT value FROM config WHERE key='system_prompt'")
        result = c.fetchone()
        conn.close()
        return result[0] if result else "You are YoAI, a helpful AI assistant."

def set_system_prompt(prompt: str):
    """Update the global system prompt."""
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('system_prompt', ?)", (prompt,))
        conn.commit()
        conn.close()

def get_user_personality(user_id: int) -> str:
    """Get personality preset for a user (default: 'default')."""
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT preset FROM user_personality WHERE user_id=?", (user_id,))
        result = c.fetchone()
        conn.close()
        return result[0] if result else "default"

def set_user_personality(user_id: int, preset: str):
    """Set personality preset for a user."""
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO user_personality (user_id, preset) VALUES (?, ?)", (user_id, preset))
        conn.commit()
        conn.close()

def is_channel_allowed(guild_id: Optional[int], channel_id: int) -> bool:
    """Check if a channel is in the allowed list for its guild (None for DM)."""
    if guild_id is None:  # DM channels are always allowed
        return True
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT 1 FROM allowed_channels WHERE guild_id=? AND channel_id=?", (guild_id, channel_id))
        result = c.fetchone()
        conn.close()
        return result is not None

def add_allowed_channel(guild_id: int, channel_id: int):
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO allowed_channels (guild_id, channel_id) VALUES (?, ?)", (guild_id, channel_id))
        conn.commit()
        conn.close()

def remove_allowed_channel(guild_id: int, channel_id: int):
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("DELETE FROM allowed_channels WHERE guild_id=? AND channel_id=?", (guild_id, channel_id))
        conn.commit()
        conn.close()

def add_message_to_history(channel_id: int, message_id: int, author_id: int, content: str, timestamp: int):
    """Insert a message into history, then if count > 20, compress oldest ones."""
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        # Insert new message
        c.execute("INSERT OR REPLACE INTO message_history (channel_id, message_id, author_id, content, timestamp) VALUES (?, ?, ?, ?, ?)",
                  (channel_id, message_id, author_id, content, timestamp))
        # Count messages for this channel
        c.execute("SELECT COUNT(*) FROM message_history WHERE channel_id=?", (channel_id,))
        count = c.fetchone()[0]
        if count > 20:
            # Fetch oldest 10 messages (sorted by timestamp, then message_id to break ties)
            c.execute("""SELECT message_id, author_id, content, timestamp FROM message_history 
                         WHERE channel_id=? ORDER BY timestamp ASC, message_id ASC LIMIT 10""", (channel_id,))
            oldest = c.fetchall()
            if oldest:
                # Build a text block to summarize
                texts = []
                for mid, aid, cnt, ts in oldest:
                    # Skip if it's already a summary (author_id=0)
                    if aid != 0:
                        texts.append(f"User {aid}: {cnt}")
                if texts:
                    summary_text = summarize_with_gemini("\n".join(texts))
                    # Delete the oldest 10 messages
                    oldest_ids = [mid for mid, _, _, _ in oldest]
                    c.execute(f"DELETE FROM message_history WHERE message_id IN ({','.join('?'*len(oldest_ids))})", oldest_ids)
                    # Insert summary message with author_id=0 (system) and timestamp = oldest[0][3] (first message's timestamp)
                    summary_timestamp = oldest[0][3]
                    c.execute("INSERT INTO message_history (channel_id, message_id, author_id, content, timestamp) VALUES (?, ?, 0, ?, ?)",
                              (channel_id, -1, summary_text, summary_timestamp))  # Use negative dummy message_id to avoid collisions
        conn.commit()
        conn.close()

def get_channel_history(channel_id: int, limit: int = 20) -> List[Dict[str, Any]]:
    """Retrieve last N messages for a channel (ordered by timestamp)."""
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("""SELECT author_id, content, timestamp FROM message_history 
                     WHERE channel_id=? ORDER BY timestamp ASC, message_id ASC LIMIT ?""", (channel_id, limit))
        rows = c.fetchall()
        conn.close()
        return [{"author_id": row[0], "content": row[1], "timestamp": row[2]} for row in rows]

def summarize_with_gemini(text: str) -> str:
    """Use Gemini to summarize a block of text with fallback across all keys."""
    try:
        # Use all keys for fallback (max_retries = total keys)
        return key_manager.generate_with_fallback(
            model_name='gemini-1.5-flash',
            contents=f"Summarize the following conversation concisely, preserving key points:\n{text}",
            max_retries=key_manager.count()  # Try all keys
        )
    except Exception as e:
        print(f"Summarization error after fallback: {e}")
        return "[Summary unavailable]"

async def generate_ai_response(channel_id: int, user_message: str, author_id: int) -> str:
    """Generate a response using Gemini with context and personality, with key fallback."""
    global TOTAL_QUERIES
    TOTAL_QUERIES += 1

    # Get channel history
    history = get_channel_history(channel_id, limit=20)
    
    # Build conversation context
    context = ""
    for msg in history:
        if msg["author_id"] == 0:
            context += f"[Summary]: {msg['content']}\n"
        else:
            context += f"User {msg['author_id']}: {msg['content']}\n"
    context += f"User {author_id}: {user_message}\nYoAI:"

    # Get system prompt and personality
    system = get_system_prompt()
    personality = get_user_personality(author_id)
    if personality == "hacker":
        system += " Respond like a hacker, using leetspeak and tech jargon."
    elif personality == "tsundere":
        system += " Respond like a tsundere anime character, with a mix of harshness and hidden kindness."

    try:
        # Use all keys for fallback (max_retries = total keys)
        response_text = key_manager.generate_with_fallback(
            model_name='gemini-1.5-flash',
            contents=context,
            system_instruction=system,
            max_retries=key_manager.count()  # Try all keys
        )
        return response_text
    except Exception as e:
        print(f"AI generation error after fallback: {e}")
        return "I'm having trouble thinking right now. Please try again later."

# -------------------- Discord Bot --------------------
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

class YoAIBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        # No manual tree assignment; base class provides bot.tree

bot = YoAIBot()

# -------------------- Slash Commands --------------------
@bot.tree.command(name="core", description="Override the global system prompt")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def core(interaction: discord.Interaction, directive: str):
    set_system_prompt(directive)
    await interaction.response.send_message(f"System prompt updated to:\n{directive}", ephemeral=True)

@bot.tree.command(name="personality", description="Choose your interaction style")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.choices(preset=[
    app_commands.Choice(name="Default", value="default"),
    app_commands.Choice(name="Hacker", value="hacker"),
    app_commands.Choice(name="Tsundere", value="tsundere"),
])
async def personality(interaction: discord.Interaction, preset: app_commands.Choice[str]):
    set_user_personality(interaction.user.id, preset.value)
    await interaction.response.send_message(f"Personality set to **{preset.name}**", ephemeral=True)

@bot.tree.command(name="hack", description="Prank a user with a fake hacking sequence")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def hack(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer()
    fake_searches = [
        "how to become a meme lord", "why is my cat ignoring me", "secret discord admin powers",
        "what does 'sus' really mean", "how to fake being productive", "is water wet?",
        "how to train your dragon irl", "anime waifu tier list", "how to hack (jk)"
    ]
    searches = random.sample(fake_searches, k=3)
    msg = await interaction.followup.send(f"`Initiating hack on {user.display_name}...`")
    await asyncio.sleep(1)
    await msg.edit(content=f"`Bypassing firewalls... [█░░░░░░░░░] 10%`")
    await asyncio.sleep(1)
    await msg.edit(content=f"`Cracking passwords... [███░░░░░░░] 30%`")
    await asyncio.sleep(1)
    await msg.edit(content=f"`Accessing search history... [██████░░░░] 60%`")
    await asyncio.sleep(1)
    await msg.edit(content=f"`Downloading data... [█████████░] 90%`")
    await asyncio.sleep(1)
    await msg.edit(content=f"`Hack complete! Leaked search history for {user.display_name}:`\n" +
                   "\n".join([f"- {s}" for s in searches]))

@bot.tree.command(name="info", description="Bot statistics")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def info(interaction: discord.Interaction):
    uptime_seconds = int(time.time() - START_TIME)
    uptime_str = str(datetime.timedelta(seconds=uptime_seconds))
    embed = discord.Embed(title="YoAI System Info", color=0x00ff00)
    embed.add_field(name="Ping", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="Uptime", value=uptime_str, inline=True)
    embed.add_field(name="Active Gemini Keys", value=key_manager.count(), inline=True)
    embed.add_field(name="Total Queries", value=TOTAL_QUERIES, inline=True)
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM message_history")
        rows = c.fetchone()[0]
        conn.close()
    embed.add_field(name="Memory Rows", value=rows, inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="setchannel", description="Allow/Disallow bot auto-reply in this channel (Admin only)")
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.default_permissions(manage_channels=True)
async def setchannel(interaction: discord.Interaction, enabled: bool):
    if not interaction.guild:
        await interaction.response.send_message("This command can only be used in servers.", ephemeral=True)
        return
    channel = interaction.channel
    if enabled:
        add_allowed_channel(interaction.guild_id, channel.id)
        await interaction.response.send_message(f"✅ YoAI will now auto-reply in {channel.mention}", ephemeral=True)
    else:
        remove_allowed_channel(interaction.guild_id, channel.id)
        await interaction.response.send_message(f"❌ YoAI will no longer auto-reply in {channel.mention}", ephemeral=True)

# -------------------- Message Handling (Auto-reply) --------------------
@bot.event
async def on_message(message: discord.Message):
    # Ignore bot's own messages
    if message.author == bot.user:
        return

    # Store every message in history for context
    add_message_to_history(
        channel_id=message.channel.id,
        message_id=message.id,
        author_id=message.author.id,
        content=message.content,
        timestamp=int(message.created_at.timestamp())
    )

    # Determine if we should reply
    should_reply = False
    if message.guild is None:  # DM
        should_reply = True
    else:
        if is_channel_allowed(message.guild.id, message.channel.id):
            should_reply = True
        # Optionally also reply if mentioned (can be added, but not required by spec)

    if should_reply:
        async with message.channel.typing():
            response = await generate_ai_response(message.channel.id, message.content, message.author.id)
            await message.reply(response)

    # Allow commands to be processed (if any)
    await bot.process_commands(message)

# -------------------- Flask Web Dashboard (Modern Anime-Themed) --------------------
flask_app = Flask(__name__)
flask_app.secret_key = FLASK_SECRET

# HTML template with anime aesthetic, liquid glass, particles, and motion VFX
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>YoAI · Anime Dashboard</title>
    <!-- Fonts & Icons -->
    <link href="https://fonts.googleapis.com/css2?family=Inter:ital,wght@0,400;0,600;0,700;1,400&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0-beta3/css/all.min.css">
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        body {
            font-family: 'Inter', sans-serif;
            min-height: 100vh;
            background: radial-gradient(circle at 20% 30%, #1a0b2e, #0d071a);
            overflow: hidden;
            position: relative;
            color: #fff;
        }
        /* Animated particle canvas */
        #particle-canvas {
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            z-index: 0;
            pointer-events: none;
        }
        /* Main glass container */
        .glass-container {
            position: relative;
            z-index: 10;
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            padding: 1.5rem;
        }
        .glass-panel {
            background: rgba(20, 10, 35, 0.3);
            backdrop-filter: blur(20px);
            -webkit-backdrop-filter: blur(20px);
            border-radius: 3rem;
            padding: 2.5rem 2rem;
            width: 100%;
            max-width: 700px;
            box-shadow: 0 25px 50px -8px rgba(0, 0, 0, 0.6), 0 0 0 2px rgba(255, 120, 240, 0.2) inset, 0 0 20px #ff6ec7;
            border: 1px solid rgba(255, 180, 255, 0.3);
            transition: transform 0.3s ease, box-shadow 0.4s ease;
            animation: float 6s infinite alternate ease-in-out;
        }
        .glass-panel:hover {
            transform: scale(1.01) translateY(-5px);
            box-shadow: 0 30px 60px -8px #ff6ec780, 0 0 0 3px rgba(255, 120, 240, 0.5) inset, 0 0 40px #ffb3ff;
        }
        @keyframes float {
            0% { transform: translateY(0px); }
            100% { transform: translateY(-12px); }
        }
        /* Anime character decoration */
        .anime-decor {
            position: absolute;
            top: -30px;
            right: -20px;
            width: 150px;
            height: 150px;
            background: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><circle cx="50" cy="45" r="25" fill="%23ffb3ff" opacity="0.3"/><circle cx="40" cy="40" r="5" fill="white"/><circle cx="60" cy="40" r="5" fill="white"/><path d="M40 60 Q50 70, 60 60" stroke="white" stroke-width="3" fill="none"/></svg>') no-repeat center;
            background-size: contain;
            opacity: 0.5;
            animation: bounce 4s infinite;
        }
        @keyframes bounce {
            0%,100%{ transform: translateY(0) rotate(0deg); }
            50%{ transform: translateY(-15px) rotate(5deg); }
        }
        /* Header */
        h1 {
            font-size: 3.2rem;
            font-weight: 700;
            background: linear-gradient(135deg, #fff, #ffb3ff, #a5d8ff);
            -webkit-background-clip: text;
            background-clip: text;
            color: transparent;
            margin-bottom: 0.5rem;
            letter-spacing: -0.02em;
            filter: drop-shadow(0 0 15px #ff9eff);
        }
        .subtitle {
            font-size: 1rem;
            color: #d9b3ff;
            margin-bottom: 2rem;
            border-left: 4px solid #ff99ff;
            padding-left: 1rem;
            font-style: italic;
        }
        /* Login form */
        .login-form {
            display: flex;
            flex-direction: column;
            gap: 1.5rem;
            margin: 2rem 0;
        }
        .input-group {
            position: relative;
        }
        .input-group i {
            position: absolute;
            left: 20px;
            top: 50%;
            transform: translateY(-50%);
            color: #ffb3ff;
            font-size: 1.3rem;
        }
        input[type="password"] {
            width: 100%;
            padding: 1.2rem 1.2rem 1.2rem 3.5rem;
            border: none;
            border-radius: 60px;
            background: rgba(255, 255, 255, 0.08);
            backdrop-filter: blur(8px);
            color: white;
            font-size: 1.2rem;
            border: 1px solid rgba(255, 200, 255, 0.3);
            transition: all 0.3s;
        }
        input[type="password"]:focus {
            outline: none;
            border-color: #ffaaff;
            box-shadow: 0 0 25px #ffaaff;
            background: rgba(255, 255, 255, 0.15);
        }
        input[type="password"]::placeholder {
            color: rgba(255, 200, 255, 0.7);
            font-weight: 300;
        }
        button {
            padding: 1.2rem;
            border: none;
            border-radius: 60px;
            background: linear-gradient(45deg, #b86eff, #ff7ce7);
            color: white;
            font-size: 1.3rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s;
            box-shadow: 0 8px 20px #b86eff80;
            border: 1px solid rgba(255,255,255,0.3);
            letter-spacing: 1px;
        }
        button:hover {
            transform: scale(1.02);
            box-shadow: 0 15px 30px #ff7ce7;
            background: linear-gradient(45deg, #c47cff, #ff95f0);
        }
        /* Dashboard stats */
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
            gap: 1.5rem;
            margin: 2.5rem 0;
        }
        .stat-card {
            background: rgba(255, 255, 255, 0.05);
            backdrop-filter: blur(4px);
            border-radius: 2rem;
            padding: 1.5rem 1rem;
            text-align: center;
            border: 1px solid rgba(255, 180, 255, 0.2);
            transition: 0.3s;
            box-shadow: 0 8px 20px rgba(0,0,0,0.4);
        }
        .stat-card:hover {
            background: rgba(255, 200, 255, 0.1);
            border-color: #ffaaff;
            transform: translateY(-6px) scale(1.02);
            box-shadow: 0 20px 30px #ff6ec780;
        }
        .stat-value {
            font-size: 2.5rem;
            font-weight: 700;
            background: linear-gradient(135deg, #fff, #ffd6ff);
            -webkit-background-clip: text;
            background-clip: text;
            color: transparent;
            line-height: 1.2;
        }
        .stat-label {
            font-size: 0.9rem;
            text-transform: uppercase;
            letter-spacing: 2px;
            color: #ccaaff;
            margin-top: 0.5rem;
        }
        .logout-btn {
            background: transparent;
            border: 2px solid #ff7ce7;
            color: #ffb3ff;
            box-shadow: none;
            margin-top: 1rem;
        }
        .logout-btn:hover {
            background: #ff7ce7;
            color: #1a0b2e;
            border-color: #ff7ce7;
        }
        /* Mobile adaptation */
        @media (max-width: 600px) {
            .glass-panel { padding: 2rem 1.5rem; border-radius: 2rem; }
            h1 { font-size: 2.5rem; }
            .stat-value { font-size: 2rem; }
            .anime-decor { width: 100px; height: 100px; top: -20px; right: -10px; }
        }
        /* small extra animation on cards */
        .stat-card i {
            font-size: 2rem;
            color: #ffb3ff;
            margin-bottom: 0.5rem;
            display: block;
        }
    </style>
</head>
<body>
    <canvas id="particle-canvas"></canvas>
    <div class="glass-container">
        <div class="glass-panel">
            <div class="anime-decor"></div>
            <h1>✨ YoAI ✨</h1>
            <div class="subtitle">where intelligence meets aesthetics</div>

            <!-- LOGIN VIEW -->
            <div id="login-view">
                <form class="login-form" onsubmit="login(event)">
                    <div class="input-group">
                        <i class="fas fa-lock"></i>
                        <input type="password" id="password" placeholder="secret phrase" required>
                    </div>
                    <button type="submit"><i class="fas fa-sign-in-alt" style="margin-right: 8px;"></i> Enter the system</button>
                </form>
            </div>

            <!-- DASHBOARD VIEW (hidden by default) -->
            <div id="dashboard-view" style="display: none;">
                <div class="stats-grid">
                    <div class="stat-card">
                        <i class="fas fa-clock"></i>
                        <div class="stat-value" id="uptime">-</div>
                        <div class="stat-label">Uptime</div>
                    </div>
                    <div class="stat-card">
                        <i class="fas fa-database"></i>
                        <div class="stat-value" id="queries">-</div>
                        <div class="stat-label">Queries</div>
                    </div>
                    <div class="stat-card">
                        <i class="fas fa-memory"></i>
                        <div class="stat-value" id="memory">-</div>
                        <div class="stat-label">Memory rows</div>
                    </div>
                </div>
                <button class="logout-btn" onclick="logout()"><i class="fas fa-door-open"></i> Logout</button>
            </div>
        </div>
    </div>

    <script>
        // Particle animation (floating sakura-like particles)
        const canvas = document.getElementById('particle-canvas');
        const ctx = canvas.getContext('2d');
        let width, height;
        let particles = [];

        function initParticles() {
            particles = [];
            const count = 70;
            for (let i = 0; i < count; i++) {
                particles.push({
                    x: Math.random() * width,
                    y: Math.random() * height,
                    size: Math.random() * 6 + 2,
                    speedX: (Math.random() - 0.5) * 0.2,
                    speedY: Math.random() * 0.5 + 0.2,
                    color: `rgba(255, ${Math.floor(150 + Math.random() * 80)}, 255, ${Math.random() * 0.4 + 0.2})`
                });
            }
        }

        function resizeCanvas() {
            width = window.innerWidth;
            height = window.innerHeight;
            canvas.width = width;
            canvas.height = height;
            initParticles();
        }

        function drawParticles() {
            ctx.clearRect(0, 0, width, height);
            for (let p of particles) {
                ctx.beginPath();
                ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
                ctx.fillStyle = p.color;
                ctx.fill();
                // move
                p.x += p.speedX;
                p.y += p.speedY;
                // wrap around
                if (p.x > width) p.x = 0;
                if (p.x < 0) p.x = width;
                if (p.y > height) p.y = 0;
            }
            requestAnimationFrame(drawParticles);
        }

        window.addEventListener('resize', resizeCanvas);
        resizeCanvas();
        drawParticles();

        // ---------- Login / Dashboard logic ----------
        async function login(event) {
            event.preventDefault();
            const pwd = document.getElementById('password').value;
            const res = await fetch('/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ password: pwd }),
                credentials: 'same-origin'
            });
            if (res.ok) {
                document.getElementById('login-view').style.display = 'none';
                document.getElementById('dashboard-view').style.display = 'block';
                fetchStats();
                setInterval(fetchStats, 3000);
            } else {
                alert('🔮 Incorrect password, try again.');
            }
        }

        async function fetchStats() {
            try {
                const res = await fetch('/api/stats', { credentials: 'same-origin' });
                if (!res.ok) throw new Error('Not authorized');
                const data = await res.json();
                document.getElementById('uptime').innerText = data.uptime;
                document.getElementById('queries').innerText = data.total_queries;
                document.getElementById('memory').innerText = data.active_memory_rows;
            } catch (e) {
                console.error(e);
                document.getElementById('login-view').style.display = 'block';
                document.getElementById('dashboard-view').style.display = 'none';
            }
        }

        async function logout() {
            await fetch('/logout', { method: 'POST', credentials: 'same-origin' });
            document.getElementById('login-view').style.display = 'block';
            document.getElementById('dashboard-view').style.display = 'none';
        }

        window.onload = async () => {
            const res = await fetch('/api/stats', { credentials: 'same-origin' });
            if (res.ok) {
                document.getElementById('login-view').style.display = 'none';
                document.getElementById('dashboard-view').style.display = 'block';
                fetchStats();
                setInterval(fetchStats, 3000);
            }
        };
    </script>
</body>
</html>
"""

@flask_app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@flask_app.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    if data and data.get('password') == "mr_yaen":
        session['logged_in'] = True
        return jsonify(success=True)
    return jsonify(success=False), 401

@flask_app.route('/logout', methods=['POST'])
def logout():
    session.pop('logged_in', None)
    return jsonify(success=True)

@flask_app.route('/api/stats')
def api_stats():
    if not session.get('logged_in'):
        return jsonify(error="Unauthorized"), 401
    uptime_seconds = int(time.time() - START_TIME)
    uptime_str = str(datetime.timedelta(seconds=uptime_seconds))
    with DB_LOCK:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM message_history")
        rows = c.fetchone()[0]
        conn.close()
    return jsonify({
        "uptime": uptime_str,
        "total_queries": TOTAL_QUERIES,
        "active_memory_rows": rows
    })

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# -------------------- Main --------------------
if __name__ == "__main__":
    # Start Flask in a background thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    # Run Discord bot (blocking) with token from helper function
    bot.run(get_discord_token())