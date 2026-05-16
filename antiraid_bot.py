import discord
from discord.ext import commands
import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
import json
import os

# ─────────────────────────────────────────
#  CONFIGURACIÓN
# ─────────────────────────────────────────
TOKEN = "TU_TOKEN_AQUI"   # ← pega tu nuevo token aquí

PREFIX = "!"

# Anti-raid: límites
JOIN_THRESHOLD      = 10    # joins en la ventana de tiempo activan el raid mode
JOIN_WINDOW         = 10    # segundos
MENTION_LIMIT       = 5     # menciones por mensaje
MESSAGE_LIMIT       = 7     # mensajes por ventana antes de mute
MESSAGE_WINDOW      = 5     # segundos
LINK_SPAM_LIMIT     = 3     # links permitidos por ventana
ACCOUNT_MIN_AGE     = 7     # días mínimos de antigüedad de la cuenta
MUTE_DURATION       = 300   # segundos (5 min)

# ─────────────────────────────────────────
#  ESTADO INTERNO
# ─────────────────────────────────────────
raid_mode   = False
join_times  = []                        # lista de timestamps de joins recientes
msg_tracker = defaultdict(list)         # user_id → [timestamps]
link_tracker= defaultdict(list)

# ─────────────────────────────────────────
#  BOT
# ─────────────────────────────────────────
intents = discord.Intents.all()
bot = commands.Bot(command_prefix=PREFIX, intents=intents)

# ── helpers ──────────────────────────────

def get_log_channel(guild: discord.Guild) -> discord.TextChannel | None:
    return discord.utils.get(guild.text_channels, name="raid-log")

async def log(guild: discord.Guild, embed: discord.Embed):
    ch = get_log_channel(guild)
    if ch:
        await ch.send(embed=embed)

async def mute_member(member: discord.Member, reason: str):
    """Agrega el rol Muted o usa timeout si no existe el rol."""
    muted_role = discord.utils.get(member.guild.roles, name="Muted")
    if muted_role:
        await member.add_roles(muted_role, reason=reason)
        await asyncio.sleep(MUTE_DURATION)
        await member.remove_roles(muted_role, reason="Mute expirado")
    else:
        until = discord.utils.utcnow() + timedelta(seconds=MUTE_DURATION)
        await member.timeout(until, reason=reason)

def make_embed(title: str, description: str, color: discord.Color) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color,
                      timestamp=discord.utils.utcnow())
    e.set_footer(text="Anti-Raid Bot")
    return e

# ─────────────────────────────────────────
#  EVENTOS
# ─────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"[✓] Conectado como {bot.user} | Modo raid: {'ACTIVO' if raid_mode else 'inactivo'}")
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching, name="por raids | !ayuda"))

# ── Detección de raid por joins masivos ──

@bot.event
async def on_member_join(member: discord.Member):
    global raid_mode
    now = datetime.utcnow()

    # Registrar join
    join_times.append(now)

    # Limpiar joins fuera de la ventana
    cutoff = now - timedelta(seconds=JOIN_WINDOW)
    join_times[:] = [t for t in join_times if t > cutoff]

    # ¿Activar raid mode?
    if len(join_times) >= JOIN_THRESHOLD and not raid_mode:
        raid_mode = True
        e = make_embed(
            "🚨 RAID DETECTADO — Modo raid ACTIVADO",
            f"{len(join_times)} usuarios se unieron en {JOIN_WINDOW}s.\n"
            "Nuevas cuentas serán expulsadas automáticamente.",
            discord.Color.red()
        )
        await log(member.guild, e)

    # En raid mode: expulsar cuentas nuevas
    if raid_mode:
        age_days = (datetime.utcnow() - member.created_at.replace(tzinfo=None)).days
        if age_days < ACCOUNT_MIN_AGE:
            try:
                await member.send(
                    f"Has sido expulsado de **{member.guild.name}** porque el servidor "
                    "está bajo un ataque raid. Intenta unirte más tarde."
                )
            except discord.Forbidden:
                pass
            await member.kick(reason="Raid mode: cuenta demasiado nueva")
            e = make_embed(
                "👢 Kick automático (raid mode)",
                f"{member.mention} (`{member.name}`) — cuenta de {age_days} días.",
                discord.Color.orange()
            )
            await log(member.guild, e)
            return

    # Fuera de raid mode: verificar antigüedad mínima
    age_days = (datetime.utcnow() - member.created_at.replace(tzinfo=None)).days
    if age_days < ACCOUNT_MIN_AGE:
        e = make_embed(
            "⚠️ Cuenta nueva detectada",
            f"{member.mention} tiene {age_days} días de antigüedad (mínimo: {ACCOUNT_MIN_AGE}).",
            discord.Color.yellow()
        )
        await log(member.guild, e)

