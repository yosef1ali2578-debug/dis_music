import os
import asyncio
import discord
from discord.ext import commands, tasks
from aiohttp import web
import yt_dlp
import time

# إعدادات البوت
TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = None
PORT = int(os.environ.get("PORT", 8080))

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",
    "extract_flat": False,
}

FFMPEG_BASE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"


class Song:
    def __init__(self, info: dict, requested_by: str = "YouTube Music Sync"):
        self.video_id = info.get("id")
        self.title = info.get("title", "أغنية غير معروفة")
        self.url = info.get("webpage_url") or f"https://www.youtube.com/watch?v={self.video_id}"
        self.stream_url = info.get("url")
        self.duration = int(info.get("duration") or 0)
        self.thumbnail = info.get("thumbnail")
        self.uploader = info.get("uploader", "غير معروف")
        self.requested_by = requested_by


class MusicState:
    def __init__(self):
        self.voice_client: discord.VoiceClient | None = None
        self.text_channel: discord.abc.Messageable | None = None
        self.panel_message: discord.Message | None = None
        self.current_song: Song | None = None
        self.queue: list[Song] = []
        self.history: list[Song] = []
        self.volume: float = 0.8
        self.start_time: float = 0.0
        self.pause_start: float = 0.0
        self.seek_offset: int = 0
        self.is_seeking: bool = False
        self.auto_mood: bool = True

    @property
    def current_position(self) -> int:
        if not self.current_song:
            return 0
        if self.voice_client and self.voice_client.is_paused():
            elapsed = self.pause_start - self.start_time
        else:
            elapsed = time.time() - self.start_time
        pos = int(self.seek_offset + max(0, elapsed))
        return min(pos, self.current_song.duration) if self.current_song.duration else pos


state = MusicState()


def format_time(seconds: int) -> str:
    mins, secs = divmod(max(0, int(seconds)), 60)
    hrs, mins = divmod(mins, 60)
    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def build_slider_bar(current: int, total: int, length: int = 18) -> str:
    if total <= 0:
        return "🔘" + "─" * (length - 1)
    ratio = min(max(current / total, 0.0), 1.0)
    dot_pos = int(ratio * (length - 1))
    return "━" * dot_pos + "🔘" + "─" * (length - 1 - dot_pos)


async def extract_song_info(query_or_url: str, extract_playlist: bool = False) -> list[Song]:
    opts = YTDL_OPTIONS.copy()
    opts["noplaylist"] = not extract_playlist

    def _extract():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(query_or_url, download=False)

    data = await asyncio.to_thread(_extract)
    songs = []
    if "entries" in data:
        for entry in data["entries"]:
            if entry:
                songs.append(Song(entry))
    elif data:
        songs.append(Song(data))
    return songs


async def fetch_mood_recommendation(video_id: str) -> Song | None:
    if not video_id:
        return None
    radio_url = f"https://music.youtube.com/watch?v={video_id}&list=RDAMVM{video_id}"
    opts = {
        "quiet": True,
        "extract_flat": True,
        "playlist_items": "2-6",
    }

    def _fetch_radio():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(radio_url, download=False)

    try:
        data = await asyncio.to_thread(_fetch_radio)
        entries = data.get("entries", []) if data else []
        history_ids = {s.video_id for s in state.history[-10:]}
        for entry in entries:
            vid = entry.get("id")
            if vid and vid != video_id and vid not in history_ids:
                full_songs = await extract_song_info(f"https://www.youtube.com/watch?v={vid}")
                if full_songs:
                    song = full_songs[0]
                    song.requested_by = "توصية تلقائية (نفس المود 🎧)"
                    return song
    except Exception as e:
        print(f"Mood recommendation error: {e}")
    return None


async def play_song(song: Song, start_at: int = 0):
    if not state.voice_client or not state.voice_client.is_connected():
        return

    if not song.stream_url:
        extracted = await extract_song_info(song.url)
        if extracted:
            song = extracted[0]

    state.is_seeking = True
    if state.voice_client.is_playing() or state.voice_client.is_paused():
        state.voice_client.stop()

    state.current_song = song
    state.seek_offset = start_at
    state.start_time = time.time()

    before_opts = f"-ss {start_at} {FFMPEG_BASE_OPTIONS}" if start_at > 0 else FFMPEG_BASE_OPTIONS
    source = discord.FFmpegPCMAudio(
        song.stream_url,
        before_options=before_opts,
        options="-vn",
    )
    volume_source = discord.PCMVolumeTransformer(source, volume=state.volume)

    def after_playing(error):
        if error:
            print(f"Playback error: {error}")
        if not state.is_seeking:
            asyncio.run_coroutine_threadsafe(handle_song_end(), bot.loop)

    state.is_seeking = False
    state.voice_client.play(volume_source, after=after_playing)
    await update_control_panel()


