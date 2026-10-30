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
from googleapiclient.http import MediaFileUpload

from PIL import Image, ImageDraw, ImageFont, ImageOps

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

IMAGE_MODEL = os.environ.get(
    "GEMINI_IMAGE_MODEL",
    "gemini-3.1-flash-image"
)

MASTER_THUMBNAIL = "karmsanyasyog.jpeg"
THUMBNAIL_OUTPUT = "generated_thumbnail.jpg"

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
            date_value,
            "%Y-%m-%d"
        ).date()

        time_part = datetime.strptime(
            time_value,
            "%H:%M"
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
        data = json.dumps(
            payload,
            ensure_ascii=False
        ).encode("utf-8")

        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=30
        ) as response:

            body = response.read().decode("utf-8")

            return json.loads(body) if body else {}

    except urllib.error.HTTPError as error:

        body = error.read().decode(
            "utf-8",
            errors="replace"
        )

        raise RuntimeError(
            f"GitHub API error {error.code}: {body}"
        )

    except Exception as error:
        raise RuntimeError(
            f"GitHub connection error: {error}"
        )


def get_progress():

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
        log(
            f"GitHub progress पढ़ने में समस्या: {error}"
        )

    if os.path.exists(PROGRESS_FILE):

        try:
            with open(
                PROGRESS_FILE,
                "r",
                encoding="utf-8"
            ) as file:

                return json.load(file), None

        except Exception as error:
            log(
                f"स्थानीय progress पढ़ने में समस्या: {error}"
            )

    return {}, None


def save_progress(progress, sha=None):

    raw = json.dumps(progress, ensure_ascii=False, indent=2)
    with open(PROGRESS_FILE, "w", encoding="utf-8") as file:
        file.write(raw)

    repository = os.environ.get("GITHUB_REPOSITORY")
    if not repository or not os.environ.get("GITHUB_TOKEN"):
        log("Progress स्थानीय फाइल में सुरक्षित की गई।")
        return

    current_sha = sha
    for attempt in range(1, 4):
        if attempt > 1 or not current_sha:
            latest = github_request(f"contents/{PROGRESS_FILE}")
            if latest and latest.get("content"):
                latest_raw = base64.b64decode(latest["content"]).decode("utf-8")
                latest_progress = json.loads(latest_raw)
                merged = dict(latest_progress)
                merged.update(progress)
                merged_broadcasts = dict(latest_progress.get("broadcasts", {}))
                for programme, records in progress.get("broadcasts", {}).items():
                    combined = dict(merged_broadcasts.get(programme, {}))
                    combined.update(records)
                    merged_broadcasts[programme] = combined
                if "broadcasts" in progress:
                    merged["broadcasts"] = merged_broadcasts
                progress.clear()
                progress.update(merged)
                raw = json.dumps(progress, ensure_ascii=False, indent=2)
                current_sha = latest.get("sha")

        payload = {
            "message": "Update Jyotirling Shivmath live progress",
            "content": base64.b64encode(raw.encode("utf-8")).decode("utf-8")
        }
        if current_sha:
            payload["sha"] = current_sha

        try:
            github_request(f"contents/{PROGRESS_FILE}", method="PUT", payload=payload)
            with open(PROGRESS_FILE, "w", encoding="utf-8") as file:
                file.write(raw)
            log("Progress GitHub पर सुरक्षित कर दी गई।")
            return
        except Exception as error:
            if "409" not in str(error) or attempt == 3:
                log(f"GitHub पर progress सुरक्षित नहीं हुई: {error}")
                raise
            log(f"Progress SHA बदल गई; नवीनतम फ़ाइल लेकर पुनः प्रयास ({attempt}/3)।")
            current_sha = None


# AUTOMATIC THUMBNAIL GENERATION
# ============================================================

def find_master_thumbnail(config):
    """
    config.json में दिए गए master thumbnail को खोजता है।
    JPEG और PNG दोनों स्वीकार किए जाते हैं।
    """
    configured = (
        config.get("thumbnail_file")
        or os.environ.get("THUMBNAIL_MASTER_FILE")
    )

    candidates = []

    if configured:
        candidates.append(configured)

    candidates.extend([
        "karmsanyasyog.jpeg",
        "karmsanyasyog.png",
        "master_thumbnail.jpeg",
        "master_thumbnail.png",
    ])

    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            log(f"Master thumbnail मिल गई: {candidate}")
            return candidate

    raise FileNotFoundError(
        "Master thumbnail नहीं मिली। "
        "karmsanyasyog.jpeg को GitHub repository के root में रखें।"
    )


