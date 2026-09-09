"""Телеграм-бот для поиска музыки.

Отправьте название песни или имя исполнителя; бот ищет по Deezer,
Audius и (опционально) Jamendo. Треки с Audius/Jamendo — легально
свободные, отправляются полными MP3-файлами. Совпадения из Deezer
получают официальное 30-секундное превью и кнопки-ссылки на полную
версию в стриминговых сервисах.
"""

import asyncio
import logging
import os
from urllib.parse import quote_plus

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
JAMENDO_CLIENT_ID = os.getenv("JAMENDO_CLIENT_ID")  # необязательно
DEEZER_API = "https://api.deezer.com"
AUDIUS_API = "https://discoveryprovider.audius.co/v1"
JAMENDO_API = "https://api.jamendo.com/v3.0"
AUDIUS_APP = "musicfinderbot"
MAX_RESULTS = 5
MAX_UPLOAD_BYTES = 49 * 1024 * 1024  # лимит Bot API на загрузку — 50 МБ

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("music-finder")

dp = Dispatcher()


async def deezer_search(session: aiohttp.ClientSession, query: str) -> list[dict]:
    async with session.get(
        f"{DEEZER_API}/search", params={"q": query, "limit": MAX_RESULTS}
    ) as resp:
        resp.raise_for_status()
        payload = await resp.json()
    return payload.get("data", [])


async def deezer_track(session: aiohttp.ClientSession, track_id: str) -> dict | None:
    async with session.get(f"{DEEZER_API}/track/{track_id}") as resp:
        resp.raise_for_status()
        track = await resp.json()
    return None if track.get("error") else track


async def audius_search(session: aiohttp.ClientSession, query: str) -> list[dict]:
    async with session.get(
        f"{AUDIUS_API}/tracks/search",
        params={"query": query, "app_name": AUDIUS_APP, "limit": MAX_RESULTS},
    ) as resp:
        resp.raise_for_status()
        payload = await resp.json()
    return [t for t in payload.get("data", []) if t.get("is_streamable")]


async def audius_track(session: aiohttp.ClientSession, track_id: str) -> dict | None:
    async with session.get(
        f"{AUDIUS_API}/tracks/{track_id}", params={"app_name": AUDIUS_APP}
    ) as resp:
        if resp.status != 200:
            return None
        payload = await resp.json()
    return payload.get("data")


async def jamendo_search(session: aiohttp.ClientSession, query: str) -> list[dict]:
    if not JAMENDO_CLIENT_ID:
        return []
    async with session.get(
        f"{JAMENDO_API}/tracks/",
        params={
            "client_id": JAMENDO_CLIENT_ID,
            "format": "json",
            "limit": MAX_RESULTS,
            "search": query,
            "audioformat": "mp32",
        },
    ) as resp:
        resp.raise_for_status()
        payload = await resp.json()
    if payload.get("headers", {}).get("status") != "success":
        log.warning("Jamendo error: %s", payload.get("headers", {}).get("error_message"))
        return []
    return [t for t in payload.get("results", []) if t.get("audio")]


async def jamendo_track(session: aiohttp.ClientSession, track_id: str) -> dict | None:
    if not JAMENDO_CLIENT_ID:
        return None
    async with session.get(
        f"{JAMENDO_API}/tracks/",
        params={
            "client_id": JAMENDO_CLIENT_ID,
            "format": "json",
            "id": track_id,
            "audioformat": "mp32",
        },
    ) as resp:
        resp.raise_for_status()
        payload = await resp.json()
    results = payload.get("results", [])
    return results[0] if results else None


def streaming_links_keyboard(artist: str, title: str, deezer_url: str) -> InlineKeyboardMarkup:
    q = quote_plus(f"{artist} {title}")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="▶️ Яндекс Музыка",
                    url=f"https://music.yandex.ru/search?text={q}",
                ),
                InlineKeyboardButton(text="▶️ Deezer", url=deezer_url),
            ],
            [
                InlineKeyboardButton(
                    text="▶️ YouTube Music",
                    url=f"https://music.youtube.com/search?q={q}",
                ),
                InlineKeyboardButton(
                    text="▶️ Spotify",
                    url=f"https://open.spotify.com/search/{q}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="▶️ Apple Music",
                    url=f"https://music.apple.com/search?term={q}",
                ),
            ],
        ]
    )


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(
        "🎵 Привет! Отправь мне название песни или имя исполнителя.\n\n"
        "Результаты с пометкой 🆓 — полные треки (Audius/Jamendo), "
        "их я пришлю прямо сюда.\n"
        "Остальные приходят с 30-секундным превью и кнопками, открывающими "
        "полную песню в Яндекс Музыке, YouTube Music, Spotify, Deezer и Apple Music."
    )


