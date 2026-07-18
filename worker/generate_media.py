import asyncio
import html
import math
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


# ============================================================
# AUTOTUBE STUDIO — MEDIA GENERATION WORKER
# ============================================================
# Bu fayl:
# 1. Supabase-dən "generating" statuslu videonu götürür.
# 2. Hər səhnə üçün Edge TTS ilə səs yaradır.
# 3. Pexels-dən mövzuya uyğun şaquli və ya üfüqi video seçir.
# 4. Altyazını hazırlayır.
# 5. Logo, səs və görüntünü FFmpeg ilə yekun videoya çevirir.
# 6. Nəticəni GitHub Actions artifact qovluğuna yazır.
#
# Aşağıdakı "DİZAYN VƏ KEYFİYYƏT AYARLARI" hissəsində rəqəmləri
# dəyişərək logo, altyazı və video görünüşünü rahat tənzimləyə bilərsən.


# ============================================================
# MƏCBURİ MÜHİT DƏYİŞƏNLƏRİ
# ============================================================

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
PEXELS_API_KEY = os.environ["PEXELS_API_KEY"]


# ============================================================
# ÜMUMİ İŞ AYARLARI
# ============================================================

OUTPUT_DIR = Path(
    os.getenv("MEDIA_OUTPUT_DIR", "media-test-output")
)

# Test zamanı maksimum neçə səhnə işlənəcək.
# 0 yazsan bütün səhnələr işlənəcək.
MAX_SCENES = int(
    os.getenv("MAX_MEDIA_TEST_SCENES", "8")
)

PEXELS_ENDPOINT = "https://api.pexels.com/videos/search"

USER_AGENT = (
    "AutoTube-Studio/2.0 "
    "(GitHub-Actions-Professional-Media-Worker)"
)

REQUEST_TIMEOUT = 90
DOWNLOAD_TIMEOUT = 240

# Pexels hər sorğu üçün neçə nəticə qaytarsın.
# Çoxaltsan uyğun nəticə tapmaq ehtimalı yüksələr,
# amma API cavabı bir qədər ağırlaşar.
PEXELS_RESULTS_PER_QUERY = 40

# Eyni səhnə üçün neçə fərqli axtarış ifadəsi yoxlanılsın.
MAX_QUERY_VARIANTS = 6

# Uyğunluq balı bundan aşağı olan Pexels nəticəsi qəbul edilməyəcək.
# Daha ciddi seçim üçün 4 və ya 5 et.
# Çox yüksəltsən bəzi mövzularda nəticə tapılmaya bilər.
MIN_RELEVANCE_SCORE = 3.0


# ============================================================
# DİZAYN VƏ KEYFİYYƏT AYARLARI
# ============================================================

# ---------------- SHORTS ÖLÇÜSÜ ----------------
SHORT_WIDTH = 1080
SHORT_HEIGHT = 1920

# ---------------- UZUN VİDEO ÖLÇÜSÜ ----------------
LONG_WIDTH = 1920
LONG_HEIGHT = 1080

# ---------------- KADR TEZLİYİ ----------------
VIDEO_FPS = 30

# ---------------- LOGO AYARLARI ----------------
# Shorts-da logonun eni.
SHORT_LOGO_WIDTH = 92

# Uzun videoda logonun eni.
LONG_LOGO_WIDTH = 118

# Logo sağ kənardan neçə piksel içəridə olsun.
LOGO_RIGHT_MARGIN = 28

# Logo yuxarıdan neçə piksel aşağıda olsun.
LOGO_TOP_MARGIN = 28

# 0.0 tam şəffaf, 1.0 tam görünən.
LOGO_OPACITY = 0.82

# Logo şəklində qara fon varsa avtomatik şəffaflaşdırır.
# 0.10 daha sərt, 0.25 daha çox qara sahəni silir.
LOGO_BLACK_SIMILARITY = 0.18

# Qara fonun kənarlarının yumşaqlığı.
LOGO_BLACK_BLEND = 0.08