# ── Anti-spam de mensajes ─────────────────

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        await bot.process_commands(message)
        return

    user_id = message.author.id
    now = datetime.utcnow()
    cutoff = now - timedelta(seconds=MESSAGE_WINDOW)

    # ── Spam de mensajes ──
    msg_tracker[user_id] = [t for t in msg_tracker[user_id] if t > cutoff]
    msg_tracker[user_id].append(now)

    if len(msg_tracker[user_id]) >= MESSAGE_LIMIT:
        await message.delete()
        await mute_member(message.author, "Anti-spam: demasiados mensajes")
        e = make_embed(
            "🔇 Mute por spam",
            f"{message.author.mention} silenciado {MUTE_DURATION}s por spam de mensajes.",
            discord.Color.orange()
        )
        await log(message.guild, e)
        await bot.process_commands(message)
        return

    # ── Anti-mention spam ──
    mention_count = len(message.mentions) + len(message.role_mentions)
    if mention_count >= MENTION_LIMIT:
        await message.delete()
        await mute_member(message.author, "Anti-spam: mass mention")
        e = make_embed(
            "🔇 Mute por mass mention",
            f"{message.author.mention} mencionó {mention_count} usuarios/roles.",
            discord.Color.red()
        )
        await log(message.guild, e)
        await bot.process_commands(message)
        return

    # ── Anti-link spam ──
    has_link = any(w.startswith(("http://", "https://", "discord.gg/"))
                   for w in message.content.split())
    if has_link:
        link_tracker[user_id] = [t for t in link_tracker[user_id] if t > cutoff]
        link_tracker[user_id].append(now)
        if len(link_tracker[user_id]) >= LINK_SPAM_LIMIT:
            await message.delete()
            await mute_member(message.author, "Anti-spam: link spam")
            e = make_embed(
                "🔇 Mute por spam de links",
                f"{message.author.mention} envió demasiados links.",
                discord.Color.orange()
            )
            await log(message.guild, e)
            await bot.process_commands(message)
            return

    await bot.process_commands(message)

# ─────────────────────────────────────────
#  COMANDOS
# ─────────────────────────────────────────

@bot.command(name="raidmode")
@commands.has_permissions(administrator=True)
async def toggle_raid_mode(ctx, estado: str = None):
    """!raidmode on | off — activa o desactiva el modo raid manualmente."""
    global raid_mode
    if estado is None:
        raid_mode = not raid_mode
    elif estado.lower() in ("on", "activar", "1"):
        raid_mode = True
    elif estado.lower() in ("off", "desactivar", "0"):
        raid_mode = False
    else:
        await ctx.send("Uso: `!raidmode on` o `!raidmode off`")
        return

    color  = discord.Color.red() if raid_mode else discord.Color.green()
    estado_str = "🚨 ACTIVADO" if raid_mode else "✅ DESACTIVADO"
    e = make_embed(f"Modo raid {estado_str}", f"Cambiado por {ctx.author.mention}.", color)
    await ctx.send(embed=e)
    await log(ctx.guild, e)