def make_shiv_background(lesson):
    """Generate thumbnail artwork with sequential AI fallback: Cloudflare, GPT, Gemini."""
    prompt = f'''
Create a high-quality devotional Indian mythological painting for a YouTube thumbnail background in widescreen 16:9 format.
Depict this specific episode from the Shiva Purana: {lesson.get('title', 'Shiv Puran episode')}
Show the important divine event and characters accurately according to the episode.
Traditional Indian devotional painting, cinematic divine lighting, tasteful saffron, gold, deep blue and Himalayan tones.
Keep important action mainly in the central and right portions. Keep the left calm for the fixed portrait frame and lower portion uncluttered for the title plaque.
NO text, letters, captions, logos, watermark, border, portrait photograph or modern objects. Generate background artwork only.
'''
    # 1) Cloudflare Workers AI — FLUX.1 Schnell
    cloudflare_token = os.environ.get("CLOUDFLARE_API_TOKEN")
    cloudflare_account = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if cloudflare_token and cloudflare_account:
        try:
            model = os.environ.get("CLOUDFLARE_IMAGE_MODEL", "@cf/black-forest-labs/flux-1-schnell")
            endpoint = f"https://api.cloudflare.com/client/v4/accounts/{cloudflare_account}/ai/run/{model}"
            request = urllib.request.Request(endpoint, data=json.dumps({"prompt":prompt,"steps":8}).encode("utf-8"), headers={"Authorization":f"Bearer {cloudflare_token}","Content-Type":"application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=180) as response:
                result = json.loads(response.read().decode("utf-8"))
            image_data = result.get("result", {}).get("image")
            if not image_data:
                raise RuntimeError("Cloudflare response में image data नहीं मिला")
            output = "shivpuran_ai_background.jpg"
            with open(output, "wb") as image_file:
                image_file.write(base64.b64decode(image_data))
            if os.path.getsize(output) <= 20000:
                raise RuntimeError("Cloudflare image file अधूरी है")
            log(f"पहला AI सफल: Cloudflare Workers AI {model}")
            return output
        except Exception as error:
            log(f"पहला AI Cloudflare असफल; अब OpenAI GPT image fallback: {error}")
    else:
        log("Cloudflare credentials अनुपलब्ध; अब OpenAI GPT image आजमाएँगे।")
    # 2) OpenAI GPT Image
    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key:
        try:
            model = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1")
            payload = {"model":model,"prompt":prompt,"size":"1536x1024","quality":"medium","output_format":"png"}
            request = urllib.request.Request("https://api.openai.com/v1/images/generations", data=json.dumps(payload).encode("utf-8"), headers={"Authorization":f"Bearer {openai_key}","Content-Type":"application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=180) as response:
                result = json.loads(response.read().decode("utf-8"))
            image_data = result.get("data", [{}])[0].get("b64_json")
            if not image_data:
                raise RuntimeError("OpenAI API ने image data नहीं लौटाया")
            output = "shivpuran_ai_background.png"
            with open(output, "wb") as image_file:
                image_file.write(base64.b64decode(image_data))
            if os.path.getsize(output) <= 20000:
                raise RuntimeError("OpenAI image file अधूरी है")
            log(f"दूसरा AI सफल: OpenAI GPT Image {model}")
            return output
        except Exception as error:
            log(f"दूसरा AI OpenAI GPT असफल; अब Gemini image fallback: {error}")
    else:
        log("OPENAI_API_KEY अनुपलब्ध; अब Gemini image आजमाएँगे।")
    # 3) Google Gemini Image
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if gemini_key and genai is not None:
        try:
            model = os.environ.get("GEMINI_IMAGE_MODEL", IMAGE_MODEL)
            client = genai.Client(api_key=gemini_key)
            response = client.models.generate_content(model=model, contents=[prompt], config={"response_modalities":["TEXT","IMAGE"],"image_config":{"aspect_ratio":"16:9","image_size":"2K"}})
            for part in response.parts:
                if getattr(part, "inline_data", None) is not None:
                    generated = part.as_image()
                    output = "shivpuran_ai_background.png"
                    generated.save(output)
                    if os.path.isfile(output) and os.path.getsize(output) > 20000:
                        log(f"तीसरा AI सफल: Gemini Image {model}")
                        return output
            raise RuntimeError("Gemini से image output प्राप्त नहीं हुआ")
        except Exception as error:
            log(f"तीसरा AI Gemini भी असफल: {error}")
    else:
        log("Gemini API या google-genai package अनुपलब्ध है।")
    raise RuntimeError("Cloudflare, OpenAI GPT और Gemini — तीनों से thumbnail background नहीं बन सका।")
def find_devanagari_font():
    """
    Hindi text के लिए Noto Devanagari font खोजता है।
    """
    candidates = [
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansDevanagari-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]

    for path in candidates:
        if os.path.isfile(path):
            return path

    raise FileNotFoundError(
        "Hindi font नहीं मिला। daily-live.yml में fonts-noto-core "
        "install करना आवश्यक है।"
    )


def fit_font(
    draw,
    text,
    font_path,
    max_width,
    start_size=76,
    min_size=34
):
    """
    दिए गए स्थान में पूरा शीर्षक फिट करने के लिए
    उपयुक्त font size चुनता है।
    """
    for size in range(start_size, min_size - 1, -2):
        font = ImageFont.truetype(
            font_path,
            size=size,
            layout_engine=ImageFont.Layout.RAQM
        )

        box = draw.textbbox(
            (0, 0),
            text,
            font=font,
            language="hi"
        )

        if box[2] - box[0] <= max_width:
            return font

    return ImageFont.truetype(
        font_path,
        size=min_size,
        layout_engine=ImageFont.Layout.RAQM
    )


def get_thumbnail_title(programme, lesson, lesson_index):
    """
    Thumbnail पर दिखने वाला अध्याय/प्रसंग शीर्षक तैयार करता है।
    """
    title = lesson.get("title", "धार्मिक प्रवचन").strip()
    chapter = lesson.get("chapter")

    if chapter is None:
        match = re.search(
            r"(?:अध्याय|अध्याय संख्या|Adhyay|Chapter)\s*[-:]?\s*(\d+)",
            title,
            re.I
        )

        if match:
            chapter = match.group(1)

    if programme == "bhagavadgita":
        if chapter is None:
            chapter = lesson_index + 1

        clean_title = re.sub(
            r"^(?:अध्याय\s*)?\d+\s*[:：.-]?\s*",
            "",
            title
        ).strip()

        return f"अध्याय {chapter}: {clean_title}"

    clean_title = re.sub(
        r"^(?:अध्याय\s*)?\d+\s*[:：.-]?\s*",
        "",
        title
    ).strip()

    if chapter is not None:
        return f"अध्याय {chapter}: {clean_title}"

    return clean_title


def create_thumbnail(
    master_path,
    programme,
    lesson,
    lesson_index,
    config
):
    """
    Master thumbnail से नया thumbnail तैयार करता है।

    Bhagavad Gita:
    Master का Krishna-Arjuna background रखा जाता है।

    Shiv Puran:
    AI से नया प्रसंग-आधारित background बनाने का प्रयास होता है।
    AI विफल होने पर master background ही रखा जाता है।
    """

    base = Image.open(master_path).convert("RGB")

    base = ImageOps.fit(
        base,
        (1280, 720),
        method=Image.Resampling.LANCZOS
    )

    # शिव पुराण के लिए नया AI background
    if programme == "shivpuran":
        try:
            background_path = make_shiv_background(lesson)

            generated = Image.open(background_path).convert("RGB")

            # Keep the portrait/header and the complete lower title/author
            # plaques untouched. Replace only the central/right story artwork.
            generated = ImageOps.fit(
                generated,
                (860, 410),
                method=Image.Resampling.LANCZOS
            )
            base.paste(generated, (420, 78))

            log("शिव पुराण का AI दृश्य thumbnail में लगाया गया।")

        except Exception as error:
            log(
                "AI background उपलब्ध नहीं हुआ। "
                f"Master background रखा जाएगा: {error}"
            )

    draw = ImageDraw.Draw(base)

    font_path = find_devanagari_font()

    title = get_thumbnail_title(
        programme,
        lesson,
        lesson_index
    )

    # अध्याय शीर्षक
    # Cover the full original title plaque, including old master text.
    plaque = (410, 480, 1248, 620)

    draw.rounded_rectangle(
        plaque,
        radius=17,
        fill=(2, 14, 35),
        outline=(246, 190, 35),
        width=3
    )

    font = fit_font(
        draw,
        title,
        font_path,
        max_width=800,
        start_size=61,
        min_size=31
    )

    bbox = draw.textbbox(
        (0, 0),
        title,
        font=font,
        language="hi",
        stroke_width=2
    )

    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]

    x = plaque[0] + (
        plaque[2] - plaque[0] - text_width
    ) // 2

    y = plaque[1] + (
        plaque[3] - plaque[1] - text_height
    ) // 2 - bbox[1]

    draw.text(
        (x, y),
        title,
        font=font,
        fill=(255, 218, 65),
        stroke_width=2,
        stroke_fill=(105, 34, 0),
        language="hi"
    )

    # शिव पुराण के लिए footer बदलना
    if programme == "shivpuran":
        # Replace the Gita footer text while keeping the outer thumbnail frame.
        draw.rectangle((500, 625, 1160, 681), fill=(20, 13, 12))
        footer = "SHIV MAHA PURAN"
        # Latin font is required for English footer; Devanagari font renders squares.
        latin_candidates = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"]
        latin_path = next((p for p in latin_candidates if os.path.isfile(p)), None)
        if not latin_path:
            raise FileNotFoundError("English footer font नहीं मिला।")
        footer_font = ImageFont.truetype(latin_path, 34)
        footer_box = draw.textbbox((0, 0), footer, font=footer_font)
        footer_width = footer_box[2] - footer_box[0]
        footer_x = (1280 - footer_width) // 2
        draw.text(
            (footer_x, 634), footer, font=footer_font,
            fill=(255, 218, 65), stroke_width=1,
            stroke_fill=(65, 30, 0)
        )

    output = THUMBNAIL_OUTPUT

    base.save(
        output,
        "JPEG",
        quality=94,
        optimize=True
    )

    log(f"Thumbnail तैयार: {output}")

    return output


def upload_thumbnail(youtube, video_id, image_path):
    """
    तैयार thumbnail को YouTube broadcast पर upload करता है।
    """
    youtube.thumbnails().set(
        videoId=video_id,
        media_body=MediaFileUpload(
            image_path,
            mimetype="image/jpeg",
            resumable=True
        )
    ).execute()

    log("Thumbnail YouTube Live पर upload हो गया।")
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
    for attempt in range(1, 9):
        try:
            response = youtube.videos().list(part="snippet", id=video_id).execute()
            items = response.get("items", [])
            if not items:
                log(f"Video metadata अभी उपलब्ध नहीं है; पुनः प्रयास {attempt}/8।")
                time.sleep(5)
                continue

            snippet = dict(items[0]["snippet"])
            saved_tags = snippet.get("tags", [])
            if saved_tags and set(desired_tags).issubset(set(saved_tags)):
                log(f"YouTube पर {len(saved_tags)} tags सुरक्षित और सत्यापित हैं।")
                return True

            snippet["tags"] = desired_tags
            youtube.videos().update(
                part="snippet",
                body={"id": video_id, "snippet": snippet}
            ).execute()

            time.sleep(2)
            check = youtube.videos().list(part="snippet", id=video_id).execute()
            check_items = check.get("items", [])
            if check_items and set(desired_tags).issubset(set(check_items[0]["snippet"].get("tags", []))):
                log("Tags YouTube पर सुरक्षित और सत्यापित हो गए।")
                return True
            log(f"Tags अभी सत्यापित नहीं हुए; पुनः प्रयास {attempt}/8।")
        except Exception as error:
            log(f"Tags verification attempt {attempt}/8: {error}")
        time.sleep(5)

    log("चेतावनी: 8 प्रयासों के बाद भी YouTube Tags सत्यापित नहीं हो सके।")
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
# GEMINI SEO METADATA GENERATION
# ============================================================

def generate_youtube_metadata(programme, lesson):
    """Try metadata providers in order: Cloudflare AI, OpenAI GPT, Gemini."""
    source_title = str(lesson.get("title", "धार्मिक प्रवचन")).strip()
    programme_name = "शिव महापुराण" if programme == "shivpuran" else "श्रीमद्भगवद्गीता"
    suffix = "|Mahant Rajesh Kumar Tiwari"
    prompt = f'''
आप हिंदी devotional YouTube चैनल के SEO शोधकर्ता हैं।
कार्यक्रम: {programme_name}
मूल पाठ/प्रसंग: {source_title}
प्रस्तुतकर्ता: {AUTHOR}
चैनल: {CHANNEL_NAME}
वेबसाइट: {WEBSITE}
प्रसंग का अर्थ न बदलें और शास्त्रीय तथ्य न गढ़ें।
title अधिकतम 100 अक्षर का हो और अंत में ठीक "|Mahant Rajesh Kumar Tiwari" हो।
description हिंदी में 2-4 अनुच्छेद हों।
15-20 tags बिना # के और 3-5 hashtags # सहित दें।
केवल JSON लौटाएं: {{"title":"...","description":"...","tags":["..."],"hashtags":["#..."]}}
'''
    def normalise(data, provider):
        title = str(data.get("title", "")).strip()
        title = re.sub(r"\s*\|\s*Mahant Rajesh Kumar Tiwari\s*$", "", title, flags=re.I).strip()
        title = title[:100-len(suffix)-1].rstrip() + " " + suffix
        description = str(data.get("description", "")).strip()
        tags = [str(x).strip().lstrip("#") for x in data.get("tags", []) if str(x).strip()]
        hashtags = [str(x).strip() if str(x).strip().startswith("#") else "#" + str(x).strip().replace(" ", "") for x in data.get("hashtags", []) if str(x).strip()]
        if not description or not tags:
            raise ValueError("AI description या tags खाली हैं")
        clean_tags = []
        total = 0
        for tag in tags:
            if tag.lower() in [x.lower() for x in clean_tags]:
                continue
            size = len(tag) + (1 if clean_tags else 0)
            if total + size <= 500:
                clean_tags.append(tag)
                total += size
            if len(clean_tags) >= 20:
                break
        if not clean_tags:
            raise ValueError("AI tags खाली हैं")
        description += "\n\n" + " ".join(list(dict.fromkeys(hashtags))[:5])
        description += f"\n\nप्रस्तुति: {AUTHOR}\n{CHANNEL_NAME}\n{WEBSITE}\n{CHANNEL_URL}"
        log(f"{provider}: metadata तैयार।")
        return title, description, clean_tags
    # 1) Cloudflare Workers AI
    cf_token = os.environ.get("CLOUDFLARE_API_TOKEN")
    cf_account = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if cf_token and cf_account:
        try:
            model = os.environ.get("CLOUDFLARE_TEXT_MODEL", "@cf/openai/gpt-oss-120b")
            endpoint = f"https://api.cloudflare.com/client/v4/accounts/{cf_account}/ai/run/{model}"
            payload = {"messages": [{"role":"system","content":"Return only valid JSON in natural Hindi."},{"role":"user","content":prompt}], "max_tokens":1800, "temperature":0.4}
            request = urllib.request.Request(endpoint, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Authorization":f"Bearer {cf_token}","Content-Type":"application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=120) as response:
                result = json.loads(response.read().decode("utf-8"))
            raw = result.get("result", {}).get("response", "")
            if not raw:
                raise ValueError("Cloudflare AI response खाली है")
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I)
            return normalise(json.loads(raw), "Cloudflare Workers AI " + model)
        except Exception as error:
            log(f"Cloudflare AI असफल; अब GPT fallback: {error}")
    else:
        log("Cloudflare credentials नहीं मिले; GPT fallback चलेगा।")
    # 2) OpenAI GPT
    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key:
        try:
            model = os.environ.get("OPENAI_TEXT_MODEL", OPENAI_MODEL)
            payload = {"model":model,"messages":[{"role":"system","content":"Return only valid JSON. You are an accurate Hindi YouTube SEO editor."},{"role":"user","content":prompt}],"response_format":{"type":"json_object"},"temperature":0.4,"max_tokens":1800}
            request = urllib.request.Request("https://api.openai.com/v1/chat/completions", data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Authorization":f"Bearer {openai_key}","Content-Type":"application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=120) as response:
                result = json.loads(response.read().decode("utf-8"))
            raw = result["choices"][0]["message"]["content"]
            return normalise(json.loads(raw), "OpenAI GPT " + model)
        except Exception as error:
            log(f"GPT असफल; अब Gemini fallback: {error}")
    else:
        log("OPENAI_API_KEY नहीं मिला; Gemini fallback चलेगा।")
    # 3) Google Gemini
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if gemini_key:
        try:
            model = os.environ.get("GEMINI_TEXT_MODEL", "gemini-2.5-flash")
            endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}"
            payload = {"contents":[{"parts":[{"text":prompt}]}],"generationConfig":{"temperature":0.4,"responseMimeType":"application/json","maxOutputTokens":1800}}
            request = urllib.request.Request(endpoint, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Content-Type":"application/json"}, method="POST")
            with urllib.request.urlopen(request, timeout=120) as response:
                result = json.loads(response.read().decode("utf-8"))
            raw = result["candidates"][0]["content"]["parts"][0]["text"]
            return normalise(json.loads(raw), "Gemini " + model)
        except Exception as error:
            log(f"Gemini भी असफल; fallback metadata: {error}")
    title = source_title[:100-len(suffix)-1].rstrip() + " " + suffix
    description = f"{source_title}\n\n{programme_name} के इस प्रसंग का आध्यात्मिक विवेचन सुनें।\n\nप्रस्तुति: {AUTHOR}\n{CHANNEL_NAME}\n{WEBSITE}\n{CHANNEL_URL}\n\n#SanatanDharma #Bhakti"
    tags = ["Jyotirling Shivmath", "Mahant Rajesh Kumar Tiwari", "Sanatan Dharma", "Hindi Pravachan", "Bhakti", "Shiv Maha Puran" if programme == "shivpuran" else "Bhagavad Gita", "Hindu Dharma", "Spiritual Discourse"]
    log("तीनों AI उपलब्ध नहीं; सुरक्षित fallback metadata उपयोग हुआ।")
    return title, description, tags

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
            # --------------------------------------------------------
    # नया YouTube Broadcast तैयार करें
    # --------------------------------------------------------

    # हर lesson के लिए Gemini से SEO title, description और tags बनाएं।
    title, description, tags = generate_youtube_metadata(programme, lesson)

    privacy_status = slot.get(
        "privacyStatus",
        "public"
    )

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "scheduledStartTime": scheduled_time.isoformat(),
            "tags": tags,
            "categoryId": "22"
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False
        },
        "contentDetails": {
            "enableAutoStart": False,
            "enableAutoStop": False,
            "enableDvr": True,
            "recordFromStart": True,
            "enableEmbed": True
        }
    }

    response = youtube.liveBroadcasts().insert(
        part="snippet,status,contentDetails",
        body=body
    ).execute()

    broadcast_id = response["id"]

    log("नया YouTube Broadcast बनाया गया।")
    log(f"Broadcast ID: {broadcast_id}")

    # --------------------------------------------------------
    # Broadcast progress सुरक्षित करें
    # --------------------------------------------------------

    save_broadcast_record(
        progress,
        programme,
        date_string,
        broadcast_id,
        lesson["title"],
        scheduled_time
    )

    save_progress(progress, progress_sha)

    # --------------------------------------------------------
    # Thumbnail तैयार करके upload करें
    # --------------------------------------------------------

    try:
        master_path = find_master_thumbnail(config)

        thumbnail_path = create_thumbnail(
            master_path=master_path,
            programme=programme,
            lesson=lesson,
            lesson_index=lesson_index,
            config=config
        )

        upload_thumbnail(
            youtube,
            broadcast_id,
            thumbnail_path
        )

    except Exception as error:
        log(
            "Thumbnail बनाने या upload करने में समस्या आई। "
            "Broadcast सुरक्षित है और प्रक्रिया जारी रहेगी। "
            f"विवरण: {error}"
        )

    # --------------------------------------------------------
    # Tags और Playlist की जाँच
    # --------------------------------------------------------

    verify_and_fix_tags(
        youtube,
        broadcast_id,
        tags
    )

    playlist_id = slot.get("playlist_id")

    ensure_playlist_assignment(
        youtube,
        playlist_id,
        broadcast_id
    )

    # --------------------------------------------------------
    # अगला Lesson सुरक्षित करें
    # --------------------------------------------------------

    advance_progress(
        programme,
        lesson_index,
        config,
        progress
    )

    save_progress(progress, progress_sha)

    log("Broadcast creation प्रक्रिया पूरी हुई।")

    return broadcast_id