# ---------------- SHORTS ALTYAZI AYARLARI ----------------
# Kiçik rəqəm = kiçik yazı.
SHORT_SUBTITLE_FONT_SIZE = 11

# Shorts altyazısı aşağıdan neçə piksel yuxarıda olsun.
# Böyütsən altyazı yuxarı qalxar.
SHORT_SUBTITLE_MARGIN_BOTTOM = 300

# Bir Shorts altyazı hissəsində maksimum söz sayı.
SHORT_SUBTITLE_MAX_WORDS = 5

# ---------------- UZUN VİDEO ALTYAZI AYARLARI ----------------
LONG_SUBTITLE_FONT_SIZE = 18

# Uzun videoda altyazının aşağıdan məsafəsi.
# Böyütsən mətn daha yuxarı qalxar.
LONG_SUBTITLE_MARGIN_BOTTOM = 105

LONG_SUBTITLE_MAX_WORDS = 9

# ---------------- ALTYAZI GÖRÜNÜŞÜ ----------------
# Yazının kənar xətt qalınlığı.
SUBTITLE_OUTLINE = 1.8

# Yazının kölgəsi. 0 kölgəsizdir.
SUBTITLE_SHADOW = 1.2

# Mətnin soldan və sağdan təhlükəsiz məsafəsi.
SUBTITLE_SIDE_MARGIN = 55

# Ağ yazının rəngi ASS formatındadır.
SUBTITLE_PRIMARY_COLOUR = "&H00FFFFFF"

# Yazının kənar rəngi.
SUBTITLE_OUTLINE_COLOUR = "&H00101010"

# BorderStyle=1 olduqda böyük qara qutu yaranmır.
# Yalnız nazik kontur və yumşaq kölgə görünür.
SUBTITLE_BORDER_STYLE = 1

# ---------------- VİDEO KEYFİYYƏTİ ----------------
# CRF aşağı olduqca keyfiyyət və fayl ölçüsü artır.
# 18–23 normal aralıqdır.
NORMALIZE_CRF = 22
FINAL_CRF = 20

NORMALIZE_PRESET = "veryfast"
FINAL_PRESET = "medium"

# Səhnə başlanğıcında və sonunda çox qısa yumşalma.
# 0.0 etsən söndürülür.
SCENE_FADE_SECONDS = 0.18


# ============================================================
# MÖVZUYA GÖRƏ SƏS PROFİLLƏRİ
# ============================================================

# Edge TTS-də həqiqi uşaq səsi hər dildə olmaya bilər.
# Buna görə uşaq məzmununda uyğun səs + bir qədər yüksək pitch istifadə edilir.
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
    "meditation": {
        "rate": "-16%",
        "pitch": "-4Hz",
        "volume": "-4%",
        "gender": "Female",
    },
    "documentary": {
        "rate": "-5%",
        "pitch": "-2Hz",
        "volume": "+0%",
        "gender": "Male",
    },
    "motivation": {
        "rate": "+1%",
        "pitch": "+2Hz",
        "volume": "+2%",
        "gender": "Male",
    },
}


# ============================================================
# UYĞUNLUQ VƏ TƏHLÜKƏSİZLİK SÖZLƏRİ
# ============================================================

# Pexels nəticəsinin URL-ində bunlardan biri varsa və səhnə bunu istəmirsə,
# nəticə rədd edilir. Bu siyahını gələcəkdə genişləndirə bilərsən.
GENERIC_REJECT_WORDS = {
    "dance",
    "dancing",
    "party",
    "nightclub",
    "concert",
    "costume",
    "fashion",
    "makeup",
    "wedding",
    "business-meeting",
    "office-team",
}

# Bu sözlər axtarış keyfiyyətinə kömək etmir.
QUERY_STOP_WORDS = {
    "a",
    "an",
    "and",
    "the",
    "in",
    "on",
    "at",
    "to",
    "of",
    "for",
    "with",
    "through",
    "from",
    "into",
    "this",
    "that",
    "video",
    "cinematic",
    "scene",
    "view",
    "footage",
}


