import asyncio
import html
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import edge_tts
import requests
from supabase import create_client


SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ[
    "SUPABASE_SERVICE_ROLE_KEY"
]
PEXELS_API_KEY = os.environ["PEXELS_API_KEY"]

OUTPUT_DIR = Path(
    os.getenv(
        "MEDIA_OUTPUT_DIR",
        "media-test-output",
    )
)

MAX_SCENES = int(
    os.getenv(
        "MAX_MEDIA_TEST_SCENES",
        "8",
    )
)

PEXELS_ENDPOINT = (
    "https://api.pexels.com/videos/search"
)

USER_AGENT = (
    "AutoTube-Studio/1.0 "
    "(GitHub-Actions-Media-Worker)"
)


def run(
    command: list[str],
) -> str:
    print(
        "RUN:",
        " ".join(str(item) for item in command),
        flush=True,
    )

    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Komanda uğursuz oldu:\n"
            f"{result.stderr[-4000:]}"
        )

    return result.stdout


def media_duration(
    path: Path,
) -> float:
    result = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            (
                "default="
                "noprint_wrappers=1:"
                "nokey=1"
            ),
            str(path),
        ]
    ).strip()

    return float(result)


def update_video(
    supabase,
    video_id: str,
    values: dict[str, Any],
) -> None:
    response = (
        supabase.table("video_queue")
        .update(values)
        .eq("id", video_id)
        .execute()
    )

    if not response.data:
        raise RuntimeError(
            "video_queue sətri yenilənmədi."
        )


def add_event(
    supabase,
    video: dict[str, Any],
    event_type: str,
    message: str,
    level: str = "info",
    payload: dict[str, Any] | None = None,
) -> None:
    values: dict[str, Any] = {
        "owner_id": video["owner_id"],
        "video_id": video["id"],
        "channel_id": video["channel_id"],
        "type": event_type,
        "message": message,
        "level": level,
    }

    if payload is not None:
        values["payload"] = payload

    supabase.table("events").insert(
        values
    ).execute()


def get_generating_video(
    supabase,
) -> dict[str, Any] | None:
    response = (
        supabase.table("video_queue")
        .select(
            "*,"
            "channel:channels("
            "id,name,language,niche,voice_id"
            ")"
        )
        .eq("status", "generating")
        .order("created_at")
        .limit(10)
        .execute()
    )

    for video in response.data or []:
        if (
            video.get("script")
            and video.get("scene_plan")
            and video.get("channel")
        ):
            return video

    return None


def clean_text(
    value: Any,
) -> str:
    return " ".join(
        str(value or "")
        .replace("\r", " ")
        .replace("\n", " ")
        .split()
    ).strip()


def normalize_scenes(
    video: dict[str, Any],
) -> list[dict[str, Any]]:
    raw_scenes = video.get("scene_plan") or []
    scenes: list[dict[str, Any]] = []

    for raw_scene in raw_scenes:
        query = clean_text(
            raw_scene.get("query")
        )

        narration = clean_text(
            raw_scene.get("narration")
        )

        if not query or not narration:
            continue

        scenes.append(
            {
                "query": query[:120],
                "narration": narration,
            }
        )

    if not scenes:
        raise RuntimeError(
            "scene_plan daxilində query və narration "
            "olan səhnə tapılmadı. Əvvəl yeni Gemini "
            "Content Generation Test başladılmalıdır."
        )

    if MAX_SCENES > 0:
        scenes = scenes[:MAX_SCENES]

    return scenes


async def resolve_voice(
    requested_voice: str | None,
    language_code: str,
) -> str:
    voices = await edge_tts.list_voices()

    available_names = {
        item.get("ShortName")
        for item in voices
        if item.get("ShortName")
    }

    if (
        requested_voice
        and requested_voice in available_names
    ):
        return requested_voice

    locale_prefixes = {
        "az": ["az-AZ"],
        "tr": ["tr-TR"],
        "en": ["en-US", "en-GB"],
    }

    preferred_locales = locale_prefixes.get(
        language_code,
        [language_code],
    )

    for locale in preferred_locales:
        for voice in voices:
            if (
                voice.get("Locale") == locale
                and voice.get("Gender") == "Female"
            ):
                return voice["ShortName"]

        for voice in voices:
            if voice.get("Locale") == locale:
                return voice["ShortName"]

    return "en-US-AriaNeural"


