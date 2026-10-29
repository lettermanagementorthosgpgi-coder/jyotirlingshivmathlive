
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google import genai
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

TIMEZONE = ZoneInfo("Asia/Kolkata")
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
AUTHOR = "Mahant Rajesh Kumar Tiwari"
CHANNEL_NAME = "JYOTIRLING SHIVMATH BARAULI LUCKNOW"
WEBSITE = "https://jyotirlingshivmath.in"
CHANNEL_URL = "https://www.youtube.com/@MAHANTRAJESHKUMARTIWARIRANJAN"
PROGRESS_FILE = "progress.json"
GITHUB_API = "https://api.github.com"
GEMINI_MODEL = "gemini-3.8-flash"
MAX_GEMINI_ATTEMPTS = 3


def load_config():
    with open("config.json", "r", encoding="utf-8") as file:
        return json.load(file)


def github_request(url, method="GET", payload=None):
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("Missing GITHUB_TOKEN environment variable.")

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Jyotirling-Shivmath-Live-Scheduler",
    }

    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode("utf-8")

    request = urllib.request.Request(
        url, data=data, headers=headers, method=method
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        if error.code == 404:
            return None
        raise RuntimeError(
            f"GitHub API error {error.code}: {detail}"
        ) from error


def get_progress():
    repository = os.environ.get("GITHUB_REPOSITORY")

    if not repository:
        if os.path.exists(PROGRESS_FILE):
            with open(PROGRESS_FILE, "r", encoding="utf-8") as file:
                return json.load(file), None
        return {}, None

    url = f"{GITHUB_API}/repos/{repository}/contents/{PROGRESS_FILE}"
    result = github_request(url)

    if not result:
        return {}, None

    content = base64.b64decode(result["content"]).decode("utf-8")
    return json.loads(content), result.get("sha")


def save_progress(progress, sha=None):
    repository = os.environ.get("GITHUB_REPOSITORY")

    if not repository:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as file:
            json.dump(progress, file, ensure_ascii=False, indent=2)
        return

    url = f"{GITHUB_API}/repos/{repository}/contents/{PROGRESS_FILE}"
    content = json.dumps(
        progress, ensure_ascii=False, indent=2
    ).encode("utf-8")

    payload = {
        "message": "Update sequential Shiv Puran lesson progress",
        "content": base64.b64encode(content).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha

    github_request(url, method="PUT", payload=payload)


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
        cache_discovery=False,
    )


def clean_text(value):
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().split())


def fallback_metadata(slot, lesson, config):
    """Used when Gemini remains unavailable."""
    title = f"{lesson} | Shiv Puran | {AUTHOR}"
    title = title[:100].rstrip()

    base_description = clean_text(slot.get("description", ""))

    description = f"""🔱 हर हर महादेव 🔱

आज का पाठ: {lesson}

इस प्रसारण में श्री शिव पुराण के पाठ से जुड़ें और भगवान शिव की भक्ति करें।

कथा व्यास: {AUTHOR}
धार्मिक मंच: {CHANNEL_NAME}

YouTube चैनल: {CHANNEL_URL}
वेबसाइट: {WEBSITE}

{base_description}

#ShivPuran #ShivKatha #JyotirlingShivmath #HarHarMahadev
""".strip()

    tags = [
        "Shiv Puran",
        "Shiv Purana",
        "Shiv Puran Katha",
        "Shiv Katha",
        "Lord Shiva",
        "Mahadev",
        "Har Har Mahadev",
        "Jyotirling Shivmath",
        "Mahant Rajesh Kumar Tiwari",
        "Hindu devotional",
    ]

    return {
        "title": title,
        "description": description[:4900],
        "tags": tags,
    }


