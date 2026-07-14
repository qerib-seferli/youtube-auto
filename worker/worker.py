import os, time, json, subprocess, pathlib, shutil, asyncio, base64
from datetime import datetime, timezone

import requests
import edge_tts
from supabase import create_client
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from PIL import Image, ImageDraw, ImageFont

SUPABASE_URL = os.environ["SUPABASE_URL"]
SERVICE_ROLE = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
OPENAI_KEY = os.environ["OPENAI_API_KEY"]
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
PEXELS_KEY = os.environ["PEXELS_API_KEY"]
GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
TOKEN_KEY = base64.b64decode(os.environ["TOKEN_ENCRYPTION_KEY"])
WORKER_ID = os.getenv("WORKER_ID", "media-worker-01")
WORK_DIR = pathlib.Path(os.getenv("WORK_DIR", "/app/work"))
MUSIC_PATH = pathlib.Path(os.getenv("MUSIC_PATH", "/app/music/default.mp3"))

if len(TOKEN_KEY) != 32:
    raise RuntimeError("TOKEN_ENCRYPTION_KEY must decode to exactly 32 bytes")

WORK_DIR.mkdir(parents=True, exist_ok=True)
sb = create_client(SUPABASE_URL, SERVICE_ROLE)


def run(command):
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr[-3000:])
    return result.stdout


def duration(path):
    return float(run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path)
    ]).strip())


def update_video(video_id, **fields):
    sb.table("video_queue").update(fields).eq("id", video_id).execute()


def add_event(video, event_type, message, level="info"):
    sb.table("events").insert({
        "owner_id": video["owner_id"],
        "video_id": video["id"],
        "channel_id": video["channel_id"],
        "type": event_type,
        "message": message,
        "level": level,
    }).execute()


def create_content(channel, video):
    recent = sb.table("topic_history").select("topic").eq("channel_id", channel["id"]).order("used_at", desc=True).limit(30).execute().data
    recent_topics = [item["topic"] for item in recent]
    target = (
        f'{channel["short_min_seconds"]}-{channel["short_max_seconds"]} seconds'
        if video["video_type"] == "short"
        else f'{channel["long_min_minutes"]}-{channel["long_max_minutes"]} minutes'
    )

    prompt = f"""
Return only valid JSON for an original YouTube video.
Language: {channel['language']}
Niche: {channel['niche']}
Audience: {channel['audience_type']}
Target duration: {target}
Custom channel instruction: {channel.get('custom_prompt') or 'none'}
Requested topic: {video.get('topic') or 'choose an original evergreen topic'}
Avoid recent topics: {recent_topics}

Return keys:
topic, title, description, tags, hashtags, thumbnail_text, script, scenes.
scenes must be an array of objects with query, narration, seconds.
Avoid copyright copying, fake claims, medical guarantees and unsafe kids content.
"""

    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["topic", "title", "description", "tags", "hashtags", "thumbnail_text", "script", "scenes"],
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

    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"},
        json={
            "model": OPENAI_MODEL,
            "input": prompt,
            "text": {"format": {"type": "json_schema", "name": "youtube_video", "strict": True, "schema": schema}},
            "max_output_tokens": 12000,
        },
        timeout=180,
    )
    if not response.ok:
        raise RuntimeError(f"OpenAI {response.status_code}: {response.text[:1000]}")

    payload = response.json()
    output_text = payload.get("output_text")
    if not output_text:
        for item in payload.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text":
                    output_text = content.get("text")
                    break
    if not output_text:
        raise RuntimeError("OpenAI returned no structured output")
    return json.loads(output_text)


async def create_voice(text, voice_id, audio_path, subtitle_path):
    communication = edge_tts.Communicate(text, voice_id)
    subtitles = edge_tts.SubMaker()
    with open(audio_path, "wb") as output:
        async for chunk in communication.stream():
            if chunk["type"] == "audio":
                output.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                subtitles.feed(chunk)
    pathlib.Path(subtitle_path).write_text(subtitles.get_srt(), encoding="utf-8")


