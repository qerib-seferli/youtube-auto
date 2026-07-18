import json
import os
import sys
from typing import Any
from urllib.parse import quote

import requests
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split()).strip()


def update_video(supabase, video_id: str, values: dict[str, Any]) -> None:
    result = supabase.table("video_queue").update(values).eq("id", video_id).execute()
    if not result.data:
        raise RuntimeError("Video sətri yenilənmədi.")


def get_pending_video(supabase):
    result = (
        supabase.table("video_queue")
        .select(
            "*,channel:channels("
            "id,name,language,niche,custom_prompt,audience_type,"
            "long_min_minutes,long_max_minutes,short_min_seconds,short_max_seconds)"
        )
        .eq("status", "pending")
        .order("created_at")
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


def detect_profile(video: dict[str, Any]) -> str:
    c = video["channel"]
    text = " ".join([
        clean_text(c.get("niche")),
        clean_text(c.get("audience_type")),
        clean_text(c.get("custom_prompt")),
        clean_text(video.get("topic")),
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

    return "voice"


def build_prompt(video: dict[str, Any], profile: str) -> str:
    c = video["channel"]
    language = {"az": "Azerbaijani", "tr": "Turkish", "en": "English"}.get(
        c["language"], c["language"]
    )

    if profile == "meditation":
        mode = """
MEDITATION MODE:
- No spoken narration.
- script must be an empty string.
- Return exactly 8 scenes.
- Every scene narration must be an empty string.
- Every scene seconds must be 15.
- Use only safe, peaceful, realistic nature footage.
- Prefer forest mist, ocean waves, rain, clouds, sunrise, rivers, lakes and mountains.
- Do not use people, faces, dancing, parties, brands, text or logos.
"""
    else:
        mode = """
VOICE MODE:
- Every scene must contain natural narration.
- script must equal all scene narrations combined.
- Use concrete, realistic visual queries that directly match narration.
- Children's content must remain safe, positive and age-appropriate.
"""

    return f"""
You are a professional YouTube content strategist and visual director.

Channel: {c['name']}
Language: {language}
Niche: {c['niche']}
Audience: {c['audience_type']}
Video type: {video['video_type']}
Requested topic: {video.get('topic') or 'Choose an original evergreen topic'}
Custom instruction: {c.get('custom_prompt') or 'None'}
Production profile: {profile}

GENERAL RULES:
- Create original content only.
- Do not mention AI, Gemini, OpenAI, ChatGPT or automation.
- Do not copy existing titles, scripts or descriptions.
- Use honest titles and YouTube-friendly SEO descriptions.
- Thumbnail text: maximum 5 words.
- Tags and hashtags must not contain #.
- Every visual query must be a short English Pexels search phrase.
- Do not use brands, celebrities, screenshots, trademarks or watermarks.

{mode}

Return only valid JSON with:
topic, title, description, tags, hashtags, thumbnail_text, script, scenes

Each scenes item:
query, narration, seconds
""".strip()


def extract_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates") or []
    if not candidates:
        raise RuntimeError(
            "Gemini cavab qaytarmadı: "
            + json.dumps(payload.get("promptFeedback", {}), ensure_ascii=False)
        )

    text = "".join(
        part.get("text", "")
        for part in candidates[0].get("content", {}).get("parts", [])
        if part.get("text")
    ).strip()

    if not text:
        raise RuntimeError("Gemini boş cavab qaytardı.")
    return text


def generate(video: dict[str, Any], profile: str) -> dict[str, Any]:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "topic", "title", "description", "tags", "hashtags",
            "thumbnail_text", "script", "scenes"
        ],
        "properties": {
            "topic": {"type": "string"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "hashtags": {"type": "array", "items": {"type": "string"}},
            "thumbnail_text": {"type": "string"},
            "script": {"type": "string"},
            "scenes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["query", "narration", "seconds"],
                    "properties": {
                        "query": {"type": "string"},
                        "narration": {"type": "string"},
                        "seconds": {"type": "number"},
                    },
                },
            },
        },
    }

    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{quote(GEMINI_MODEL, safe='')}:generateContent"
    )

    response = requests.post(
        endpoint,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": GEMINI_API_KEY,
        },
        json={
            "contents": [{"role": "user", "parts": [{"text": build_prompt(video, profile)}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": schema,
                "temperature": 0.65,
                "maxOutputTokens": 16000,
            },
        },
        timeout=240,
    )

    if not response.ok:
        raise RuntimeError(f"Gemini xətası {response.status_code}: {response.text[:1500]}")

    return json.loads(extract_text(response.json()))


def normalize(content: dict[str, Any], profile: str) -> dict[str, Any]:
    scenes = []

    for raw in content.get("scenes") or []:
        query = clean_text(raw.get("query"))
        narration = clean_text(raw.get("narration"))

        try:
            seconds = float(raw.get("seconds") or 15)
        except (TypeError, ValueError):
            seconds = 15.0

        if profile == "meditation":
            narration = ""
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
        raise RuntimeError("Gemini etibarlı səhnə planı yaratmadı.")

    if profile == "meditation":
        scenes = scenes[:8]
        script = ""
    else:
        script = "\n\n".join(scene["narration"] for scene in scenes)

    return {
        "topic": clean_text(content.get("topic")),
        "title": clean_text(content.get("title")),
        "description": str(content.get("description") or "").strip(),
        "tags": [clean_text(x).lstrip("#") for x in content.get("tags", []) if clean_text(x)][:30],
        "hashtags": [clean_text(x).lstrip("#") for x in content.get("hashtags", []) if clean_text(x)][:15],
        "thumbnail_text": clean_text(content.get("thumbnail_text"))[:80],
        "script": script,
        "scenes": scenes,
    }


def main() -> None:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    video = get_pending_video(supabase)

    if not video:
        print("Növbədə pending statuslu video yoxdur.")
        return

    profile = detect_profile(video)
    video_id = video["id"]

    print("Video:", video_id)
    print("Kanal:", video["channel"]["name"])
    print("Məzmun profili:", profile)

    update_video(supabase, video_id, {"status": "generating", "error_message": None})

    try:
        content = normalize(generate(video, profile), profile)

        if not content["title"]:
            raise RuntimeError("Gemini başlıq yaratmadı.")
        if profile != "meditation" and not content["script"]:
            raise RuntimeError("Gemini ssenari yaratmadı.")

        update_video(
            supabase,
            video_id,
            {
                "topic": content["topic"],
                "title": content["title"][:100],
                "description": content["description"],
                "tags": content["tags"],
                "hashtags": content["hashtags"],
                "thumbnail_text": content["thumbnail_text"],
                "script": content["script"],
                "scene_plan": content["scenes"],
                "status": "generating",
                "error_message": None,
            },
        )

        supabase.table("events").insert({
            "owner_id": video["owner_id"],
            "video_id": video_id,
            "channel_id": video["channel"]["id"],
            "level": "info",
            "type": "content_generated",
            "message": (
                "Meditasiya metadata-sı və sakit səhnə planı hazırlandı."
                if profile == "meditation"
                else "Gemini ilə ssenari və səhnə planı hazırlandı."
            ),
            "payload": {
                "provider": "gemini",
                "model": GEMINI_MODEL,
                "profile": profile,
                "scene_count": len(content["scenes"]),
            },
        }).execute()

        print("Məzmun uğurla hazırlandı.")
        print("Başlıq:", content["title"])
        print("Səhnə sayı:", len(content["scenes"]))

    except Exception as error:
        update_video(
            supabase,
            video_id,
            {"status": "failed", "error_message": str(error)[:1500]},
        )
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"CONTENT GENERATION FAILED: {error}", file=sys.stderr)
        raise
