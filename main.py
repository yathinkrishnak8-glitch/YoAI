import discord
import os
import google.generativeai as genai
from keep_alive import keep_alive

# Setup Gemini AI
genai.configure(api_key=os.environ.get("GEMINI_TOKEN"))

# Core instruction for how the bot should behave
model = genai.GenerativeModel(
    model_name="gemini-1.5-flash",
    system_instruction="You are a helpful and intelligent Discord bot. Provide answers that are detailed but concise, adjusting your response length appropriately based on the situation and the user's prompt."
)

# Setup Discord Intents
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f'Logged in as {client.user}')
    await client.change_presence(activity=discord.Game(name="Chatting with the server"))

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    if client.user in message.mentions:
        user_message = message.content.replace(f'<@{client.user.id}>', '').strip()
        
        if not user_message:
            await message.channel.send("How can I help you today?")
            return

        try:
            async with message.channel.typing():
                response = model.generate_content(user_message)
                await message.reply(response.text)
        except Exception as e:
            await message.reply("Give me a second, my circuits are crossed. Try again!")
            print(e)

keep_alive()
client.run(os.environ.get('TOKEN'))
