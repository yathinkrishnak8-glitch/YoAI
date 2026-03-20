import discord
from discord import app_commands
from discord.ext import commands, tasks
from quart import Quart, request, session, jsonify, render_template_string
import aiosqlite
import os
import random
import time
import asyncio
import datetime
import re
import gc  
import requests

try:
    import resource 
except ImportError:
    resource = None

# NEW GOOGLE SDK
from google import genai
from google.genai import types

# -------------------- Configuration & Globals --------------------
START_TIME = time.time()
TOTAL_QUERIES = 0
QUERY_TIMESTAMPS = [] 
DB_PATH = "yoai.db"
DB_CONN = None

# ASYNC TRAFFIC CONTROLLERS
DB_LOCK = asyncio.Lock()
ALERT_LOCK = asyncio.Lock()
LAST_ALERT_TIME = 0.0

CONFIG_CACHE = {}
CHANNEL_BUFFERS = {}
CHANNEL_TIMERS = {}

GEMINI_KEYS = os.environ.get("GEMINI_API_KEYS", "").split(",")
if not GEMINI_KEYS or GEMINI_KEYS == [""]:
    raise ValueError("GEMINI_API_KEYS environment variable not set or empty")

FLASK_SECRET = os.environ.get("FLASK_SECRET", "yoai_persistent_secret_key_123")
PORT = int(os.environ.get("PORT", 5000))

# -------------------- Database Setup --------------------
async def init_db():
    global DB_CONN
    if DB_CONN is None:
        DB_CONN = await aiosqlite.connect(DB_PATH)
        
    async with DB_LOCK:
        await DB_CONN.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")
        await DB_CONN.execute("CREATE TABLE IF NOT EXISTS allowed_channels (guild_id INTEGER, channel_id INTEGER, PRIMARY KEY (guild_id, channel_id))")
        await DB_CONN.execute("""CREATE TABLE IF NOT EXISTS message_history (
            channel_id INTEGER, message_id INTEGER PRIMARY KEY, author_id INTEGER,
            content TEXT, timestamp INTEGER
        )""")
        await DB_CONN.execute("""CREATE TABLE IF NOT EXISTS system_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL,
            user TEXT,
            trace TEXT
        )""")
        
        defaults = {
            'system_prompt': 'You are YoAI, a highly intelligent assistant.',
            'current_model': 'gemini-2.5-flash-lite',
            'global_personality': 'default',
            'status_type': 'watching',
            'status_text': 'over the Matrix',
            'response_delay': '0',
            'engine_status': 'online',
            'safety_hate': 'BLOCK_NONE',
            'safety_harassment': 'BLOCK_NONE',
            'safety_explicit': 'BLOCK_NONE',
            'safety_dangerous': 'BLOCK_NONE'
        }
        
        for k, v in defaults.items():
            await DB_CONN.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (k, v))
        await DB_CONN.commit()
        
        async with DB_CONN.execute("SELECT key, value FROM config") as cursor:
            async for row in cursor:
                CONFIG_CACHE[row[0]] = row[1]

def get_config(key: str, default: str) -> str:
    return CONFIG_CACHE.get(key, default)

async def set_config(key: str, value: str):
    CONFIG_CACHE[key] = value
    async with DB_LOCK:
        await DB_CONN.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
        await DB_CONN.commit()

async def log_system_error(user: str, trace: str):
    try:
        async with DB_LOCK:
            await DB_CONN.execute("INSERT INTO system_errors (timestamp, user, trace) VALUES (?, ?, ?)", (time.time(), user, trace))
            await DB_CONN.execute("DELETE FROM system_errors WHERE id NOT IN (SELECT id FROM system_errors ORDER BY id DESC LIMIT 50)")
            await DB_CONN.commit()
    except Exception as e:
        print(f"CRITICAL DB LOGGING ERROR: {e}")