async def create_voice(
    text: str,
    voice_id: str,
    output_path: Path,
) -> None:
    communication = edge_tts.Communicate(
        text=text,
        voice=voice_id,
        rate="-2%",
        volume="+0%",
        pitch="+0Hz",
    )

    await communication.save(
        str(output_path)
    )

    if (
        not output_path.exists()
        or output_path.stat().st_size < 1000
    ):
        raise RuntimeError(
            "Edge TTS etibarlı audio yaratmadı."
        )


def download_file(
    url: str,
    destination: Path,
) -> None:
    with requests.get(
        url,
        headers={
            "User-Agent": USER_AGENT,
        },
        stream=True,
        timeout=180,
    ) as response:
        response.raise_for_status()

        with destination.open("wb") as output:
            for chunk in response.iter_content(
                chunk_size=1024 * 1024
            ):
                if chunk:
                    output.write(chunk)

    if (
        not destination.exists()
        or destination.stat().st_size < 10_000
    ):
        raise RuntimeError(
            "Pexels video faylı düzgün endirilmədi."
        )


def choose_pexels_file(
    video: dict[str, Any],
    portrait: bool,
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []

    for file in video.get(
        "video_files",
        [],
    ):
        link = file.get("link")
        width = int(file.get("width") or 0)
        height = int(file.get("height") or 0)

        if not link or not width or not height:
            continue

        orientation_ok = (
            height > width
            if portrait
            else width >= height
        )

        if not orientation_ok:
            continue

        if width > 2560 or height > 2560:
            continue

        target_width = (
            1080
            if portrait
            else 1920
        )

        target_height = (
            1920
            if portrait
            else 1080
        )

        score = (
            abs(width - target_width)
            + abs(height - target_height)
        )

        candidates.append(
            {
                "url": link,
                "width": width,
                "height": height,
                "score": score,
            }
        )

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: item["score"]
    )

    return candidates[0]


def search_pexels_video(
    query: str,
    portrait: bool,
    avoided_ids: set[str],
    fallback_query: str,
) -> dict[str, Any]:
    queries = [
        query,
        fallback_query,
        "cinematic background",
        "nature landscape",
    ]

    for current_query in queries:
        response = requests.get(
            PEXELS_ENDPOINT,
            headers={
                "Authorization": PEXELS_API_KEY,
                "User-Agent": USER_AGENT,
            },
            params={
                "query": current_query,
                "orientation": (
                    "portrait"
                    if portrait
                    else "landscape"
                ),
                "size": "medium",
                "per_page": 25,
            },
            timeout=60,
        )

        if response.status_code == 401:
            raise RuntimeError(
                "PEXELS_API_KEY düzgün deyil "
                "və ya GitHub secret-ə əlavə edilməyib."
            )

        response.raise_for_status()

        for video in response.json().get(
            "videos",
            [],
        ):
            asset_id = str(
                video.get("id")
            )

            if (
                not asset_id
                or asset_id in avoided_ids
            ):
                continue

            selected_file = choose_pexels_file(
                video,
                portrait,
            )

            if not selected_file:
                continue

            return {
                "id": asset_id,
                "download_url": (
                    selected_file["url"]
                ),
                "source_url": video.get("url"),
                "query_used": current_query,
                "width": selected_file["width"],
                "height": selected_file["height"],
            }

    raise RuntimeError(
        f"Pexels videosu tapılmadı: {query}"
    )


def seconds_to_srt_time(
    seconds: float,
) -> str:
    milliseconds = max(
        0,
        round(seconds * 1000),
    )

    hours = milliseconds // 3_600_000
    milliseconds %= 3_600_000

    minutes = milliseconds // 60_000
    milliseconds %= 60_000

    secs = milliseconds // 1000
    milliseconds %= 1000

    return (
        f"{hours:02}:"
        f"{minutes:02}:"
        f"{secs:02},"
        f"{milliseconds:03}"
    )