# ============================================================
# ƏSAS KÖMƏKÇİ FUNKSİYALAR
# ============================================================

def run(command: list[str]) -> str:
    """Terminal komandasını işlədir və xəta olarsa aydın log qaytarır."""

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
            f"{result.stderr[-5000:]}"
        )

    return result.stdout


def media_duration(path: Path) -> float:
    """Audio və ya videonun real müddətini saniyə ilə ölçür."""

    result = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
    ).strip()

    return float(result)


def update_video(
    supabase,
    video_id: str,
    values: dict[str, Any],
) -> None:
    """video_queue sətrini yeniləyir."""

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
    """Frontend fəaliyyət tarixçəsi üçün Azərbaycan dilində hadisə yazır."""

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

    supabase.table("events").insert(values).execute()


def get_generating_video(
    supabase,
) -> dict[str, Any] | None:
    """Ssenarisi hazır olan ilk generating videosunu tapır."""

    response = (
        supabase.table("video_queue")
        .select(
            "*,"
            "channel:channels("
            "id,name,language,niche,voice_id,"
            "audience_type,custom_prompt"
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


def clean_text(value: Any) -> str:
    """Mətndə artıq boşluqları və sətir qırılmalarını təmizləyir."""

    return " ".join(
        str(value or "")
        .replace("\r", " ")
        .replace("\n", " ")
        .split()
    ).strip()


def normalize_word(value: str) -> str:
    """Sözü Pexels uyğunluq müqayisəsi üçün sadələşdirir."""

    value = unquote(value).lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-")


def tokenize_query(query: str) -> set[str]:
    """Axtarış ifadəsini əsas İngilis açar sözlərə bölür."""

    tokens = {
        token
        for token in normalize_word(query).split("-")
        if len(token) >= 3 and token not in QUERY_STOP_WORDS
    }

    return tokens


def detect_content_profile(
    channel: dict[str, Any],
    video: dict[str, Any],
) -> str:
    """
    Kanal nişi, auditoriya və mövzuya əsasən səs/media profilini müəyyən edir.
    """

    combined = " ".join(
        [
            clean_text(channel.get("niche")),
            clean_text(channel.get("audience_type")),
            clean_text(channel.get("custom_prompt")),
            clean_text(video.get("topic")),
            clean_text(video.get("title")),
        ]
    ).lower()

    kids_words = {
        "kids",
        "kid",
        "child",
        "children",
        "toddler",
        "cartoon",
        "uşaq",
        "çocuk",
        "cocuk",
    }

    meditation_words = {
        "meditation",
        "meditasiya",
        "meditasyon",
        "sleep",
        "relax",
        "relaxation",
        "calm",
        "healing",
        "yoga",
        "mindfulness",
        "asmr",
    }

    documentary_words = {
        "documentary",
        "history",
        "science",
        "facts",
        "technology",
        "sənədli",
        "belgesel",
    }

    motivation_words = {
        "motivation",
        "motivasiya",
        "motivasyon",
        "success",
        "discipline",
        "inspiration",
    }

    if any(word in combined for word in kids_words):
        return "kids"

    if any(word in combined for word in meditation_words):
        return "meditation"

    if any(word in combined for word in documentary_words):
        return "documentary"

    if any(word in combined for word in motivation_words):
        return "motivation"

    return "default"


def normalize_scenes(
    video: dict[str, Any],
) -> list[dict[str, Any]]:
    """Gemini scene_plan məlumatını yoxlayıb təmiz formaya salır."""

    raw_scenes = video.get("scene_plan") or []
    scenes: list[dict[str, Any]] = []

    for raw_scene in raw_scenes:
        query = clean_text(raw_scene.get("query"))
        narration = clean_text(raw_scene.get("narration"))

        if not query or not narration:
            continue

        scenes.append(
            {
                "query": query[:140],
                "narration": narration,
            }
        )

    if not scenes:
        raise RuntimeError(
            "scene_plan daxilində query və narration olan səhnə tapılmadı. "
            "Əvvəl Gemini Content Generation Test başladılmalıdır."
        )

    if MAX_SCENES > 0:
        scenes = scenes[:MAX_SCENES]

    return scenes


# ============================================================
# EDGE TTS — SƏS SEÇİMİ VƏ YARADILMASI
# ============================================================

async def resolve_voice(
    requested_voice: str | None,
    language_code: str,
    preferred_gender: str,
) -> str:
    """Kanal dili və profilə uyğun Edge TTS səsi seçir."""

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
        "ru": ["ru-RU"],
        "de": ["de-DE"],
        "es": ["es-ES", "es-MX"],
        "fr": ["fr-FR"],
    }

    preferred_locales = locale_prefixes.get(
        language_code,
        [language_code],
    )

    # Əvvəl istənilən gender üzrə uyğun səsi axtarır.
    for locale in preferred_locales:
        for voice in voices:
            if (
                voice.get("Locale") == locale
                and voice.get("Gender") == preferred_gender
            ):
                return voice["ShortName"]

    # Gender uyğun gəlməsə həmin dilin ilk səsini seçir.
    for locale in preferred_locales:
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
    """Edge TTS ilə səs yaradır və müvəqqəti 403 xətalarında yenidən sınayır."""

    last_error: Exception | None = None

    for attempt in range(1, 4):
        try:
            if output_path.exists():
                output_path.unlink()

            print(
                f"Edge TTS səs cəhdi: {attempt}/3",
                flush=True,
            )

            communication = edge_tts.Communicate(
                text=text,
                voice=voice_id,
                rate=profile["rate"],
                volume=profile["volume"],
                pitch=profile["pitch"],
            )

            await communication.save(str(output_path))

            if (
                not output_path.exists()
                or output_path.stat().st_size < 1000
            ):
                raise RuntimeError(
                    "Edge TTS boş və ya natamam audio yaratdı."
                )

            return

        except Exception as error:
            last_error = error

            print(
                f"Edge TTS cəhdi uğursuz oldu: {error}",
                flush=True,
            )

            if attempt < 3:
                await asyncio.sleep(attempt * 5)

    raise RuntimeError(
        "Edge TTS 3 cəhddən sonra səs yarada bilmədi. "
        f"Son xəta: {last_error}"
    )


# ============================================================
# PEXELS — PEŞƏKAR VİDEO AXTARIŞI VƏ UYĞUNLUQ YOXLAMASI
# ============================================================

def download_file(
    url: str,
    destination: Path,
) -> None:
    """Pexels videosunu hissə-hissə təhlükəsiz endirir."""

    with requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        stream=True,
        timeout=DOWNLOAD_TIMEOUT,
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
    """Pexels videosunun ölçüyə ən uyğun MP4 variantını seçir."""

    candidates: list[dict[str, Any]] = []

    for file in video.get("video_files", []):
        link = file.get("link")
        width = int(file.get("width") or 0)
        height = int(file.get("height") or 0)
        file_type = str(file.get("file_type") or "")

        if not link or not width or not height:
            continue

        if file_type and "mp4" not in file_type.lower():
            continue

        orientation_ok = (
            height > width
            if portrait
            else width >= height
        )

        if not orientation_ok:
            continue

        # GitHub Actions-da lazımsız 4K faylların endirilməsinin qarşısını alır.
        if width > 2560 or height > 2560:
            continue

        target_width = (
            SHORT_WIDTH if portrait else LONG_WIDTH
        )
        target_height = (
            SHORT_HEIGHT if portrait else LONG_HEIGHT
        )

        resolution_distance = (
            abs(width - target_width)
            + abs(height - target_height)
        )

        # Həddən artıq kiçik fayllara cərimə verir.
        low_resolution_penalty = (
            5000
            if width < 720 or height < 720
            else 0
        )

        candidates.append(
            {
                "url": link,
                "width": width,
                "height": height,
                "score": (
                    resolution_distance
                    + low_resolution_penalty
                ),
            }
        )

    if not candidates:
        return None

    candidates.sort(key=lambda item: item["score"])
    return candidates[0]


def build_query_variants(
    query: str,
    niche: str,
    profile_name: str,
) -> list[str]:
    """Bir səhnə üçün daha dəqiq Pexels axtarış variantları yaradır."""

    clean_query = clean_text(query)
    clean_niche = clean_text(niche)

    variants = [
        clean_query,
        f"{clean_query} cinematic",
        f"{clean_query} nature" if "forest" in clean_query.lower() else "",
        f"{clean_query} realistic",
    ]

    if clean_niche:
        variants.append(
            f"{clean_query} {clean_niche}"
        )

    if profile_name == "kids":
        variants.append(
            f"{clean_query} child friendly"
        )

    elif profile_name == "meditation":
        variants.extend(
            [
                f"{clean_query} peaceful",
                f"{clean_query} slow relaxing nature",
            ]
        )

    elif profile_name == "documentary":
        variants.append(
            f"{clean_query} documentary"
        )

    elif profile_name == "motivation":
        variants.append(
            f"{clean_query} inspirational"
        )

    unique: list[str] = []

    for value in variants:
        value = clean_text(value)

        if value and value.lower() not in {
            item.lower() for item in unique
        }:
            unique.append(value)

    return unique[:MAX_QUERY_VARIANTS]


def source_slug(source_url: str | None) -> str:
    """Pexels source URL-dən videonun təsviri olan slug hissəsini çıxarır."""

    if not source_url:
        return ""

    path = urlparse(source_url).path.strip("/")
    parts = path.split("/")

    if not parts:
        return ""

    return normalize_word(parts[-1])


def relevance_score(
    query: str,
    source_url: str | None,
    profile_name: str,
) -> tuple[float, list[str]]:
    """
    Sorğu sözləri ilə Pexels URL təsvirini müqayisə edir.
    Uyğunsuz rəqs, kostyum və s. nəticələri mümkün qədər rədd edir.
    """

    query_tokens = tokenize_query(query)
    slug = source_slug(source_url)
    slug_tokens = set(slug.split("-"))

    matched = sorted(
        query_tokens.intersection(slug_tokens)
    )

    score = float(len(matched) * 2)

    # Sorğunun ən əsas sözü URL-də varsa əlavə bal.
    important_tokens = list(query_tokens)

    if important_tokens:
        first_token = important_tokens[0]

        if first_token in slug_tokens:
            score += 1.0

    # Sorğudakı iki söz ardıcıl slug daxilində görünürsə əlavə bal.
    normalized_query = normalize_word(query)
    query_parts = [
        part
        for part in normalized_query.split("-")
        if part not in QUERY_STOP_WORDS
    ]

    for index in range(len(query_parts) - 1):
        phrase = (
            f"{query_parts[index]}-"
            f"{query_parts[index + 1]}"
        )

        if phrase in slug:
            score += 1.5

    # Rədd ediləcək söz sorğunun özündə yoxdursa nəticəni cəzalandırır.
    for reject_word in GENERIC_REJECT_WORDS:
        if (
            reject_word in slug
            and reject_word not in normalized_query
        ):
            score -= 6.0

    # Meditasiya məzmununda uyğun sakit sözlərə üstünlük.
    if profile_name == "meditation":
        calm_words = {
            "calm",
            "peaceful",
            "relaxing",
            "nature",
            "ocean",
            "forest",
            "water",
            "sunset",
            "clouds",
            "rain",
        }

        score += len(
            calm_words.intersection(slug_tokens)
        ) * 0.75

    # Uşaq məzmununda gecə klubu, alkoqol və qorxulu nəticələr qəbul edilmir.
    if profile_name == "kids":
        unsafe_kids_words = {
            "alcohol",
            "beer",
            "wine",
            "weapon",
            "gun",
            "blood",
            "nightclub",
            "horror",
        }

        if unsafe_kids_words.intersection(slug_tokens):
            score -= 20.0

    return score, matched


def search_pexels_video(
    query: str,
    portrait: bool,
    avoided_ids: set[str],
    fallback_query: str,
    profile_name: str,
) -> dict[str, Any]:
    """
    Bir neçə Pexels sorğusunu yoxlayır, bütün nəticələri ballandırır
    və ən uyğun videonu seçir.
    """

    queries = build_query_variants(
        query,
        fallback_query,
        profile_name,
    )

    accepted_candidates: list[dict[str, Any]] = []

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
                "per_page": PEXELS_RESULTS_PER_QUERY,
            },
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code == 401:
            raise RuntimeError(
                "PEXELS_API_KEY düzgün deyil və ya "
                "GitHub secret-ə əlavə edilməyib."
            )

        response.raise_for_status()

        for video in response.json().get("videos", []):
            asset_id = str(video.get("id") or "")

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

            source_url = video.get("url")

            score, matched_words = relevance_score(
                query,
                source_url,
                profile_name,
            )

            accepted_candidates.append(
                {
                    "id": asset_id,
                    "download_url": selected_file["url"],
                    "source_url": source_url,
                    "query_used": current_query,
                    "original_query": query,
                    "width": selected_file["width"],
                    "height": selected_file["height"],
                    "relevance_score": round(score, 2),
                    "matched_words": matched_words,
                }
            )

    if not accepted_candidates:
        raise RuntimeError(
            f"Pexels nəticəsi tapılmadı: {query}"
        )

    accepted_candidates.sort(
        key=lambda item: (
            item["relevance_score"],
            item["width"] * item["height"],
        ),
        reverse=True,
    )

    best = accepted_candidates[0]

    if best["relevance_score"] < MIN_RELEVANCE_SCORE:
        raise RuntimeError(
            "Pexels-də səhnəyə kifayət qədər uyğun video tapılmadı. "
            f"Sorğu: {query}. "
            f"Ən yüksək uyğunluq balı: {best['relevance_score']}. "
            "Gemini səhnə query-sini daha konkret yaratmalıdır."
        )

    print(
        "Seçilən Pexels videosu:",
        best["id"],
        "| uyğunluq balı:",
        best["relevance_score"],
        "| uyğun sözlər:",
        best["matched_words"],
        flush=True,
    )

    return best


