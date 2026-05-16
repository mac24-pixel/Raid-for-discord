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
TOKEN = "MTUwNDAwODEwMDk0NzAzNDE1Mg.GuMgQs.aq6Qzx5euA3Rb8oO2dTfuXx6ASc3rJPKQSUcYY"

PREFIX = "!"

# Anti-raid: límites
JOIN_THRESHOLD      = 10
JOIN_WINDOW         = 10
MENTION_LIMIT       = 5
MESSAGE_LIMIT       = 7
MESSAGE_WINDOW      = 5
LINK_SPAM_LIMIT     = 3
ACCOUNT_MIN_AGE     = 7
MUTE_DURATION       = 300

# ─────────────────────────────────────────
#  ESTADO INTERNO
# ─────────────────────────────────────────
raid_mode   = False
join_times  = []
msg_tracker = defaultdict(list)
link_tracker= defaultdict(list)

guild_config: dict[int, dict] = defaultdict(lambda: {
    "autorole_id":    None,
    "verify_role_id": None,
    "raid_alert_role_id": None,
    "antibot":        False,
    "slowmode":       0,
    "spam_filter":    True,
    "ticket_category": None,
    "ticket_counter": 0,
})

whitelist: dict[int, set] = defaultdict(set)

raid_stats: dict[int, dict] = defaultdict(lambda: {
    "joins":  [],
    "kicks":  [],
    "mutes":  [],
    "stat_reset_at": datetime.utcnow(),
})

warnings: dict[int, dict] = defaultdict(lambda: defaultdict(int))
action_logs: dict[int, list] = defaultdict(list)

# Sistema de tickets: guild_id -> channel_id -> {creator_id, title}
tickets: dict[int, dict] = defaultdict(dict)

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

def _log_action(guild_id: int, action_type: str, user_id: int, reason: str = ""):
    action_logs[guild_id].append({
        "timestamp": datetime.utcnow(),
        "type": action_type,
        "user_id": user_id,
        "reason": reason
    })

# ─────────────────────────────────────────
#  EVENTOS
# ─────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"[✓] Conectado como {bot.user} | Modo raid: {'ACTIVO' if raid_mode else 'inactivo'}")
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching, name="por raids | !ayuda"))

@bot.event
async def on_member_join(member: discord.Member):
    global raid_mode
    now = datetime.utcnow()
    guild_id = member.guild.id
    cfg = guild_config[guild_id]

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
        _log_action(guild_id, "kick", member.id, "Bot expulsado automáticamente")
        return

    _record_join(guild_id)
    join_times.append(now)
    cutoff = now - timedelta(seconds=JOIN_WINDOW)
    join_times[:] = [t for t in join_times if t > cutoff]

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

    if raid_mode:
        if member.id in whitelist[guild_id]:
            pass
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
                _log_action(guild_id, "kick", member.id, "Raid mode: cuenta nueva")
                e = make_embed(
                    "👢 Kick automático (raid mode)",
                    f"{member.mention} (`{member.name}`) — cuenta de {age_days} días.",
                    discord.Color.orange()
                )
                await log(member.guild, e)
                return

    age_days = (datetime.utcnow() - member.created_at.replace(tzinfo=None)).days
    if age_days < ACCOUNT_MIN_AGE:
        e = make_embed(
            "⚠️ Cuenta nueva detectada",
            f"{member.mention} tiene {age_days} días de antigüedad (mínimo: {ACCOUNT_MIN_AGE}).",
            discord.Color.yellow()
        )
        await log(member.guild, e)

    # Autorole
    autorole_id = cfg.get("autorole_id")
    if autorole_id:
        autorole = member.guild.get_role(autorole_id)
        if autorole:
            try:
                await member.add_roles(autorole, reason="Autorole: asignación automática")
            except discord.Forbidden:
                pass

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        await bot.process_commands(message)
        return

    user_id = message.author.id
    guild_id = message.guild.id
    now = datetime.utcnow()
    cutoff = now - timedelta(seconds=MESSAGE_WINDOW)
    cfg = guild_config[guild_id]

    if user_id in whitelist[guild_id]:
        await bot.process_commands(message)
        return

    if cfg["spam_filter"]:
        msg_tracker[user_id] = [t for t in msg_tracker[user_id] if t > cutoff]
        msg_tracker[user_id].append(now)

        if len(msg_tracker[user_id]) >= MESSAGE_LIMIT:
            await message.delete()
            await mute_member(message.author, "Anti-spam: demasiados mensajes")
            _record_mute(guild_id)
            _log_action(guild_id, "mute", user_id, "Anti-spam: demasiados mensajes")
            e = make_embed(
                "🔇 Mute por spam",
                f"{message.author.mention} silenciado {MUTE_DURATION}s por spam de mensajes.",
                discord.Color.orange()
            )
            await log(message.guild, e)
            await bot.process_commands(message)
            return

        mention_count = len(message.mentions) + len(message.role_mentions)
        if mention_count >= MENTION_LIMIT:
            await message.delete()
            await mute_member(message.author, "Anti-spam: mass mention")
            _record_mute(guild_id)
            _log_action(guild_id, "mute", user_id, "Mass mention")
            e = make_embed(
                "🔇 Mute por mass mention",
                f"{message.author.mention} mencionó {mention_count} usuarios/roles.",
                discord.Color.red()
            )
            await log(message.guild, e)
            await bot.process_commands(message)
            return

        has_link = any(w.startswith(("http://", "https://", "discord.gg/"))
                       for w in message.content.split())
        if has_link:
            link_tracker[user_id] = [t for t in link_tracker[user_id] if t > cutoff]
            link_tracker[user_id].append(now)
            if len(link_tracker[user_id]) >= LINK_SPAM_LIMIT:
                await message.delete()
                await mute_member(message.author, "Anti-spam: link spam")
                _record_mute(guild_id)
                _log_action(guild_id, "mute", user_id, "Link spam")
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
    _log_action(ctx.guild.id, "raidmode_toggle", ctx.author.id, f"Modo raid: {raid_mode}")

