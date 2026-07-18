import asyncio
import html
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import edge_tts
import requests
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
PEXELS_API_KEY = os.environ["PEXELS_API_KEY"]

OUTPUT_DIR = Path(os.getenv("MEDIA_OUTPUT_DIR", "media-test-output"))
MAX_SCENES = int(os.getenv("MAX_MEDIA_TEST_SCENES", "8"))

# 120 = 2 dəqiqəlik meditasiya testi.
# Sonra 600 yazaraq 10 dəqiqə edə bilərsən.
MEDITATION_TEST_SECONDS = int(
    os.getenv("MEDITATION_TEST_SECONDS", "120")
)

PEXELS_ENDPOINT = "https://api.pexels.com/videos/search"
USER_AGENT = "AutoTube-Studio/3.0"
REQUEST_TIMEOUT = 90
DOWNLOAD_TIMEOUT = 240
PEXELS_RESULTS_PER_QUERY = 40

SHORT_WIDTH = 1080
SHORT_HEIGHT = 1920
LONG_WIDTH = 1920
LONG_HEIGHT = 1080
VIDEO_FPS = 30

SHORT_LOGO_WIDTH = 92
LONG_LOGO_WIDTH = 118
LOGO_RIGHT_MARGIN = 28
LOGO_TOP_MARGIN = 28
LOGO_OPACITY = 0.82
LOGO_BLACK_SIMILARITY = 0.18
LOGO_BLACK_BLEND = 0.08

SHORT_SUBTITLE_FONT_SIZE = 11
SHORT_SUBTITLE_MARGIN_BOTTOM = 300
SHORT_SUBTITLE_MAX_WORDS = 5

LONG_SUBTITLE_FONT_SIZE = 18
LONG_SUBTITLE_MARGIN_BOTTOM = 105
LONG_SUBTITLE_MAX_WORDS = 9

SUBTITLE_OUTLINE = 1.8
SUBTITLE_SHADOW = 1.2
SUBTITLE_SIDE_MARGIN = 55

NORMALIZE_CRF = 22
FINAL_CRF = 20
NORMALIZE_PRESET = "veryfast"
FINAL_PRESET = "medium"
SCENE_FADE_SECONDS = 0.35

VOICE_PROFILES = {
    "default": {
        "rate": "-2%",
        "pitch": "+0Hz",
        "volume": "+0%",
        "gender": "Female",
    },
    "kids": {
        "rate": "+4%",
        "pitch": "+12Hz",
        "volume": "+0%",
        "gender": "Female",
    },
}

SAFE_MEDITATION_WORDS = {
    "forest", "ocean", "waves", "water", "rain", "clouds", "sunrise",
    "sunset", "river", "mountain", "nature", "peaceful", "calm",
    "mist", "trees", "lake", "waterfall",
}

REJECT_MEDITATION_WORDS = {
    "person", "people", "woman", "man", "girl", "boy", "face",
    "dance", "dancing", "party", "concert", "costume", "fashion",
    "city", "traffic", "office", "business", "phone", "computer",
}


def run(command: list[str]) -> str:
    print("RUN:", " ".join(str(x) for x in command), flush=True)
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError("Komanda uğursuz oldu:\n" + result.stderr[-5000:])
    return result.stdout


def media_duration(path: Path) -> float:
    return float(run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]).strip())


def update_video(supabase, video_id: str, values: dict[str, Any]) -> None:
    result = supabase.table("video_queue").update(values).eq("id", video_id).execute()
    if not result.data:
        raise RuntimeError("video_queue sətri yenilənmədi.")


def add_event(
    supabase,
    video: dict[str, Any],
    event_type: str,
    message: str,
    level: str = "info",
    payload: dict[str, Any] | None = None,
) -> None:
    values = {
        "owner_id": video["owner_id"],
        "video_id": video["id"],
        "channel_id": video["channel_id"],
        "type": event_type,
        "message": message,
        "level": level,
    }
    if payload is not None:
        values["payload"] = payload
    supabase.table("events").insert(values).execute()


def get_generating_video(supabase):
    result = (
        supabase.table("video_queue")
        .select(
            "*,channel:channels("
            "id,name,language,niche,voice_id,audience_type,custom_prompt)"
        )
        .eq("status", "generating")
        .order("created_at")
        .limit(10)
        .execute()
    )

    for video in result.data or []:
        if video.get("scene_plan") and video.get("channel"):
            return video
    return None


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split()).strip()


