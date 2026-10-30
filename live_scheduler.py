
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

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

try:
    from google import genai
except ImportError:
    genai = None


# ============================================================
# BASIC SETTINGS
# ============================================================

TIMEZONE = ZoneInfo("Asia/Kolkata")

SCOPES = [
    "https://www.googleapis.com/auth/youtube.force-ssl"
]

AUTHOR = "Mahant Rajesh Kumar Tiwari Ranjan"
CHANNEL_NAME = "JYOTIRLING SHIVMATH BARAULI LUCKNOW"

WEBSITE = "https://jyotirlingshivmath.in"
CHANNEL_URL = (
    "https://www.youtube.com/"
    "@MAHANTRAJESHKUMARTIWARIRANJAN"
)

PROGRESS_FILE = "progress.json"
CONFIG_FILE = "config.json"

GITHUB_API = "https://api.github.com"

OPENAI_MODEL = "gpt-4o-mini"
GEMINI_MODEL = "gemini-3.8-flash"

MAX_AI_ATTEMPTS = 2


# ============================================================
# GENERAL HELPERS
# ============================================================

def log(message):
    print(
        f"[{datetime.now(TIMEZONE).strftime('%d-%m-%Y %H:%M:%S')}] "
        f"{message}",
        flush=True
    )


def fail(message):
    log("ERROR: " + str(message))
    sys.exit(1)


def load_config():
    if not os.path.exists(CONFIG_FILE):
        fail("config.json file नहीं मिली।")

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as file:
            config = json.load(file)
    except Exception as error:
        fail(f"config.json पढ़ने में समस्या: {error}")

    if not isinstance(config.get("schedule"), list):
        fail("config.json में schedule की सूची नहीं मिली।")

    if not isinstance(config.get("lessons"), dict):
        fail("config.json में lessons की सूची नहीं मिली।")

    return config


def parse_date_time(date_value, time_value):
    try:
        date_part = datetime.strptime(
            date_value, "%Y-%m-%d"
        ).date()

        time_part = datetime.strptime(
            time_value, "%H:%M"
        ).time()

        return datetime.combine(
            date_part,
            time_part,
            tzinfo=TIMEZONE
        )
    except ValueError:
        fail(
            "तारीख YYYY-MM-DD और समय HH:MM प्रारूप में दें।"
        )


def normalise_lesson(lesson):
    """
    पुराने config में lesson string हो सकती है।
    नए config में lesson object भी स्वीकार किया जाएगा।
    """

    if isinstance(lesson, str):
        return {
            "title": lesson,
            "chapter": None,
            "meaning": ""
        }

    if isinstance(lesson, dict):
        title = (
            lesson.get("title")
            or lesson.get("hindi_name")
            or lesson.get("sanskrit_name")
            or "धार्मिक प्रवचन"
        )

        return {
            "title": str(title),
            "chapter": lesson.get("chapter"),
            "meaning": str(lesson.get("meaning", ""))
        }

    return {
        "title": str(lesson),
        "chapter": None,
        "meaning": ""
    }


# ============================================================
# GITHUB PROGRESS STORAGE
# ============================================================

def github_request(path, method="GET", payload=None):
    token = os.environ.get("GITHUB_TOKEN")
    repository = os.environ.get("GITHUB_REPOSITORY")

    if not token or not repository:
        return None

    url = f"{GITHUB_API}/repos/{repository}/{path}"

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    data = None

    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}

    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"GitHub API error {error.code}: {body}"
        )

    except Exception as error:
        raise RuntimeError(f"GitHub connection error: {error}")