# ============================================================
# YOUTUBE AUTHENTICATION
# ============================================================

def get_youtube_service():
    token_json = os.environ.get("YOUTUBE_TOKEN_JSON")

    if not token_json:
        fail("YOUTUBE_TOKEN_JSON GitHub Secret उपलब्ध नहीं है।")

    try:
        token_data = json.loads(token_json)

        credentials = Credentials.from_authorized_user_info(
            token_data,
            SCOPES
        )

        return build(
            "youtube",
            "v3",
            credentials=credentials,
            cache_discovery=False
        )

    except Exception as error:
        fail(f"YouTube authentication विफल: {error}")


# ============================================================
# SCHEDULED TIME
# ============================================================

def get_scheduled_time(slot):
    """Use the hour/minute fields already stored in config.json."""
    now = datetime.now(TIMEZONE)
    try:
        hour = int(slot["hour"])
        minute = int(slot["minute"])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("hour/minute out of range")
    except (KeyError, TypeError, ValueError) as error:
        fail(f"Schedule में hour/minute गलत हैं: {error}")

    scheduled = now.replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    if scheduled <= now:
        scheduled += timedelta(days=1)
    return scheduled


# ============================================================
# MAIN
# ============================================================

def main():
    log("YouTube Live Scheduler शुरू हो रहा है।")

    config = load_config()

    progress, progress_sha = get_progress()

    action = os.environ.get(
        "ACTION",
        "create"
    ).strip().lower()

    programme = os.environ.get(
        "PROGRAMME",
        ""
    ).strip().lower()

    # सुरक्षित इमेज टेस्ट: YouTube API, Broadcast और progress में कोई बदलाव नहीं।
    if action == "test_image":
        if programme not in ("shivpuran", "bhagavadgita"):
            fail("test_image के लिए PROGRAMME shivpuran या bhagavadgita चुनें।")
        slot = get_slot(config, programme)
        lesson_index, lesson = get_next_lesson(programme, config, progress)
        master_path = find_master_thumbnail(config)
        output_path = create_thumbnail(
            master_path, programme, lesson, lesson_index, config
        )
        log(f"सुरक्षित thumbnail test पूरा: {output_path}")
        log("कोई YouTube Broadcast नहीं बनाया गया और progress नहीं बदली गई।")
        return

    youtube = get_youtube_service()

    # --------------------------------------------------------
    # पुराना Broadcast का समय बदलना
    # --------------------------------------------------------

    if action == "update_time":
        broadcast_id = os.environ.get(
            "BROADCAST_ID",
            ""
        ).strip()

        date_text = os.environ.get("BROADCAST_DATE", "").strip()
        actual_time = os.environ.get("ACTUAL_TIME", "").strip()

        if not broadcast_id:
            fail("BROADCAST_ID उपलब्ध नहीं है।")
        if not date_text or not actual_time:
            fail("BROADCAST_DATE और ACTUAL_TIME दोनों आवश्यक हैं।")

        new_time = parse_date_time(date_text, actual_time)

        update_existing_broadcast_time(
            youtube,
            broadcast_id,
            new_time
        )

        return

    # --------------------------------------------------------
    # Programme निर्धारित करें
    # --------------------------------------------------------

    if not programme:
        current_time = datetime.now(TIMEZONE)

        if current_time.hour < 13:
            programme = "shivpuran"
        else:
            programme = "bhagavadgita"

    slot = get_slot(
        config,
        programme
    )

    # --------------------------------------------------------
    # अगला Lesson चुनें
    # --------------------------------------------------------

    lesson_index, lesson = get_next_lesson(
        programme,
        config,
        progress
    )

    scheduled_time = get_scheduled_time(slot)

    log(f"Programme: {programme}")
    log(f"Lesson: {lesson['title']}")
    log(
        "Scheduled time: "
        + scheduled_time.strftime("%d-%m-%Y %I:%M %p")
    )

    # --------------------------------------------------------
    # Broadcast बनाएँ
    # --------------------------------------------------------

    broadcast_id = create_broadcast(
        youtube=youtube,
        slot=slot,
        lesson=lesson,
        lesson_index=lesson_index,
        scheduled_time=scheduled_time,
        config=config,
        progress=progress,
        progress_sha=progress_sha
    )

    log("कार्य पूरा हुआ।")
    log(f"YouTube Broadcast ID: {broadcast_id}")

    log(
        "Live अपने मोबाइल से manually शुरू करें। "
        "Scheduler अपने आप Live शुरू नहीं करेगा।"
    )


# ============================================================
# SCRIPT ENTRY POINT
# ============================================================

if __name__ == "__main__":
    try:
        main()

    except Exception as error:
        log(f"Scheduler में गंभीर त्रुटि: {error}")
        raise
