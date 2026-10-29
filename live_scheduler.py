
import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


TIMEZONE = ZoneInfo("Asia/Kolkata")
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]


def load_config():
    with open("config.json", "r", encoding="utf-8") as file:
        return json.load(file)


def get_youtube():
    token_json = os.environ.get("YOUTUBE_TOKEN_JSON")

    if not token_json:
        raise RuntimeError(
            "GitHub Secret YOUTUBE_TOKEN_JSON is missing."
        )

    token_data = json.loads(token_json)
    credentials = Credentials.from_authorized_user_info(
        token_data, SCOPES
    )

    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())

    if not credentials.valid:
        raise RuntimeError(
            "YouTube OAuth credentials are invalid or expired."
        )

    return build(
        "youtube", "v3",
        credentials=credentials,
        cache_discovery=False
    )


def choose_schedule(config, now):
    schedules = config["schedule"]

    # GitHub's scheduled runs are in UTC; select the matching
    # India-time slot. Manual runs use the nearest configured slot.
    exact = [
        item for item in schedules
        if item["hour"] == now.hour
        and item["minute"] == now.minute
    ]

    if exact:
        return exact[0]

    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        return min(
            schedules,
            key=lambda item: abs(
                item["hour"] * 60 + item["minute"]
                - (now.hour * 60 + now.minute)
            )
        )

    raise RuntimeError(
        f"No schedule configured for India time {now:%H:%M}."
    )


def tomorrow_start(slot, now):
    start_date = now.date() + timedelta(days=1)
    return datetime(
        start_date.year,
        start_date.month,
        start_date.day,
        slot["hour"],
        slot["minute"],
        tzinfo=TIMEZONE
    )


def find_existing_broadcast(youtube, title, start_time):
    request = youtube.liveBroadcasts().list(
        part="snippet,status",
        broadcastStatus="upcoming",
        maxResults=50
    )

    while request:
        response = request.execute()

        for item in response.get("items", []):
            snippet = item["snippet"]
            existing_title = snippet.get("title", "")
            existing_start = snippet.get("scheduledStartTime")

            if existing_title != title or not existing_start:
                continue

            existing_dt = datetime.fromisoformat(
                existing_start.replace("Z", "+00:00")
            ).astimezone(TIMEZONE)

            if existing_dt.date() == start_time.date():
                return item["id"]

        request = youtube.liveBroadcasts().list_next(
            request, response
        )

    return None


def create_broadcast(youtube, config, slot, start_time):
    title = slot["title"]
    description = slot["description"]

    existing_id = find_existing_broadcast(
        youtube, title, start_time
    )

    if existing_id:
        print(
            f"Already scheduled for {start_time:%d-%m-%Y %I:%M %p}: "
            f"https://www.youtube.com/watch?v={existing_id}"
        )
        return

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

    print("Scheduled YouTube broadcast successfully.")
    print(f"Title: {title}")
    print(f"Scheduled: {start_time:%d-%m-%Y %I:%M %p} IST")
    print(f"Privacy: {config.get('privacy_status', 'private')}")
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