@bot.command(name="lockdown")
@commands.has_permissions(administrator=True)
async def lockdown(ctx):
    """!lockdown — quita permisos de escritura a @everyone en todos los canales."""
    everyone = ctx.guild.default_role
    bloqueados = 0
    for channel in ctx.guild.text_channels:
        try:
            overwrite = channel.overwrites_for(everyone)
            overwrite.send_messages = False
            await channel.set_permissions(everyone, overwrite=overwrite,
                                          reason=f"Lockdown por {ctx.author}")
            bloqueados += 1
        except discord.Forbidden:
            pass
    e = make_embed(
        "🔒 Lockdown activado",
        f"{bloqueados} canales bloqueados por {ctx.author.mention}.",
        discord.Color.red()
    )
    await ctx.send(embed=e)
    await log(ctx.guild, e)


@bot.command(name="unlock")
@commands.has_permissions(administrator=True)
async def unlock(ctx):
    """!unlock — restaura permisos de escritura a @everyone."""
    everyone = ctx.guild.default_role
    desbloqueados = 0
    for channel in ctx.guild.text_channels:
        try:
            overwrite = channel.overwrites_for(everyone)
            overwrite.send_messages = None   # hereda del rol
            await channel.set_permissions(everyone, overwrite=overwrite,
                                          reason=f"Unlock por {ctx.author}")
            desbloqueados += 1
        except discord.Forbidden:
            pass
    e = make_embed(
        "🔓 Lockdown levantado",
        f"{desbloqueados} canales desbloqueados por {ctx.author.mention}.",
        discord.Color.green()
    )
    await ctx.send(embed=e)
    await log(ctx.guild, e)


@bot.command(name="masskick")
@commands.has_permissions(administrator=True)
async def masskick(ctx, min_days: int = 7):
    """!masskick [días] — expulsa todos los miembros con cuenta menor a N días."""
    expulsados = 0
    for member in ctx.guild.members:
        if member.bot or member == ctx.author:
            continue
        age = (datetime.utcnow() - member.created_at.replace(tzinfo=None)).days
        if age < min_days:
            try:
                await member.kick(reason=f"masskick: cuenta de {age} días")
                expulsados += 1
            except discord.Forbidden:
                pass
    e = make_embed(
        "👢 Mass kick completado",
        f"{expulsados} usuarios expulsados (cuentas < {min_days} días).\nEjecutado por {ctx.author.mention}.",
        discord.Color.orange()
    )
    await ctx.send(embed=e)
    await log(ctx.guild, e)


@bot.command(name="massmute")
@commands.has_permissions(administrator=True)
async def massmute(ctx, min_days: int = 7):
    """!massmute [días] — silencia todos los miembros con cuenta menor a N días."""
    silenciados = 0
    until = discord.utils.utcnow() + timedelta(seconds=MUTE_DURATION)
    for member in ctx.guild.members:
        if member.bot or member == ctx.author:
            continue
        age = (datetime.utcnow() - member.created_at.replace(tzinfo=None)).days
        if age < min_days:
            try:
                await member.timeout(until, reason=f"massmute: cuenta de {age} días")
                silenciados += 1
            except discord.Forbidden:
                pass
    e = make_embed(
        "🔇 Mass mute completado",
        f"{silenciados} usuarios silenciados (cuentas < {min_days} días).\nEjecutado por {ctx.author.mention}.",
        discord.Color.orange()
    )
    await ctx.send(embed=e)
    await log(ctx.guild, e)


@bot.command(name="purge")
@commands.has_permissions(manage_messages=True)
async def purge(ctx, cantidad: int = 10):
    """!purge [n] — elimina los últimos N mensajes del canal (máx 100)."""
    cantidad = min(cantidad, 100)
    borrados = await ctx.channel.purge(limit=cantidad + 1)
    e = make_embed(
        "🗑️ Purge completado",
        f"{len(borrados) - 1} mensajes eliminados en {ctx.channel.mention}.",
        discord.Color.blurple()
    )
    msg = await ctx.send(embed=e)
    await asyncio.sleep(5)
    await msg.delete()