@dp.message(F.text & ~F.text.startswith("/"))
async def handle_search(message: Message) -> None:
    query = message.text.strip()
    async with aiohttp.ClientSession() as session:
        deezer_res, audius_res, jamendo_res = await asyncio.gather(
            deezer_search(session, query),
            audius_search(session, query),
            jamendo_search(session, query),
            return_exceptions=True,
        )
    if isinstance(deezer_res, BaseException):
        log.warning("Deezer search failed: %s", deezer_res)
        deezer_res = []
    if isinstance(audius_res, BaseException):
        log.warning("Audius search failed: %s", audius_res)
        audius_res = []
    if isinstance(jamendo_res, BaseException):
        log.warning("Jamendo search failed: %s", jamendo_res)
        jamendo_res = []

    buttons = (
        [
            [
                InlineKeyboardButton(
                    text=f"🆓 {t['user']['name']} — {t['title']}",
                    callback_data=f"aud:{t['id']}",
                )
            ]
            for t in audius_res[:MAX_RESULTS]
        ]
        + [
            [
                InlineKeyboardButton(
                    text=f"🆓 {t['artist_name']} — {t['name']}",
                    callback_data=f"jam:{t['id']}",
                )
            ]
            for t in jamendo_res[:MAX_RESULTS]
        ]
        + [
            [
                InlineKeyboardButton(
                    text=f"{t['artist']['name']} — {t['title']}",
                    callback_data=f"dz:{t['id']}",
                )
            ]
            for t in deezer_res[:MAX_RESULTS]
        ]
    )

    if not buttons:
        await message.answer(
            f"По запросу «{query}» ничего не нашлось. Попробуй другое написание?"
        )
        return

    await message.answer(
        "Вот что нашлось — выбирай (🆓 = полный трек):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


async def download(session: aiohttp.ClientSession, url: str, **kwargs) -> bytes:
    async with session.get(
        url, timeout=aiohttp.ClientTimeout(total=300), **kwargs
    ) as resp:
        resp.raise_for_status()
        return await resp.read()


async def send_full_track(
    callback: CallbackQuery,
    audio_bytes: bytes,
    artist: str,
    title: str,
    duration: int | None,
    source: str,
    status: Message,
) -> None:
    if len(audio_bytes) > MAX_UPLOAD_BYTES:
        await status.edit_text(
            "😔 Этот трек больше лимита Telegram на загрузку ботом (50 МБ)."
        )
        return
    await callback.message.answer_audio(
        audio=BufferedInputFile(audio_bytes, filename=f"{artist} - {title}.mp3"),
        title=title,
        performer=artist,
        duration=duration,
        caption=f"🎵 {artist} — {title} (полный трек, {source})",
    )
    await status.delete()


@dp.callback_query(F.data.startswith("dz:"))
async def send_deezer_preview(callback: CallbackQuery) -> None:
    track_id = callback.data.split(":", 1)[1]
    await callback.answer("Загружаю превью…")

    async with aiohttp.ClientSession() as session:
        try:
            track = await deezer_track(session, track_id)
            if not track or not track.get("preview"):
                await callback.message.answer("😔 Для этого трека нет превью.")
                return
            audio_bytes = await download(session, track["preview"])
        except aiohttp.ClientError:
            log.exception("Failed to fetch preview for track %s", track_id)
            await callback.message.answer(
                "😔 Не удалось загрузить превью, попробуй ещё раз."
            )
            return

    artist = track["artist"]["name"]
    title = track["title"]
    await callback.message.answer_audio(
        audio=BufferedInputFile(
            audio_bytes, filename=f"{artist} - {title} (превью).mp3"
        ),
        title=f"{title} (превью 30 сек)",
        performer=artist,
        caption=(
            f"🎵 {artist} — {title}\nАльбом: {track['album']['title']}\n"
            "Полная песня — в один тап ниже:"
        ),
        reply_markup=streaming_links_keyboard(artist, title, track["link"]),
    )


@dp.callback_query(F.data.startswith("aud:"))
async def send_audius_full(callback: CallbackQuery) -> None:
    track_id = callback.data.split(":", 1)[1]
    await callback.answer("Скачиваю полный трек…")

    async with aiohttp.ClientSession() as session:
        try:
            track = await audius_track(session, track_id)
            if not track:
                await callback.message.answer("😔 Этот трек больше недоступен.")
                return
            status = await callback.message.answer(
                f"⬇️ Скачиваю {track['user']['name']} — {track['title']}…"
            )
            audio_bytes = await download(
                session,
                f"{AUDIUS_API}/tracks/{track_id}/stream",
                params={"app_name": AUDIUS_APP},
            )
        except aiohttp.ClientError:
            log.exception("Failed to stream Audius track %s", track_id)
            await callback.message.answer(
                "😔 Не удалось скачать трек, попробуй ещё раз."
            )
            return

    await send_full_track(
        callback,
        audio_bytes,
        track["user"]["name"],
        track["title"],
        track.get("duration"),
        "Audius",
        status,
    )


@dp.callback_query(F.data.startswith("jam:"))
async def send_jamendo_full(callback: CallbackQuery) -> None:
    track_id = callback.data.split(":", 1)[1]
    await callback.answer("Скачиваю полный трек…")

    async with aiohttp.ClientSession() as session:
        try:
            track = await jamendo_track(session, track_id)
            if not track or not track.get("audio"):
                await callback.message.answer("😔 Этот трек больше недоступен.")
                return
            status = await callback.message.answer(
                f"⬇️ Скачиваю {track['artist_name']} — {track['name']}…"
            )
            audio_bytes = await download(session, track["audio"])
        except aiohttp.ClientError:
            log.exception("Failed to stream Jamendo track %s", track_id)
            await callback.message.answer(
                "😔 Не удалось скачать трек, попробуй ещё раз."
            )
            return

    await send_full_track(
        callback,
        audio_bytes,
        track["artist_name"],
        track["name"],
        track.get("duration"),
        "Jamendo",
        status,
    )


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "Не задан BOT_TOKEN. Скопируйте .env.example в .env и вставьте токен."
        )
    if not JAMENDO_CLIENT_ID:
        log.info("JAMENDO_CLIENT_ID не задан — источник Jamendo отключён.")
    bot = Bot(token=BOT_TOKEN)
    log.info("Бот запущен, начинаю polling…")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