def get_progress():
    """
    GitHub पर मौजूद progress.json को प्राथमिकता।
    स्थानीय फाइल को बैकअप के रूप में इस्तेमाल किया जाता है।
    """

    try:
        result = github_request(
            f"contents/{PROGRESS_FILE}"
        )

        if result and result.get("content"):
            raw = base64.b64decode(
                result["content"]
            ).decode("utf-8")

            progress = json.loads(raw)

            return progress, result.get("sha")

    except Exception as error:
        log(f"GitHub progress पढ़ने में समस्या: {error}")

    if os.path.exists(PROGRESS_FILE):
        try:
            with open(
                PROGRESS_FILE, "r", encoding="utf-8"
            ) as file:
                return json.load(file), None
        except Exception as error:
            log(f"स्थानीय progress पढ़ने में समस्या: {error}")

    return {}, None


def save_progress(progress, sha=None):
    raw = json.dumps(
        progress,
        ensure_ascii=False,
        indent=2
    )

    with open(
        PROGRESS_FILE, "w", encoding="utf-8"
    ) as file:
        file.write(raw)

    repository = os.environ.get("GITHUB_REPOSITORY")

    if not repository or not os.environ.get("GITHUB_TOKEN"):
        log("Progress स्थानीय फाइल में सुरक्षित की गई।")
        return

    payload = {
        "message": "Update Jyotirling Shivmath live progress",
        "content": base64.b64encode(
            raw.encode("utf-8")
        ).decode("utf-8")
    }

    if sha:
        payload["sha"] = sha

    try:
        github_request(
            f"contents/{PROGRESS_FILE}",
            method="PUT",
            payload=payload
        )
        log("Progress GitHub पर सुरक्षित कर दी गई।")

    except Exception as error:
        log(f"GitHub पर progress सुरक्षित नहीं हुई: {error}")
        raise


# ============================================================
# YOUTUBE CONNECTION
# ============================================================

def get_youtube():
    token_json = os.environ.get("YOUTUBE_TOKEN_JSON")

    if not token_json:
        fail("YOUTUBE_TOKEN_JSON secret उपलब्ध नहीं है।")

    try:
        token_data = json.loads(token_json)

        credentials = Credentials.from_authorized_user_info(
            token_data,
            SCOPES
        )

        if credentials.expired and credentials.refresh_token:
            from google.auth.transport.requests import Request
            credentials.refresh(Request())

            log(
                "YouTube access token refresh हो गया। "
                "GitHub secret में नया token रखना आवश्यक हो सकता है।"
            )

        if not credentials.valid:
            fail(
                "YouTube credentials valid नहीं हैं। "
                "OAuth token दोबारा जाँचें।"
            )

        youtube = build(
            "youtube",
            "v3",
            credentials=credentials,
            cache_discovery=False
        )

        log("YouTube API से सफलतापूर्वक कनेक्शन हो गया।")
        return youtube

    except Exception as error:
        fail(f"YouTube connection failed: {error}")


# ============================================================
# AI METADATA GENERATION
# ============================================================

def metadata_prompt(slot, lesson, config):
    programme = slot["key"]

    if programme == "bhagavadgita":
        subject = "श्रीमद्भगवद्गीता"
    else:
        subject = "शिव पुराण"

    return f"""
आप एक अनुभवी हिन्दी धार्मिक सामग्री लेखक और YouTube SEO विशेषज्ञ हैं।

कार्यक्रम: {subject}
पाठ का नाम: {lesson['title']}
अध्याय: {lesson.get('chapter') or 'लागू नहीं'}
विवरण: {lesson.get('meaning', '')}

चैनल: {CHANNEL_NAME}
वक्ता: {AUTHOR}
वेबसाइट: {WEBSITE}
YouTube: {CHANNEL_URL}

एक मौलिक, शुद्ध हिन्दी YouTube Live शीर्षक, विवरण और टैग तैयार करें।

नियम:
1. शीर्षक आकर्षक, धार्मिक और विषय के अनुरूप हो।
2. शीर्षक में पाठ का वास्तविक नाम अवश्य रहे।
3. विवरण में पाठ का संक्षिप्त परिचय हो।
4. चैनल और वक्ता का नाम शामिल करें।
5. वेबसाइट और YouTube लिंक शामिल करें।
6. गलत धार्मिक तथ्य या काल्पनिक श्लोक न जोड़ें।
7. Tags हिन्दी और English दोनों में दें।
8. Tags की संख्या 15 से 25 के बीच रखें।
9. उत्तर केवल वैध JSON में दें।

JSON प्रारूप:
{{
  "title": "YouTube शीर्षक",
  "description": "पूरा विवरण",
  "tags": ["टैग 1", "टैग 2"]
}}
"""