def detect_profile(channel: dict[str, Any], video: dict[str, Any]) -> str:
    text = " ".join([
        clean_text(channel.get("niche")),
        clean_text(channel.get("audience_type")),
        clean_text(channel.get("custom_prompt")),
        clean_text(video.get("topic")),
        clean_text(video.get("title")),
    ]).lower()

    if any(x in text for x in (
        "meditation", "meditasiya", "meditasyon", "relax",
        "sleep", "calm", "healing", "yoga", "mindfulness", "ambient"
    )):
        return "meditation"

    if any(x in text for x in (
        "kids", "kid", "child", "children", "cartoon", "uşaq", "çocuk", "cocuk"
    )):
        return "kids"

    return "default"


def normalize_scenes(video: dict[str, Any], profile: str) -> list[dict[str, Any]]:
    scenes = []

    for raw in video.get("scene_plan") or []:
        query = clean_text(raw.get("query"))
        narration = clean_text(raw.get("narration"))

        try:
            seconds = float(raw.get("seconds") or 15)
        except (TypeError, ValueError):
            seconds = 15.0

        if not query:
            continue
        if profile != "meditation" and not narration:
            continue

        scenes.append({
            "query": query[:140],
            "narration": narration,
            "seconds": max(3.0, min(seconds, 45.0)),
        })

    if not scenes:
        raise RuntimeError("Etibarlı scene_plan tapılmadı.")

    if MAX_SCENES > 0:
        scenes = scenes[:MAX_SCENES]

    if profile == "meditation":
        per_scene = MEDITATION_TEST_SECONDS / len(scenes)
        for scene in scenes:
            scene["seconds"] = per_scene
            scene["narration"] = ""

    return scenes


async def resolve_voice(
    requested_voice: str | None,
    language_code: str,
    gender: str,
) -> str:
    voices = await edge_tts.list_voices()
    names = {x.get("ShortName") for x in voices if x.get("ShortName")}

    if requested_voice and requested_voice in names:
        return requested_voice

    locales = {
        "az": ["az-AZ"],
        "tr": ["tr-TR"],
        "en": ["en-US", "en-GB"],
        "ru": ["ru-RU"],
    }.get(language_code, [language_code])

    for locale in locales:
        for voice in voices:
            if voice.get("Locale") == locale and voice.get("Gender") == gender:
                return voice["ShortName"]

    for locale in locales:
        for voice in voices:
            if voice.get("Locale") == locale:
                return voice["ShortName"]

    return "en-US-AriaNeural"


async def create_voice(
    text: str,
    voice_id: str,
    output_path: Path,
    profile: dict[str, str],
) -> None:
    last_error = None

    for attempt in range(1, 4):
        try:
            if output_path.exists():
                output_path.unlink()

            print(f"Edge TTS səs cəhdi: {attempt}/3", flush=True)

            await edge_tts.Communicate(
                text=text,
                voice=voice_id,
                rate=profile["rate"],
                volume=profile["volume"],
                pitch=profile["pitch"],
            ).save(str(output_path))

            if not output_path.exists() or output_path.stat().st_size < 1000:
                raise RuntimeError("Edge TTS natamam audio yaratdı.")
            return

        except Exception as error:
            last_error = error
            if attempt < 3:
                await asyncio.sleep(attempt * 5)

    raise RuntimeError(f"Edge TTS səs yarada bilmədi: {last_error}")