@bot.command(name="ban")
@commands.has_permissions(ban_members=True)
async def ban(ctx, member: discord.Member, *, razon: str = "Sin razón especificada"):
    """!ban @usuario [razón] — banea a un usuario."""
    await member.ban(reason=f"{razon} | por {ctx.author}")
    e = make_embed("🔨 Ban", f"{member.mention} baneado.\nRazón: {razon}", discord.Color.red())
    await ctx.send(embed=e)
    await log(ctx.guild, e)


@bot.command(name="unban")
@commands.has_permissions(ban_members=True)
async def unban(ctx, user_id: int):
    """!unban [ID] — desbanea a un usuario por su ID."""
    user = await bot.fetch_user(user_id)
    await ctx.guild.unban(user, reason=f"Unban por {ctx.author}")
    e = make_embed("✅ Unban", f"{user} (`{user_id}`) desbaneado.", discord.Color.green())
    await ctx.send(embed=e)
    await log(ctx.guild, e)


@bot.command(name="estado")
async def estado(ctx):
    """!estado — muestra el estado actual del bot anti-raid."""
    color = discord.Color.red() if raid_mode else discord.Color.green()
    e = discord.Embed(title="📊 Estado del Bot Anti-Raid", color=color,
                      timestamp=discord.utils.utcnow())
    e.add_field(name="Modo raid",       value="🚨 ACTIVO" if raid_mode else "✅ Inactivo", inline=True)
    e.add_field(name="Joins recientes", value=str(len(join_times)), inline=True)
    e.add_field(name="Umbral raid",     value=f"{JOIN_THRESHOLD} en {JOIN_WINDOW}s", inline=True)
    e.add_field(name="Edad mínima",     value=f"{ACCOUNT_MIN_AGE} días", inline=True)
    e.add_field(name="Duración mute",   value=f"{MUTE_DURATION}s", inline=True)
    e.add_field(name="Spam límite",     value=f"{MESSAGE_LIMIT} msg/{MESSAGE_WINDOW}s", inline=True)
    e.set_footer(text="Anti-Raid Bot")
    await ctx.send(embed=e)


@bot.command(name="ayuda")
async def ayuda(ctx):
    """!ayuda — muestra todos los comandos."""
    e = discord.Embed(title="🛡️ Comandos Anti-Raid", color=discord.Color.blurple(),
                      timestamp=discord.utils.utcnow())
    cmds = [
        ("!raidmode [on/off]",  "Activa/desactiva modo raid"),
        ("!lockdown",           "Bloquea escritura en todos los canales"),
        ("!unlock",             "Restaura permisos de escritura"),
        ("!masskick [días]",    "Expulsa cuentas nuevas (def: 7 días)"),
        ("!massmute [días]",    "Silencia cuentas nuevas (def: 7 días)"),
        ("!purge [n]",          "Borra últimos N mensajes (máx 100)"),
        ("!ban @user [razón]",  "Banea un usuario"),
        ("!unban [ID]",         "Desbanea por ID"),
        ("!estado",             "Muestra estado del bot"),
    ]
    for nombre, desc in cmds:
        e.add_field(name=f"`{nombre}`", value=desc, inline=False)
    e.set_footer(text="Anti-Raid Bot • Solo admins pueden usar comandos de moderación")
    await ctx.send(embed=e)


# ─────────────────────────────────────────
#  MANEJO DE ERRORES
# ─────────────────────────────────────────

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ No tienes permisos para usar este comando.", delete_after=5)
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("❌ Usuario no encontrado.", delete_after=5)
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Argumento inválido. Revisa `!ayuda`.", delete_after=5)
    else:
        raise error


# ─────────────────────────────────────────
#  ARRANQUE
# ─────────────────────────────────────────
bot.run(TOKEN)
