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
TOKEN = "MTUwNDAwODEwMDk0NzAzNDE1Mg.GuMgQs.aq6Qzx5euA3Rb8oO2dTfuXx6ASc3rJPKQSUcYY"   # ← pega tu nuevo token aquí

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

# ── Estado avanzado ───────────────────────
# Configuración por servidor (guild_id → dict)
guild_config: dict[int, dict] = defaultdict(lambda: {
    "autorole_id":    None,   # ID del rol asignado a nuevos miembros
    "verify_role_id": None,   # ID del rol de verificación requerido
    "raid_alert_role_id": None,  # ID del rol que se menciona en alertas de raid
    "antibot":        False,  # Expulsar bots automáticamente
    "slowmode":       0,      # Segundos de slowmode activo en todos los canales
})

# Whitelist de usuarios que omiten las comprobaciones anti-raid (guild_id → set of user_ids)
whitelist: dict[int, set] = defaultdict(set)

# Estadísticas de raid (guild_id → dict)
raid_stats: dict[int, dict] = defaultdict(lambda: {
    "joins":  [],   # timestamps de joins en la última hora
    "kicks":  [],   # timestamps de kicks en la última hora
    "mutes":  [],   # timestamps de mutes en la última hora
    "stat_reset_at": datetime.utcnow(),
})

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

def _prune_stats(guild_id: int):
    """Elimina entradas de estadísticas con más de 1 hora de antigüedad."""
    cutoff = datetime.utcnow() - timedelta(hours=1)
    s = raid_stats[guild_id]
    s["joins"] = [t for t in s["joins"] if t > cutoff]
    s["kicks"] = [t for t in s["kicks"] if t > cutoff]
    s["mutes"] = [t for t in s["mutes"] if t > cutoff]

def _record_join(guild_id: int):
    raid_stats[guild_id]["joins"].append(datetime.utcnow())
    _prune_stats(guild_id)

def _record_kick(guild_id: int):
    raid_stats[guild_id]["kicks"].append(datetime.utcnow())
    _prune_stats(guild_id)

def _record_mute(guild_id: int):
    raid_stats[guild_id]["mutes"].append(datetime.utcnow())
    _prune_stats(guild_id)

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
    guild_id = member.guild.id
    cfg = guild_config[guild_id]

    # ── Antibot: expulsar bots automáticamente ──
    if member.bot and cfg["antibot"]:
        try:
            await member.kick(reason="Antibot: bot detectado automáticamente")
        except discord.Forbidden:
            pass
        e = make_embed(
            "🤖 Bot expulsado (antibot)",
            f"{member.mention} (`{member.name}`) fue expulsado automáticamente por ser un bot.",
            discord.Color.red()
        )
        await log(member.guild, e)
        _record_kick(guild_id)
        return

    # Registrar join en estadísticas
    _record_join(guild_id)

    # Registrar join para detección de raid
    join_times.append(now)

    # Limpiar joins fuera de la ventana
    cutoff = now - timedelta(seconds=JOIN_WINDOW)
    join_times[:] = [t for t in join_times if t > cutoff]

    # ¿Activar raid mode?
    if len(join_times) >= JOIN_THRESHOLD and not raid_mode:
        raid_mode = True
        alert_mention = ""
        alert_role_id = cfg.get("raid_alert_role_id")
        if alert_role_id:
            alert_role = member.guild.get_role(alert_role_id)
            if alert_role:
                alert_mention = f"{alert_role.mention} "
        e = make_embed(
            "🚨 RAID DETECTADO — Modo raid ACTIVADO",
            f"{alert_mention}{len(join_times)} usuarios se unieron en {JOIN_WINDOW}s.\n"
            "Nuevas cuentas serán expulsadas automáticamente.",
            discord.Color.red()
        )
        log_ch = get_log_channel(member.guild)
        if log_ch:
            await log_ch.send(content=alert_mention if alert_mention else None, embed=e)
        else:
            await log(member.guild, e)

    # En raid mode: expulsar cuentas nuevas (whitelist omite el check)
    if raid_mode:
        if member.id in whitelist[guild_id]:
            pass  # usuario en whitelist, no expulsar
        else:
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
                _record_kick(guild_id)
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

    # ── Autorole: asignar rol automáticamente ──
    autorole_id = cfg.get("autorole_id")
    if autorole_id:
        autorole = member.guild.get_role(autorole_id)
        if autorole:
            try:
                await member.add_roles(autorole, reason="Autorole: asignación automática")
            except discord.Forbidden:
                pass