def create_procedural_ambient_audio(
    output_path: Path,
    duration: float,
) -> None:
    """
    Hazır mahnı istifadə etmir.

    FFmpeg ilə sıfırdan eşidilən, yumşaq ambient akkord yaradır:
    - xışıltı yoxdur;
    - üç harmonik ton var;
    - səs çox zəif qalmır;
    - başlanğıc və son yumşaqdır;
    - üçüncü tərəf musiqisi istifadə edilmir.
    """

    fade_out_start = max(
        0.0,
        duration - 6.0,
    )

    run(
        [
            "ffmpeg",
            "-y",

            # Aşağı ambient ton
            "-f",
            "lavfi",
            "-i",
            (
                "sine="
                "frequency=130.81:"
                f"duration={duration:.3f}:"
                "sample_rate=48000"
            ),

            # Orta ambient ton
            "-f",
            "lavfi",
            "-i",
            (
                "sine="
                "frequency=196.00:"
                f"duration={duration:.3f}:"
                "sample_rate=48000"
            ),

            # Yuxarı harmonik ton
            "-f",
            "lavfi",
            "-i",
            (
                "sine="
                "frequency=261.63:"
                f"duration={duration:.3f}:"
                "sample_rate=48000"
            ),

            "-filter_complex",
            (
                # Əsas ton daha aydın eşidilir
                "[0:a]"
                "volume=0.20,"
                "lowpass=f=500"
                "[tone1];"

                # Orta ton
                "[1:a]"
                "volume=0.12,"
                "lowpass=f=700"
                "[tone2];"

                # Yuxarı ton
                "[2:a]"
                "volume=0.07,"
                "lowpass=f=900"
                "[tone3];"

                # Tonlar birləşdirilir
                "[tone1][tone2][tone3]"
                "amix=inputs=3:"
                "duration=longest:"
                "normalize=0,"

                # Yumşaq əks-səda
                "aecho="
                "in_gain=0.75:"
                "out_gain=0.65:"
                "delays=700|1400:"
                "decays=0.22|0.10,"

                # Başlanğıc və son yumşaldılır
                "afade=t=in:st=0:d=5,"
                f"afade=t=out:st={fade_out_start:.3f}:d=6,"

                # Səsin həddən artıq yüksəlməsinin qarşısını alır
                "alimiter=limit=0.75,"

                # Son ümumi səviyyə
                "volume=1.7"
                "[ambient]"
            ),

            "-map",
            "[ambient]",
            "-t",
            f"{duration:.3f}",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            str(output_path),
        ]
    )

    if (
        not output_path.exists()
        or output_path.stat().st_size < 10_000
    ):
        raise RuntimeError(
            "Ambient audio yaradılmadı və ya fayl boşdur."
        )


def download_file(url: str, destination: Path) -> None:
    with requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        stream=True,
        timeout=DOWNLOAD_TIMEOUT,
    ) as response:
        response.raise_for_status()
        with destination.open("wb") as output:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    output.write(chunk)

    if not destination.exists() or destination.stat().st_size < 10_000:
        raise RuntimeError("Pexels videosu düzgün endirilmədi.")


def choose_file(video: dict[str, Any], portrait: bool):
    candidates = []

    for file in video.get("video_files", []):
        link = file.get("link")
        width = int(file.get("width") or 0)
        height = int(file.get("height") or 0)

        if not link or not width or not height:
            continue
        if portrait and height <= width:
            continue
        if not portrait and width < height:
            continue
        if width > 2560 or height > 2560:
            continue

        target_w = SHORT_WIDTH if portrait else LONG_WIDTH
        target_h = SHORT_HEIGHT if portrait else LONG_HEIGHT
        penalty = 5000 if width < 720 or height < 720 else 0

        candidates.append({
            "url": link,
            "width": width,
            "height": height,
            "score": abs(width - target_w) + abs(height - target_h) + penalty,
        })

    if not candidates:
        return None

    candidates.sort(key=lambda x: x["score"])
    return candidates[0]


def source_slug(url: str | None) -> str:
    if not url:
        return ""
    part = urlparse(url).path.strip("/").split("/")[-1]
    return re.sub(
        r"[^a-z0-9]+",
        "-",
        unquote(part).lower(),
    ).strip("-")


def meditation_score(query: str, url: str | None) -> float:
    slug_tokens = set(source_slug(url).split("-"))
    query_tokens = set(re.sub(r"[^a-z0-9]+", " ", query.lower()).split())

    score = len(query_tokens.intersection(slug_tokens)) * 2.0
    score += len(SAFE_MEDITATION_WORDS.intersection(slug_tokens)) * 0.7

    if REJECT_MEDITATION_WORDS.intersection(slug_tokens):
        score -= 20.0

    return score


