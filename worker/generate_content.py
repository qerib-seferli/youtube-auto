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
    else:
        duration = (
            f"{channel['long_min_minutes']}–"
            f"{channel['long_max_minutes']} minutes"
        )

    requested_topic = (
        video.get("topic")
        or "Choose an original topic yourself"
    )

    output_language = language_name(
        channel["language"]
    )

    return f"""
You are a professional YouTube content strategist and script writer.

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
- Do not use deceptive or misleading claims.
- Do not mention artificial intelligence.
- Avoid repetition.
- The narration must sound natural when spoken aloud.
- The opening must be strong enough to keep the viewer watching.
- The title must be attractive but honest.
- The description must be suitable for YouTube SEO.
- Thumbnail text must contain no more than 5 words.
- Tags must not contain the # symbol.
- Hashtags must not contain the # symbol.
- Visual keywords must be short English stock-video search phrases.
- For children's content, keep the content safe, positive and age-appropriate.

RETURN THESE FIELDS
- topic
- title
- description
- tags
- hashtags
- thumbnail_text
- script
- visual_keywords
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
            f"Prompt feedback: {json.dumps(prompt_feedback, ensure_ascii=False)}"
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
            "visual_keywords",
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
            "visual_keywords": {
                "type": "array",
                "items": {
                    "type": "string",
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
                "temperature": 0.8,
                "maxOutputTokens": 12000,
            },
        },
        timeout=180,
    )

    if not response.ok:
        raise RuntimeError(
            f"Gemini xətası {response.status_code}: "
            f"{response.text[:1500]}"
        )

    payload = response.json()
    output_text = extract_gemini_text(payload)

    try:
        content = json.loads(output_text)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "Gemini etibarlı JSON qaytarmadı. "
            f"Xəta: {error}. "
            f"Cavabın əvvəli: {output_text[:500]}"
        ) from error

    return content


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
    print(
        "Kanal:",
        video["channel"]["name"],
    )
    print(
        "AI provider: Gemini"
    )
    print(
        "Model:",
        GEMINI_MODEL,
    )
    print(
        "Mərhələ: Ssenari və metadata hazırlanır."
    )

    update_video(
        supabase,
        video_id,
        {
            "status": "generating",
            "error_message": None,
        },
    )

    try:
        content = generate_content(video)

        visual_keywords = [
            str(keyword).strip()
            for keyword in content.get(
                "visual_keywords",
                [],
            )
            if str(keyword).strip()
        ]

        scene_plan = [
            {
                "query": keyword,
                "seconds": 8,
            }
            for keyword in visual_keywords[:30]
        ]

        tags = [
            str(tag).strip()
            for tag in content.get(
                "tags",
                [],
            )
            if str(tag).strip()
        ]

        hashtags = [
            str(tag)
            .strip()
            .lstrip("#")
            for tag in content.get(
                "hashtags",
                [],
            )
            if str(tag).strip()
        ]

        title = str(
            content["title"]
        ).strip()

        description = str(
            content["description"]
        ).strip()

        script = str(
            content["script"]
        ).strip()

        topic = str(
            content["topic"]
        ).strip()

        thumbnail_text = str(
            content["thumbnail_text"]
        ).strip()

        if not script:
            raise RuntimeError(
                "Gemini ssenari yaratmadı."
            )

        update_video(
            supabase,
            video_id,
            {
                "topic": topic,
                "title": title[:100],
                "description": description,
                "tags": tags[:30],
                "hashtags": hashtags[:15],
                "thumbnail_text": thumbnail_text[:80],
                "script": script,
                "scene_plan": scene_plan,
                "status": "generating",
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
                    "Gemini ilə ssenari, başlıq və "
                    "YouTube metadata-sı uğurla hazırlandı."
                ),
                "payload": {
                    "provider": "gemini",
                    "model": GEMINI_MODEL,
                    "title": title,
                    "topic": topic,
                },
            }
        ).execute()

        print(
            "Ssenari uğurla yaradıldı."
        )
        print(
            "Başlıq:",
            title,
        )
        print(
            "Mövzu:",
            topic,
        )
        print(
            "Ssenari simvol sayı:",
            len(script),
        )
        print(
            "Növbəti mərhələ: Səs hazırlanması."
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
