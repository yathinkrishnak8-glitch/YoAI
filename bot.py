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

# -------------------- Flask Web Dashboard --------------------
flask_app = Flask(__name__)
flask_app.secret_key = FLASK_SECRET

# HTML template (single page with login + dashboard)
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>YoAI Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }
        body {
            background: linear-gradient(135deg, #1e1e2f 0%, #2a2a40 100%);
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            color: #fff;
        }
        .glass-panel {
            background: rgba(255, 255, 255, 0.1);
            backdrop-filter: blur(10px);
            border-radius: 20px;
            padding: 30px;
            width: 90%;
            max-width: 600px;
            box-shadow: 0 8px 32px rgba(0,0,0,0.3);
            border: 1px solid rgba(255,255,255,0.2);
        }
        h1 { text-align: center; margin-bottom: 20px; font-weight: 300; letter-spacing: 2px; }
        .login-form { display: flex; flex-direction: column; gap: 15px; }
        input[type="password"] {
            padding: 15px;
            border: none;
            border-radius: 10px;
            background: rgba(255,255,255,0.2);
            color: white;
            font-size: 16px;
        }
        input[type="password"]::placeholder { color: rgba(255,255,255,0.6); }
        button {
            padding: 15px;
            border: none;
            border-radius: 10px;
            background: #6c5ce7;
            color: white;
            font-size: 16px;
            cursor: pointer;
            transition: background 0.3s;
        }
        button:hover { background: #5b4bc4; }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 20px;
            margin-top: 30px;
        }
        .stat-card {
            background: rgba(0,0,0,0.3);
            border-radius: 15px;
            padding: 20px;
            text-align: center;
            backdrop-filter: blur(5px);
        }
        .stat-value { font-size: 2em; font-weight: bold; color: #a8e6cf; }
        .stat-label { font-size: 0.9em; opacity: 0.8; margin-top: 5px; }
        .logout-btn {
            margin-top: 20px;
            background: #d63031;
        }
        .logout-btn:hover { background: #c0392b; }
        @media (max-width: 600px) {
            .glass-panel { padding: 20px; }
            .stats-grid { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>
    <div class="glass-panel" id="app">
        <h1>🔐 YoAI System</h1>
        <div id="login-view">
            <form class="login-form" onsubmit="login(event)">
                <input type="password" id="password" placeholder="Enter password" required>
                <button type="submit">Login</button>
            </form>
        </div>
        <div id="dashboard-view" style="display: none;">
            <div class="stats-grid">
                <div class="stat-card">
                    <div class="stat-value" id="uptime">-</div>
                    <div class="stat-label">Uptime</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" id="queries">-</div>
                    <div class="stat-label">Total Queries</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value" id="memory">-</div>
                    <div class="stat-label">Memory Rows</div>
                </div>
            </div>
            <button class="logout-btn" onclick="logout()">Logout</button>
        </div>
    </div>
    <script>
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
                alert('Invalid password');
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
                // If unauthorized, show login again
                document.getElementById('login-view').style.display = 'block';
                document.getElementById('dashboard-view').style.display = 'none';
            }
        }

        async function logout() {
            await fetch('/logout', { method: 'POST', credentials: 'same-origin' });
            document.getElementById('login-view').style.display = 'block';
            document.getElementById('dashboard-view').style.display = 'none';
        }

        // Check if already logged in on page load
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