def search_pexels(
    query: str,
    portrait: bool,
    avoided_ids: set[str],
    profile: str,
) -> dict[str, Any]:
    queries = (
        [
            query,
            f"{query} peaceful nature",
            "peaceful forest mist",
            "slow ocean waves",
            "calm mountain clouds",
        ]
        if profile == "meditation"
        else [query, f"{query} realistic", f"{query} cinematic"]
    )

    candidates = []

    for current_query in queries:
        response = requests.get(
            PEXELS_ENDPOINT,
            headers={
                "Authorization": PEXELS_API_KEY,
                "User-Agent": USER_AGENT,
            },
            params={
                "query": current_query,
                "orientation": "portrait" if portrait else "landscape",
                "size": "medium",
                "per_page": PEXELS_RESULTS_PER_QUERY,
            },
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code == 401:
            raise RuntimeError("PEXELS_API_KEY düzgün deyil.")
        response.raise_for_status()

        for video in response.json().get("videos", []):
            asset_id = str(video.get("id") or "")
            if not asset_id or asset_id in avoided_ids:
                continue

            selected = choose_file(video, portrait)
            if not selected:
                continue

            score = (
                meditation_score(query, video.get("url"))
                if profile == "meditation"
                else 1.0
            )

            candidates.append({
                "id": asset_id,
                "download_url": selected["url"],
                "source_url": video.get("url"),
                "query_used": current_query,
                "original_query": query,
                "width": selected["width"],
                "height": selected["height"],
                "relevance_score": round(score, 2),
            })

    if not candidates:
        raise RuntimeError(f"Pexels nəticəsi tapılmadı: {query}")

    candidates.sort(
        key=lambda x: (
            x["relevance_score"],
            x["width"] * x["height"],
        ),
        reverse=True,
    )
    return candidates[0]


def srt_time(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    h = ms // 3_600_000
    ms %= 3_600_000
    m = ms // 60_000
    ms %= 60_000
    s = ms // 1000
    ms %= 1000
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def subtitle_chunks(text: str, max_words: int) -> list[str]:
    text = clean_text(html.unescape(text))
    chunks = []

    for sentence in re.split(r"(?<=[.!?…])\s+", text):
        words = sentence.split()
        while words:
            chunks.append(" ".join(words[:max_words]))
            words = words[max_words:]

    return [x for x in chunks if x]


def append_subtitles(
    entries: list[dict[str, Any]],
    narration: str,
    start: float,
    duration: float,
    portrait: bool,
) -> None:
    max_words = (
        SHORT_SUBTITLE_MAX_WORDS
        if portrait
        else LONG_SUBTITLE_MAX_WORDS
    )
    chunks = subtitle_chunks(narration, max_words)
    if not chunks:
        return

    weights = [max(1, len(x)) for x in chunks]
    total = sum(weights)
    current = start

    for index, chunk in enumerate(chunks):
        end = (
            start + duration
            if index == len(chunks) - 1
            else min(
                start + duration,
                current + max(0.8, duration * weights[index] / total),
            )
        )
        entries.append({"start": current, "end": end, "text": chunk})
        current = end


def write_srt(entries: list[dict[str, Any]], path: Path) -> None:
    lines = []

    for index, entry in enumerate(entries, start=1):
        lines += [
            str(index),
            f"{srt_time(entry['start'])} --> {srt_time(entry['end'])}",
            entry["text"],
            "",
        ]

    path.write_text("\n".join(lines), encoding="utf-8")


def normalize_video(
    source: Path,
    output: Path,
    duration: float,
    portrait: bool,
) -> None:
    width, height = (
        (SHORT_WIDTH, SHORT_HEIGHT)
        if portrait
        else (LONG_WIDTH, LONG_HEIGHT)
    )

    filters = [
        f"scale={width}:{height}:force_original_aspect_ratio=increase",
        f"crop={width}:{height}",
        f"fps={VIDEO_FPS}",
        "setsar=1",
    ]

    if duration > SCENE_FADE_SECONDS * 2:
        filters += [
            f"fade=t=in:st=0:d={SCENE_FADE_SECONDS:.3f}",
            (
                "fade=t=out:"
                f"st={duration - SCENE_FADE_SECONDS:.3f}:"
                f"d={SCENE_FADE_SECONDS:.3f}"
            ),
        ]

    filters.append("format=yuv420p")

    run([
        "ffmpeg", "-y",
        "-stream_loop", "-1",
        "-i", str(source),
        "-t", f"{duration:.3f}",
        "-an",
        "-vf", ",".join(filters),
        "-c:v", "libx264",
        "-preset", NORMALIZE_PRESET,
        "-crf", str(NORMALIZE_CRF),
        "-pix_fmt", "yuv420p",
        str(output),
    ])


def concat_files(files: list[Path], list_path: Path) -> None:
    list_path.write_text(
        "\n".join(f"file '{x.resolve().as_posix()}'" for x in files),
        encoding="utf-8",
    )


def concat_videos(files: list[Path], output: Path) -> None:
    listing = output.parent / "video-concat.txt"
    concat_files(files, listing)
    run([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(listing),
        "-c", "copy",
        str(output),
    ])


def concat_audio(files: list[Path], output: Path) -> None:
    listing = output.parent / "audio-concat.txt"
    concat_files(files, listing)
    run([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(listing),
        "-c:a", "libmp3lame",
        "-b:a", "192k",
        str(output),
    ])


def escape_path(path: Path) -> str:
    return (
        path.resolve().as_posix()
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", r"\'")
        .replace(",", r"\,")
    )


def render_final(
    visual: Path,
    audio: Path,
    subtitles: Path,
    output: Path,
    portrait: bool,
    logo: Path | None,
    show_subtitles: bool,
) -> None:
    command = [
        "ffmpeg", "-y",
        "-i", str(visual),
        "-i", str(audio),
    ]

    filters = []
    current = "[0:v]"

    if logo and logo.exists():
        logo_width = SHORT_LOGO_WIDTH if portrait else LONG_LOGO_WIDTH
        command += ["-i", str(logo)]

        filters.append(
            "[2:v]"
            f"scale={logo_width}:-1,format=rgba,"
            f"colorkey=0x000000:{LOGO_BLACK_SIMILARITY}:{LOGO_BLACK_BLEND},"
            f"colorchannelmixer=aa={LOGO_OPACITY}[logo]"
        )
        filters.append(
            f"{current}[logo]"
            f"overlay=W-w-{LOGO_RIGHT_MARGIN}:{LOGO_TOP_MARGIN}[branded]"
        )
        current = "[branded]"

    if show_subtitles:
        font_size = (
            SHORT_SUBTITLE_FONT_SIZE
            if portrait
            else LONG_SUBTITLE_FONT_SIZE
        )
        margin = (
            SHORT_SUBTITLE_MARGIN_BOTTOM
            if portrait
            else LONG_SUBTITLE_MARGIN_BOTTOM
        )
        style = (
            "FontName=DejaVu Sans,"
            f"FontSize={font_size},"
            "PrimaryColour=&H00FFFFFF,"
            "OutlineColour=&H00101010,"
            "BackColour=&H00000000,"
            "BorderStyle=1,"
            f"Outline={SUBTITLE_OUTLINE},"
            f"Shadow={SUBTITLE_SHADOW},"
            "Alignment=2,"
            f"MarginL={SUBTITLE_SIDE_MARGIN},"
            f"MarginR={SUBTITLE_SIDE_MARGIN},"
            f"MarginV={margin}"
        )

        filters.append(
            f"{current}subtitles='{escape_path(subtitles)}':"
            f"force_style='{style}'[finalvideo]"
        )
        current = "[finalvideo]"

    if filters:
        command += [
            "-filter_complex", ";".join(filters),
            "-map", current,
            "-map", "1:a:0",
        ]
    else:
        command += ["-map", "0:v:0", "-map", "1:a:0"]

    command += [
        "-shortest",
        "-c:v", "libx264",
        "-preset", FINAL_PRESET,
        "-crf", str(FINAL_CRF),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        str(output),
    ]

    run(command)


def save_sources(sources: list[dict[str, Any]], path: Path) -> None:
    lines = ["AutoTube Studio — media mənbələri", ""]

    for index, source in enumerate(sources, start=1):
        lines += [
            f"Səhnə {index}",
            f"Orijinal sorğu: {source['original_query']}",
            f"İstifadə olunan sorğu: {source['query_used']}",
            f"Uyğunluq balı: {source['relevance_score']}",
            f"Pexels ID: {source['id']}",
            f"Mənbə: {source.get('source_url') or 'məlum deyil'}",
            "",
        ]

    path.write_text("\n".join(lines), encoding="utf-8")


async def create_media(supabase, video: dict[str, Any]) -> Path:
    channel = video["channel"]
    profile = detect_profile(channel, video)
    portrait = video["video_type"] == "short"
    scenes = normalize_scenes(video, profile)

    shutil.rmtree(OUTPUT_DIR, ignore_errors=True)

    work = OUTPUT_DIR / "work"
    voices = work / "voice"
    sources = work / "source"
    normalized = work / "normalized"

    for folder in [OUTPUT_DIR, work, voices, sources, normalized]:
        folder.mkdir(parents=True, exist_ok=True)

    update_video(
        supabase,
        video["id"],
        {"status": "rendering", "error_message": None},
    )

    add_event(
        supabase,
        video,
        "media_generation_started",
        (
            "Meditasiya görüntüləri və müəllif hüququ riski olmayan "
            "ambient səs hazırlanır."
            if profile == "meditation"
            else "Səs, uyğun görüntülər və video hazırlanır."
        ),
        payload={"content_profile": profile, "scene_count": len(scenes)},
    )

    print("Məzmun profili:", profile)
    print("Format:", "1080x1920" if portrait else "1920x1080")

    voice_id = None
    voice_profile = None

    if profile != "meditation":
        voice_profile = VOICE_PROFILES[profile]
        voice_id = await resolve_voice(
            channel.get("voice_id"),
            channel.get("language") or "en",
            voice_profile["gender"],
        )
        print("Səs:", voice_id)
    else:
        print("Danışıq yoxdur. Procedural ambient audio yaradılacaq.")

    audio_files = []
    video_files = []
    subtitle_entries = []
    media_sources = []
    avoided_ids = set()
    current_time = 0.0

    for index, scene in enumerate(scenes, start=1):
        print(f"Səhnə {index}/{len(scenes)}: {scene['query']}")

        if profile == "meditation":
            duration = float(scene["seconds"])
        else:
            scene_audio = voices / f"scene-{index:03}.mp3"
            await create_voice(
                scene["narration"],
                voice_id or "en-US-AriaNeural",
                scene_audio,
                voice_profile or VOICE_PROFILES["default"],
            )
            duration = max(1.5, media_duration(scene_audio))
            audio_files.append(scene_audio)

        asset = search_pexels(
            scene["query"],
            portrait,
            avoided_ids,
            profile,
        )
        avoided_ids.add(asset["id"])
        media_sources.append(asset)

        source_path = sources / f"scene-{index:03}.mp4"
        output_path = normalized / f"scene-{index:03}.mp4"

        download_file(asset["download_url"], source_path)
        normalize_video(
            source_path,
            output_path,
            duration,
            portrait,
        )

        if profile != "meditation":
            append_subtitles(
                subtitle_entries,
                scene["narration"],
                current_time,
                duration,
                portrait,
            )

        current_time += duration
        video_files.append(output_path)

    visual = work / "visual.mp4"
    audio = OUTPUT_DIR / "audio.mp3"
    subtitles = OUTPUT_DIR / "subtitles.srt"
    final = OUTPUT_DIR / "final.mp4"
    source_report = OUTPUT_DIR / "sources.txt"

    concat_videos(video_files, visual)

    if profile == "meditation":
        create_procedural_ambient_audio(audio, current_time)
        subtitles.write_text("", encoding="utf-8")
    else:
        concat_audio(audio_files, audio)
        write_srt(subtitle_entries, subtitles)

    save_sources(media_sources, source_report)

    logo = Path("img/logo.png")
    render_final(
        visual,
        audio,
        subtitles,
        final,
        portrait,
        logo if logo.exists() else None,
        show_subtitles=(profile != "meditation"),
    )

    duration = media_duration(final)

    update_video(
        supabase,
        video["id"],
        {
            "status": "ready",
            "duration_seconds": round(duration, 2),
            "error_message": None,
        },
    )

    add_event(
        supabase,
        video,
        "media_test_ready",
        (
            "Üfüqi meditasiya videosu və procedural ambient səs hazırdır."
            if profile == "meditation"
            else "Video renderi uğurla tamamlandı."
        ),
        payload={
            "content_profile": profile,
            "duration_seconds": round(duration, 2),
            "format": "1080x1920" if portrait else "1920x1080",
        },
    )

    print("MEDIA TEST UĞURLUDUR")
    print("Video:", final)
    print("Audio:", audio)
    return final


def main() -> None:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    video = get_generating_video(supabase)

    if not video:
        print("Media yaratmaq üçün uyğun generating video yoxdur.")
        return

    print("Video:", video["id"])
    print("Başlıq:", video.get("title"))
    print("Kanal:", video["channel"]["name"])

    try:
        asyncio.run(create_media(supabase, video))
    except Exception as error:
        update_video(
            supabase,
            video["id"],
            {"status": "failed", "error_message": str(error)[:1500]},
        )
        add_event(
            supabase,
            video,
            "media_generation_failed",
            f"Media hazırlanarkən xəta baş verdi: {str(error)[:420]}",
            level="error",
            payload={"error": str(error)[:1000]},
        )
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"MEDIA GENERATION FAILED: {error}", file=sys.stderr)
        raise