@bot.command(name="kick")
@commands.has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, razon: str = "Sin razón especificada"):
    """!kick @usuario [razón] — expulsa a un usuario."""
    await member.kick(reason=f"{razon} | por {ctx.author}")
    _record_kick(ctx.guild.id)
    _log_action(ctx.guild.id, "kick", member.id, razon)
    e = make_embed("👢 Kick", f"{member.mention} expulsado.\nRazón: {razon}", discord.Color.orange())
    await ctx.send(embed=e)
    await log(ctx.guild, e)

@bot.command(name="mute")
@commands.has_permissions(moderate_members=True)
async def mute(ctx, member: discord.Member, minutos: int = 5, *, razon: str = "Sin razón"):
    """!mute @usuario [minutos] [razón] — silencia a un usuario."""
    minutos = min(max(minutos, 1), 40320)
    until = discord.utils.utcnow() + timedelta(minutes=minutos)
    
    try:
        await member.timeout(until, reason=f"{razon} | por {ctx.author}")
        _record_mute(ctx.guild.id)
        _log_action(ctx.guild.id, "mute", member.id, razon)
        e = make_embed(
            "🔇 Mute",
            f"{member.mention} silenciado por **{minutos} minutos**.\nRazón: {razon}",
            discord.Color.orange()
        )
        await ctx.send(embed=e)
        await log(ctx.guild, e)
    except discord.Forbidden:
        await ctx.send("❌ No puedo silenciar a este usuario.", delete_after=5)

@bot.command(name="unmute")
@commands.has_permissions(moderate_members=True)
async def unmute(ctx, member: discord.Member):
    """!unmute @usuario — dessilencia a un usuario."""
    try:
        await member.timeout(None, reason=f"Unmute por {ctx.author}")
        e = make_embed("🔊 Unmute", f"{member.mention} dessilenciado.", discord.Color.green())
        await ctx.send(embed=e)
        await log(ctx.guild, e)
        _log_action(ctx.guild.id, "unmute", member.id, "Unmute")
    except discord.Forbidden:
        await ctx.send("❌ No puedo dessilenciar a este usuario.", delete_after=5)

@bot.command(name="unmuteall")
@commands.has_permissions(administrator=True)
async def unmuteall(ctx):
    """!unmuteall — dessilencia a TODOS los usuarios silenciados."""
    dessilenciados = 0
    for member in ctx.guild.members:
        if member.timed_out:
            try:
                await member.timeout(None, reason=f"Unmuteall por {ctx.author}")
                dessilenciados += 1
                _log_action(ctx.guild.id, "unmute", member.id, "Unmuteall")
            except discord.Forbidden:
                pass
    
    e = make_embed(
        "🔊 Unmuteall completado",
        f"**{dessilenciados}** usuarios dessilenciados por {ctx.author.mention}.",
        discord.Color.green()
    )
    await ctx.send(embed=e)
    await log(ctx.guild, e)