# -------------------- Smart Cluster Load Balancer --------------------
class GeminiKeyManager:
    def __init__(self, keys: list):
        self.key_objects = []
        self.key_mapping = {}
        self.all_keys = []
        for i, k in enumerate(keys):
            k = k.strip()
            if not k: continue
            name = f"Node {i+1}"
            actual_key = k
            if ":" in k and not k.startswith("AIza"):
                parts = k.split(":", 1)
                name = parts[0].strip()
                actual_key = parts[1].strip()
            self.key_objects.append({'index': i + 1, 'name': name, 'key': actual_key})
            self.key_mapping[actual_key] = f"{name} ({actual_key[:8]}...)"
            self.all_keys.append(actual_key)
        self.key_cooldowns = {k: 0.0 for k in self.all_keys}
        self.key_usage = {k: [] for k in self.all_keys} 
        self.current_key_idx = 0 
        self.dead_keys = set()
        self.lock = asyncio.Lock()
        
    def get_dynamic_safety(self):
        return [
            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH, threshold=getattr(types.HarmBlockThreshold, get_config('safety_hate', 'BLOCK_NONE'))),
            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_HARASSMENT, threshold=getattr(types.HarmBlockThreshold, get_config('safety_harassment', 'BLOCK_NONE'))),
            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, threshold=getattr(types.HarmBlockThreshold, get_config('safety_explicit', 'BLOCK_NONE'))),
            types.SafetySetting(category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, threshold=getattr(types.HarmBlockThreshold, get_config('safety_dangerous', 'BLOCK_NONE'))),
        ]
    
    async def get_stats(self) -> dict:
        async with self.lock:
            now = time.time()
            total = len(self.all_keys)
            dead = len(self.dead_keys)
            cooldown = sum(1 for k in self.all_keys if k not in self.dead_keys and self.key_cooldowns[k] > now)
            active = total - dead - cooldown
            return {"total": total, "active": active, "cooldown": cooldown, "dead": dead}
            
    async def run_diagnostics(self) -> list:
        results = []
        now = time.time()
        for obj in self.key_objects:
            key = obj['key']
            masked_key = f"{key[:8]}•••••••••••••••••••••••••••••{key[-4:]}"
            try:
                client = genai.Client(api_key=key)
                await asyncio.wait_for(client.aio.models.generate_content(model='gemini-2.5-flash-lite', contents="ping"), timeout=15.0)
                async with self.lock:
                    if key in self.dead_keys: self.dead_keys.remove(key)
                    self.key_cooldowns[key] = 0.0
                results.append({"index": obj['index'], "name": obj['name'], "masked_key": masked_key, "status": "ONLINE", "detail": "Healthy", "unlock_time": 0, "color": "#10b981"})
            except Exception as e:
                async with self.lock:
                    self.key_cooldowns[key] = now + 60.0
                results.append({"index": obj['index'], "name": obj['name'], "masked_key": masked_key, "status": "COOLDOWN", "detail": "Rate Limited", "unlock_time": now+60, "color": "#f59e0b"})
        return results

    async def generate_with_fallback(self, target_model: str, contents: list, system_instruction: str = None) -> str:
        fallback_models = [target_model, 'gemini-2.5-flash-lite', 'gemini-2.5-flash']
        last_error = None
        dynamic_safety = self.get_dynamic_safety()
        for model_name in fallback_models:
            async with self.lock:
                now = time.time()
                available_keys = [k for k in self.all_keys if k not in self.dead_keys and self.key_cooldowns[k] <= now]
                if not available_keys: raise Exception("Cluster Exhausted.")
                key = available_keys[self.current_key_idx % len(available_keys)]
                self.current_key_idx += 1
            try:
                client = genai.Client(api_key=key)
                config = types.GenerateContentConfig(system_instruction=system_instruction, safety_settings=dynamic_safety)
                response = await asyncio.wait_for(client.aio.models.generate_content(model=model_name, contents=contents, config=config), timeout=30.0)
                return response.text
            except Exception as e:
                last_error = e
                continue
        raise Exception(f"Cascade Failure: {str(last_error)}")

