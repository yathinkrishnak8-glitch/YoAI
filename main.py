import discord
import os
from keep_alive import keep_alive

intents = discord.Intents.default()
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f'Logged in as {client.user}')
    await client.change_presence(activity=discord.Game(name="Free Fire Max"))

keep_alive()
client.run(os.environ.get('TOKEN'))
