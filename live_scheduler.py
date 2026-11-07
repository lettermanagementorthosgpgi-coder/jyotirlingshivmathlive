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
    """Normalize lesson data and infer a Gita chapter number from its title when absent."""
    if isinstance(lesson, dict):
        title = (
            lesson.get("title")
            or lesson.get("hindi_name")
            or lesson.get("sanskrit_name")
            or "धार्मिक प्रवचन"
        )
        chapter = lesson.get("chapter")
    else:
        title = str(lesson)
        chapter = None

    if chapter is None:
        match = re.search(r"अध्याय\\s*(\\d+)", str(title))
        if match:
            chapter = int(match.group(1))

    result = {
        "title": str(title),
        "chapter": chapter,
        "meaning": str(lesson.get("meaning", "")) if isinstance(lesson, dict) else ""
    }
    if isinstance(lesson, dict):
        result.update({
            "verse_start": lesson.get("verse_start"),
            "verse_end": lesson.get("verse_end"),
            "verse_range": lesson.get("verse_range", "")
        })
    return result


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
    if programme == "bhagavadgita" and lesson.get("verse_range"):
        query += " " + str(lesson["verse_range"])
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
    """Return an accurate chapter label; never infer chapters from lesson-list positions."""
    title = str(lesson.get("title", "धार्मिक प्रवचन")).strip()
    chapter = lesson.get("chapter")

    if programme == "bhagavadgita":
        # config entries use: अध्याय संख्या — संस्कृत नाम — हिन्दी अर्थ
        parts = title.split(" — ", 2)
        if len(parts) == 1:
            parts = title.split(" – ", 2)
        if len(parts) >= 2:
            return parts[1].strip()
        return title

    # Prefer explicit chapter metadata or a chapter number written in the title.
    if chapter is None:
        match = re.search(
            r"(?:अध्याय|Adhyay|Chapter)\s*[-:]?\s*(\d+)",
            title, re.I
        )
        if match:
            chapter = match.group(1)

    # Shiv Puran lesson-list positions are NOT chapter numbers: a chapter can
    # contain several topics. Use only source-verified topic-to-chapter mappings.
    if programme == "shivpuran" and chapter is None:
        topic = title.split(" - ", 1)[1].strip() if " - " in title else title
        verified_chapters = {
            "त्रिपुर सहित उनके स्वामियों के वध की प्रार्थना": 6,
            "देवताओं द्वारा शिव-स्तवन": 7,
            "दिव्य रथ का निर्माण": 8,
            "भगवान शिव की यात्रा": 9,
            "त्रिपुरासुर-वध": 10,
        }
        chapter = verified_chapters.get(topic)

    clean_title = re.sub(
        r"^(?:अध्याय\s*)?\d+\s*[:：.\-—–]?\s*", "", title
    ).strip()

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
        # Unknown chapter is intentionally omitted rather than guessed.
        return section_label

    if chapter is not None:
        return f"अध्याय {chapter}: {clean_title}"
    return clean_title

def get_gita_thumbnail_meaning(lesson):
    """Extract the Hindi explanation after the Sanskrit name in config.json."""
    title = str(lesson.get("title", "")).strip()
    title = re.sub(
        r"^(?:अध्याय\\s*)?\\d+\\s*[:：.\\-—–]?\\s*", "", title
    ).strip()
    parts = re.split(r"\\s*[—–]\\s*", title, maxsplit=2)
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
        # Chapter number is stored at the start of the config title:
        # "अध्याय 16 — Sanskrit name — Hindi meaning".
        # Read the first field directly; this avoids fragile escaped regexes.
        chapter_prefix = str(lesson.get("title", "")).split(" — ", 1)[0]
        chapter_match = re.search(r"[0-9]+", chapter_prefix)
        chapter_number = str(
            lesson.get("chapter")
            or (chapter_match.group(0) if chapter_match else "")
        )
        chapter_name = get_thumbnail_title(
            programme, lesson, lesson_index, config
        )
        # Strip explanatory Hindi meaning; keep only the Sanskrit chapter name.
        chapter_name = chapter_name.split(" — ", 1)[0].split(" – ", 1)[0].strip()
        verse_range = str(lesson.get("verse_range", "")).strip()
        chapter_line = (
            f"अध्याय {chapter_number} ({verse_range}): {chapter_name}"
            if chapter_number and verse_range
            else (f"अध्याय {chapter_number}: {chapter_name}" if chapter_number else chapter_name)
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

        # 1) Dynamic hook: never show a chapter-16-only message on other chapters.
        verse_range = str(lesson.get("verse_range", "")).strip()
        hook = (
            f"{verse_range} का अर्थ और जीवन-संदेश"
            if verse_range else f"{chapter_name} का अर्थ और जीवन-संदेश"
        )
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
            fill=(20, 13, 12), outline=(246, 190, 35), width=3
        )
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


