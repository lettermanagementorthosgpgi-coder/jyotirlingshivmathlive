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




def github_upload_binary(path, local_path):
    """Persist a generated image in the repository for the next retry run."""
    with open(local_path, "rb") as image_file:
        encoded = base64.b64encode(image_file.read()).decode("ascii")
    current = None
    try:
        current = github_request(f"contents/{path}")
    except Exception:
        current = None
    payload = {
        "message": f"Save retry thumbnail {path}",
        "content": encoded
    }
    if current and current.get("sha"):
        payload["sha"] = current["sha"]
    github_request(f"contents/{path}", method="PUT", payload=payload)
    log(f"Retry thumbnail GitHub पर सुरक्षित: {path}")
    return path


def github_download_binary(path, local_path):
    result = github_request(f"contents/{path}")
    if not result or not result.get("content"):
        raise RuntimeError(f"Retry image GitHub पर नहीं मिली: {path}")
    raw = base64.b64decode(result["content"])
    with open(local_path, "wb") as image_file:
        image_file.write(raw)
    if len(raw) < 20000:
        raise RuntimeError("GitHub से मिली retry thumbnail अधूरी है")
    return local_path


def research_youtube_topic(youtube, programme, lesson):
    """Use YouTube search results as grounded keyword context, not as factual authority."""
    query = str(lesson.get("title", "")).strip()
    if programme == "shivpuran":
        query += " शिव पुराण कथा"
    else:
        query += " भगवद्गीता"
    response = youtube.search().list(
        part="snippet",
        q=query,
        type="video",
        maxResults=5,
        relevanceLanguage="hi"
    ).execute()
    rows = []
    for item in response.get("items", []):
        snippet = item.get("snippet", {})
        title = str(snippet.get("title", "")).strip()
        description = str(snippet.get("description", "")).strip()
        if title:
            rows.append({"title": title[:180], "description": description[:350]})
    if not rows:
        raise RuntimeError("YouTube topic research में प्रासंगिक परिणाम नहीं मिले")
    return rows


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


def get_thumbnail_title(programme, lesson, lesson_index, config=None):
    """Return the Sanskrit chapter name for Gita; preserve other programmes."""
    title = str(lesson.get("title", "धार्मिक प्रवचन")).strip()
    chapter = lesson.get("chapter")

    if programme == "bhagavadgita":
        # config entries use: अध्याय संख्या — संस्कृत नाम — हिन्दी अर्थ
        # Use literal separators so the Sanskrit name is extracted exactly.
        parts = title.split(" — ", 2)
        if len(parts) == 1:
            parts = title.split(" – ", 2)
        if len(parts) >= 2:
            return parts[1].strip()
        return title

    if chapter is None:
        match = re.search(
            r"(?:अध्याय|अध्याय संख्या|Adhyay|Chapter)\\s*[-:]?\\s*(\\d+)",
            title, re.I
        )
        if match:
            chapter = match.group(1)

    clean_title = re.sub(
        r"^(?:अध्याय\\s*)?\\d+\\s*[:：.\\-—–]?\\s*", "", title
    ).strip()

    if chapter is None and config:
        lessons = config.get("lessons", {}).get("shivpuran", [])
        section_prefix = title.split(" - ", 1)[0].strip()
        if "संहिता" in section_prefix:
            chapter = 0
            for index, item in enumerate(lessons):
                item_title = normalise_lesson(item).get("title", "").strip()
                if item_title.split(" - ", 1)[0].strip() == section_prefix:
                    chapter += 1
                if index == lesson_index:
                    break

    if programme == "shivpuran":
        section = title.split(" - ", 1)[0].strip()
        if "द्वितीय खंड" in title:
            section_label = "श्री रुद्र संहिता — द्वितीय खंड"
        elif section:
            section_label = section.replace("-", "—").strip()
        else:
            section_label = "श्री रुद्र संहिता"
        if chapter is not None:
            return f"{section_label} — अध्याय {chapter}"
        return section_label

    if chapter is not None:
        return f"अध्याय {chapter}: {clean_title}"
    return clean_title