@bot.command(name="warn")
@commands.has_permissions(moderate_members=True)
async def warn(ctx, member: discord.Member, *, razon: str = "Sin razón"):
    """!warn @usuario [razón] — advierte a un usuario (acumula 3 = expulsión)."""
    guild_id = ctx.guild.id
    warnings[guild_id][member.id] += 1
    warn_count = warnings[guild_id][member.id]
    
    _log_action(guild_id, "warn", member.id, razon)
    
    e = make_embed(
        "⚠️ Advertencia",
        f"{member.mention} ha recibido una advertencia.\n"
        f"Razón: {razon}\n\n"
        f"**Advertencias: {warn_count}/3**",
        discord.Color.yellow()
    )
    await ctx.send(embed=e)
    await log(ctx.guild, e)
    
    if warn_count >= 3:
        try:
            await member.kick(reason=f"Expulsado tras 3 advertencias. Última: {razon}")
            _record_kick(guild_id)
            _log_action(guild_id, "kick", member.id, "Expulsión automática por 3 advertencias")
            
            e = make_embed(
                "👢 Expulsión automática",
                f"{member.mention} expulsado por acumular 3 advertencias.",
                discord.Color.red()
            )
            await log(ctx.guild, e)
        except discord.Forbidden:
            pass

@bot.command(name="clearwarnings")
@commands.has_permissions(administrator=True)
async def clearwarnings(ctx, member: discord.Member = None):
    """!clearwarnings [@usuario] — elimina advertencias (todas si no se especifica usuario)."""
    guild_id = ctx.guild.id
    
    if member is None:
        warnings[guild_id].clear()
        e = make_embed(
            "🧹 Advertencias eliminadas",
            "Todas las advertencias del servidor han sido eliminadas.",
            discord.Color.green()
        )
        _log_action(guild_id, "clearwarnings_all", ctx.author.id, "Limpiar todas")
    else:
        old_warns = warnings[guild_id][member.id]
        warnings[guild_id][member.id] = 0
        e = make_embed(
            "🧹 Advertencias eliminadas",
            f"{member.mention} tiene ahora 0 advertencias (tenía {old_warns}).",
            discord.Color.green()
        )
        _log_action(guild_id, "clearwarnings", member.id, f"Limpiar de {old_warns} a 0")
    
    await ctx.send(embed=e)
    await log(ctx.guild, e)

