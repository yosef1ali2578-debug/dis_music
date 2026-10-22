import os
import asyncio
import discord
from discord.ext import commands, tasks
from aiohttp import web
import yt_dlp
import time

# إعدادات البوت
TOKEN = os.environ.get("DISCORD_TOKEN", "MTU2MjY0NDI1ODU4NDc5MzE1OA.GlDIST.T8s0_YqlbHCJBhIZjdka97p5Pbw-pS0HJM5ErQ")
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

    state.