BHAGAVADGITA_CHAPTER_COUNT = 18


def apply_gita_verse_range(slot_key, index, lesson, progress):
    """Attach the manually selected verse range only to its matching chapter."""
    if slot_key != "bhagavadgita":
        return lesson
    selected = progress.get("bhagavadgita_verse_range")
    if not isinstance(selected, dict) or int(selected.get("chapter", -1)) != index + 1:
        return lesson
    start = int(selected["start"])
    end = int(selected["end"])
    lesson["verse_start"] = start
    lesson["verse_end"] = end
    lesson["verse_range"] = f"श्लोक {start}–{end}"
    return lesson


def get_next_lesson(slot_key, config, progress):
    lessons = config["lessons"].get(slot_key, [])

    if not lessons:
        fail(f"{slot_key} के लिए lessons उपलब्ध नहीं हैं।")

    cycle_length = (
        BHAGAVADGITA_CHAPTER_COUNT
        if slot_key == "bhagavadgita"
        else len(lessons)
    )
    if len(lessons) < cycle_length:
        fail(
            f"{slot_key} की lessons सूची अधूरी है: "
            f"{len(lessons)} / {cycle_length}"
        )

    next_title = progress.get(f"{slot_key}_next_title")
    if next_title:
        for index, item in enumerate(lessons[:cycle_length]):
            lesson = normalise_lesson(item)
            if lesson["title"] == next_title:
                return index, apply_gita_verse_range(slot_key, index, lesson, progress)
        log(
            f"Saved next title नहीं मिला: {next_title}. "
            "Integer progress इस्तेमाल होगी।"
        )

    try:
        index = int(progress.get(slot_key, 0))
    except (TypeError, ValueError):
        index = 0

    # भगवद्गीता की progress हमेशा 0–17 के चक्र में रहेगी।
    index %= cycle_length
    lesson = normalise_lesson(lessons[index])
    return index, apply_gita_verse_range(slot_key, index, lesson, progress)


def advance_progress(slot_key, index, config, progress):
    lessons = config["lessons"].get(slot_key, [])
    cycle_length = (
        BHAGAVADGITA_CHAPTER_COUNT
        if slot_key == "bhagavadgita"
        else len(lessons)
    )

    if not lessons or len(lessons) < cycle_length:
        fail(f"{slot_key} की lessons सूची अधूरी है।")

    if slot_key == "bhagavadgita" and progress.get("bhagavadgita_verse_range"):
        selected = progress["bhagavadgita_verse_range"]
        if int(selected.get("chapter", -1)) == int(index) + 1:
            log("भगवद्गीता का अध्याय/श्लोक क्रम मैन्युअल है; अगला अध्याय तभी बदलेगा जब नया श्लोक-खंड दिया जाएगा।")
            return

    next_index = (int(index) + 1) % cycle_length
    if next_index == 0:
        log(
            f"{slot_key}: सभी {cycle_length} अध्याय पूरे हुए; "
            "क्रम फिर अध्याय 1 से शुरू होगा।"
        )

    progress[slot_key] = next_index
    progress[f"{slot_key}_next_title"] = normalise_lesson(
        lessons[next_index]
    )["title"]


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

