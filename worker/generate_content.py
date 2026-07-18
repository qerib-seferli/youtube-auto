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
GEMINI_MODEL = os.getenv(
    "GEMINI_MODEL",
    "gemini-3.5-flash",
)


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
            "Video sətri yenilənmədi."
        )


def get_pending_video(supabase):
    response = (
        supabase.table("video_queue")
        .select(
            "*,"
            "channel:channels("
            "id,name,language,niche,custom_prompt,"
            "audience_type,long_min_minutes,long_max_minutes,"
            "short_min_seconds,short_max_seconds"
            ")"
        )
        .eq("status", "pending")
        .order("created_at")
        .limit(1)
        .execute()
    )

    if not response.data:
        return None

    return response.data[0]


def language_name(language_code: str) -> str:
    languages = {
        "en": "English",
        "tr": "Turkish",
        "az": "Azerbaijani",
    }

    return languages.get(
        language_code,
        language_code,
    )


def build_prompt(
    video: dict[str, Any],
) -> str:
    channel = video["channel"]
    video_type = video["video_type"]

    if video_type == "short":
        duration = (
            f"{channel['short_min_seconds']}–"
            f"{channel['short_max_seconds']} seconds"
        )

        scene_instruction = """
Create 4 to 8 scenes.
Each scene should normally contain 5 to 12 seconds of narration.
The complete video must remain suitable for a YouTube Short.
""".strip()
    else:
        duration = (
            f"{channel['long_min_minutes']}–"
            f"{channel['long_max_minutes']} minutes"
        )

        scene_instruction = """
Create enough scenes to cover the requested duration naturally.
Normally create between 12 and 30 scenes.
Each scene should normally contain 12 to 30 seconds of narration.
""".strip()

    requested_topic = (
        video.get("topic")
        or "Choose an original evergreen topic yourself"
    )

    output_language = language_name(
        channel["language"]
    )

    return f"""
You are a professional YouTube content strategist,
script writer and visual director.

Create completely original YouTube content for the channel below.

CHANNEL INFORMATION
Channel name: {channel['name']}
Output language: {output_language}
Niche: {channel['niche']}
Audience: {channel['audience_type']}
Video type: {video_type}
Target duration: {duration}
Requested topic: {requested_topic}
Custom instruction: {channel.get('custom_prompt') or 'None'}

IMPORTANT RULES
- Write all viewer-facing content in {output_language}.
- Create original content only.
- Do not copy existing videos, titles, scripts or descriptions.
- Do not mention artificial intelligence.
- Do not mention Gemini, OpenAI, ChatGPT or content automation.
- Do not include production notes in the narration.
- Do not use deceptive or misleading claims.
- Avoid repetition, filler and generic introductions.
- Start with a strong and honest viewer hook.
- Narration must sound natural when spoken aloud.
- Use clear punctuation for text-to-speech.
- The title must be attractive but honest.
- The description must be suitable for YouTube SEO.
- Thumbnail text must contain no more than 5 words.
- Tags must not contain the # symbol.
- Hashtags must not contain the # symbol.
- Every scene must contain useful narration.
- Every scene query must be a short English Pexels stock-video search phrase.
- Do not put company names, trademarks or celebrity names in visual queries.
- Do not request text, logos, screenshots or watermarks in visual queries.
- Scene seconds must realistically match the narration length.
- The script must represent the same narration as all scenes combined.
- For children's content, remain safe, positive and age-appropriate.

SCENE REQUIREMENTS
{scene_instruction}

RETURN THESE FIELDS
- topic
- title
- description
- tags
- hashtags
- thumbnail_text
- script
- scenes

Each scenes item must contain:
- query: short English stock-video search phrase
- narration: narration in {output_language}
- seconds: estimated scene duration
""".strip()


def extract_gemini_text(
    payload: dict[str, Any],
) -> str:
    candidates = payload.get("candidates") or []

    if not candidates:
        prompt_feedback = payload.get(
            "promptFeedback",
            {},
        )

        raise RuntimeError(
            "Gemini cavab namizədi qaytarmadı. "
            f"Prompt feedback: "
            f"{json.dumps(prompt_feedback, ensure_ascii=False)}"
        )

    content = (
        candidates[0]
        .get("content", {})
    )

    parts = content.get("parts") or []

    text_parts = [
        part.get("text", "")
        for part in parts
        if part.get("text")
    ]

    output_text = "".join(text_parts).strip()

    if not output_text:
        finish_reason = candidates[0].get(
            "finishReason",
            "unknown",
        )

        raise RuntimeError(
            "Gemini boş mətn qaytardı. "
            f"Finish reason: {finish_reason}"
        )

    return output_text


def clean_text(value: Any) -> str:
    return " ".join(
        str(value or "")
        .replace("\r", " ")
        .replace("\n", " ")
        .split()
    ).strip()