def split_subtitle_text(
    text: str,
    max_words: int = 8,
) -> list[str]:
    text = clean_text(
        html.unescape(text)
    )

    sentence_parts = re.split(
        r"(?<=[.!?…])\s+",
        text,
    )

    chunks: list[str] = []

    for sentence in sentence_parts:
        words = sentence.split()

        while words:
            current = words[:max_words]
            words = words[max_words:]

            chunks.append(
                " ".join(current)
            )

    return [
        chunk
        for chunk in chunks
        if chunk
    ]


def append_scene_subtitles(
    entries: list[dict[str, Any]],
    narration: str,
    scene_start: float,
    scene_duration: float,
) -> None:
    chunks = split_subtitle_text(
        narration
    )

    if not chunks:
        return

    weights = [
        max(1, len(chunk))
        for chunk in chunks
    ]

    total_weight = sum(weights)
    current_time = scene_start

    for index, chunk in enumerate(chunks):
        if index == len(chunks) - 1:
            end_time = (
                scene_start
                + scene_duration
            )
        else:
            fraction = (
                weights[index]
                / total_weight
            )

            chunk_duration = max(
                0.8,
                scene_duration * fraction,
            )

            end_time = min(
                scene_start + scene_duration,
                current_time + chunk_duration,
            )

        entries.append(
            {
                "start": current_time,
                "end": end_time,
                "text": chunk,
            }
        )

        current_time = end_time


def write_srt(
    entries: list[dict[str, Any]],
    output_path: Path,
) -> None:
    lines: list[str] = []

    for index, entry in enumerate(
        entries,
        start=1,
    ):
        lines.extend(
            [
                str(index),
                (
                    f"{seconds_to_srt_time(entry['start'])}"
                    " --> "
                    f"{seconds_to_srt_time(entry['end'])}"
                ),
                entry["text"],
                "",
            ]
        )

    output_path.write_text(
        "\n".join(lines),
        encoding="utf-8",
    )


def normalize_scene_video(
    source_path: Path,
    output_path: Path,
    duration: float,
    portrait: bool,
) -> None:
    width, height = (
        (1080, 1920)
        if portrait
        else (1920, 1080)
    )

    filter_value = (
        f"scale={width}:{height}:"
        "force_original_aspect_ratio=increase,"
        f"crop={width}:{height},"
        "fps=30,"
        "setsar=1,"
        "format=yuv420p"
    )

    run(
        [
            "ffmpeg",
            "-y",
            "-stream_loop",
            "-1",
            "-i",
            str(source_path),
            "-t",
            f"{duration:.3f}",
            "-an",
            "-vf",
            filter_value,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            str(output_path),
        ]
    )


def write_concat_file(
    files: list[Path],
    output_path: Path,
) -> None:
    output_path.write_text(
        "\n".join(
            f"file '{path.resolve().as_posix()}'"
            for path in files
        ),
        encoding="utf-8",
    )


def concat_scene_videos(
    files: list[Path],
    output_path: Path,
) -> None:
    concat_file = (
        output_path.parent
        / "video-concat.txt"
    )

    write_concat_file(
        files,
        concat_file,
    )

    run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            str(output_path),
        ]
    )


def concat_scene_audio(
    files: list[Path],
    output_path: Path,
) -> None:
    concat_file = (
        output_path.parent
        / "audio-concat.txt"
    )

    write_concat_file(
        files,
        concat_file,
    )

    run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            str(output_path),
        ]
    )


def escape_subtitle_path(
    path: Path,
) -> str:
    value = path.resolve().as_posix()

    return (
        value
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", r"\'")
        .replace(",", r"\,")
    )