def clean_metadata(data, slot, lesson):
    if not isinstance(data, dict):
        raise ValueError("AI response JSON object नहीं है।")

    title = str(data.get("title", "")).strip()
    description = str(data.get("description", "")).strip()
    tags = data.get("tags", [])

    if not title or not description:
        raise ValueError("Title या description खाली है।")

    if not isinstance(tags, list):
        raise ValueError("Tags सूची के रूप में नहीं हैं।")

    tags = [
        str(tag).strip()
        for tag in tags
        if str(tag).strip()
    ]

    tags = list(dict.fromkeys(tags))[:30]

    if not tags:
        tags = [
            "Jyotirling Shivmath",
            "Mahant Rajesh Kumar Tiwari",
            lesson["title"],
            "Sanatan Dharma",
            "Hindi Pravachan"
        ]

    footer = (
        f"\n\nवक्ता: {AUTHOR}"
        f"\nचैनल: {CHANNEL_NAME}"
        f"\nवेबसाइट: {WEBSITE}"
        f"\nYouTube: {CHANNEL_URL}"
    )

    if WEBSITE not in description:
        description += footer

    return {
        "title": title[:100],
        "description": description,
        "tags": tags
    }


def request_openai(prompt):
    api_key = os.environ.get("OPENAI_API_KEY")

    if not api_key:
        raise RuntimeError("OPENAI_API_KEY उपलब्ध नहीं है।")

    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You create accurate Hindi devotional "
                    "YouTube metadata. Return valid JSON only."
                )
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        "response_format": {
            "type": "json_object"
        },
        "temperature": 0.7
    }

    request = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        },
        method="POST"
    )

    with urllib.request.urlopen(request, timeout=60) as response:
        result = json.loads(response.read().decode("utf-8"))

    content = result["choices"][0]["message"]["content"]
    return json.loads(content)


def request_gemini(prompt):
    api_key = os.environ.get("GEMINI_API_KEY")

    if not api_key:
        raise RuntimeError("GEMINI_API_KEY उपलब्ध नहीं है।")

    if genai is None:
        raise RuntimeError("Gemini package उपलब्ध नहीं है।")

    client = genai.Client(api_key=api_key)

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt
    )

    content = (response.text or "").strip()

    content = re.sub(
        r"^```(?:json)?\s*|\s*```$",
        "",
        content,
        flags=re.IGNORECASE
    )

    return json.loads(content)


def fallback_metadata(slot, lesson):
    programme = slot["key"]

    if programme == "bhagavadgita":
        subject = "श्रीमद्भगवद्गीता"
    else:
        subject = "शिव पुराण"

    title = f"{subject} | {lesson['title']} | Hindi Live Katha"

    description = f"""
{subject} का पावन पाठ एवं आध्यात्मिक विवेचन।

आज का विषय: {lesson['title']}

{lesson.get('meaning', '')}

वक्ता: {AUTHOR}
चैनल: {CHANNEL_NAME}

भगवान शिव एवं सनातन धर्म से संबंधित धार्मिक कथाओं,
प्रवचनों और आध्यात्मिक ज्ञान के लिए हमारे चैनल से जुड़ें।

वेबसाइट: {WEBSITE}
YouTube: {CHANNEL_URL}
"""

    tags = [
        "Jyotirling Shivmath",
        "Mahant Rajesh Kumar Tiwari",
        "Sanatan Dharma",
        "Hindi Live",
        "Shiv Puran",
        "Bhagavad Gita",
        "Hindi Katha",
        "Spiritual Discourse",
        lesson["title"]
    ]

    return {
        "title": title[:100],
        "description": description.strip(),
        "tags": tags
    }