def generate_youtube_metadata(programme, lesson, research_context=None):
    """Generate high-quality metadata; never silently publish generic fallback."""
    source_title = str(lesson.get("title", "धार्मिक प्रवचन")).strip()
    programme_name = "शिव महापुराण" if programme == "shivpuran" else "श्रीमद्भगवद्गीता"
    suffix = "|Mahant Rajesh Kumar Tiwari"
    verse_range = str(lesson.get("verse_range", "")).strip() if programme == "bhagavadgita" else ""
    range_instruction = (
        f"आज केवल {verse_range} पर प्रवचन है। Title, description, tags और hashtags में यही श्लोक-सीमा स्पष्ट रखें; पूरे अध्याय का प्रवचन होने का दावा न करें।"
        if verse_range else ""
    )
    prompt = f'''
आप एक वरिष्ठ हिंदी धार्मिक शोधकर्ता, YouTube SEO विशेषज्ञ और devotional content writer हैं।
कार्यक्रम: {programme_name}
आज का वास्तविक पाठ/प्रसंग: {source_title}
आज की श्लोक-सीमा: {verse_range or "लागू नहीं"}
विशेष निर्देश: {range_instruction}
YouTube पर इसी विषय की खोज से प्राप्त संदर्भ (केवल खोज-शब्द/प्रासंगिकता समझने हेतु; इन्हें शास्त्रीय प्रमाण न मानें):
{json.dumps(research_context or [], ensure_ascii=False)}
चैनल: {CHANNEL_NAME}
प्रस्तुतकर्ता: {AUTHOR}
वेबसाइट: {WEBSITE}

इस खास प्रसंग पर आकर्षक, स्पष्ट और खोज में उपयोगी metadata लिखें। सामान्य/खाली वाक्य जैसे 'इस प्रसंग का आध्यात्मिक विवेचन सुनें' न लिखें।
TITLE: प्रसंग के वास्तविक विषय को सीधे बताएँ। मुख्य खोज-शब्द शुरुआत में रखें। शीर्षक जिज्ञासा जगाए पर भ्रामक/सनसनीखेज न हो। 100 अक्षरों के भीतर रखें। अंत में ठीक यह suffix अनिवार्य है: |Mahant Rajesh Kumar Tiwari
DESCRIPTION: कम से कम 180 और लगभग 300-450 शब्दों में स्वाभाविक, उपयोगी हिंदी विवरण दें। पहले 2 वाक्यों में इसी प्रसंग का सार और दर्शक को क्या जानने मिलेगा लिखें। फिर प्रसंग की पृष्ठभूमि/मुख्य घटनाक्रम को केवल दिए गए शीर्षक से समर्थित सीमा में समझाएँ; शास्त्रीय तथ्य न गढ़ें। फिर इस पाठ का महत्व, किस प्रकार के दर्शक के लिए उपयोगी है, और अंत में चैनल परिचय व subscribe अनुरोध दें। Keyword stuffing न करें।
TAGS: 18-25 वास्तविक खोज वाक्यांश दें; Hindi और English दोनों में, विषय-विशिष्ट; generic tags कम रखें; # न लगाएँ।
HASHTAGS: 4-6 प्रासंगिक hashtags दें।
केवल valid JSON लौटाएँ: {{"title":"...","description":"...","tags":["..."],"hashtags":["#..."]}}
'''
    def extract_text(result):
        box = result.get("result", result) if isinstance(result, dict) else {}
        if isinstance(box, dict):
            for key in ("response", "text", "output_text", "content"):
                value = box.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            choices = box.get("choices") or result.get("choices") or []
            if choices:
                message = choices[0].get("message", {})
                value = message.get("content") or choices[0].get("text")
                if isinstance(value, str) and value.strip():
                    return value.strip()
        raise ValueError("AI response में text नहीं मिला: " + json.dumps(result, ensure_ascii=False)[:700])
    def normalise(data, provider):
        title = str(data.get("title", "")).strip()
        title = re.sub(r"\s*\|\s*Mahant Rajesh Kumar Tiwari\s*$", "", title, flags=re.I).strip()
        if not title:
            raise ValueError("AI ने title नहीं दिया")
        # Gita SEO title must always name the exact verse range first.
        if verse_range and verse_range.casefold() not in title.casefold():
            title = f"भगवद्गीता {verse_range} | {title}"
        # पूरे शब्दों को सुरक्षित रखते हुए suffix सहित 100 अक्षरों में रखें।
        max_prefix = 100 - len(suffix) - 1
        if len(title) > max_prefix:
            words = title.split()
            kept = []
            for word in words:
                candidate = " ".join(kept + [word])
                if len(candidate) > max_prefix:
                    break
                kept.append(word)
            title = " ".join(kept).rstrip(" -|:।")
        if not title:
            raise ValueError("SEO title suffix से पहले खाली हो गया")
        title = title + " " + suffix
        if not title.endswith(suffix) or len(title) > 100:
            raise ValueError("Title suffix/length validation failed")
        description = str(data.get("description", "")).strip()
        if verse_range and verse_range.casefold() not in description.casefold():
            description = f"आज का पाठ: श्रीमद्भगवद्गीता, अध्याय {lesson.get('chapter')}, {verse_range}. इस प्रवचन में इसी श्लोक-खंड का अर्थ और भावार्थ प्रस्तुत है।\n\n" + description
        if len(description) < 500:
            raise ValueError(f"Description बहुत छोटा है ({len(description)} characters); दोबारा प्रयास आवश्यक")
        raw_tags = data.get("tags", [])
        if not isinstance(raw_tags, list):
            raise ValueError("AI tags सूची के रूप में नहीं मिले")
        if verse_range:
            raw_tags = list(raw_tags) + [
                f"भगवद्गीता अध्याय {lesson.get('chapter')} {verse_range}",
                f"Bhagavad Gita chapter {lesson.get('chapter')} {verse_range}",
                f"Gita shlok {lesson.get('verse_start')}-{lesson.get('verse_end')}"
            ]
        hashtags = [str(x).strip() if str(x).strip().startswith("#") else "#" + str(x).strip().replace(" ", "") for x in data.get("hashtags", []) if str(x).strip()]
        if len(raw_tags) < 12:
            raise ValueError("AI ने पर्याप्त विषय-विशिष्ट tags नहीं दिए")
        # YouTube tags को plain text, unique और conservative कुल सीमा में रखें।
        clean_tags = []
        seen_tags = set()
        total = 0
        for item in raw_tags:
            tag = re.sub(r"[\x00-\x1f\x7f]", " ", str(item))
            tag = re.sub(r"\s+", " ", tag).strip().lstrip("#").strip()
            tag = tag.replace(",", " ").strip()
            if not tag:
                continue
            key = tag.casefold()
            if key in seen_tags:
                continue
            size = len(tag) + (1 if clean_tags else 0)
            if total + size > 450:
                continue
            clean_tags.append(tag)
            seen_tags.add(key)
            total += size
            if len(clean_tags) >= 20:
                break
        if len(clean_tags) < 12:
            raise ValueError(f"साफ़ करने के बाद केवल {len(clean_tags)} valid tags बचे")
        if len(hashtags) < 3:
            raise ValueError("AI ने पर्याप्त hashtags नहीं दिए")
        description += "\n\n" + " ".join(list(dict.fromkeys(hashtags))[:6])
        description += f"\n\nप्रस्तुति: {AUTHOR}\n{CHANNEL_NAME}\n{WEBSITE}\n{CHANNEL_URL}"
        log(f"{provider}: metadata तैयार; title={title!r}, description={len(description)} chars, tags={len(clean_tags)}")
        return title, description, clean_tags
    # 1) Cloudflare Workers AI
    cf_token = os.environ.get("CLOUDFLARE_API_TOKEN")
    cf_account = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    if cf_token and cf_account:
        try:
            model = os.environ.get("CLOUDFLARE_TEXT_MODEL", "@cf/openai/gpt-oss-120b")
            endpoint = f"https://api.cloudflare.com/client/v4/accounts/{cf_account}/ai/run/{model}"
            payload = {"messages":[{"role":"system","content":"You are an expert Hindi SEO editor. Return only JSON."},{"role":"user","content":prompt}],"max_tokens":2400,"temperature":0.5}
            request = urllib.request.Request(endpoint,data=json.dumps(payload,ensure_ascii=False).encode("utf-8"),headers={"Authorization":f"Bearer {cf_token}","Content-Type":"application/json"},method="POST")
            with urllib.request.urlopen(request,timeout=150) as response:
                result = json.loads(response.read().decode("utf-8"))
            raw = extract_text(result)
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I)
            return normalise(json.loads(raw), "Cloudflare Workers AI " + model)
        except Exception as error:
            log(f"Cloudflare AI असफल; अब GPT: {error}")
    else:
        log("Cloudflare credentials अनुपलब्ध; GPT fallback")
    # 2) OpenAI GPT
    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key:
        try:
            model = os.environ.get("OPENAI_TEXT_MODEL", OPENAI_MODEL)
            payload = {"model":model,"messages":[{"role":"system","content":"Return only valid JSON. You are an expert Hindi YouTube SEO researcher."},{"role":"user","content":prompt}],"response_format":{"type":"json_object"},"temperature":0.5,"max_completion_tokens":2400}
            request = urllib.request.Request("https://api.openai.com/v1/chat/completions",data=json.dumps(payload,ensure_ascii=False).encode("utf-8"),headers={"Authorization":f"Bearer {openai_key}","Content-Type":"application/json"},method="POST")
            with urllib.request.urlopen(request,timeout=150) as response:
                result = json.loads(response.read().decode("utf-8"))
            raw = result["choices"][0]["message"]["content"]
            return normalise(json.loads(raw), "OpenAI GPT " + model)
        except Exception as error:
            log(f"GPT असफल; अब Gemini: {error}")
    else:
        log("OPENAI_API_KEY अनुपलब्ध; Gemini fallback")
    # 3) Gemini — current stable model; 2.5 Flash is not used as default
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if gemini_key:
        try:
            model = os.environ.get("GEMINI_TEXT_MODEL", "gemini-2.5-flash")
            endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={gemini_key}"
            payload = {"contents":[{"parts":[{"text":prompt}]}],"generationConfig":{"temperature":0.5,"responseMimeType":"application/json","maxOutputTokens":2400}}
            request = urllib.request.Request(endpoint,data=json.dumps(payload,ensure_ascii=False).encode("utf-8"),headers={"Content-Type":"application/json"},method="POST")
            with urllib.request.urlopen(request,timeout=150) as response:
                result = json.loads(response.read().decode("utf-8"))
            raw = result["candidates"][0]["content"]["parts"][0]["text"]
            return normalise(json.loads(raw), "Gemini " + model)
        except Exception as error:
            log(f"Gemini भी असफल: {error}")
    raise RuntimeError("तीनों AI से मान्य metadata नहीं मिला; सुरक्षा के लिए Broadcast नहीं बनाया जाएगा।")