def find_pexels_clip(query, portrait, avoided_ids):
    response = requests.get(
        "https://api.pexels.com/videos/search",
        headers={"Authorization": PEXELS_KEY},
        params={
            "query": query,
            "orientation": "portrait" if portrait else "landscape",
            "size": "large",
            "per_page": 30,
        },
        timeout=40,
    )
    response.raise_for_status()

    for video in response.json().get("videos", []):
        asset_id = str(video["id"])
        if asset_id in avoided_ids:
            continue
        files = sorted(video.get("video_files", []), key=lambda item: item.get("width", 0) * item.get("height", 0), reverse=True)
        for file in files:
            width, height = file.get("width", 0), file.get("height", 0)
            correct_orientation = height > width if portrait else width >= height
            if file.get("link") and correct_orientation:
                return {
                    "id": asset_id,
                    "url": file["link"],
                    "source_url": video.get("url"),
                }
    raise RuntimeError(f"Pexels clip not found for: {query}")


def download(url, destination):
    with requests.get(url, stream=True, timeout=120) as response:
        response.raise_for_status()
        with open(destination, "wb") as output:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    output.write(chunk)


def render_video(scene_files, audio_path, subtitle_path, output_path, portrait):
    width, height = (1080, 1920) if portrait else (1920, 1080)
    audio_duration = duration(audio_path)
    expected = sum(max(1, float(scene["seconds"])) for scene in scene_files)
    ratio = audio_duration / expected if expected else 1
    normalized = []

    for index, scene in enumerate(scene_files):
        normalized_path = output_path.parent / f"normalized-{index:03}.mp4"
        scene_duration = max(1, float(scene["seconds"]) * ratio)
        run([
            "ffmpeg", "-y", "-stream_loop", "-1", "-i", str(scene["file"]),
            "-t", str(scene_duration), "-an", "-vf",
            f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},fps=30,format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", str(normalized_path),
        ])
        normalized.append(normalized_path)

    concat_file = output_path.parent / "concat.txt"
    concat_file.write_text("\n".join(f"file '{path}'" for path in normalized), encoding="utf-8")
    visual_path = output_path.parent / "visual.mp4"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(visual_path)])

    subtitle_filter = str(subtitle_path).replace("\\", "/").replace(":", "\\:")
    command = [
        "ffmpeg", "-y", "-i", str(visual_path), "-i", str(audio_path),
        "-vf", f"subtitles='{subtitle_filter}'", "-map", "0:v", "-map", "1:a",
        "-t", str(audio_duration), "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
        str(output_path),
    ]
    run(command)


def create_thumbnail(text, source_video, output_path):
    frame_path = output_path.parent / "thumbnail-frame.jpg"
    run(["ffmpeg", "-y", "-ss", "1", "-i", str(source_video), "-frames:v", "1", str(frame_path)])
    image = Image.open(frame_path).convert("RGB")
    image = image.resize((1280, 720))
    draw = ImageDraw.Draw(image, "RGBA")
    draw.rectangle((0, 0, 800, 720), fill=(0, 0, 0, 170))
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 82)
    draw.text((55, 275), text.upper()[:28], font=font, fill="white", stroke_width=5, stroke_fill="#111")
    image.save(output_path, "JPEG", quality=88, optimize=True)


def decrypt_refresh_token(ciphertext):
    version, iv_raw, encrypted_raw = ciphertext.split(".")
    if version != "v1":
        raise RuntimeError("Unsupported token encryption version")
    iv = base64.urlsafe_b64decode(iv_raw + "==")
    encrypted = base64.urlsafe_b64decode(encrypted_raw + "==")
    return AESGCM(TOKEN_KEY).decrypt(iv, encrypted, None).decode()


def upload_to_youtube(channel, video, video_path, thumbnail_path):
    secret = sb.table("channel_secrets").select("*").eq("channel_id", channel["id"]).single().execute().data
    credentials = Credentials(
        token=None,
        refresh_token=decrypt_refresh_token(secret["refresh_token_cipher"]),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=secret["scopes"],
    )
    youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
    description = (video["description"] or "") + "\n\n" + " ".join("#" + item.lstrip("#") for item in video["hashtags"])
    request = youtube.videos().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": video["title"][:100],
                "description": description,
                "tags": video["tags"][:30],
                "categoryId": "22",
            },
            "status": {
                "privacyStatus": channel["privacy_status"],
                "selfDeclaredMadeForKids": channel["made_for_kids"],
            },
        },
        media_body=MediaFileUpload(str(video_path), mimetype="video/mp4", resumable=True),
    )
    response = None
    while response is None:
        _, response = request.next_chunk()
    youtube.thumbnails().set(
        videoId=response["id"],
        media_body=MediaFileUpload(str(thumbnail_path), mimetype="image/jpeg"),
    ).execute()
    return response["id"]