# ============================================================
# ALTYAZI HAZIRLANMASI
# ============================================================

def seconds_to_srt_time(seconds: float) -> str:
    """Saniyəni SRT zaman formatına çevirir."""

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
    max_words: int,
) -> list[str]:
    """Uzun mətni ekranda rahat oxunan kiçik hissələrə bölür."""

    text = clean_text(html.unescape(text))

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

            chunks.append(" ".join(current))

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
    portrait: bool,
) -> None:
    """Səhnə narrasiyasını səs müddətinə uyğun altyazılara bölür."""

    max_words = (
        SHORT_SUBTITLE_MAX_WORDS
        if portrait
        else LONG_SUBTITLE_MAX_WORDS
    )

    chunks = split_subtitle_text(
        narration,
        max_words,
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
                scene_start + scene_duration
            )
        else:
            fraction = (
                weights[index] / total_weight
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
    """Altyazı siyahısını subtitles.srt faylına yazır."""

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


# ============================================================
# FFMPEG — VİDEO NORMALİZASİYASI VƏ RENDER
# ============================================================

def normalize_scene_video(
    source_path: Path,
    output_path: Path,
    duration: float,
    portrait: bool,
) -> None:
    """Pexels videosunu standart ölçü, FPS və müddətə salır."""

    width, height = (
        (SHORT_WIDTH, SHORT_HEIGHT)
        if portrait
        else (LONG_WIDTH, LONG_HEIGHT)
    )

    filters = [
        (
            f"scale={width}:{height}:"
            "force_original_aspect_ratio=increase"
        ),
        f"crop={width}:{height}",
        f"fps={VIDEO_FPS}",
        "setsar=1",
    ]

    if (
        SCENE_FADE_SECONDS > 0
        and duration > SCENE_FADE_SECONDS * 2
    ):
        fade_out_start = max(
            0.0,
            duration - SCENE_FADE_SECONDS,
        )

        filters.extend(
            [
                (
                    "fade=t=in:"
                    f"st=0:d={SCENE_FADE_SECONDS:.3f}"
                ),
                (
                    "fade=t=out:"
                    f"st={fade_out_start:.3f}:"
                    f"d={SCENE_FADE_SECONDS:.3f}"
                ),
            ]
        )

    filters.append("format=yuv420p")

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
            ",".join(filters),
            "-c:v",
            "libx264",
            "-preset",
            NORMALIZE_PRESET,
            "-crf",
            str(NORMALIZE_CRF),
            "-pix_fmt",
            "yuv420p",
            str(output_path),
        ]
    )