key_manager = GeminiKeyManager(GEMINI_KEYS)

# -------------------- Background Memory Compression --------------------
async def background_summarize(channel_id, oldest):
    texts = [f"User: {cnt}" for mid, aid, cnt, ts in oldest if aid != 0]
    if not texts: return
    try:
        summary_text = await key_manager.generate_with_fallback('gemini-2.5-flash-lite', [f"Summarize concisely:\n{chr(10).join(texts)}"])
        async with DB_LOCK:
            await DB_CONN.execute("INSERT INTO message_history (channel_id, message_id, author_id, content, timestamp) VALUES (?, -1, 0, ?, ?)", (channel_id, summary_text, oldest[0][3]))
            await DB_CONN.commit()
    except: pass

# -------------------- Helper Functions --------------------
async def is_channel_allowed(guild_id: int, channel_id: int) -> bool:
    if guild_id is None: return True
    async with DB_LOCK:
        async with DB_CONN.execute("SELECT 1 FROM allowed_channels WHERE guild_id=? AND channel_id=?", (guild_id, channel_id)) as cursor:
            return await cursor.fetchone() is not None

async def add_message_to_history(channel_id: int, message_id: int, author_id: int, content: str, timestamp: int):
    async with DB_LOCK:
        await DB_CONN.execute("INSERT OR REPLACE INTO message_history VALUES (?, ?, ?, ?, ?)", (channel_id, message_id, author_id, content, timestamp))
        await DB_CONN.commit()

# -------------------- Discord Bot --------------------
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

class YoAIBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
    async def setup_hook(self):
        await init_db()

bot = YoAIBot()

@bot.command(name="sync")
async def sync_cmds(ctx):
    if ctx.author.id != 1285791141266063475: return
    await bot.tree.sync()
    await ctx.send("✅ Slash commands registered.")

# SLASH COMMANDS
@bot.tree.command(name="info", description="Bot statistics")
async def info(interaction: discord.Interaction):
    uptime = str(datetime.timedelta(seconds=int(time.time() - START_TIME)))
    embed = discord.Embed(title="🏎️ YoAI Apex Engine", color=0xff2a2a)
    embed.add_field(name="Uptime", value=uptime)
    embed.add_field(name="Queries", value=str(TOTAL_QUERIES))
    embed.add_field(name="Architect", value="**mr_yaen (Yathin)**")
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Web Dashboard", url="https://yoai.onrender.com"))
    await interaction.response.send_message(embed=embed, view=view)

@bot.tree.command(name="invite", description="Invite link (auto-leaves if used here)")
async def invite_cmd(interaction: discord.Interaction):
    url = f"https://discord.com/api/oauth2/authorize?client_id={bot.user.id}&permissions=0&scope=bot%20applications.commands"
    if interaction.guild:
        await interaction.response.send_message(f"👋 Leaving Matrix sector. Invite: {url}")
        await interaction.guild.leave()
    else:
        await interaction.response.send_message(f"🔗 Invite: {url}")

@bot.event
async def on_message(message):
    if message.author == bot.user: return
    
    is_dm = message.guild is None
    is_mentioned = bot.user in message.mentions
    is_allowed = True if is_dm else await is_channel_allowed(message.guild.id, message.channel.id)

    if is_dm or is_mentioned or is_allowed:
        channel_id = message.channel.id
        if channel_id not in CHANNEL_BUFFERS:
            CHANNEL_BUFFERS[channel_id] = {'content': [], 'author': message.author, 'channel': message.channel, 'message': message}
        
        CHANNEL_BUFFERS[channel_id]['content'].append(message.content)
        if channel_id in CHANNEL_TIMERS: CHANNEL_TIMERS[channel_id].cancel()
        CHANNEL_TIMERS[channel_id] = bot.loop.create_task(process_buffer(channel_id))
    await bot.process_commands(message)