def generate_metadata(slot, lesson, config):
    prompt = metadata_prompt(slot, lesson, config)

    for attempt in range(1, MAX_AI_ATTEMPTS + 1):
        try:
            log(f"OpenAI से metadata बनाया जा रहा है ({attempt})...")
            data = request_openai(prompt)
            return clean_metadata(data, slot, lesson)

        except Exception as error:
            log(f"OpenAI metadata failed: {error}")

    for attempt in range(1, MAX_AI_ATTEMPTS + 1):
        try:
            log(f"Gemini backup इस्तेमाल हो रहा है ({attempt})...")
            data = request_gemini(prompt)
            return clean_metadata(data, slot, lesson)

        except Exception as error:
            log(f"Gemini metadata failed: {error}")

    log("दोनों AI सेवाएँ उपलब्ध नहीं हैं। Static metadata इस्तेमाल होगा।")

    return fallback_metadata(slot, lesson)


# ============================================================
# PROGRAMME AND LESSON SELECTION
# ============================================================

def get_slot(config, programme):
    for slot in config["schedule"]:
        if slot.get("key") == programme:
            return slot

    fail(f"config.json में programme '{programme}' नहीं मिला।")


def get_next_lesson(slot_key, config, progress):
    lessons = config["lessons"].get(slot_key, [])

    if not lessons:
        fail(f"{slot_key} के लिए lessons उपलब्ध नहीं हैं।")

    # Title cursor पुराने integer progress से अधिक विश्वसनीय है।
    next_title = progress.get(f"{slot_key}_next_title")

    if next_title:
        for index, item in enumerate(lessons):
            lesson = normalise_lesson(item)

            if lesson["title"] == next_title:
                return index, lesson

        log(
            f"Saved next title नहीं मिला: {next_title}. "
            "Integer progress इस्तेमाल होगा।"
        )

    index = int(progress.get(slot_key, 0))

    if index < 0 or index >= len(lessons):
        fail(
            f"{slot_key} की progress सीमा से बाहर है: {index}"
        )

    return index, normalise_lesson(lessons[index])


def advance_progress(slot_key, index, config, progress):
    lessons = config["lessons"].get(slot_key, [])
    next_index = index + 1

    if next_index >= len(lessons):
        next_index = 0
        log(f"{slot_key}: पूरी सूची समाप्त हुई; क्रम फिर शुरू होगा।")

    progress[slot_key] = next_index

    if lessons:
        next_lesson = normalise_lesson(lessons[next_index])
        progress[f"{slot_key}_next_title"] = next_lesson["title"]


# ============================================================
# BROADCAST LOOKUP AND DUPLICATE PROTECTION
# ============================================================

def get_saved_broadcast(progress, programme, date_string):
    broadcasts = progress.get("broadcasts", {})
    programme_data = broadcasts.get(programme, {})
    return programme_data.get(date_string)


def save_broadcast_record(
    progress,
    programme,
    date_string,
    broadcast_id,
    lesson_title,
    scheduled_time
):
    if "broadcasts" not in progress:
        progress["broadcasts"] = {}

    if programme not in progress["broadcasts"]:
        progress["broadcasts"][programme] = {}

    progress["broadcasts"][programme][date_string] = {
        "id": broadcast_id,
        "lesson": lesson_title,
        "scheduled_time": scheduled_time.isoformat()
    }


def find_existing_broadcast(youtube, scheduled_time):
    response = youtube.liveBroadcasts().list(
        part="id,snippet,status",
        broadcastStatus="upcoming",
        maxResults=50
    ).execute()

    target = scheduled_time.astimezone(
        TIMEZONE
    ).replace(microsecond=0)

    for broadcast in response.get("items", []):
        value = broadcast.get("snippet", {}).get(
            "scheduledStartTime"
        )

        if not value:
            continue

        existing = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        ).astimezone(TIMEZONE).replace(microsecond=0)

        if existing == target:
            return broadcast

    return None