def render_final_video(
    visual_path: Path,
    audio_path: Path,
    subtitle_path: Path,
    output_path: Path,
    portrait: bool,
    logo_path: Path | None,
) -> None:
    subtitle_file = escape_subtitle_path(
        subtitle_path
    )

    font_size = (
        18
        if portrait
        else 22
    )

    margin_vertical = (
        160
        if portrait
        else 65
    )

    subtitle_style = (
        "FontName=DejaVu Sans,"
        f"FontSize={font_size},"
        "PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00101010,"
        "BackColour=&H70000000,"
        "BorderStyle=3,"
        "Outline=2,"
        "Shadow=0,"
        "Alignment=2,"
        f"MarginV={margin_vertical}"
    )

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(visual_path),
        "-i",
        str(audio_path),
    ]

    if (
        logo_path
        and logo_path.exists()
    ):
        command.extend(
            [
                "-i",
                str(logo_path),
                "-filter_complex",
                (
                    "[2:v]"
                    "scale=150:-1,"
                    "format=rgba,"
                    "colorchannelmixer=aa=0.88"
                    "[logo];"
                    "[0:v][logo]"
                    "overlay=W-w-35:35"
                    "[branded];"
                    "[branded]"
                    f"subtitles='{subtitle_file}':"
                    f"force_style='{subtitle_style}'"
                    "[finalvideo]"
                ),
                "-map",
                "[finalvideo]",
                "-map",
                "1:a:0",
            ]
        )
    else:
        command.extend(
            [
                "-vf",
                (
                    f"subtitles='{subtitle_file}':"
                    f"force_style='{subtitle_style}'"
                ),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
            ]
        )

    command.extend(
        [
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "21",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )

    run(command)


def save_media_sources(
    sources: list[dict[str, Any]],
    output_path: Path,
) -> None:
    lines = [
        "AutoTube Studio media test sources",
        "",
    ]

    for index, source in enumerate(
        sources,
        start=1,
    ):
        lines.extend(
            [
                f"Scene {index}",
                f"Query: {source['query_used']}",
                f"Pexels ID: {source['id']}",
                (
                    "Source: "
                    f"{source.get('source_url') or 'unknown'}"
                ),
                "",
            ]
        )

    output_path.write_text(
        "\n".join(lines),
        encoding="utf-8",
    )


async def create_media(
    supabase,
    video: dict[str, Any],
) -> Path:
    video_id = video["id"]
    channel = video["channel"]
    portrait = (
        video["video_type"] == "short"
    )

    scenes = normalize_scenes(video)

    shutil.rmtree(
        OUTPUT_DIR,
        ignore_errors=True,
    )

    work_dir = OUTPUT_DIR / "work"
    voice_dir = work_dir / "voice"
    source_dir = work_dir / "source"
    normalized_dir = work_dir / "normalized"

    for directory in [
        OUTPUT_DIR,
        work_dir,
        voice_dir,
        source_dir,
        normalized_dir,
    ]:
        directory.mkdir(
            parents=True,
            exist_ok=True,
        )

    update_video(
        supabase,
        video_id,
        {
            "status": "rendering",
            "error_message": None,
        },
    )

    add_event(
        supabase,
        video,
        "media_generation_started",
        (
            "Səs, Pexels görüntüləri və "
            "test videosu hazırlanmağa başladı."
        ),
        payload={
            "scene_count": len(scenes),
            "test_mode": True,
        },
    )

    voice_id = await resolve_voice(
        channel.get("voice_id"),
        channel.get("language") or "en",
    )

    print("İstifadə olunan səs:", voice_id)
    print("Test səhnə sayı:", len(scenes))
    print(
        "Video formatı:",
        "1080x1920"
        if portrait
        else "1920x1080",
    )

    audio_files: list[Path] = []
    normalized_files: list[Path] = []
    subtitle_entries: list[dict[str, Any]] = []
    media_sources: list[dict[str, Any]] = []
    avoided_ids: set[str] = set()

    current_time = 0.0

    fallback_query = clean_text(
        channel.get("niche")
    ) or "cinematic background"

    for index, scene in enumerate(
        scenes,
        start=1,
    ):
        print(
            f"\nSəhnə {index}/{len(scenes)}",
            flush=True,
        )
        print("Query:", scene["query"])

        audio_path = (
            voice_dir
            / f"scene-{index:03}.mp3"
        )

        await create_voice(
            scene["narration"],
            voice_id,
            audio_path,
        )

        scene_duration = max(
            1.5,
            media_duration(audio_path),
        )

        print(
            "Audio müddəti:",
            round(scene_duration, 2),
            "saniyə",
        )

        asset = search_pexels_video(
            scene["query"],
            portrait,
            avoided_ids,
            fallback_query,
        )

        avoided_ids.add(asset["id"])
        media_sources.append(asset)

        source_path = (
            source_dir
            / f"scene-{index:03}.mp4"
        )

        normalized_path = (
            normalized_dir
            / f"scene-{index:03}.mp4"
        )

        download_file(
            asset["download_url"],
            source_path,
        )

        normalize_scene_video(
            source_path,
            normalized_path,
            scene_duration,
            portrait,
        )

        append_scene_subtitles(
            subtitle_entries,
            scene["narration"],
            current_time,
            scene_duration,
        )

        current_time += scene_duration
        audio_files.append(audio_path)
        normalized_files.append(
            normalized_path
        )

    visual_path = work_dir / "visual.mp4"
    audio_path = OUTPUT_DIR / "audio.mp3"
    subtitle_path = OUTPUT_DIR / "subtitles.srt"
    final_path = OUTPUT_DIR / "final.mp4"
    sources_path = OUTPUT_DIR / "sources.txt"

    concat_scene_videos(
        normalized_files,
        visual_path,
    )

    concat_scene_audio(
        audio_files,
        audio_path,
    )

    write_srt(
        subtitle_entries,
        subtitle_path,
    )

    save_media_sources(
        media_sources,
        sources_path,
    )

    repository_logo = Path(
        "img/logo.png"
    )

    render_final_video(
        visual_path,
        audio_path,
        subtitle_path,
        final_path,
        portrait,
        (
            repository_logo
            if repository_logo.exists()
            else None
        ),
    )

    final_duration = media_duration(
        final_path
    )

    update_video(
        supabase,
        video_id,
        {
            "status": "ready",
            "duration_seconds": round(
                final_duration,
                2,
            ),
            "error_message": None,
        },
    )

    add_event(
        supabase,
        video,
        "media_test_ready",
        (
            "Səs, görüntü, altyazı və FFmpeg "
            "render testi uğurla tamamlandı."
        ),
        payload={
            "voice_id": voice_id,
            "scene_count": len(scenes),
            "duration_seconds": round(
                final_duration,
                2,
            ),
            "format": (
                "1080x1920"
                if portrait
                else "1920x1080"
            ),
            "test_mode": True,
        },
    )

    print("\nMEDIA TEST UĞURLUDUR")
    print("Video:", final_path)
    print("Audio:", audio_path)
    print("Altyazı:", subtitle_path)
    print(
        "Müddət:",
        round(final_duration, 2),
        "saniyə",
    )

    return final_path


def main() -> None:
    supabase = create_client(
        SUPABASE_URL,
        SUPABASE_KEY,
    )

    video = get_generating_video(
        supabase
    )

    if not video:
        print(
            "Media yaratmaq üçün generating statuslu, "
            "script və scene_plan məlumatı olan video yoxdur."
        )
        print(
            "Əvvəl Gemini Content Generation Test başladın."
        )
        return

    print("Video tapıldı:", video["id"])
    print("Başlıq:", video.get("title"))
    print("Kanal:", video["channel"]["name"])

    try:
        asyncio.run(
            create_media(
                supabase,
                video,
            )
        )
    except Exception as error:
        update_video(
            supabase,
            video["id"],
            {
                "status": "failed",
                "error_message": str(error)[:1500],
            },
        )

        add_event(
            supabase,
            video,
            "media_generation_failed",
            str(error)[:500],
            level="error",
        )

        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            f"MEDIA GENERATION FAILED: {error}",
            file=sys.stderr,
        )
        raise
