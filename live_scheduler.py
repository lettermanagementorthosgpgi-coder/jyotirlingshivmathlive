
import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google import genai
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

TIMEZONE = ZoneInfo("Asia/Kolkata")
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
AUTHOR = "Mahant Rajesh Kumar Tiwari"


def load_config():
    with open("config.json", "r", encoding="utf-8") as file:
        return json.load(file)


def get_youtube():
    raw = os.environ.get("YOUTUBE_TOKEN_JSON")
    if not raw:
        raise RuntimeError("Missing YOUTUBE_TOKEN_JSON secret.")

    credentials = Credentials.from_authorized_user_info(
        json.loads(raw), SCOPES
    )

    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())

    if not credentials.valid:
        raise RuntimeError("YouTube OAuth credentials are invalid.")

    return build(
        "youtube", "v3",
        credentials=credentials,
        cache_discovery=False
    )


def get_ai_title(slot, lesson):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing GEMINI_API_KEY secret.")

    client = genai.Client(api_key=api_key)

    prompt = f"""
You are a YouTube SEO title writer for a Hindu devotional channel.

Programme: {slot["key"]}
Verified chapter/lesson: {lesson}

Create ONE searchable YouTube title.
Rules:
- Start with the lesson/chapter topic, not a generic channel slogan.
- Use relevant common search keywords naturally.
- End exactly with: | {AUTHOR}
- Do not add hashtags, quotes, explanations, or emojis.
- Do not invent chapter numbers, scripture claims, or lesson details.
- Keep the title concise and within YouTube's 100-character limit.

Return only the final title.
"""

    response = client.models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt
    )

    title = (response.text or "").strip().strip('"').strip("'")
    title = " ".join(title.split())

    if not title or AUTHOR not in title:
        raise RuntimeError("AI did not return a valid title.")

    if len(title) > 100:
        title = title[:100 - len(AUTHOR) - 3].rstrip(" |:-") + " | " + AUTHOR

    return title


def choose_schedule(config, now):
    schedules = config["schedule"]

    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        selected = os.environ.get("PROGRAMME", "").strip()
        if selected:
            for item in schedules:
                if item["key"] == selected:
                    return item
        raise RuntimeError(
            "Manual run requires PROGRAMME to be selected. "
            "Use shivpuran or bhagavad_gita."
        )

    for item in schedules:
        if (item["hour"], item["minute"]) == (now.hour, now.minute):
            return item

    raise RuntimeError(
        f"No configured slot matches India time {now:%H:%M}."
    )


def tomorrow_start(slot, now):
    day = now.date() + timedelta(days=1)
    return datetime(
        day.year, day.month, day.day,
        slot["hour"], slot["minute"], tzinfo=TIMEZONE
    )


def find_existing_broadcast(youtube, title, start_time):
    request = youtube.liveBroadcasts().list(
        part="snippet",
        broadcastStatus="upcoming",
        maxResults=50
    )

    while request:
        response = request.execute()
        for item in response.get("items", []):
            snippet = item.get("snippet", {})
            existing_start = snippet.get("scheduledStartTime")
            if not existing_start:
                continue

            existing_dt = datetime.fromisoformat(
                existing_start.replace("Z", "+00:00")
            ).astimezone(TIMEZONE)

            if (
                snippet.get("title") == title
                and existing_dt.date() == start_time.date()
            ):
                return item["id"]

        request = youtube.liveBroadcasts().list_next(request, response)

    return None


def create_broadcast(youtube, config, slot, start_time):
    lesson_map = config.get("lessons", {})
    programme_lessons = lesson_map.get(slot["key"], [])

    if not programme_lessons:
        raise RuntimeError(
            f"No verified lesson list found for {slot['key']}. "
            "Add the verified chapter/lesson list to config.json first."
        )

    # Choose the next lesson using the scheduled date.
    lesson = programme_lessons[
        start_time.date().toordinal() % len(programme_lessons)
    ]

    title = get_ai_title(slot, lesson)

    existing_id = find_existing_broadcast(youtube, title, start_time)
    if existing_id:
        print(f"Already scheduled: https://www.youtube.com/watch?v={existing_id}")
        return

    description = slot.get("description", "")

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "scheduledStartTime": start_time.isoformat()
        },
        "status": {
            "privacyStatus": config.get("privacy_status", "private"),
            "selfDeclaredMadeForKids": False
        },
        "contentDetails": {
            "enableAutoStart": False,
            "enableAutoStop": False
        }
    }

    result = youtube.liveBroadcasts().insert(
        part="snippet,status,contentDetails",
        body=body
    ).execute()

    video_id = result["id"]
    print("Broadcast scheduled.")
    print(f"Lesson: {lesson}")
    print(f"Title: {title}")
    print(f"Time: {start_time:%d-%m-%Y %I:%M %p} IST")
    print(f"Auto-start: OFF")
    print(f"URL: https://www.youtube.com/watch?v={video_id}")


def main():
    config = load_config()
    now = datetime.now(TIMEZONE)
    print(f"India time: {now:%Y-%m-%d %H:%M:%S}")

    slot = choose_schedule(config, now)
    start_time = tomorrow_start(slot, now)

    youtube = get_youtube()
    create_broadcast(youtube, config, slot, start_time)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