# ── Anti-spam de mensajes ─────────────────

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        await bot.process_commands(message)
        return

    user_id = message.author.id
    guild_id = message.guild.id
    now = datetime.utcnow()
    cutoff = now - timedelta(seconds=MESSAGE_WINDOW)

    # Usuarios en whitelist omiten todas las comprobaciones de spam
    if user_id in whitelist[guild_id]:
        await bot.process_commands(message)
        return

    # ── Spam de mensajes ──
    msg_tracker[user_id] = [t for t in msg_tracker[user_id] if t > cutoff]
    msg_tracker[user_id].append(now)

    if len(msg_tracker[user_id]) >= MESSAGE_LIMIT:
        await message.delete()
        await mute_member(message.author, "Anti-spam: demasiados mensajes")
        _record_mute(guild_id)
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
        _record_mute(guild_id)
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
            _record_mute(guild_id)
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
        ("!raidmode [on/off]",          "Activa/desactiva modo raid"),
        ("!lockdown",                   "Bloquea escritura en todos los canales"),
        ("!unlock",                     "Restaura permisos de escritura"),
        ("!masskick [días]",            "Expulsa cuentas nuevas (def: 7 días)"),
        ("!massmute [días]",            "Silencia cuentas nuevas (def: 7 días)"),
        ("!purge [n]",                  "Borra últimos N mensajes (máx 100)"),
        ("!ban @user [razón]",          "Banea un usuario"),
        ("!unban [ID]",                 "Desbanea por ID"),
        ("!estado",                     "Muestra estado del bot"),
        ("!autorole [role_id]",         "Asigna rol automáticamente a nuevos miembros"),
        ("!antibot",                    "Activa/desactiva expulsión automática de bots"),
        ("!whitelist add/remove @user", "Añade o elimina usuario de la whitelist"),
        ("!raid-stats",                 "Muestra estadísticas de raid de la última hora"),
        ("!slowmode [segundos]",        "Aplica slowmode a todos los canales (0 = desactivar)"),
        ("!nuke-spam",                  "Elimina mensajes recientes de bots/spam"),
        ("!verify-role [role_id]",      "Establece rol de verificación requerido"),
        ("!raid-alert [@rol]",          "Configura rol a mencionar en alertas de raid"),
        ("!config",                     "Muestra la configuración anti-raid actual"),
        ("!reset-stats",                "Reinicia las estadísticas de raid"),
    ]
    for nombre, desc in cmds:
        e.add_field(name=f"`{nombre}`", value=desc, inline=False)
    e.set_footer(text="Anti-Raid Bot • Solo admins pueden usar comandos de moderación")
    await ctx.send(embed=e)


# ─────────────────────────────────────────
#  NUEVOS COMANDOS AVANZADOS
# ─────────────────────────────────────────

@bot.command(name="autorole")
@commands.has_permissions(administrator=True)
async def autorole(ctx, role_id: int = None):
    """!autorole [role_id] — asigna un rol automáticamente a nuevos miembros."""
    cfg = guild_config[ctx.guild.id]
    if role_id is None:
        # Desactivar autorole
        cfg["autorole_id"] = None
        e = make_embed(
            "🎭 Autorole desactivado",
            "Ya no se asignará ningún rol automáticamente a los nuevos miembros.",
            discord.Color.orange()
        )
        await ctx.send(embed=e)
        await log(ctx.guild, e)
        return

    role = ctx.guild.get_role(role_id)
    if role is None:
        await ctx.send(
            embed=make_embed("❌ Error", f"No se encontró ningún rol con ID `{role_id}`.",
                             discord.Color.red())
        )
        return

    cfg["autorole_id"] = role_id
    e = make_embed(
        "🎭 Autorole configurado",
        f"El rol {role.mention} será asignado automáticamente a todos los nuevos miembros.\n"
        f"Útil para cuarentena o verificación.",
        discord.Color.green()
    )
    await ctx.send(embed=e)
    await log(ctx.guild, e)