def get_gita_thumbnail_meaning(lesson):
    """Extract the Hindi explanation after the Sanskrit name in config.json."""
    title = str(lesson.get("title", "")).strip()
    title = re.sub(
        r"^(?:अध्याय\\s*)?\\d+\\s*[:：.\\-—–]?\\s*", "", title
    ).strip()    parts = re.split(r"\\s*[—–]\\s*", title, maxsplit=2)
    return parts[1].strip() if len(parts) > 1 else ""


def split_balanced_devanagari(text):
    """Split at a word boundary, keeping both lines as similar in width as possible."""
    words = text.split()
    if len(words) < 2:
        return [text]
    candidates = []
    for i in range(1, len(words)):
        left, right = " ".join(words[:i]), " ".join(words[i:])
        candidates.append((abs(len(left) - len(right)), [left, right]))
    return min(candidates, key=lambda item: item[0])[1]


def wrap_text_to_width(draw, text, font, max_width):
    """Wrap Hindi text at word boundaries so every line stays inside its plaque."""
    words = str(text).split()
    if not words:
        return [""]
    lines = []
    current = words[0]
    for word in words[1:]:
        candidate = current + " " + word
        box = draw.textbbox((0, 0), candidate, font=font, language="hi", stroke_width=1)
        if box[2] - box[0] <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines

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
        lesson_index,
        config
    )

    # Bhagavad Gita: three opaque, tightly stacked plaques.
    # The chapter line contains ONLY chapter number and chapter name.
    if programme == "bhagavadgita":
        chapter_match = re.search(
            r"(?:अध्याय\\s*)?(\\d+)", str(lesson.get("title", ""))
        )
        chapter_number = str(
            lesson.get("chapter") or (chapter_match.group(1) if chapter_match else "")
        )
        chapter_name = get_thumbnail_title(
            programme, lesson, lesson_index, config
        )
        # Strip explanatory Hindi meaning; keep only the Sanskrit chapter name.
        chapter_name = chapter_name.split(" — ", 1)[0].split(" – ", 1)[0].strip()
        chapter_line = (
            f"अध्याय {chapter_number}: {chapter_name}"
            if chapter_number else chapter_name
        )

        # Keep the author's name plaque on the lower-left unobstructed.
        # These three panels share identical edges and touch without gaps.
        left, right = 450, 1248
        panels = [
            (left, 438, right, 512),
            (left, 512, right, 590),
            (left, 590, right, 668),
        ]
        panel_colors = [
            (2, 14, 35),       # deep navy — tricky hook
            (25, 12, 48),      # deep violet — chapter title
            (20, 13, 12),      # deep brown — Bhagavad Gita footer
        ]
        for panel, color in zip(panels, panel_colors):
            draw.rectangle(panel, fill=color)
            draw.rectangle(
                (panel[0] + 3, panel[1] + 3, panel[2] - 3, panel[3] - 3),
                outline=(246, 190, 35), width=2
            )

        # 1) Tricky title — retained as approved.
        hook = "दैवी गुण अपनाएँ — आसुरी प्रवृत्तियों से बचें"
        hook_font = fit_font(
            draw, hook, font_path, max_width=760,
            start_size=39, min_size=28
        )
        hook_box = draw.textbbox(
            (0, 0), hook, font=hook_font, language="hi", stroke_width=1
        )
        hook_width = hook_box[2] - hook_box[0]
        hook_height = hook_box[3] - hook_box[1]
        hook_x = left + (right - left - hook_width) // 2
        hook_y = panels[0][1] + (
            panels[0][3] - panels[0][1] - hook_height
        ) // 2 - hook_box[1]
        draw.text(
            (hook_x, hook_y), hook, font=hook_font,
            fill=(255, 255, 255), stroke_width=1,
            stroke_fill=(15, 15, 15), language="hi"
        )

        # 2) Chapter title — one line only; no explanation or introduction.
        # Match the compact title treatment of the existing Gita footer.
        title_font = fit_font(
            draw, chapter_line, font_path, max_width=760,
            start_size=42, min_size=30
        )
        title_box = draw.textbbox(
            (0, 0), chapter_line, font=title_font,
            language="hi", stroke_width=1
        )
        title_width = title_box[2] - title_box[0]
        title_height = title_box[3] - title_box[1]
        title_x = left + (right - left - title_width) // 2
        title_y = panels[1][1] + (
            panels[1][3] - panels[1][1] - title_height
        ) // 2 - title_box[1]
        draw.text(
            (title_x, title_y), chapter_line, font=title_font,
            fill=(255, 218, 65), stroke_width=1,
            stroke_fill=(105, 34, 0), language="hi"
        )

        # 3) Book name — bottom panel, matching the same title scale.
        footer = "श्रीमद्भगवद्गीता"
        footer_font = fit_font(
            draw, footer, font_path, max_width=760,
            start_size=42, min_size=30
        )
        footer_box = draw.textbbox(
            (0, 0), footer, font=footer_font,
            language="hi", stroke_width=1
        )
        footer_width = footer_box[2] - footer_box[0]
        footer_height = footer_box[3] - footer_box[1]
        footer_x = left + (right - left - footer_width) // 2
        footer_y = panels[2][1] + (
            panels[2][3] - panels[2][1] - footer_height
        ) // 2 - footer_box[1]
        draw.text(
            (footer_x, footer_y), footer, font=footer_font,
            fill=(255, 218, 65), stroke_width=1,
            stroke_fill=(65, 30, 0), language="hi"
        )

        # Remove the old stray "अध्याय" lettering left of the new stack,
        # while leaving the author's name plaque below it intact.
        draw.rectangle((370, 438, 450, 590), fill=(4, 20, 39))
    else:
        # Shiv Puran: three aligned layers. The hook uses the exact same
        # horizontal bounds as the chapter and book-name plaques.
        lesson_title = str(lesson.get("title", "")).strip()
        if "श्रीहरि और वीरभद्र का युद्ध" in lesson_title:
            topic_hook = "जब वीरभद्र ने रोक दिया श्रीहरि का सुदर्शन चक्र!"
        else:
            topic_hook = re.sub(
                r"^.*?\\s-\\s*", "", lesson_title, count=1
            ).strip() or lesson_title

        # Clear the complete hook area with an opaque plaque before drawing.
        # This removes any older master-image text that could show underneath.
        hook_plaque = (410, 448, 1248, 540)
        draw.rounded_rectangle(
            hook_plaque, radius=15, fill=(2, 14, 35),
            outline=(246, 190, 35), width=3
        )
        draw.rounded_rectangle(
            (418, 455, 1240, 533), radius=10,
            outline=(125, 83, 22), width=2
        )

        # Keep the hook font visibly smaller than the chapter line; wrap
        # at the shared plaque width instead of shrinking a long sentence.
        hook_font = ImageFont.truetype(
            font_path, size=30,
            layout_engine=ImageFont.Layout.RAQM
        )
        hook_lines = wrap_text_to_width(
            draw, topic_hook, hook_font, max_width=790
        )
        # Keep the hook within two balanced lines and inside the same bounds.
        while len(hook_lines) > 2 and hook_font.size > 20:
            hook_font = ImageFont.truetype(
                font_path, size=hook_font.size - 2,
                layout_engine=ImageFont.Layout.RAQM
            )
            hook_lines = wrap_text_to_width(
                draw, topic_hook, hook_font, max_width=790
            )

        hook_boxes = [
            draw.textbbox((0, 0), line, font=hook_font,
                          language="hi", stroke_width=1)
            for line in hook_lines
        ]
        hook_heights = [box[3] - box[1] for box in hook_boxes]
        hook_gap = 1
        hook_total_height = sum(hook_heights) + hook_gap * (len(hook_lines) - 1)
        hook_y = hook_plaque[1] + (
            hook_plaque[3] - hook_plaque[1] - hook_total_height
        ) // 2
        for line, box, line_height in zip(
            hook_lines, hook_boxes, hook_heights
        ):
            line_width = box[2] - box[0]
            hook_x = hook_plaque[0] + (
                hook_plaque[2] - hook_plaque[0] - line_width
            ) // 2
            draw.text(
                (hook_x, hook_y - box[1]), line, font=hook_font,
                fill=(255, 255, 255), stroke_width=1,
                stroke_fill=(15, 15, 15), language="hi"
            )
            hook_y += line_height + hook_gap

        # Middle layer: original chapter/section name, in its established plaque.
        plaque = (410, 550, 1248, 611)
        draw.rounded_rectangle(
            plaque, radius=15, fill=(2, 14, 35),
            outline=(246, 190, 35), width=3
        )
        chapter_font = fit_font(
            draw, title, font_path, max_width=790,
            start_size=42, min_size=22
        )
        chapter_box = draw.textbbox(
            (0, 0), title, font=chapter_font,
            language="hi", stroke_width=1
        )
        chapter_width = chapter_box[2] - chapter_box[0]
        chapter_height = chapter_box[3] - chapter_box[1]
        chapter_x = plaque[0] + (plaque[2] - plaque[0] - chapter_width) // 2
        chapter_y = plaque[1] + (plaque[3] - plaque[1] - chapter_height) // 2 - chapter_box[1]
        draw.text(
            (chapter_x, chapter_y), title, font=chapter_font,
            fill=(255, 218, 65), stroke_width=1,
            stroke_fill=(105, 34, 0), language="hi"
        )

    # शिव पुराण के लिए footer बदलना
    if programme == "shivpuran":
        # मुख्य विषय-पट्टिका के बराबर चौड़ी, सजावटी footer banner.
        footer_banner = (410, 625, 1248, 686)
        draw.rounded_rectangle(
            footer_banner, radius=15,
            fill=(20, 13, 12), outline=(246, 190, 35), width=3        )
        draw.rounded_rectangle(
            (418, 632, 1240, 679), radius=10,
            outline=(125, 83, 22), width=2
        )
        footer = "शिव महापुराण"
        footer_font = fit_font(
            draw, footer, font_path, max_width=790,
            start_size=45, min_size=30
        )
        footer_box = draw.textbbox((0, 0), footer, font=footer_font)
        footer_width = footer_box[2] - footer_box[0]
        footer_height = footer_box[3] - footer_box[1]
        footer_x = footer_banner[0] + (footer_banner[2] - footer_banner[0] - footer_width) // 2
        footer_y = footer_banner[1] + (footer_banner[3] - footer_banner[1] - footer_height) // 2 - footer_box[1]
        draw.text(
            (footer_x, footer_y), footer, font=footer_font,
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
    # एक ही invalid payload को बार-बार भेजने के बजाय पहले sanitize करें।
    clean = []
    seen = set()
    total = 0
    for item in desired_tags or []:
        tag = re.sub(r"[\x00-\x1f\x7f]", " ", str(item))
        tag = re.sub(r"\s+", " ", tag).strip().lstrip("#").replace(",", " ").strip()
        if not tag or tag.casefold() in seen:
            continue
        size = len(tag) + (1 if clean else 0)
        if total + size > 450:
            continue
        clean.append(tag)
        seen.add(tag.casefold())
        total += size
        if len(clean) == 20:
            break
    if len(clean) < 5:
        log("चेतावनी: valid tags बहुत कम हैं; YouTube update छोड़ा गया।")
        return False

    for attempt in range(1, 4):
        try:
            response = youtube.videos().list(part="snippet", id=video_id).execute()
            items = response.get("items", [])
            if not items:
                log(f"Video metadata अभी उपलब्ध नहीं; प्रयास {attempt}/3।")
                time.sleep(5)
                continue

            snippet = dict(items[0]["snippet"])
            saved_tags = snippet.get("tags", [])
            if set(clean).issubset(set(saved_tags)):
                log(f"YouTube पर {len(saved_tags)} tags सुरक्षित और सत्यापित हैं।")
                return True

            snippet["tags"] = clean
            youtube.videos().update(
                part="snippet",
                body={"id": video_id, "snippet": snippet}
            ).execute()

            check = youtube.videos().list(part="snippet", id=video_id).execute()
            check_items = check.get("items", [])
            if check_items and set(clean).issubset(set(check_items[0]["snippet"].get("tags", []))):
                log("Tags YouTube पर सुरक्षित और सत्यापित हो गए।")
                return True
            log(f"Tags अभी सत्यापित नहीं हुए; प्रयास {attempt}/3।")
        except HttpError as error:
            log(f"YouTube ने tags update अस्वीकार किया: {error}")
            if getattr(error, "status_code", None) == 400 or "invalidTags" in str(error):
                log("invalidTags मिला; वही अनुरोध दोहराया नहीं जाएगा।")
                return False
        except Exception as error:
            log(f"Tags verification प्रयास {attempt}/3: {error}")
        if attempt < 3:
            time.sleep(5)

    log("चेतावनी: 3 प्रयासों के बाद भी YouTube Tags सत्यापित नहीं हो सके।")
    return False