@bot.command(name="get-logs")
@commands.has_permissions(administrator=True)
async def get_logs(ctx, limite: int = 20):
    """!get-logs [límite] — muestra las últimas acciones registradas."""
    guild_id = ctx.guild.id
    logs = action_logs[guild_id][-limite:]
    
    if not logs:
        await ctx.send("📋 No hay registros disponibles.", delete_after=5)
        return
    
    e = discord.Embed(
        title="📋 Registro de Acciones",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    
    description = ""
    for log_entry in reversed(logs):
        timestamp = log_entry["timestamp"].strftime("%H:%M:%S")
        action = log_entry["type"]
        user_id = log_entry["user_id"]
        reason = log_entry["reason"]
        description += f"**[{timestamp}]** `{action}` | <@{user_id}> | {reason}\n"
    
    e.description = description if description else "Sin acciones"
    e.set_footer(text=f"Mostrando últimas {len(logs)} acciones")
    await ctx.send(embed=e)

@bot.command(name="spam-filter")
@commands.has_permissions(administrator=True)
async def spam_filter(ctx, estado: str = None):
    """!spam-filter [on/off] — activa o desactiva el filtro de spam."""
    cfg = guild_config[ctx.guild.id]
    
    if estado is None:
        cfg["spam_filter"] = not cfg["spam_filter"]
    elif estado.lower() in ("on", "activar", "1"):
        cfg["spam_filter"] = True
    elif estado.lower() in ("off", "desactivar", "0"):
        cfg["spam_filter"] = False
    else:
        await ctx.send("Uso: `!spam-filter on` o `!spam-filter off`")
        return
    
    estado_actual = cfg["spam_filter"]
    color = discord.Color.green() if estado_actual else discord.Color.red()
    estado_str = "✅ ACTIVADO" if estado_actual else "❌ DESACTIVADO"
    
    e = make_embed(
        f"Filtro de spam {estado_str}",
        f"El filtro de spam ha sido {'activado' if estado_actual else 'desactivado'} por {ctx.author.mention}.",
        color
    )
    await ctx.send(embed=e)
    await log(ctx.guild, e)
    _log_action(ctx.guild.id, "spam_filter", ctx.author.id, f"Filtro: {estado_actual}")

@bot.command(name="reglas")
async def reglas(ctx):
    """!reglas — muestra las reglas del servidor."""
    e = discord.Embed(
        title="📜 Reglas del Servidor",
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow()
    )
    
    reglas = [
        ("1️⃣ Respeto", "Trata a todos con respeto. No se toleran insultos, burlas o discriminación."),
        ("2️⃣ No spam", "No envíes mensajes repetitivos, links innecesarios o contenido spam."),
        ("3️⃣ Contenido apropiado", "No se permite contenido sexual, violento, ilegal o perturbador."),
        ("4️⃣ Sin raid", "No intentes hacer raid, invitar botnets o ataques al servidor."),
        ("5️⃣ Sin publicidad", "No hagas publicidad de otros servidores o productos sin permiso."),
        ("6️⃣ Sin spoilers", "Usa spoilers al compartir contenido que puede arruinar películas/series."),
        ("7️⃣ Idioma", "Mantén un idioma apropiado. Sin palabras ofensivas excesivas."),
        ("8️⃣ No harassment", "No acosar, abusar o amenazar a otros miembros."),
        ("9️⃣ Privacidad", "No compartas datos personales de otros sin consentimiento."),
        ("🔟 Obedece a mods", "Respeta a los moderadores y sigue sus instrucciones."),
    ]
    
    for titulo, descripcion in reglas:
        e.add_field(name=titulo, value=descripcion, inline=False)
    
    e.add_field(
        name="⚠️ Violaciones de reglas",
        value="**Primero:** Advertencia\n**Segundo:** Mute\n**Tercero:** Expulsión\n**Grave:** Ban inmediato",
        inline=False
    )
    
    e.set_footer(text="Cumple las reglas para mantener un servidor seguro y amigable 💪")
    await ctx.send(embed=e)

# ─────────────────────────────────────────
#  SISTEMA DE AUTOROLE
# ─────────────────────────────────────────

@bot.command(name="autorole-set")
@commands.has_permissions(administrator=True)
async def autorole_set(ctx, role: discord.Role = None):
    """!autorole-set [@rol] — configura el rol automático para nuevos miembros."""
    guild_id = ctx.guild.id
    cfg = guild_config[guild_id]
    
    if role is None:
        cfg["autorole_id"] = None
        e = make_embed(
            "🎭 Autorole desactivado",
            "Ya no se asignará ningún rol automáticamente a los nuevos miembros.",
            discord.Color.orange()
        )
        await ctx.send(embed=e)
        await log(ctx.guild, e)
        _log_action(guild_id, "autorole", ctx.author.id, "Desactivar autorole")
        return
    
    cfg["autorole_id"] = role.id
    e = make_embed(
        "🎭 Autorole configurado",
        f"El rol {role.mention} será asignado automáticamente a todos los nuevos miembros.",
        discord.Color.green()
    )
    await ctx.send(embed=e)
    await log(ctx.guild, e)
    _log_action(guild_id, "autorole", ctx.author.id, f"Configurar: {role.name}")

# ─────────────────────────────────────────
#  SISTEMA DE TICKETS
# ─────────────────────────────────────────

@bot.command(name="ticket-setup")
@commands.has_permissions(administrator=True)
async def ticket_setup(ctx, category: discord.CategoryChannel = None):
    """!ticket-setup [#categoría] — configura la categoría para los tickets."""
    guild_id = ctx.guild.id
    cfg = guild_config[guild_id]
    
    if category is None:
        cfg["ticket_category"] = None
        cfg["ticket_counter"] = 0
        e = make_embed(
            "🎫 Sistema de tickets desactivado",
            "Los tickets ya no se pueden crear.",
            discord.Color.orange()
        )
        await ctx.send(embed=e)
        return
    
    cfg["ticket_category"] = category.id
    cfg["ticket_counter"] = 0
    e = make_embed(
        "🎫 Sistema de tickets configurado",
        f"Los tickets se crearán en: {category.mention}",
        discord.Color.green()
    )
    await ctx.send(embed=e)
    _log_action(guild_id, "ticket_setup", ctx.author.id, f"Categoría: {category.name}")

@bot.command(name="ticket")
async def ticket_cmd(ctx, accion: str = None, *, asunto: str = None):
    """!ticket create [asunto] | !ticket add @user | !ticket remove @user | !ticket close"""
    guild_id = ctx.guild.id
    cfg = guild_config[guild_id]
    
    if accion is None:
        await ctx.send(
            embed=make_embed(
                "❌ Uso incorrecto",
                "Usa: `!ticket create [asunto]` | `!ticket add @user` | `!ticket remove @user` | `!ticket close`",
                discord.Color.red()
            )
        )
        return
    
    accion = accion.lower()
    
    # ── Crear ticket ──
    if accion == "create":
        if cfg["ticket_category"] is None:
            await ctx.send("❌ El sistema de tickets no está configurado.", delete_after=5)
            return
        
        if asunto is None:
            await ctx.send("❌ Debes proporcionar un asunto para el ticket.", delete_after=5)
            return
        
        category = ctx.guild.get_channel(cfg["ticket_category"])
        if category is None:
            await ctx.send("❌ La categoría de tickets no existe.", delete_after=5)
            return
        
        # Incrementar contador
        cfg["ticket_counter"] += 1
        ticket_number = cfg["ticket_counter"]
        channel_name = f"ticket-{ticket_number}"
        
        # Crear permisos
        overwrites = {
            ctx.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            ctx.author: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            ctx.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True)
        }
        
        try:
            ticket_channel = await category.create_text_channel(
                channel_name,
                overwrites=overwrites,
                reason=f"Ticket creado por {ctx.author}"
            )
            
            tickets[guild_id][ticket_channel.id] = {
                "creator_id": ctx.author.id,
                "title": asunto,
                "created_at": datetime.utcnow()
            }
            
            e = discord.Embed(
                title=f"🎫 Ticket #{ticket_number}",
                description=f"**Asunto:** {asunto}\n**Creador:** {ctx.author.mention}",
                color=discord.Color.blurple(),
                timestamp=datetime.utcnow()
            )
            e.add_field(
                name="📝 Comandos disponibles",
                value="`!ticket add @user` - Agregar usuario\n`!ticket remove @user` - Remover usuario\n`!ticket close` - Cerrar ticket",
                inline=False
            )
            e.set_footer(text="Anti-Raid Bot • Sistema de Tickets")
            
            await ticket_channel.send(embed=e)
            await ctx.send(f"✅ Ticket creado: {ticket_channel.mention}", delete_after=5)
            _log_action(guild_id, "ticket_create", ctx.author.id, f"Ticket #{ticket_number}: {asunto}")
            
        except discord.Forbidden:
            await ctx.send("❌ No tengo permisos para crear canales.", delete_after=5)
    
    # ── Agregar usuario ──
    elif accion == "add":
        if not isinstance(ctx.channel, discord.TextChannel) or ctx.channel.id not in tickets[guild_id]:
            await ctx.send("❌ Este comando solo funciona dentro de un ticket.", delete_after=5)
            return
        
        if ctx.message.mentions:
            user = ctx.message.mentions[0]
            try:
                await ctx.channel.set_permissions(user, view_channel=True, send_messages=True)
                e = make_embed(
                    "✅ Usuario agregado",
                    f"{user.mention} ahora puede ver y escribir en este ticket.",
                    discord.Color.green()
                )
                await ctx.send(embed=e)
                _log_action(guild_id, "ticket_add", user.id, f"Agregado al ticket {ctx.channel.name}")
            except discord.Forbidden:
                await ctx.send("❌ No puedo cambiar permisos.", delete_after=5)
        else:
            await ctx.send("❌ Debes mencionar a un usuario.", delete_after=5)
    
    # ── Remover usuario ──
    elif accion == "remove":
        if not isinstance(ctx.channel, discord.TextChannel) or ctx.channel.id not in tickets[guild_id]:
            await ctx.send("❌ Este comando solo funciona dentro de un ticket.", delete_after=5)
            return
        
        if ctx.message.mentions:
            user = ctx.message.mentions[0]
            try:
                await ctx.channel.set_permissions(user, view_channel=False, send_messages=False)
                e = make_embed(
                    "✅ Usuario removido",
                    f"{user.mention} ya no puede ver este ticket.",
                    discord.Color.orange()
                )
                await ctx.send(embed=e)
                _log_action(guild_id, "ticket_remove", user.id, f"Removido del ticket {ctx.channel.name}")
            except discord.Forbidden:
                await ctx.send("❌ No puedo cambiar permisos.", delete_after=5)
        else:
            await ctx.send("❌ Debes mencionar a un usuario.", delete_after=5)
    
    # ── Cerrar ticket ──
    elif accion == "close":
        if not isinstance(ctx.channel, discord.TextChannel) or ctx.channel.id not in tickets[guild_id]:
            await ctx.send("❌ Este comando solo funciona dentro de un ticket.", delete_after=5)
            return
        
        ticket_info = tickets[guild_id][ctx.channel.id]
        ticket_name = ctx.channel.name
        
        e = make_embed(
            "🎫 Ticket cerrado",
            f"El ticket {ticket_name} ha sido cerrado por {ctx.author.mention}.",
            discord.Color.red()
        )
        await ctx.send(embed=e)
        
        del tickets[guild_id][ctx.channel.id]
        _log_action(guild_id, "ticket_close", ctx.author.id, f"Cerrado: {ticket_name}")
        
        await asyncio.sleep(2)
        try:
            await ctx.channel.delete(reason=f"Ticket cerrado por {ctx.author}")
        except discord.Forbidden:
            pass