@bot.command(name="antibot")
@commands.has_permissions(administrator=True)
async def antibot(ctx):
    """!antibot — activa o desactiva la expulsión automática de bots."""
    cfg = guild_config[ctx.guild.id]
    cfg["antibot"] = not cfg["antibot"]
    estado = cfg["antibot"]
    color = discord.Color.red() if estado else discord.Color.green()
    estado_str = "🤖 ACTIVADO" if estado else "✅ DESACTIVADO"
    e = make_embed(
        f"Antibot {estado_str}",
        f"La expulsión automática de bots ha sido **{'activada' if estado else 'desactivada'}** "
        f"por {ctx.author.mention}.",
        color
    )
    await ctx.send(embed=e)
    await log(ctx.guild, e)


@bot.command(name="whitelist")
@commands.has_permissions(administrator=True)
async def whitelist_cmd(ctx, accion: str, member: discord.Member = None):
    """!whitelist add/remove @usuario — gestiona la whitelist anti-raid."""
    if accion.lower() not in ("add", "remove", "añadir", "eliminar", "list", "lista"):
        await ctx.send(
            embed=make_embed("❌ Uso incorrecto",
                             "Uso: `!whitelist add @usuario` | `!whitelist remove @usuario` | `!whitelist list`",
                             discord.Color.red())
        )
        return

    guild_id = ctx.guild.id

    # Mostrar lista
    if accion.lower() in ("list", "lista"):
        wl = whitelist[guild_id]
        if not wl:
            desc = "La whitelist está vacía."
        else:
            lines = []
            for uid in wl:
                user = ctx.guild.get_member(uid)
                lines.append(f"• {user.mention if user else f'ID: {uid}'}")
            desc = "\n".join(lines)
        e = make_embed("📋 Whitelist actual", desc, discord.Color.blurple())
        await ctx.send(embed=e)
        return

    if member is None:
        await ctx.send(
            embed=make_embed("❌ Error", "Debes mencionar a un usuario.", discord.Color.red())
        )
        return

    if accion.lower() in ("add", "añadir"):
        whitelist[guild_id].add(member.id)
        e = make_embed(
            "✅ Whitelist actualizada",
            f"{member.mention} ha sido añadido a la whitelist y omitirá las comprobaciones anti-raid.",
            discord.Color.green()
        )
    else:
        whitelist[guild_id].discard(member.id)
        e = make_embed(
            "🗑️ Whitelist actualizada",
            f"{member.mention} ha sido eliminado de la whitelist.",
            discord.Color.orange()
        )

    await ctx.send(embed=e)
    await log(ctx.guild, e)


@bot.command(name="raid-stats")
@commands.has_permissions(administrator=True)
async def raid_stats_cmd(ctx):
    """!raid-stats — muestra estadísticas de raid de la última hora."""
    guild_id = ctx.guild.id
    _prune_stats(guild_id)
    s = raid_stats[guild_id]
    reset_at = s["stat_reset_at"]
    reset_str = reset_at.strftime("%d/%m/%Y %H:%M UTC")

    color = discord.Color.red() if raid_mode else discord.Color.blurple()
    e = discord.Embed(
        title="📊 Estadísticas de Raid (última hora)",
        color=color,
        timestamp=discord.utils.utcnow()
    )
    e.add_field(name="🚪 Joins",  value=str(len(s["joins"])),  inline=True)
    e.add_field(name="👢 Kicks",  value=str(len(s["kicks"])),  inline=True)
    e.add_field(name="🔇 Mutes",  value=str(len(s["mutes"])),  inline=True)
    e.add_field(name="🚨 Modo raid", value="ACTIVO" if raid_mode else "Inactivo", inline=True)
    e.add_field(name="🔄 Stats desde", value=reset_str, inline=True)
    e.set_footer(text="Anti-Raid Bot")
    await ctx.send(embed=e)


