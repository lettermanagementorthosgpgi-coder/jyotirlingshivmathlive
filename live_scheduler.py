import base64
import json
import os
import sys
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
PROGRESS_FILE = "progress.json"
GITHUB_API = "https://api.github.com"


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
        "User-Agent": "Jyotirling-Shivmath-Live-Scheduler"
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
        "content": base64.b64encode(content).decode("ascii")
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
        cache_discovery=False
    )


def get_ai_title(slot, lesson):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing GEMINI_API_KEY secret.")

    client = genai.Client(api_key=api_key)

    prompt = f"""
You write accurate, searchable YouTube titles for a Hindu
devotional channel.

Programme: {slot["key"]}
Verified lesson from the user's list: {lesson}

Write one concise searchable title in Hindi or English, whichever
best fits the lesson. Start with the actual lesson topic.
Do not invent chapter numbers or add unsupported claims.
No hashtags, emojis, quotation marks or explanations.
The title must end exactly with: | {AUTHOR}
Maximum total length: 100 characters.
Return only the title.
"""

    response = client.models.generate_content(
       model="gemini-3.8-flash",
        contents=prompt
    )

    title = " ".join((response.text or "").strip().split())
    title = title.strip("\"'“”‘’")

    suffix = f" | {AUTHOR}"
    if not title:
        raise RuntimeError("Gemini returned an empty title.")

    if title.endswith(suffix):
        topic = title[:-len(suffix)].strip()
    else:
        topic = title.rsplit("|", 1)[0].strip() if "|" in title else title

    max_topic_length = 100 - len(suffix)
    topic = topic[:max_topic_length].rstrip(" |:-")
    if not topic:
        raise RuntimeError("Gemini returned an invalid title topic.")

    return topic + suffix


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
        tzinfo=TIMEZONE
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

            if existing_dt == start_time:
                return item["id"], snippet.get("title", "")

        request = youtube.liveBroadcasts().list_next(request, response)

    return None, None


def create_broadcast(youtube, config, slot, start_time):
    lesson_map = config.get("lessons", {})
    programme_lessons = lesson_map.get(slot["key"], [])

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

    title = get_ai_title(slot, lesson)
    description = (
        f"{slot.get('description', '')}\n\n"
        f"आज का पाठ: {lesson}"
    ).strip()

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "scheduledStartTime": start_time.isoformat(),
            "categoryId": str(config.get("category_id", "22"))
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
    print("Broadcast scheduled successfully.")
    print(f"Lesson: {lesson}")
    print(f"Title: {title}")
    print(f"Time: {start_time:%d-%m-%Y %I:%M %p} IST")
    print("Privacy: " + config.get("privacy_status", "private"))
    print("Auto-start: OFF")
    print(f"URL: https://www.youtube.com/watch?v={video_id}")

    progress[slot["key"]] = (lesson_index + 1) % len(programme_lessons)
    save_progress(progress, progress_sha)
    print(
        f"Progress saved. Next lesson index: "
        f"{progress[slot['key']]}"
    )


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