async def process_buffer(channel_id):
    await asyncio.sleep(2.0) # Stable 2s Debouncer
    if channel_id not in CHANNEL_BUFFERS: return
    data = CHANNEL_BUFFERS.pop(channel_id)
    content = "\n".join(data['content'])
    
    global TOTAL_QUERIES, LAST_ALERT_TIME
    TOTAL_QUERIES += 1
    QUERY_TIMESTAMPS.append(time.time())

    try:
        await add_message_to_history(channel_id, data['message'].id, data['author'].id, content, int(time.time()))
        system = get_config('system_prompt', 'You are YoAI.')
        response = await key_manager.generate_with_fallback(get_config('current_model', 'gemini-2.5-flash-lite'), [content], system)
        for i in range(0, len(response), 2000):
            await data['message'].reply(response[i:i+2000], mention_author=False)
            await asyncio.sleep(1.0)
    except Exception as e:
        await log_system_error(str(data['author']), str(e))
        async with ALERT_LOCK:
            if time.time() - LAST_ALERT_TIME > 15.0:
                LAST_ALERT_TIME = time.time()
                await data['message'].reply("There is an error. Sent to yaen.")

# -------------------- Dashboard HTML --------------------
HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>YoAI Cockpit</title>
    <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;500;700&display=swap" rel="stylesheet">
    <style>
        :root { --accent: #ff2a2a; --glass: rgba(15, 15, 15, 0.6); }
        body { margin: 0; background: #000; color: #fff; font-family: 'Space Grotesk'; overflow-x: hidden; }
        .bg-flow { position: fixed; width: 100vw; height: 100vh; background: radial-gradient(circle at 50% 50%, #1a0000, #000); z-index: -1; }
        .nav { width: 250px; position: fixed; height: 100vh; background: var(--glass); backdrop-filter: blur(20px); border-right: 1px solid rgba(255,255,255,0.1); padding: 30px; }
        .tab { padding: 15px; margin-bottom: 10px; cursor: pointer; border-radius: 8px; transition: 0.3s; color: #888; font-weight: bold; }
        .tab:hover { background: rgba(255,255,255,0.05); color: #fff; }
        .tab.active { background: rgba(255,42,42,0.2); color: #fff; border-left: 4px solid var(--accent); }
        .content { margin-left: 310px; padding: 50px; }
        .glass-card { background: var(--glass); backdrop-filter: blur(30px); border: 1px solid rgba(255,255,255,0.1); border-radius: 15px; padding: 30px; margin-bottom: 30px; }
        .meter-box { display: flex; gap: 30px; justify-content: center; }
        .meter { width: 200px; height: 100px; background: #111; border: 3px solid var(--accent); border-radius: 100px 100px 0 0; position: relative; overflow: hidden; }
        .needle { width: 3px; height: 80px; background: var(--accent); position: absolute; bottom: 0; left: 50%; transform-origin: bottom center; transition: 1s; box-shadow: 0 0 10px var(--accent); }
        .glitch { font-size: 2.5rem; font-weight: 700; text-shadow: 0 0 15px var(--accent); }
        .feature-item { padding: 15px; border-left: 3px solid var(--accent); background: rgba(255,255,255,0.02); margin-bottom: 10px; border-radius: 0 8px 8px 0; }
        input, textarea, select { width: 100%; background: #000; border: 1px solid #333; color: #fff; padding: 12px; border-radius: 8px; margin-top: 10px; }
        button { background: var(--accent); border: none; color: #fff; padding: 12px 25px; border-radius: 8px; cursor: pointer; margin-top: 15px; font-weight: bold; }
    </style>
</head>
<body>
    <div class="bg-flow"></div>
    <div class="nav">
        <h2 style="color:var(--accent)">YoAI APEX</h2>
        <div class="tab active" onclick="show('cockpit')">COCKPIT</div>
        <div class="tab" onclick="show('janitor')">JANITOR</div>
        <div class="tab" onclick="show('credits')">CREDITS</div>
        <div class="tab" onclick="show('admin')">ADMIN</div>
    </div>
    <div class="content">
        <div id="section-cockpit">
            <h1 class="glitch">Engine Cockpit</h1>
            <div class="meter-box">
                <div class="glass-card">
                    <div class="meter"><div class="needle" style="transform: translateX(-50%) rotate({{rpm_deg}}deg)"></div></div>
                    <h3>RPM: {{rpm}}</h3>
                </div>
                <div class="glass-card">
                    <div class="meter"><div class="needle" style="transform: translateX(-50%) rotate({{rlpd_deg}}deg)"></div></div>
                    <h3>RLPD: {{total}}</h3>
                </div>
            </div>
        </div>
        <div id="section-janitor" style="display:none">
            <h1>System Janitor</h1>
            <div class="glass-card">
                <p>Purge dead memory objects and defragment the SQLite core.</p>
                <button onclick="fetch('/api/gc', {method:'POST'})">Flush RAM (GC)</button>
                <button onclick="fetch('/api/vacuum', {method:'POST'})">Vacuum DB</button>
            </div>
        </div>
        <div id="section-credits" style="display:none">
            <h1>Architecture Credits</h1>
            <div class="glass-card">
                <h2 class="glitch">𝕸r_𝖄aen (Yathin)</h2>
                <p>Master Architect & Lead Developer</p>
                <h2 style="color: cyan">✨ ℜhys ✨</h2>
                <p>Head Testing Partner</p>
            </div>
            <div class="glass-card">
                <h3>Feature Matrix</h3>
                <div class="feature-item">⚡ Async aiosqlite Single-Pipeline</div>
                <div class="feature-item">⚖️ Round-Robin Multi-Key Load Balancer</div>
                <div class="feature-item">🛡️ Discord Cloudflare 1015 Anti-Spam Guard</div>
            </div>
        </div>
    </div>
    <script>
        function show(id) {
            ['cockpit','janitor','credits','admin'].forEach(s => document.getElementById('section-'+s).style.display='none');
            document.getElementById('section-'+id).style.display='block';
        }
        setInterval(()=>location.reload(), 5000);
    </script>
</body>
</html>
"""

app = Quart(__name__)
app.secret_key = FLASK_SECRET

@app.route('/')
async def index():
    now = time.time()
    QUERY_TIMESTAMPS[:] = [ts for ts in QUERY_TIMESTAMPS if now - ts < 60.0]
    rpm = len(QUERY_TIMESTAMPS)
    rpm_deg = -90 + (min(rpm, 100) / 100) * 180
    rlpd_deg = -90 + (min(TOTAL_QUERIES, 1000) / 1000) * 180
    return await render_template_string(HTML_TEMPLATE, rpm=rpm, rpm_deg=rpm_deg, total=TOTAL_QUERIES, rlpd_deg=rlpd_deg)

@app.route('/api/gc', methods=['POST'])
async def api_gc():
    gc.collect()
    return jsonify(success=True)

# -------------------- Main Startup Logic --------------------
async def main():
    # 1. IP Check
    try:
        ip = requests.get('https://api.ipify.org').text
        print(f"[BOOT] Server IP: {ip}")
    except: pass

    # 2. Start Web Cockpit immediately
    print(f"[BOOT] Starting Dashboard on Port {PORT}")
    asyncio.create_task(app.run_task(host="0.0.0.0", port=PORT))
    
    # 3. Cloudflare Shield (10-second silence)
    print("[BOOT] Cooling down (10s) to prevent 1015 Bans...")
    await asyncio.sleep(10)
    
    # 4. Start Engine
    print("[BOOT] Igniting YoAI...")
    await bot.start(os.environ.get("DISCORD_BOT_TOKEN"))

if __name__ == "__main__":
    asyncio.run(main())