# ============================================================
# CREATE NEW BROADCAST
# ============================================================

def get_retry_target(now):
    """Return (programme, scheduled datetime, deadline) only inside its preparation window."""
    morning_start = now.replace(hour=19, minute=0, second=0, microsecond=0)
    morning_end_today = now.replace(hour=6, minute=0, second=0, microsecond=0)
    evening_start = now.replace(hour=8, minute=0, second=0, microsecond=0)
    evening_end = now.replace(hour=18, minute=0, second=0, microsecond=0)

    if now >= morning_start:
        target_date = (now + timedelta(days=1)).date()
        scheduled = datetime.combine(target_date, datetime.min.time(), tzinfo=TIMEZONE).replace(hour=7)
        deadline = scheduled.replace(hour=6)
        return "shivpuran", scheduled, deadline
    if now <= morning_end_today:
        scheduled = now.replace(hour=7, minute=0, second=0, microsecond=0)
        deadline = now.replace(hour=6, minute=0, second=0, microsecond=0)
        return "shivpuran", scheduled, deadline
    if evening_start <= now <= evening_end:
        scheduled = now.replace(hour=19, minute=0, second=0, microsecond=0)
        deadline = evening_end
        return "bhagavadgita", scheduled, deadline
    return None, None, None


def safe_fallback_metadata(programme, lesson):
    programme_name = "शिव महापुराण" if programme == "shivpuran" else "श्रीमद्भगवद्गीता"
    suffix = "|Mahant Rajesh Kumar Tiwari"
    base = str(lesson.get("title", programme_name)).strip()
    if programme == "bhagavadgita" and lesson.get("verse_range"):
        base = f"भगवद्गीता {lesson['verse_range']} | {base}"
    max_prefix = 100 - len(suffix) - 1
    words = []
    for word in base.split():
        candidate = " ".join(words + [word])
        if len(candidate) > max_prefix:
            break
        words.append(word)
    title = (" ".join(words) or programme_name) + " " + suffix
    verse_text = f" ({lesson.get('verse_range')})" if programme == "bhagavadgita" and lesson.get("verse_range") else ""
    description = (
        f"{programme_name} का आज का पाठ: {lesson.get('title', '')}{verse_text}. "
        f"इस धार्मिक पाठ में दिए गए प्रसंग का वाचन एवं व्याख्या प्रस्तुत की जाएगी। "
        f"प्रस्तुति: {AUTHOR}\n{CHANNEL_NAME}\n{WEBSITE}\n{CHANNEL_URL}"
    )
    tags = ["शिव पुराण" if programme == "shivpuran" else "भगवद्गीता",
            "Jyotirling Shivmath", "Mahant Rajesh Kumar Tiwari",
            "धार्मिक कथा", "सनातन धर्म"]
    return title, description, tags