@bot.command(name="slowmode")
@commands.has_permissions(administrator=True)
async def slowmode(ctx, segundos: int = 0):
    """!slowmode [segundos] — aplica slowmode a todos los canales (0 para desactivar)."""
    if segundos < 0 or segundos > 21600:
        await ctx.send(
            embed=make_embed("❌ Error",
                             "El slowmode debe estar entre **0** y **21600** segundos (6 horas).",
                             discord.Color.red())
        )
        return

    guild_config[ctx.guild.id]["slowmode"] = segundos
    aplicados = 0
    for channel in ctx.guild.text_channels:
        try:
            await channel.edit(slowmode_delay=segundos,
                               reason=f"Slowmode {'activado' if segundos else 'desactivado'} por {ctx.author}")
            aplicados += 1
        except discord.Forbidden:
            pass

    if segundos == 0:
        desc = f"Slowmode **desactivado** en {aplicados} canales por {ctx.author.mention}."
        color = discord.Color.green()
        title = "⏩ Slowmode desactivado"
    else:
        desc = f"Slowmode de **{segundos}s** aplicado en {aplicados} canales por {ctx.author.mention}."
        color = discord.Color.orange()
        title = "🐢 Slowmode activado"

    e = make_embed(title, desc, color)
    await ctx.send(embed=e)
    await log(ctx.guild, e)


@bot.command(name="nuke-spam")
@commands.has_permissions(administrator=True)
async def nuke_spam(ctx, limite: int = 100):
    """!nuke-spam [límite] — elimina mensajes recientes de bots y usuarios marcados como spam."""
    limite = min(max(limite, 1), 500)

    def es_spam(msg: discord.Message) -> bool:
        # Considera spam: mensajes de bots, mensajes con muchos links, o mensajes con mass mentions
        if msg.author.bot:
            return True
        link_count = sum(1 for w in msg.content.split()
                         if w.startswith(("http://", "https://", "discord.gg/")))
        if link_count >= LINK_SPAM_LIMIT:
            return True
        if len(msg.mentions) + len(msg.role_mentions) >= MENTION_LIMIT:
            return True
        return False

    try:
        borrados = await ctx.channel.purge(limit=limite, check=es_spam)
        e = make_embed(
            "💥 Nuke-spam completado",
            f"Se eliminaron **{len(borrados)}** mensajes de spam/bots en {ctx.channel.mention}.\n"
            f"Ejecutado por {ctx.author.mention}.",
            discord.Color.orange()
        )
    except discord.Forbidden:
        e = make_embed("❌ Error", "No tengo permisos para eliminar mensajes en este canal.",
                       discord.Color.red())

    msg = await ctx.send(embed=e)
    await log(ctx.guild, e)
    await asyncio.sleep(8)
    try:
        await msg.delete()
    except discord.NotFound:
        pass


@bot.command(name="verify-role")
@commands.has_permissions(administrator=True)
async def verify_role(ctx, role_id: int = None):
    """!verify-role [role_id] — establece el rol de verificación requerido."""
    cfg = guild_config[ctx.guild.id]
    if role_id is None:
        cfg["verify_role_id"] = None
        e = make_embed(
            "🔓 Rol de verificación eliminado",
            "Ya no se requiere un rol de verificación.",
            discord.Color.orange()
        )
        await ctx.send(embed=e)
        await log(ctx.guild, e)
        return

    role = ctx.guild.get_role(role_id)
    if role is None:
        await ctx.send(
            embed=make_embed("❌ Error", f"No se encontró ningún rol con ID `{role_id}`.",
                             discord.Color.red())
        )
        return

    cfg["verify_role_id"] = role_id
    e = make_embed(
        "🔐 Rol de verificación configurado",
        f"El rol {role.mention} es ahora el rol de verificación requerido.\n"
        f"Los miembros sin este rol serán considerados no verificados.",
        discord.Color.green()
    )
    await ctx.send(embed=e)
    await log(ctx.guild, e)