def write_concat_file(
    files: list[Path],
    output_path: Path,
) -> None:
    """FFmpeg concat üçün fayl siyahısı yaradır."""

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
    """Bütün video səhnələrini ardıcıl birləşdirir."""

    concat_file = (
        output_path.parent
        / "video-concat.txt"
    )

    write_concat_file(files, concat_file)

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
    """Bütün səhnə səslərini bir MP3 faylında birləşdirir."""

    concat_file = (
        output_path.parent
        / "audio-concat.txt"
    )

    write_concat_file(files, concat_file)

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


def escape_subtitle_path(path: Path) -> str:
    """Altyazı yolunu FFmpeg filter sintaksisi üçün təhlükəsiz edir."""

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
    """
    Altyazı, şəffaf logo, görüntü və səsi birləşdirib final.mp4 yaradır.
    """

    subtitle_file = escape_subtitle_path(
        subtitle_path
    )

    font_size = (
        SHORT_SUBTITLE_FONT_SIZE
        if portrait
        else LONG_SUBTITLE_FONT_SIZE
    )

    margin_vertical = (
        SHORT_SUBTITLE_MARGIN_BOTTOM
        if portrait
        else LONG_SUBTITLE_MARGIN_BOTTOM
    )

    subtitle_style = (
        "FontName=DejaVu Sans,"
        f"FontSize={font_size},"
        f"PrimaryColour={SUBTITLE_PRIMARY_COLOUR},"
        f"OutlineColour={SUBTITLE_OUTLINE_COLOUR},"
        "BackColour=&H00000000,"
        f"BorderStyle={SUBTITLE_BORDER_STYLE},"
        f"Outline={SUBTITLE_OUTLINE},"
        f"Shadow={SUBTITLE_SHADOW},"
        "Alignment=2,"
        f"MarginL={SUBTITLE_SIDE_MARGIN},"
        f"MarginR={SUBTITLE_SIDE_MARGIN},"
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

    if logo_path and logo_path.exists():
        logo_width = (
            SHORT_LOGO_WIDTH
            if portrait
            else LONG_LOGO_WIDTH
        )

        command.extend(
            [
                "-i",
                str(logo_path),
                "-filter_complex",
                (
                    # Qara fonu şəffaflaşdırır.
                    "[2:v]"
                    f"scale={logo_width}:-1,"
                    "format=rgba,"
                    f"colorkey=0x000000:"
                    f"{LOGO_BLACK_SIMILARITY}:"
                    f"{LOGO_BLACK_BLEND},"
                    f"colorchannelmixer=aa={LOGO_OPACITY}"
                    "[logo];"

                    # Logonu sağ yuxarı yerləşdirir.
                    "[0:v][logo]"
                    f"overlay=W-w-{LOGO_RIGHT_MARGIN}:"
                    f"{LOGO_TOP_MARGIN}"
                    "[branded];"

                    # Altyazını əlavə edir.
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
            FINAL_PRESET,
            "-crf",
            str(FINAL_CRF),
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


# ============================================================
# MƏNBƏ HESABATI
# ============================================================

def save_media_sources(
    sources: list[dict[str, Any]],
    output_path: Path,
) -> None:
    """İstifadə olunan Pexels videolarının şəffaf hesabatını yaradır."""

    lines = [
        "AutoTube Studio — media mənbələri",
        "",
    ]

    for index, source in enumerate(
        sources,
        start=1,
    ):
        lines.extend(
            [
                f"Səhnə {index}",
                (
                    "Orijinal sorğu: "
                    f"{source['original_query']}"
                ),
                (
                    "İstifadə olunan sorğu: "
                    f"{source['query_used']}"
                ),
                (
                    "Uyğunluq balı: "
                    f"{source['relevance_score']}"
                ),
                (
                    "Uyğun sözlər: "
                    f"{', '.join(source['matched_words']) or 'yoxdur'}"
                ),
                f"Pexels ID: {source['id']}",
                (
                    "Mənbə: "
                    f"{source.get('source_url') or 'məlum deyil'}"
                ),
                "",
            ]
        )

    output_path.write_text(
        "\n".join(lines),
        encoding="utf-8",
    )


# ============================================================
# ƏSAS MEDIA YARATMA PROSESİ
# ============================================================

async def create_media(
    supabase,
    video: dict[str, Any],
) -> Path:
    """Videonun bütün səs, görüntü, altyazı və render prosesini idarə edir."""

    video_id = video["id"]
    channel = video["channel"]

    portrait = (
        video["video_type"] == "short"
    )

    scenes = normalize_scenes(video)

    profile_name = detect_content_profile(
        channel,
        video,
    )

    voice_profile = VOICE_PROFILES[
        profile_name
    ]

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
            "Səs, uyğun görüntülər, altyazı və "
            "peşəkar video renderi hazırlanmağa başladı."
        ),
        payload={
            "scene_count": len(scenes),
            "content_profile": profile_name,
            "test_mode": True,
        },
    )

    voice_id = await resolve_voice(
        channel.get("voice_id"),
        channel.get("language") or "en",
        voice_profile["gender"],
    )

    print("Məzmun profili:", profile_name)
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

    fallback_query = (
        clean_text(channel.get("niche"))
        or "cinematic nature"
    )

    for index, scene in enumerate(
        scenes,
        start=1,
    ):
        print(
            f"\nSəhnə {index}/{len(scenes)}",
            flush=True,
        )
        print(
            "Səhnə sorğusu:",
            scene["query"],
            flush=True,
        )

        scene_audio_path = (
            voice_dir
            / f"scene-{index:03}.mp3"
        )

        await create_voice(
            scene["narration"],
            voice_id,
            scene_audio_path,
            voice_profile,
        )

        scene_duration = max(
            1.5,
            media_duration(scene_audio_path),
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
            profile_name,
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
            portrait,
        )

        current_time += scene_duration
        audio_files.append(scene_audio_path)
        normalized_files.append(
            normalized_path
        )

    visual_path = work_dir / "visual.mp4"
    final_audio_path = OUTPUT_DIR / "audio.mp3"
    subtitle_path = OUTPUT_DIR / "subtitles.srt"
    final_path = OUTPUT_DIR / "final.mp4"
    sources_path = OUTPUT_DIR / "sources.txt"

    concat_scene_videos(
        normalized_files,
        visual_path,
    )

    concat_scene_audio(
        audio_files,
        final_audio_path,
    )

    write_srt(
        subtitle_entries,
        subtitle_path,
    )

    save_media_sources(
        media_sources,
        sources_path,
    )

    repository_logo = Path("img/logo.png")

    render_final_video(
        visual_path,
        final_audio_path,
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
            "Səs, uyğun görüntülər, altyazı və "
            "peşəkar FFmpeg renderi uğurla tamamlandı."
        ),
        payload={
            "voice_id": voice_id,
            "content_profile": profile_name,
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
    print("Audio:", final_audio_path)
    print("Altyazı:", subtitle_path)
    print("Mənbələr:", sources_path)
    print(
        "Müddət:",
        round(final_duration, 2),
        "saniyə",
    )

    return final_path


# ============================================================
# PROQRAMIN BAŞLANĞICI
# ============================================================

def main() -> None:
    """Supabase bağlantısını qurur və media prosesini başladır."""

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
            (
                "Media hazırlanarkən xəta baş verdi: "
                f"{str(error)[:420]}"
            ),
            level="error",
            payload={
                "error": str(error)[:1000],
            },
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