async def handle_song_end():
    if state.current_song:
        state.history.append(state.current_song)

    if state.queue:
        next_song = state.queue.pop(0)
        await play_song(next_song, start_at=0)
    elif state.auto_mood and state.current_song and state.current_song.video_id:
        mood_song = await fetch_mood_recommendation(state.current_song.video_id)
        if mood_song:
            await play_song(mood_song, start_at=0)
        else:
            state.current_song = None
            await update_control_panel()
    else:
        state.current_song = None
        await update_control_panel()


class AddSongModal(discord.ui.Modal, title="إضافة أغنية أو بلاي ليست"):
    query = discord.ui.TextInput(
        label="اسم الأغنية أو رابط يوتيوب / يوتيوب ميوزيك",
        placeholder="اكتب اسم الأغنية أو الصق الرابط هنا...",
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not state.voice_client or not state.voice_client.is_connected():
            if interaction.user.voice and interaction.user.voice.channel:
                state.voice_client = await interaction.user.voice.channel.connect()
            else:
                await interaction.followup.send("❌ يجب أن تكون في روم صوتي أولاً!", ephemeral=True)
                return

        is_playlist = "list=" in self.query.value
        songs = await extract_song_info(self.query.value, extract_playlist=is_playlist)
        if not songs:
            await interaction.followup.send("❌ لم يتم العثور على نتائج.", ephemeral=True)
            return

        for s in songs:
            s.requested_by = interaction.user.display_name

        if not state.current_song:
            first = songs.pop(0)
            state.queue.extend(songs)
            await play_song(first)
            await interaction.followup.send(f"▶️ تم تشغيل **{first.title}**", ephemeral=True)
        else:
            state.queue.extend(songs)
            await update_control_panel()
            await interaction.followup.send(f"✅ تمت إضافة **{len(songs)}** أغنية إلى الطابور.", ephemeral=True)


class SeekSliderSelect(discord.ui.Select):
    def __init__(self, duration: int):
        options = []
        if duration > 0:
            for pct in range(0, 100, 10):
                sec = int((pct / 100) * duration)
                options.append(
                    discord.SelectOption(
                        label=f"الانتقال إلى {pct}% ({format_time(sec)})",
                        value=str(sec),
                        description=f"تقديم/تأخير الأغنية إلى الدقيقة {format_time(sec)}",
                    )
                )
        else:
            options.append(discord.SelectOption(label="السلايدر متاح عند تشغيل أغنية", value="0"))

        super().__init__(
            placeholder="🎚️ سلايدر التحكم في وقت الأغنية (Seek Slider)...",
            min_values=1,
            max_values=1,
            options=options,
            disabled=(duration <= 0),
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if state.current_song:
            target_sec = int(self.values[0])
            await play_song(state.current_song, start_at=target_sec)


class MusicControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        duration = state.current_song.duration if state.current_song else 0
        self.add_item(SeekSliderSelect(duration))

    @discord.ui.button(label="⏮️ السابق", style=discord.ButtonStyle.secondary, row=1)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if state.history:
            if state.current_song:
                state.queue.insert(0, state.current_song)
            prev_song = state.history.pop()
            await play_song(prev_song, start_at=0)

    @discord.ui.button(label="⏪ -10s", style=discord.ButtonStyle.secondary, row=1)
    async def rewind_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if state.current_song:
            new_pos = max(0, state.current_position - 10)
            await play_song(state.current_song, start_at=new_pos)

    @discord.ui.button(label="⏯️ إيقاف/تشغيل", style=discord.ButtonStyle.primary, row=1)
    async def play_pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if state.voice_client:
            if state.voice_client.is_playing():
                state.pause_start = time.time()
                state.voice_client.pause()
            elif state.voice_client.is_paused():
                state.start_time += time.time() - state.pause_start
                state.voice_client.resume()
            await update_control_panel()

    @discord.ui.button(label="⏩ +10s", style=discord.ButtonStyle.secondary, row=1)
    async def forward_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        if state.current_song:
            new_pos = min(state.current_song.duration - 2, state.current_position + 10)
            await play_song(state.current_song, start_at=max(0, new_pos))

    @discord.ui.button(label="⏭️ تخطي", style=discord.ButtonStyle.secondary, row=1)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await handle_song_end()

    @discord.ui.button(label="🔉 خفض", style=discord.ButtonStyle.secondary, row=2)
    async def vol_down_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        state.volume = max(0.0, round(state.volume - 0.1, 2))
        if state.voice_client and state.voice_client.source:
            state.voice_client.source.volume = state.volume
        await update_control_panel()

    @discord.ui.button(label="🔊 رفع", style=discord.ButtonStyle.secondary, row=2)
    async def vol_up_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        state.volume = min(2.0, round(state.volume + 0.1, 2))
        if state.voice_client and state.voice_client.source:
            state.voice_client.source.volume = state.volume
        await update_control_panel()

    @discord.ui.button(label="🎧 مود تلقائي", style=discord.ButtonStyle.success, row=2)
    async def mood_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        state.auto_mood = not state.auto_mood
        await update_control_panel()

    @discord.ui.button(label="➕ إضافة أغنية", style=discord.ButtonStyle.primary, row=2)
    async def add_song_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddSongModal())

    @discord.ui.button(label="🗑️ مسح الطابور", style=discord.ButtonStyle.danger, row=2)
    async def clear_queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        state.queue.clear()
        await update_control_panel()


def build_panel_embed() -> discord.Embed:
    embed = discord.Embed(title="🎛️ لوحة التحكم بالموسيقى (Discord Music Panel)", color=0x1D4E89)
    if state.current_song:
        song = state.current_song
        pos = state.current_position
        bar = build_slider_bar(pos, song.duration)
        status_icon = "⏸️ متوقف مؤقتاً" if (state.voice_client and state.voice_client.is_paused()) else "▶️ قيد التشغيل"

        embed.description = (
            f"### [{song.title}]({song.url})\n"
            f"**القناة:** `{song.uploader}` | **بطلب من:** `{song.requested_by}`\n\n"
            f"**الحالة:** {status_icon} | **الصوت:** `{int(state.volume * 100)}%` | "
            f"**تشغيل نفس المود تلقائياً:** `{'مفعل ✅' if state.auto_mood else 'معطل ❌'}`\n\n"
            f"`{format_time(pos)}` {bar} `{format_time(song.duration)}`"
        )
        if song.thumbnail:
            embed.set_thumbnail(url=song.thumbnail)
    else:
        embed.description = "لا توجد أغنية قيد التشغيل حالياً. اضغط على **➕ إضافة أغنية** أو شغل أغنية من يوتيوب ميوزيك."

    if state.queue:
        q_list = "\n".join(
            [f"`{i+1}.` **{s.title}** (`{format_time(s.duration)}`)" for i, s in enumerate(state.queue[:8])]
        )
        if len(state.queue) > 8:
            q_list += f"\n... و **{len(state.queue) - 8}** أغاني أخرى في الطابور"
        embed.add_field(name=f"📜 طابور الانتظار (Queue: {len(state.queue)})", value=q_list, inline=False)
    else:
        embed.add_field(
            name="📜 طابور الانتظار (Queue)",
            value="الطابور فارغ — سيتم تشغيل أغنية بنفس المود تلقائياً بعد انتهاء الأغنية الحالية." if state.auto_mood else "الطابور فارغ.",
            inline=False,
        )
    return embed


async def update_control_panel():
    if not state.text_channel:
        return
    embed = build_panel_embed()
    view = MusicControlView()
    try:
        if state.panel_message:
            await state.panel_message.edit(embed=embed, view=view)
        else:
            state.panel_message = await state.text_channel.send(embed=embed, view=view)
    except Exception:
        state.panel_message = await state.text_channel.send(embed=embed, view=view)


@tasks.loop(seconds=5)
async def refresh_panel_task():
    if state.current_song and state.panel_message:
        await update_control_panel()


@bot.command(name="panel")
async def create_panel(ctx: commands.Context):
    state.text_channel = ctx.channel
    if ctx.author.voice and ctx.author.voice.channel:
        if not state.voice_client or not state.voice_client.is_connected():
            state.voice_client = await ctx.author.voice.channel.connect()
    state.panel_message = None
    await update_control_panel()


async def handle_health(request: web.Request):
    return web.Response(text="Discord Music Bot is running!")


async def handle_sync(request: web.Request):
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }
    if request.method == "OPTIONS":
        return web.Response(status=200, headers=headers)

    data = await request.json()
    action = data.get("action")
    video_id = data.get("videoId")
    position = int(float(data.get("position", 0)))
    playlist_items = data.get("playlist", [])

    if not state.voice_client or not state.voice_client.is_connected():
        for guild in bot.guilds:
            for vc in guild.voice_channels:
                if len([m for m in vc.members if not m.bot]) > 0:
                    state.voice_client = await vc.connect()
                    break

    if action == "track_change" and video_id:
        url = f"https://www.youtube.com/watch?v={video_id}"
        songs = await extract_song_info(url)
        if songs:
            await play_song(songs[0], start_at=position)

        if playlist_items:
            state.queue.clear()
            for item_id in playlist_items[:10]:
                if item_id != video_id:
                    extracted = await extract_song_info(f"https://www.youtube.com/watch?v={item_id}")
                    if extracted:
                        state.queue.append(extracted[0])
            await update_control_panel()

    elif action == "seek" and state.current_song:
        if abs(state.current_position - position) >= 2:
            await play_song(state.current_song, start_at=position)

    elif action == "pause" and state.voice_client and state.voice_client.is_playing():
        state.pause_start = time.time()
        state.voice_client.pause()
        await update_control_panel()

    elif action == "play" and state.voice_client and state.voice_client.is_paused():
        state.start_time += time.time() - state.pause_start
        state.voice_client.resume()
        await update_control_panel()

    return web.json_response({"status": "ok"}, headers=headers)


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_route("*", "/sync", handle_sync)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"🌐 Sync API Server running on port {PORT}")


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    await start_web_server()
    if not refresh_panel_task.is_running():
        refresh_panel_task.start()


if __name__ == "__main__":
    bot.run(TOKEN)