def get_broadcast_by_id(youtube, broadcast_id):
    response = youtube.liveBroadcasts().list(
        part="id,snippet,status",
        id=broadcast_id
    ).execute()

    items = response.get("items", [])

    return items[0] if items else None


# ============================================================
# UPDATE EXISTING BROADCAST TIME
# ============================================================

def update_existing_broadcast_time(
    youtube,
    broadcast_id,
    new_time
):
    broadcast = get_broadcast_by_id(
        youtube,
        broadcast_id
    )

    if not broadcast:
        fail("दिया गया पुराना Broadcast ID YouTube पर नहीं मिला।")

    status = broadcast.get("status", {}).get(
        "lifeCycleStatus"
    )

    if status != "ready":
        fail(
            "इस Broadcast का समय नहीं बदला जा सकता। "
            f"वर्तमान स्थिति: {status}"
        )

    snippet = broadcast.get("snippet", {}).copy()

    snippet["scheduledStartTime"] = (
        new_time.astimezone(TIMEZONE)
        .isoformat()
    )

    body = {
        "id": broadcast_id,
        "snippet": snippet
    }

    youtube.liveBroadcasts().update(
        part="snippet",
        body=body
    ).execute()

    log(
        "उसी पुराने Broadcast का समय बदल दिया गया: "
        + new_time.strftime("%d-%m-%Y %I:%M %p")
    )

    log(
        "Broadcast ID वही रखा गया है: "
        + broadcast_id
    )

    return broadcast_id


# ============================================================
# YOUTUBE TAG VERIFICATION
# ============================================================

def verify_and_fix_tags(youtube, video_id, desired_tags):
    for attempt in range(1, 4):
        try:
            response = youtube.videos().list(
                part="snippet",
                id=video_id
            ).execute()

            items = response.get("items", [])

            if not items:
                log("Video अभी videos.list में उपलब्ध नहीं है।")
                time.sleep(3)
                continue

            snippet = items[0]["snippet"]
            saved_tags = snippet.get("tags", [])

            if saved_tags:
                log(
                    f"YouTube पर {len(saved_tags)} tags सुरक्षित हैं।"
                )
                return True

            log("YouTube पर tags नहीं मिले; दोबारा save कर रहे हैं।")

            snippet["tags"] = desired_tags

            youtube.videos().update(
                part="snippet",
                body={
                    "id": video_id,
                    "snippet": snippet
                }
            ).execute()

            check = youtube.videos().list(
                part="snippet",
                id=video_id
            ).execute()

            check_items = check.get("items", [])

            if check_items and check_items[0]["snippet"].get("tags"):
                log("Tags दोबारा जाँचने पर सुरक्षित मिले।")
                return True

        except Exception as error:
            log(f"Tags verification attempt {attempt}: {error}")

        time.sleep(3)

    log("चेतावनी: Tags की पुष्टि नहीं हो सकी।")
    return False


# ============================================================
# PLAYLIST ASSIGNMENT
# ============================================================

def ensure_playlist_assignment(
    youtube,
    playlist_id,
    video_id
):
    if not playlist_id:
        log("Playlist ID नहीं है; playlist assignment छोड़ा गया।")
        return False

    try:
        response = youtube.playlistItems().list(
            part="contentDetails",
            playlistId=playlist_id,
            videoId=video_id,
            maxResults=5
        ).execute()

        if response.get("items"):
            log("Video पहले से playlist में मौजूद है।")
            return True

        youtube.playlistItems().insert(
            part="snippet",
            body={
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {
                        "kind": "youtube#video",
                        "videoId": video_id
                    }
                }
            }
        ).execute()

        log("Video संबंधित YouTube playlist में जोड़ दिया गया।")
        return True

    except Exception as error:
        log(f"Playlist assignment में समस्या: {error}")
        return False