def generate_metadata(slot, lesson, config):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY missing; using fallback metadata.")
        return fallback_metadata(slot, lesson, config)

    client = genai.Client(api_key=api_key)

    prompt = f"""
You are an accurate Hindi YouTube SEO editor for a Hindu devotional channel.

Channel: {CHANNEL_NAME}
Speaker: {AUTHOR}
Programme: {slot["key"]}
Verified lesson name from the user's ordered list: {lesson}
Configured programme description: {slot.get("description", "")}
Website: {WEBSITE}
YouTube channel: {CHANNEL_URL}

Return ONLY one valid JSON object with these keys:
"title": a searchable title, maximum 100 characters, ending with
"| {AUTHOR}".
"description": detailed, natural Hindi description of approximately
150-300 words. Explain only what is supported by the verified lesson
name and supplied programme details. Do not invent story events, verses,
chapter numbers, scripture quotations, dates, timings or claims.
Include the speaker, channel, website and a suitable invitation to watch
and subscribe. Add 3-5 relevant hashtags at the end.
"tags": an array of 8-15 relevant YouTube search tags. Each tag must be
short and relevant. Do not put # before tags.

Do not add markdown fences or any text outside the JSON object.
"""

    for attempt in range(1, MAX_GEMINI_ATTEMPTS + 1):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "temperature": 0.3,
                },
            )

            raw = (response.text or "").strip()
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)

            data = json.loads(raw)
            title = clean_text(data.get("title", ""))
            description = str(data.get("description", "")).strip()
            tags = data.get("tags", [])

            if not title or not description:
                raise ValueError("Gemini returned incomplete metadata.")

            suffix = f" | {AUTHOR}"
            if title.endswith(suffix):
                topic = title[:-len(suffix)].strip()
            else:
                topic = title.rsplit("|", 1)[0].strip()

            topic = topic[:100 - len(suffix)].rstrip(" |:-")
            title = topic + suffix

            if len(title) > 100:
                title = title[:100].rstrip()

            if not isinstance(tags, list):
                tags = []

            tags = [
                clean_text(tag).lstrip("#")
                for tag in tags
                if isinstance(tag, str) and clean_text(tag)
            ]

            # YouTube allows up to 500 characters for all video tags.
            final_tags = []
            total_length = 0
            for tag in tags:
                if tag.lower() not in {t.lower() for t in final_tags}:
                    extra = len(tag) + (1 if final_tags else 0)
                    if total_length + extra <= 500:
                        final_tags.append(tag)
                        total_length += extra

            if not final_tags:
                final_tags = fallback_metadata(
                    slot, lesson, config
                )["tags"]

            # Ensure the description always includes basic channel details.
            footer = (
                f"\n\nकथा व्यास: {AUTHOR}"
                f"\n{CHANNEL_NAME}"
                f"\nYouTube: {CHANNEL_URL}"
                f"\nवेबसाइट: {WEBSITE}"
            )
            if CHANNEL_URL not in description:
                description += footer

            description = description[:4900].strip()

            print(f"Gemini metadata generated on attempt {attempt}.")
            return {
                "title": title,
                "description": description,
                "tags": final_tags,
            }

        except Exception as error:
            print(
                f"Gemini metadata attempt {attempt}/"
                f"{MAX_GEMINI_ATTEMPTS} failed: {error}"
            )
            if attempt < MAX_GEMINI_ATTEMPTS:
                time.sleep(2 ** attempt)

    print("Gemini unavailable; using fallback metadata.")
    return fallback_metadata(slot, lesson, config)


def choose_schedule(config, now):
    schedules = config.get("schedule", [])
    if not schedules:
        raise RuntimeError("No schedule entries found in config.json.")

    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        selected = os.environ.get("PROGRAMME", "").strip()
        if not selected:
            if len(schedules) == 1:
                return schedules[0]
            raise RuntimeError(
                "Set PROGRAMME to one of the configured schedule keys."
            )

        for item in schedules:
            if item["key"] == selected:
                return item

        raise RuntimeError(f"Unknown PROGRAMME: {selected}")

    if len(schedules) == 1:
        return schedules[0]

    for item in schedules:
        scheduled_minutes = item["hour"] * 60 + item["minute"]
        current_minutes = now.hour * 60 + now.minute
        if abs(scheduled_minutes - current_minutes) <= 15:
            return item

    raise RuntimeError(
        f"No configured slot matches India time {now:%H:%M}."
    )