def prepare_retry_run(config, progress, progress_sha, youtube, programme_override=None):
    now = datetime.now(TIMEZONE)
    if programme_override:
        if programme_override not in ("shivpuran", "bhagavadgita"):
            fail("PROGRAMME केवल shivpuran या bhagavadgita हो सकता है।")
        programme = programme_override
        selected_slot = get_slot(config, programme)
        scheduled_time = get_scheduled_time(selected_slot)
        deadline = scheduled_time - timedelta(hours=1)
        log(f"Manual programme selection honoured: {programme}")
    else:
        programme, scheduled_time, deadline = get_retry_target(now)
        if not programme:
            log("अभी किसी कार्यक्रम की preparation window नहीं है; कोई काम नहीं किया जाएगा।")
            return

    if programme == "bhagavadgita":
        selected_range = progress.get("bhagavadgita_verse_range")
        if not isinstance(selected_range, dict):
            log("भगवद्गीता की आज की श्लोक-सीमा अभी दर्ज नहीं है; गलत पूरे-अध्याय वाला SEO/Broadcast रोक दिया गया।")
            return
        current_index, current_lesson = get_next_lesson(programme, config, progress)
        if int(selected_range.get("chapter", -1)) != current_index + 1:
            log("भगवद्गीता की श्लोक-सीमा और चुना हुआ अध्याय मेल नहीं खाते; तैयारी रोक दी गई।")
            return

    date_string = scheduled_time.strftime("%Y-%m-%d")
    saved = get_saved_broadcast(progress, programme, date_string)
    if saved:
        pending_all = progress.setdefault("pending_preparation", {})
        pending = pending_all.get(programme, {})
        saved_id = saved.get("id") if isinstance(saved, dict) else saved

        # Broadcast पहले बन चुका हो, लेकिन thumbnail upload विफल हुआ हो,
        # तो उसी Broadcast ID पर सुरक्षित thumbnail दोबारा upload करें।
        if (
            pending.get("date") == date_string
            and pending.get("thumbnail_asset")
            and saved_id
        ):
            try:
                github_download_binary(
                    pending["thumbnail_asset"],
                    THUMBNAIL_OUTPUT
                )
                upload_thumbnail(youtube, saved_id, THUMBNAIL_OUTPUT)

                # Upload failure के कारण पहले run में progress advance नहीं हुई थी।
                # केवल तभी advance करें जब saved Broadcast उसी अगले पाठ का हो।
                saved_lesson = saved.get("lesson") if isinstance(saved, dict) else None
                lesson_index, next_lesson = get_next_lesson(programme, config, progress)
                if saved_lesson and saved_lesson == next_lesson["title"]:
                    advance_progress(programme, lesson_index, config, progress)
                else:
                    log("Saved Broadcast और current next lesson अलग हैं; progress को सुरक्षित रखने के लिए advance नहीं किया गया।")

                pending_all.pop(programme, None)
                save_progress(progress, None)
                log(f"{programme} का pending thumbnail उसी Broadcast पर सफलतापूर्वक upload हुआ: {saved_id}")
            except Exception as error:
                log(f"Thumbnail retry अभी असफल; pending तैयारी सुरक्षित रहेगी और अगली retry में पुनः प्रयास होगा: {error}")
            return

        log(f"{programme} के लिए {date_string} का Broadcast पहले से दर्ज है; नया Broadcast नहीं बनाया जाएगा।")
        return

    slot = get_slot(config, programme)
    lesson_index, lesson = get_next_lesson(programme, config, progress)
    pending_all = progress.setdefault("pending_preparation", {})
    pending = pending_all.get(programme, {})
    if pending.get("date") != date_string or pending.get("lesson") != lesson["title"]:
        pending = {
            "date": date_string,
            "lesson": lesson["title"],
            "research": None,
            "metadata": None,
            "thumbnail_asset": None
        }
        pending_all[programme] = pending
        save_progress(progress, progress_sha)

    log(f"Retry programme: {programme}; target: {scheduled_time.isoformat()}; lesson: {lesson['title']}")

    if not pending.get("research"):
        try:
            pending["research"] = research_youtube_topic(youtube, programme, lesson)
            save_progress(progress, None)
            log("YouTube topic research सुरक्षित हो गया।")
        except Exception as error:
            log(f"YouTube topic research अभी असफल; अगली retry में पुनः प्रयास होगा: {error}")

    if not pending.get("metadata") and pending.get("research"):
        try:
            title, description, tags = generate_youtube_metadata(
                programme, lesson, pending.get("research")
            )
            pending["metadata"] = {
                "title": title,
                "description": description,
                "tags": tags
            }
            save_progress(progress, None)
            log("SEO title, description और tags तैयार होकर सुरक्षित हो गए।")
        except Exception as error:
            log(f"SEO metadata पैकेज (title/description/tags) अभी तैयार नहीं; अगली retry में केवल यही पैकेज दोबारा बनेगा: {error}")

    if not pending.get("thumbnail_asset"):
        try:
            master_path = find_master_thumbnail(config)
            thumbnail_path = create_thumbnail(
                master_path, programme, lesson, lesson_index, config
            )
            asset_path = f"retry_assets/{programme}_{date_string}.jpg"
            github_upload_binary(asset_path, thumbnail_path)
            pending["thumbnail_asset"] = asset_path
            save_progress(progress, None)
            log("Thumbnail तैयार और retry के लिए सुरक्षित हो गया।")
        except Exception as error:
            log(f"Thumbnail अभी तैयार नहीं; अगली retry में केवल thumbnail दोबारा बनेगा: {error}")

    metadata_ready = bool(pending.get("metadata"))
    thumbnail_ready = bool(pending.get("thumbnail_asset"))
    ready = metadata_ready and thumbnail_ready
    at_deadline = now >= deadline
    if not ready and not at_deadline:
        missing = []
        if not metadata_ready:
            missing.extend(["SEO title", "description", "tags"])
        if not thumbnail_ready:
            missing.append("thumbnail")
        log("अभी केवल ये सामग्री शेष है: " + ", ".join(missing) + "। अगली retry तक Broadcast नहीं बनेगा।")
        return

    if ready:
        metadata = pending["metadata"]
        github_download_binary(pending["thumbnail_asset"], THUMBNAIL_OUTPUT)
        log("चारों सामग्री तैयार हैं; निर्धारित समय का Broadcast बनाया जाएगा।")
    else:
        log("Preparation deadline आ गई; उपलब्ध सामग्री सुरक्षित रखते हुए केवल अनुपलब्ध सामग्री fallback से भरी जाएगी।")
        fallback_title, fallback_description, fallback_tags = safe_fallback_metadata(programme, lesson)
        saved_metadata = pending.get("metadata") or {}
        metadata = {
            "title": saved_metadata.get("title") or fallback_title,
            "description": saved_metadata.get("description") or fallback_description,
            "tags": saved_metadata.get("tags") or fallback_tags
        }
        if thumbnail_ready:
            github_download_binary(pending["thumbnail_asset"], THUMBNAIL_OUTPUT)
        else:
            master_path = find_master_thumbnail(config)
            fallback_image = Image.open(master_path).convert("RGB")
            fallback_image = ImageOps.fit(fallback_image, (1280, 720), method=Image.Resampling.LANCZOS)
            fallback_image.save(THUMBNAIL_OUTPUT, "JPEG", quality=94, optimize=True)

    broadcast_id = create_broadcast(
        youtube=youtube,
        slot=slot,
        lesson=lesson,
        lesson_index=lesson_index,
        scheduled_time=scheduled_time,
        config=config,
        progress=progress,
        progress_sha=None,
        prepared_metadata=metadata,
        prepared_thumbnail_path=THUMBNAIL_OUTPUT,
        raise_thumbnail_error=True
    )
    pending_all.pop(programme, None)
    save_progress(progress, None)
    log(f"Retry scheduling पूरा: {broadcast_id}")