# ============================================================
# CREATE NEW BROADCAST
# ============================================================

def create_broadcast(
    youtube,
    slot,
    lesson,
    lesson_index,
    scheduled_time,
    config,
    progress,
    progress_sha
):
    programme = slot["key"]
    date_string = scheduled_time.strftime("%Y-%m-%d")

    # पहले से saved ID हो तो नया broadcast न बनाएँ।
    saved = get_saved_broadcast(
        progress,
        programme,
        date_string
    )

    if saved:
        saved_id = (
            saved.get("id")
            if isinstance(saved, dict)
            else saved
        )

        existing = get_broadcast_by_id(
            youtube,
            saved_id
        )

        if existing:
            log(
                "इस तारीख का Broadcast पहले से मौजूद है। "
                "नया Broadcast नहीं बनाया जाएगा।"
            )
            return saved_id

    # उसी समय का broadcast पहले से मौजूद हो तो duplicate न बनाएँ।
    existing = find_existing_broadcast(
        youtube,
        scheduled_time
    )

    if existing:
        broadcast_id = existing["id"]

        log(
            "इसी समय का Broadcast पहले से मौजूद है: "
            + broadcast_id
        )

        save_broadcast_record(
            progress,
            programme,
            date_string,
            broadcast_id,
            lesson["title"],
            scheduled_time
        )

        save_progress(progress, progress_sha)

        return broadcast_id

    metadata = generate_metadata(
        slot,
        lesson,
        config
    )

    privacy_status = config.get(
        "privacy_status",
        "private"
    )

    body = {
        "snippet": {
            "title": metadata["title"],
            "description": metadata["description"],
            "tags": metadata["tags"],
            "scheduledStartTime": scheduled_time.isoformat(),
            "categoryId": "22"
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False
        },
        "contentDetails": {
            "enableAutoStart": False,
            "enableAutoStop": False
        }
    }

    response = youtube.liveBroadcasts().insert(
        part="snippet,status,contentDetails",
        body=body
    ).execute()

    broadcast_id = response["id"]

    log("नया YouTube Live Broadcast बन गया।")
    log("Broadcast ID: " + broadcast_id)

    # Broadcast और lesson progress सुरक्षित करें।
    save_broadcast_record(
        progress,
        programme,
        date_string,
        broadcast_id,
        lesson["title"],
        scheduled_time
    )

    advance_progress(
        programme,
        lesson_index,
        config,
        progress
    )

    # Progress पहले सुरक्षित करें, ताकि playlist की समस्या से
    # दोबारा नया broadcast न बन जाए।
    save_progress(progress, progress_sha)

    # Tags की जाँच।
    verify_and_fix_tags(
        youtube,
        broadcast_id,
        metadata["tags"]
    )

    # Playlist में जोड़ें।
    playlist_id = slot.get("playlist_id")

    if not playlist_id:
        playlist_id = config.get(
            "playlists", {}
        ).get(programme)

    ensure_playlist_assignment(
        youtube,
        playlist_id,
        broadcast_id
    )

    log("YouTube Live URL:")
    log(f"https://www.youtube.com/watch?v={broadcast_id}")

    log(
        "ध्यान दें: प्रसारण स्वतः शुरू नहीं होगा। "
        "आपको फोन से इसे manually शुरू करना है।"
    )

    return broadcast_id


# ============================================================
# SCHEDULE SELECTION
# ============================================================

def select_default_schedule(config, now):
    """
    Automatic run में वर्तमान समय के अनुसार programme चुनें।
    Default schedule अगले दिन के लिए बनेगा।
    """

    candidates = []

    for slot in config["schedule"]:
        hour = int(slot.get("hour", 7))
        minute = int(slot.get("minute", 0))

        scheduled_today = now.replace(
            hour=hour,
            minute=minute,
            second=0,
            microsecond=0
        )

        difference = abs(
            (now - scheduled_today).total_seconds()
        )

        candidates.append((difference, slot))

    candidates.sort(key=lambda item: item[0])

    return candidates[0][1]