@bot.command(name="raid-alert")
@commands.has_permissions(administrator=True)
async def raid_alert(ctx, role: discord.Role = None):
    """!raid-alert [@rol] — configura el rol que se menciona cuando se detecta un raid."""
    cfg = guild_config[ctx.guild.id]
    if role is None:
        cfg["raid_alert_role_id"] = None
        e = make_embed(
            "🔕 Alerta de raid desactivada",
            "No se mencionará ningún rol cuando se detecte un raid.",
            discord.Color.orange()
        )
        await ctx.send(embed=e)
        await log(ctx.guild, e)
        return

    cfg["raid_alert_role_id"] = role.id
    e = make_embed(
        "🔔 Alerta de raid configurada",
        f"{role.mention} será mencionado automáticamente cuando se detecte un raid.",
        discord.Color.green()
    )
    await ctx.send(embed=e)
    await log(ctx.guild, e)


@bot.command(name="config")
@commands.has_permissions(administrator=True)
async def config_cmd(ctx):
    """!config — muestra la configuración anti-raid actual del servidor."""
    cfg = guild_config[ctx.guild.id]
    guild_id = ctx.guild.id

    # Resolver nombres de roles
    def role_name(role_id):
        if role_id is None:
            return "No configurado"
        r = ctx.guild.get_role(role_id)
        return r.mention if r else f"ID: {role_id} (no encontrado)"

    wl_count = len(whitelist[guild_id])

    e = discord.Embed(
        title="⚙️ Configuración Anti-Raid",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    e.add_field(name="🚨 Modo raid",          value="ACTIVO" if raid_mode else "Inactivo",    inline=True)
    e.add_field(name="🤖 Antibot",            value="✅ Activo" if cfg["antibot"] else "❌ Inactivo", inline=True)
    e.add_field(name="🐢 Slowmode global",    value=f"{cfg['slowmode']}s" if cfg["slowmode"] else "Desactivado", inline=True)
    e.add_field(name="🎭 Autorole",           value=role_name(cfg["autorole_id"]),            inline=True)
    e.add_field(name="🔐 Rol verificación",   value=role_name(cfg["verify_role_id"]),         inline=True)
    e.add_field(name="🔔 Rol alerta raid",    value=role_name(cfg["raid_alert_role_id"]),     inline=True)
    e.add_field(name="📋 Whitelist",          value=f"{wl_count} usuario(s)",                 inline=True)
    e.add_field(name="⏱️ Umbral raid",        value=f"{JOIN_THRESHOLD} joins en {JOIN_WINDOW}s", inline=True)
    e.add_field(name="📅 Edad mínima cuenta", value=f"{ACCOUNT_MIN_AGE} días",                inline=True)
    e.add_field(name="🔇 Duración mute",      value=f"{MUTE_DURATION}s",                      inline=True)
    e.add_field(name="💬 Límite spam",        value=f"{MESSAGE_LIMIT} msg/{MESSAGE_WINDOW}s", inline=True)
    e.add_field(name="🔗 Límite links",       value=f"{LINK_SPAM_LIMIT} links/{MESSAGE_WINDOW}s", inline=True)
    e.set_footer(text="Anti-Raid Bot")
    await ctx.send(embed=e)


@bot.command(name="reset-stats")
@commands.has_permissions(administrator=True)
async def reset_stats(ctx):
    """!reset-stats — reinicia las estadísticas de raid del servidor."""
    guild_id = ctx.guild.id
    s = raid_stats[guild_id]
    s["joins"].clear()
    s["kicks"].clear()
    s["mutes"].clear()
    s["stat_reset_at"] = datetime.utcnow()
    e = make_embed(
        "🔄 Estadísticas reiniciadas",
        f"Todas las estadísticas de raid han sido reiniciadas por {ctx.author.mention}.",
        discord.Color.green()
    )
    await ctx.send(embed=e)
    await log(ctx.guild, e)


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