def next_day_start(slot, now):
    day = now.date() + timedelta(days=1)
    return datetime(
        day.year, day.month, day.day,
        int(slot["hour"]), int(slot["minute"]),
        tzinfo=TIMEZONE,
    )


def get_next_lesson(slot_key, programme_lessons, progress):
    if not programme_lessons:
        raise RuntimeError(f"No lessons found for {slot_key}.")

    index = int(progress.get(slot_key, 0))
    if index < 0 or index >= len(programme_lessons):
        index = 0

    return index, programme_lessons[index]


def find_existing_broadcast(youtube, start_time):
    request = youtube.liveBroadcasts().list(
        part="snippet,status",
        broadcastStatus="upcoming",
        maxResults=50,
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

            if existing_dt == start_time:
                return item["id"], snippet.get("title", "")

        request = youtube.liveBroadcasts().list_next(request, response)

    return None, None


def create_broadcast(youtube, config, slot, start_time):
    programme_lessons = config.get("lessons", {}).get(slot["key"], [])

    if not programme_lessons:
        raise RuntimeError(
            f"No verified lesson list found for {slot['key']}. "
            "Check the lessons array in config.json."
        )

    existing_id, existing_title = find_existing_broadcast(
        youtube, start_time
    )
    if existing_id:
        print("A broadcast already exists for this date and time.")
        print(f"Title: {existing_title}")
        print(f"URL: https://www.youtube.com/watch?v={existing_id}")
        print("Lesson progress was not changed.")
        return

    progress, progress_sha = get_progress()
    lesson_index, lesson = get_next_lesson(
        slot["key"], programme_lessons, progress
    )

    metadata = generate_metadata(slot, lesson, config)
    title = metadata["title"]
    description = metadata["description"]
    tags = metadata["tags"]

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags,
            "scheduledStartTime": start_time.isoformat(),
            "categoryId": str(config.get("category_id", "22")),
        },
        "status": {
            "privacyStatus": config.get("privacy_status", "private"),
            "selfDeclaredMadeForKids": False,
        },
        "contentDetails": {
            "enableAutoStart": False,
            "enableAutoStop": False,
        },
    }

    result = youtube.liveBroadcasts().insert(
        part="snippet,status,contentDetails",
        body=body,
    ).execute()

    video_id = result["id"]
    print("Broadcast scheduled successfully.")
    print(f"Lesson: {lesson}")
    print(f"Title: {title}")
    print(f"Tags: {', '.join(tags)}")
    print(f"Description length: {len(description)} characters")
    print(f"Time: {start_time:%d-%m-%Y %I:%M %p} IST")
    print("Privacy: " + config.get("privacy_status", "private"))
    print("Auto-start: OFF")
    print(f"URL: https://www.youtube.com/watch?v={video_id}")

    # Save progress only after YouTube confirms broadcast creation.
    progress[slot["key"]] = (
        lesson_index + 1
    ) % len(programme_lessons)

    save_progress(progress, progress_sha)
    print(f"Progress saved. Next lesson index: {progress[slot['key']]}")


def main():
    config = load_config()
    now = datetime.now(TIMEZONE)

    print(f"India time: {now:%Y-%m-%d %H:%M:%S}")
    slot = choose_schedule(config, now)
    start_time = next_day_start(slot, now)

    print(f"Programme: {slot['key']}")
    print(f"Scheduled start: {start_time:%Y-%m-%d %H:%M} IST")

    youtube = get_youtube()
    create_broadcast(youtube, config, slot, start_time)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