def generate_content(
    video: dict[str, Any],
) -> dict[str, Any]:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "topic",
            "title",
            "description",
            "tags",
            "hashtags",
            "thumbnail_text",
            "script",
            "scenes",
        ],
        "properties": {
            "topic": {
                "type": "string",
            },
            "title": {
                "type": "string",
            },
            "description": {
                "type": "string",
            },
            "tags": {
                "type": "array",
                "items": {
                    "type": "string",
                },
            },
            "hashtags": {
                "type": "array",
                "items": {
                    "type": "string",
                },
            },
            "thumbnail_text": {
                "type": "string",
            },
            "script": {
                "type": "string",
            },
            "scenes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "query",
                        "narration",
                        "seconds",
                    ],
                    "properties": {
                        "query": {
                            "type": "string",
                        },
                        "narration": {
                            "type": "string",
                        },
                        "seconds": {
                            "type": "number",
                        },
                    },
                },
            },
        },
    }

    encoded_model = quote(
        GEMINI_MODEL,
        safe="",
    )

    endpoint = (
        "https://generativelanguage.googleapis.com/"
        f"v1beta/models/{encoded_model}:generateContent"
    )

    response = requests.post(
        endpoint,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": GEMINI_API_KEY,
        },
        json={
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": build_prompt(video),
                        }
                    ],
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseJsonSchema": schema,
                "temperature": 0.75,
                "maxOutputTokens": 16000,
            },
        },
        timeout=240,
    )

    if not response.ok:
        raise RuntimeError(
            f"Gemini xətası {response.status_code}: "
            f"{response.text[:1500]}"
        )

    payload = response.json()
    output_text = extract_gemini_text(payload)

    try:
        return json.loads(output_text)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "Gemini etibarlı JSON qaytarmadı. "
            f"Xəta: {error}. "
            f"Cavabın əvvəli: {output_text[:500]}"
        ) from error


def normalize_content(
    content: dict[str, Any],
) -> dict[str, Any]:
    raw_scenes = content.get("scenes") or []
    scenes: list[dict[str, Any]] = []

    for raw_scene in raw_scenes:
        query = clean_text(
            raw_scene.get("query")
        )

        narration = clean_text(
            raw_scene.get("narration")
        )

        try:
            seconds = float(
                raw_scene.get("seconds") or 8
            )
        except (TypeError, ValueError):
            seconds = 8.0

        seconds = max(
            3.0,
            min(seconds, 45.0),
        )

        if not query or not narration:
            continue

        scenes.append(
            {
                "query": query[:120],
                "narration": narration,
                "seconds": round(seconds, 2),
            }
        )

    if not scenes:
        raise RuntimeError(
            "Gemini etibarlı səhnə planı yaratmadı."
        )

    # Səs və görüntünün tam uyğun olması üçün əsas script
    # səhnə narration-larından yenidən yaradılır.
    script = "\n\n".join(
        scene["narration"]
        for scene in scenes
    )

    tags = [
        clean_text(tag).lstrip("#")
        for tag in content.get("tags", [])
        if clean_text(tag)
    ]

    hashtags = [
        clean_text(tag).lstrip("#")
        for tag in content.get("hashtags", [])
        if clean_text(tag)
    ]

    return {
        "topic": clean_text(
            content.get("topic")
        ),
        "title": clean_text(
            content.get("title")
        ),
        "description": str(
            content.get("description") or ""
        ).strip(),
        "tags": tags[:30],
        "hashtags": hashtags[:15],
        "thumbnail_text": clean_text(
            content.get("thumbnail_text")
        ),
        "script": script,
        "scenes": scenes,
    }


def main() -> None:
    supabase = create_client(
        SUPABASE_URL,
        SUPABASE_KEY,
    )

    video = get_pending_video(supabase)

    if not video:
        print(
            "Növbədə pending statuslu video yoxdur."
        )
        return

    video_id = video["id"]

    print("Video tapıldı:", video_id)
    print("Kanal:", video["channel"]["name"])
    print("AI provider: Gemini")
    print("Model:", GEMINI_MODEL)
    print("Mərhələ: Ssenari və səhnə planı hazırlanır.")

    update_video(
        supabase,
        video_id,
        {
            "status": "generating",
            "error_message": None,
        },
    )

    try:
        generated = generate_content(video)
        content = normalize_content(generated)

        if not content["script"]:
            raise RuntimeError(
                "Gemini ssenari yaratmadı."
            )

        if not content["title"]:
            raise RuntimeError(
                "Gemini başlıq yaratmadı."
            )

        update_video(
            supabase,
            video_id,
            {
                "topic": content["topic"],
                "title": content["title"][:100],
                "description": content["description"],
                "tags": content["tags"],
                "hashtags": content["hashtags"],
                "thumbnail_text": (
                    content["thumbnail_text"][:80]
                ),
                "script": content["script"],
                "scene_plan": content["scenes"],
                "status": "generating",
                "error_message": None,
            },
        )

        channel = video["channel"]

        supabase.table("events").insert(
            {
                "owner_id": video["owner_id"],
                "video_id": video_id,
                "channel_id": channel["id"],
                "level": "info",
                "type": "content_generated",
                "message": (
                    "Gemini ilə ssenari, metadata və "
                    "səhnə planı uğurla hazırlandı."
                ),
                "payload": {
                    "provider": "gemini",
                    "model": GEMINI_MODEL,
                    "title": content["title"],
                    "topic": content["topic"],
                    "scene_count": len(
                        content["scenes"]
                    ),
                },
            }
        ).execute()

        print("Ssenari uğurla yaradıldı.")
        print("Başlıq:", content["title"])
        print("Mövzu:", content["topic"])
        print(
            "Səhnə sayı:",
            len(content["scenes"]),
        )
        print(
            "Ssenari simvol sayı:",
            len(content["script"]),
        )
        print(
            "Növbəti mərhələ: Media və səs testi."
        )

    except Exception as error:
        update_video(
            supabase,
            video_id,
            {
                "status": "failed",
                "error_message": str(error)[:1500],
            },
        )

        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            f"CONTENT GENERATION FAILED: {error}",
            file=sys.stderr,
        )
        raise