def process_video(video):
    work = WORK_DIR / video["id"]
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    channel = sb.table("channels").select("*").eq("id", video["channel_id"]).single().execute().data

    update_video(video["id"], status="generating", error_message=None)
    add_event(video, "started", "Video hazırlanmağa başladı")
    content = create_content(channel, video)
    update_video(
        video["id"],
        topic=content["topic"], title=content["title"], description=content["description"],
        tags=content["tags"], hashtags=content["hashtags"], script=content["script"],
        scene_plan=content["scenes"], thumbnail_text=content["thumbnail_text"],
    )

    audio_path = work / "audio.mp3"
    subtitle_path = work / "subtitles.srt"
    asyncio.run(create_voice(content["script"], channel["voice_id"], audio_path, subtitle_path))

    recent_assets = sb.table("media_history").select("provider_asset_id").eq("channel_id", channel["id"]).order("used_at", desc=True).limit(100).execute().data
    avoided = {item["provider_asset_id"] for item in recent_assets}
    portrait = video["video_type"] == "short"
    scene_files = []

    for index, scene in enumerate(content["scenes"]):
        asset = find_pexels_clip(scene["query"], portrait, avoided)
        avoided.add(asset["id"])
        local_path = work / f"scene-{index:03}.mp4"
        download(asset["url"], local_path)
        scene_files.append({**scene, "file": local_path, "asset": asset})

    update_video(video["id"], status="rendering")
    output_path = work / "final.mp4"
    thumbnail_path = work / "thumbnail.jpg"
    render_video(scene_files, audio_path, subtitle_path, output_path, portrait)
    create_thumbnail(content["thumbnail_text"], scene_files[0]["file"], thumbnail_path)
    update_video(video["id"], status="ready", duration_seconds=duration(output_path))

    refreshed = sb.table("video_queue").select("*").eq("id", video["id"]).single().execute().data
    if refreshed.get("publish_at"):
        publish_time = datetime.fromisoformat(refreshed["publish_at"].replace("Z", "+00:00"))
        if publish_time > datetime.now(timezone.utc):
            update_video(video["id"], locked_by=None, locked_at=None)
            add_event(video, "ready", "Video hazırdır və paylaşım vaxtını gözləyir")
            return

    update_video(video["id"], status="uploading")
    youtube_id = upload_to_youtube(channel, refreshed, output_path, thumbnail_path)
    update_video(
        video["id"], status="uploaded", youtube_video_id=youtube_id,
        youtube_url=f"https://youtu.be/{youtube_id}", locked_by=None, locked_at=None,
    )
    add_event(video, "uploaded", "Video YouTube-a yükləndi")

    sb.table("topic_history").insert({
        "channel_id": channel["id"], "video_id": video["id"], "topic": content["topic"],
    }).execute()
    for scene in scene_files:
        sb.table("media_history").insert({
            "channel_id": channel["id"], "video_id": video["id"], "provider": "pexels",
            "provider_asset_id": scene["asset"]["id"], "source_url": scene["asset"]["source_url"],
        }).execute()


while True:
    try:
        jobs = sb.rpc("claim_next_video", {"p_worker_id": WORKER_ID}).execute().data
        if not jobs:
            sb.table("worker_state").upsert({
                "worker_id": WORKER_ID,
                "last_seen_at": datetime.now(timezone.utc).isoformat(),
                "version": "1.0.0",
            }).execute()
            time.sleep(8)
            continue

        video = jobs[0]
        try:
            process_video(video)
        except Exception as error:
            update_video(video["id"], status="failed", error_message=str(error)[:1500], locked_by=None, locked_at=None)
            add_event(video, "failed", str(error)[:500], "error")
    except Exception as loop_error:
        print(loop_error, flush=True)
        time.sleep(15)