@bot.command(name="ayuda")
async def ayuda(ctx):
    """!ayuda — muestra todos los comandos."""
    e = discord.Embed(title="🛡️ Comandos Anti-Raid", color=discord.Color.blurple(),
                      timestamp=discord.utils.utcnow())
    
    mod_cmds = [
        ("!kick @user [razón]",         "Expulsa a un usuario"),
        ("!mute @user [min] [razón]",  "Silencia a un usuario (minutos)"),
        ("!unmute @user",               "Dessilencia a un usuario"),
        ("!unmuteall",                  "Dessilencia a TODOS"),
        ("!warn @user [razón]",         "Advierte a un usuario (3 = expulsión)"),
        ("!clearwarnings [@user]",      "Limpia advertencias"),
    ]
    
    info_cmds = [
        ("!get-logs [limite]",          "Ver últimas acciones registradas"),
        ("!spam-filter [on/off]",       "Activa/desactiva filtro de spam"),
        ("!reglas",                     "Muestra las reglas del servidor"),
    ]
    
    sistema_cmds = [
        ("!autorole-set [@rol]",        "Configura rol automático para nuevos miembros"),
        ("!ticket-setup [#categoría]",  "Configura el sistema de tickets"),
        ("!ticket create [asunto]",     "Crea un nuevo ticket"),
    ]
    
    e.add_field(name="🔨 Moderación", value="━━━━━━━━━━━━━━━━━━", inline=False)
    for nombre, desc in mod_cmds:
        e.add_field(name=f"`{nombre}`", value=desc, inline=False)
    
    e.add_field(name="📊 Información", value="━━━━━━━━━━━━━━━━━━", inline=False)
    for nombre, desc in info_cmds:
        e.add_field(name=f"`{nombre}`", value=desc, inline=False)
    
    e.add_field(name="⚙️ Sistemas", value="━━━━━━━━━━━━━━━━━━", inline=False)
    for nombre, desc in sistema_cmds:
        e.add_field(name=f"`{nombre}`", value=desc, inline=False)
    
    e.set_footer(text="Anti-Raid Bot • Solo admins pueden usar comandos de moderación")
    await ctx.send(embed=e)

@bot.command(name="estado")
async def estado(ctx):
    """!estado — muestra el estado actual del bot anti-raid."""
    color = discord.Color.red() if raid_mode else discord.Color.green()
    e = discord.Embed(title="📊 Estado del Bot Anti-Raid", color=color,
                      timestamp=discord.utils.utcnow())
    e.add_field(name="Modo raid", value="🚨 ACTIVO" if raid_mode else "✅ Inactivo", inline=True)
    e.add_field(name="Filtro spam", value="✅ Activo" if guild_config[ctx.guild.id]["spam_filter"] else "❌ Inactivo", inline=True)
    e.set_footer(text="Anti-Raid Bot")
    await ctx.send(embed=e)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ No tienes permisos para usar este comando.", delete_after=5)
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("❌ Usuario no encontrado.", delete_after=5)
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Argumento inválido. Revisa `!ayuda`.", delete_after=5)
    else:
        print(f"Error: {error}")

bot.run(TOKEN)