def create_broadcast(
    youtube,
    slot,
    lesson,
    lesson_index,
    scheduled_time,
    config,
    progress,
    progress_sha,
    prepared_metadata=None,
    prepared_thumbnail_path=None,
    raise_thumbnail_error=False
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

        # यदि यह retry-preparation से मिला पुराना Broadcast है, तो
        # उसी ID पर prepared thumbnail upload किए बिना pending साफ न करें।
        if prepared_thumbnail_path:
            try:
                upload_thumbnail(youtube, broadcast_id, prepared_thumbnail_path)
            except Exception as error:
                log(f"पहले से मौजूद Broadcast पर thumbnail upload असफल: {error}")
                if raise_thumbnail_error:
                    raise

        advance_progress(
            programme,
            lesson_index,
            config,
            progress
        )
        save_progress(progress, None)
        return broadcast_id
            # --------------------------------------------------------
    # नया YouTube Broadcast तैयार करें
    # --------------------------------------------------------

    # हर lesson के लिए Gemini से SEO title, description और tags बनाएं।
    if prepared_metadata:
        title = prepared_metadata["title"]
        description = prepared_metadata["description"]
        tags = prepared_metadata["tags"]
    else:
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
        thumbnail_path = prepared_thumbnail_path
        if not thumbnail_path:
            master_path = find_master_thumbnail(config)
            thumbnail_path = create_thumbnail(
                master_path=master_path,
                programme=programme,
                lesson=lesson,
                lesson_index=lesson_index,
                config=config
            )
        upload_thumbnail(youtube, broadcast_id, thumbnail_path)

    except Exception as error:
        log(
            "Thumbnail बनाने या upload करने में समस्या आई। "
            "Broadcast सुरक्षित है। "
            f"विवरण: {error}"
        )
        if raise_thumbnail_error:
            # Retry workflow को pending state सुरक्षित रखने दें।
            raise

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

def update_gita_verse_range(config, progress):
    """Save the user's chapter and verse range without creating a Broadcast."""
    try:
        chapter = int(os.environ.get("GITA_CHAPTER", "").strip())
        verse_start = int(os.environ.get("GITA_VERSE_START", "").strip())
        verse_end = int(os.environ.get("GITA_VERSE_END", "").strip())
    except (TypeError, ValueError):
        fail("अध्याय संख्या, श्लोक प्रारंभ और श्लोक समाप्त — तीनों में पूर्णांक भरें।")

    lessons = config.get("lessons", {}).get("bhagavadgita", [])
    if chapter < 1 or chapter > BHAGAVADGITA_CHAPTER_COUNT or chapter > len(lessons):
        fail("गीता अध्याय संख्या 1 से 18 के बीच होनी चाहिए।")
    verse_counts = [47, 72, 43, 42, 29, 47, 30, 28, 34, 42, 55, 20, 35, 27, 20, 24, 28, 78]
    max_verse = verse_counts[chapter - 1]
    if verse_start < 1 or verse_end < verse_start or verse_end > max_verse:
        fail(f"अध्याय {chapter} के लिए श्लोक सीमा 1 से {max_verse} के बीच होनी चाहिए।")

    lesson = normalise_lesson(lessons[chapter - 1])
    progress["bhagavadgita"] = chapter - 1
    progress["bhagavadgita_next_title"] = lesson["title"]
    progress["bhagavadgita_verse_range"] = {
        "chapter": chapter,
        "start": verse_start,
        "end": verse_end
    }
    # Discard stale SEO/thumbnail preparation so the next retry rebuilds both.
    pending = progress.get("pending_preparation", {})
    pending.pop("bhagavadgita", None)
    if not pending:
        progress.pop("pending_preparation", None)
    save_progress(progress, None)
    log(f"गीता अपडेट सुरक्षित: अध्याय {chapter}, श्लोक {verse_start}–{verse_end}")
    log("अभी कोई YouTube Broadcast नहीं बनाया गया।")


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

    if action == "update_gita_range":
        if programme != "bhagavadgita":
            fail("Update Gita Chapter & Shlok के लिए programme bhagavadgita चुनें।")
        update_gita_verse_range(config, progress)
        return

    # सुरक्षित metadata test: YouTube API/Broadcast/progress में कोई बदलाव नहीं।
    if action == "test_metadata":
        if programme not in ("shivpuran", "bhagavadgita"):
            fail("test_metadata के लिए PROGRAMME चुनें।")
        lesson_index, lesson = get_next_lesson(programme, config, progress)
        log(f"Metadata test lesson: {lesson['title']}")
        title, description, tags = generate_youtube_metadata(programme, lesson)
        log("===== GENERATED TITLE =====")
        log(title)
        log("===== GENERATED DESCRIPTION =====")
        log(description)
        log("===== GENERATED TAGS =====")
        log(json.dumps(tags, ensure_ascii=False))
        log("Metadata test पूरा; कोई Broadcast या progress बदलाव नहीं हुआ।")
        return

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

    if action == "retry_thumbnail":
        if programme not in ("shivpuran", "bhagavadgita"):
            fail("retry_thumbnail के लिए PROGRAMME चुनें।")
        date_text = os.environ.get("BROADCAST_DATE", "").strip()
        if not date_text:
            fail("retry_thumbnail के लिए BROADCAST_DATE (YYYY-MM-DD) आवश्यक है।")
        try:
            datetime.strptime(date_text, "%Y-%m-%d")
        except ValueError:
            fail("BROADCAST_DATE का प्रारूप YYYY-MM-DD होना चाहिए।")

        saved = get_saved_broadcast(progress, programme, date_text)
        saved_id = saved.get("id") if isinstance(saved, dict) else saved
        broadcast_id = os.environ.get("BROADCAST_ID", "").strip() or saved_id
        if not broadcast_id:
            fail(f"{programme} के लिए {date_text} का saved Broadcast ID नहीं मिला।")

        asset_path = f"retry_assets/{programme}_{date_text}.jpg"
        github_download_binary(asset_path, THUMBNAIL_OUTPUT)
        upload_thumbnail(youtube, broadcast_id, THUMBNAIL_OUTPUT)
        log(f"पुराने Broadcast पर thumbnail सफलतापूर्वक upload हुआ: {broadcast_id}")
        log("इस repair action ने कोई नया Broadcast नहीं बनाया और progress आगे नहीं बढ़ाई।")
        return

    if action == "prepare_retry":
        prepare_retry_run(config, progress, progress_sha, youtube, programme or None)
        return

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