# ============================================================
# MAIN
# ============================================================

def main():
    log("Jyotirling Shivmath Live Scheduler शुरू हो रहा है।")

    config = load_config()

    now = datetime.now(TIMEZONE)

    programme = os.environ.get(
        "PROGRAMME",
        ""
    ).strip().lower()

    action = os.environ.get(
        "ACTION",
        "create"
    ).strip().lower()

    progress, progress_sha = get_progress()

    youtube = get_youtube()

    # --------------------------------------------------------
    # Programme selection
    # --------------------------------------------------------

    if not programme:
        slot = select_default_schedule(config, now)
        programme = slot["key"]
    else:
        slot = get_slot(config, programme)

    log(f"Programme: {programme}")
    log(f"Action: {action}")

    # --------------------------------------------------------
    # UPDATE TIME: existing broadcast only
    # --------------------------------------------------------

    if action == "update_time":
        date_value = os.environ.get(
            "BROADCAST_DATE",
            ""
        ).strip()

        time_value = os.environ.get(
            "ACTUAL_TIME",
            ""
        ).strip()

        if not date_value or not time_value:
            fail(
                "समय बदलने के लिए BROADCAST_DATE और "
                "ACTUAL_TIME दोनों देना आवश्यक है।"
            )

        new_time = parse_date_time(
            date_value,
            time_value
        )

        saved = get_saved_broadcast(
            progress,
            programme,
            date_value
        )

        broadcast_id = os.environ.get(
            "BROADCAST_ID",
            ""
        ).strip()

        if not broadcast_id and saved:
            broadcast_id = (
                saved.get("id")
                if isinstance(saved, dict)
                else saved
            )

        if not broadcast_id:
            fail(
                "इस तारीख का पुराना Broadcast ID नहीं मिला। "
                "नया Broadcast बनाने के बजाय पहले सही तारीख जाँचें।"
            )

        update_existing_broadcast_time(
            youtube,
            broadcast_id,
            new_time
        )

        if saved and isinstance(saved, dict):
            saved["scheduled_time"] = new_time.isoformat()

        save_progress(progress, progress_sha)

        log("समय बदलने का कार्य पूरा हुआ।")
        return

    # --------------------------------------------------------
    # CREATE: new broadcast
    # --------------------------------------------------------

    if action != "create":
        fail(
            "ACTION केवल create या update_time हो सकता है।"
        )

    date_value = os.environ.get(
        "BROADCAST_DATE",
        ""
    ).strip()

    time_value = os.environ.get(
        "ACTUAL_TIME",
        ""
    ).strip()

    if date_value and time_value:
        scheduled_time = parse_date_time(
            date_value,
            time_value
        )
    elif date_value or time_value:
        fail(
            "तारीख और समय दोनों दें, या दोनों खाली छोड़ें।"
        )
    else:
        scheduled_time = (
            now + timedelta(days=1)
        ).replace(
            hour=int(slot.get("hour", 7)),
            minute=int(slot.get("minute", 0)),
            second=0,
            microsecond=0
        )

    if scheduled_time <= now:
        fail(
            "चुना गया समय वर्तमान समय से आगे होना चाहिए।"
        )

    lesson_index, lesson = get_next_lesson(
        programme,
        config,
        progress
    )

    log("अगला पाठ: " + lesson["title"])

    log(
        "निर्धारित समय: "
        + scheduled_time.strftime("%d-%m-%Y %I:%M %p")
    )

    create_broadcast(
        youtube,
        slot,
        lesson,
        lesson_index,
        scheduled_time,
        config,
        progress,
        progress_sha
    )

    log("Scheduler का कार्य पूरा हुआ।")


if __name__ == "__main__":
    try:
        main()

    except HttpError as error:
        fail(f"YouTube API error: {error}")

    except Exception as error:
        fail(f"Unexpected error: {error}")
