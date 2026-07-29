from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import random
import re
import sqlite3
import tempfile
import time
from collections import Counter
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field
from PIL import Image, ImageStat

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("BEAUTY_FEED_DATA", BASE_DIR / "data")).resolve()
MEDIA_DIR = DATA_DIR / "media"
DB_PATH = DATA_DIR / "feed.db"
DATA_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

ARTICLE_HOSTS = {"mp.weixin.qq.com"}
IMAGE_HOST_SUFFIXES = ("qpic.cn", "qq.com", "weixin.qq.com")
MAX_ARTICLE_BYTES = int(os.getenv("MUSEFLOW_MAX_ARTICLE_BYTES", 12 * 1024 * 1024))
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_IMAGES_PER_ARTICLE = 40
FEATURE_DIM = 64
ACCOUNT_BIZ_PATTERN = re.compile(r"(?:var\s+biz\s*=\s*|__biz=)[\"']?([A-Za-z0-9_=-]+)")

app = FastAPI(title="MuseFlow", version="1.0.0")


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sha256 TEXT NOT NULL UNIQUE,
                local_path TEXT NOT NULL,
                source_url TEXT NOT NULL,
                article_url TEXT NOT NULL,
                article_title TEXT NOT NULL DEFAULT '',
                account_name TEXT NOT NULL DEFAULT '',
                alt_text TEXT NOT NULL DEFAULT '',
                width INTEGER NOT NULL,
                height INTEGER NOT NULL,
                mime_type TEXT NOT NULL,
                visual_json TEXT NOT NULL,
                text_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'approved',
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_images_status ON images(status, id DESC);

            CREATE TABLE IF NOT EXISTS discovered_articles (
                article_url TEXT PRIMARY KEY,
                account_name TEXT NOT NULL,
                account_biz TEXT NOT NULL,
                article_title TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL,
                found_images INTEGER NOT NULL DEFAULT 0,
                imported_images INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                discovered_at INTEGER NOT NULL,
                processed_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_discovered_articles_state
            ON discovered_articles(state, processed_at DESC);

            CREATE TABLE IF NOT EXISTS interactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                image_id INTEGER NOT NULL,
                event TEXT NOT NULL,
                value REAL NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                FOREIGN KEY(image_id) REFERENCES images(id)
            );
            CREATE INDEX IF NOT EXISTS idx_interactions_user ON interactions(user_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id TEXT PRIMARY KEY,
                visual_json TEXT NOT NULL,
                text_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            """
        )


init_db()


class IngestRequest(BaseModel):
    url: str
    rights_confirmed: bool = False
    adult_confirmed: bool = False


class InteractionRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=80)
    image_id: int
    event: str
    value: float = 0


class ModerationRequest(BaseModel):
    status: str


def zeros() -> list[float]:
    return [0.0] * FEATURE_DIM


def normalize(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in values))
    return [v / norm for v in values] if norm > 1e-12 else values


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def hashed_text_features(text: str) -> list[float]:
    vector = zeros()
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    tokens = re.findall(r"[\u4e00-\u9fff]|[a-z0-9]{2,}", normalized)
    features = tokens + [normalized[i : i + 2] for i in range(max(0, len(normalized) - 1))]
    for token, count in Counter(features).items():
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "little") % FEATURE_DIM
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[index] += sign * (1.0 + math.log(count))
    return normalize(vector)


def visual_features(image: Image.Image) -> list[float]:
    rgb = image.convert("RGB")
    thumb = rgb.copy()
    thumb.thumbnail((256, 256))
    histogram = thumb.histogram()
    vector: list[float] = []
    pixels = max(1, thumb.width * thumb.height)
    for channel in range(3):
        values = histogram[channel * 256 : (channel + 1) * 256]
        for bucket in range(16):
            vector.append(sum(values[bucket * 16 : (bucket + 1) * 16]) / pixels)
    stat = ImageStat.Stat(thumb)
    means = [x / 255 for x in stat.mean[:3]]
    stds = [x / 128 for x in stat.stddev[:3]]
    ratio = min(3.0, rgb.height / max(1, rgb.width)) / 3.0
    vector.extend(means + stds + [ratio, 1.0 if rgb.height >= rgb.width else 0.0])
    while len(vector) < FEATURE_DIM:
        i = len(vector)
        vector.append(math.sin((i + 1) * (means[i % 3] + 0.1)) * 0.08)
    return normalize(vector[:FEATURE_DIM])


def is_public_host(hostname: str) -> bool:
    try:
        infos = __import__("socket").getaddrinfo(hostname, None)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return bool(infos)
    except Exception:
        return False


def validate_article_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or host not in ARTICLE_HOSTS:
        raise ValueError("仅支持 https://mp.weixin.qq.com 的公开文章链接")
    if not is_public_host(host):
        raise ValueError("来源地址解析失败或不是公网地址")
    return url


def validate_image_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"https", "http"}:
        raise ValueError("图片地址协议不受支持")
    if not any(host == suffix or host.endswith("." + suffix) for suffix in IMAGE_HOST_SUFFIXES):
        raise ValueError("图片域名不在公众号资源白名单")
    if not is_public_host(host):
        raise ValueError("图片地址不是公网地址")
    return url


def fetch_limited(client: httpx.Client, url: str, limit: int, referer: str | None = None) -> tuple[bytes, str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; MuseFlow/1.0; authorized-content-import)",
        "Accept": "text/html,image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    with client.stream("GET", url, headers=headers) as response:
        response.raise_for_status()
        length = int(response.headers.get("content-length", "0") or 0)
        if length > limit:
            raise ValueError("资源体积超过限制")
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > limit:
                raise ValueError("资源体积超过限制")
            chunks.append(chunk)
        return b"".join(chunks), response.headers.get("content-type", "")


def extract_account_biz(html: bytes) -> str:
    text = html.decode("utf-8", "ignore")
    match = ACCOUNT_BIZ_PATTERN.search(text)
    return match.group(1) if match else ""


def _extract_js_array(text: str, marker: str) -> str:
    marker_index = text.find(marker)
    if marker_index < 0:
        return ""
    start = text.find("[", marker_index + len(marker))
    if start < 0:
        return ""
    depth = 0
    quote = ""
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return text[start + 1 : index]
    return ""


def parse_article(html: bytes, article_url: str) -> tuple[str, str, list[dict[str, str]]]:
    text = html.decode("utf-8", "ignore")
    soup = BeautifulSoup(html, "html.parser")
    title_meta = soup.select_one('meta[property="og:title"]')
    title = (title_meta.get("content", "") if title_meta else "") or (soup.title.string if soup.title else "")
    account = ""
    account_node = soup.select_one("#js_name, .profile_nickname, .rich_media_meta_nickname")
    if account_node:
        account = account_node.get_text(" ", strip=True)
    if not account:
        account_match = re.search(r"\bnick_name\s*:\s*(['\"])(.*?)\1", text)
        if account_match:
            account = account_match.group(2)
    images: list[dict[str, str]] = []
    seen: set[str] = set()

    def add_image(raw: str, alt: str = "") -> None:
        raw = urljoin(article_url, raw.strip().replace("\\/", "/").replace("\\u0026", "&"))
        if not raw or raw in seen or len(images) >= MAX_IMAGES_PER_ARTICLE:
            return
        try:
            validate_image_url(raw)
        except ValueError:
            return
        seen.add(raw)
        images.append({"url": raw, "alt": alt.strip()[:300]})

    selectors = "#js_content img, .rich_media_content img"
    for node in soup.select(selectors):
        add_image(
            node.get("data-src") or node.get("data-original") or node.get("src") or "",
            node.get("alt") or node.get("data-type") or "",
        )

    if not images:
        picture_list = _extract_js_array(text, "picture_page_info_list")
        for match in re.finditer(r"\bcdn_url\s*:\s*(['\"])(https?://.*?)\1", picture_list):
            add_image(match.group(2))
            if len(images) >= MAX_IMAGES_PER_ARTICLE:
                break
    return title.strip()[:300], account[:120], images


def save_image(
    content: bytes,
    source_url: str,
    article_url: str,
    title: str,
    account: str,
    alt: str,
    status: str = "approved",
) -> int | None:
    if status not in {"approved", "pending", "blocked"}:
        raise ValueError("图片状态无效")
    digest = hashlib.sha256(content).hexdigest()
    with db() as conn:
        existing = conn.execute("SELECT id FROM images WHERE sha256 = ?", (digest,)).fetchone()
        if existing:
            return None
    try:
        image = Image.open(BytesIO(content))
        image.verify()
        image = Image.open(BytesIO(content)).convert("RGB")
    except Exception as exc:
        raise ValueError("下载内容不是有效图片") from exc
    width, height = image.size
    if width < 360 or height < 480:
        return None
    if width * height > 45_000_000:
        raise ValueError("图片像素尺寸过大")
    filename = f"{digest}.webp"
    final_path = MEDIA_DIR / filename
    with tempfile.NamedTemporaryFile(dir=MEDIA_DIR, suffix=".webp", delete=False) as temp:
        temp_path = Path(temp.name)
    try:
        image.save(temp_path, "WEBP", quality=90, method=4)
        temp_path.replace(final_path)
    finally:
        temp_path.unlink(missing_ok=True)
    visual = visual_features(image)
    text = hashed_text_features(" ".join([title, account, alt]))
    with db() as conn:
        cursor = conn.execute(
            """INSERT INTO images
            (sha256, local_path, source_url, article_url, article_title, account_name, alt_text,
             width, height, mime_type, visual_json, text_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                digest,
                filename,
                source_url,
                article_url,
                title,
                account,
                alt,
                width,
                height,
                "image/webp",
                json.dumps(visual),
                json.dumps(text),
                status,
                int(time.time()),
            ),
        )
        return int(cursor.lastrowid)


def profile_for(user_id: str) -> tuple[list[float], list[float], int]:
    with db() as conn:
        row = conn.execute("SELECT visual_json, text_json FROM user_profiles WHERE user_id = ?", (user_id,)).fetchone()
        count = conn.execute("SELECT COUNT(*) AS n FROM interactions WHERE user_id = ?", (user_id,)).fetchone()["n"]
    if not row:
        return zeros(), zeros(), count
    return json.loads(row["visual_json"]), json.loads(row["text_json"]), count


def event_weight(event: str, value: float) -> float:
    if event == "like":
        return 1.8
    if event == "dislike":
        return -2.0
    if event == "skip":
        return -0.65
    if event == "view":
        return max(-0.15, min(0.75, (value - 1.5) / 12.0))
    raise ValueError("event 必须是 view、like、dislike 或 skip")


def update_profile(user_id: str, visual: list[float], text: list[float], weight: float) -> None:
    old_visual, old_text, _ = profile_for(user_id)
    learning_rate = min(0.35, 0.12 + abs(weight) * 0.08)
    direction = 1.0 if weight >= 0 else -1.0
    new_visual = normalize([(1 - learning_rate) * a + learning_rate * direction * b for a, b in zip(old_visual, visual)])
    new_text = normalize([(1 - learning_rate) * a + learning_rate * direction * b for a, b in zip(old_text, text)])
    now = int(time.time())
    with db() as conn:
        conn.execute(
            """INSERT INTO user_profiles(user_id, visual_json, text_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET visual_json=excluded.visual_json,
            text_json=excluded.text_json, updated_at=excluded.updated_at""",
            (user_id, json.dumps(new_visual), json.dumps(new_text), now),
        )


def ranked_feed(user_id: str, limit: int) -> list[dict[str, Any]]:
    user_visual, user_text, interaction_count = profile_for(user_id)
    with db() as conn:
        rows = conn.execute(
            """SELECT * FROM images WHERE status = 'approved'
            ORDER BY id DESC LIMIT 500"""
        ).fetchall()
        recent = {
            row["image_id"]
            for row in conn.execute(
                "SELECT image_id FROM interactions WHERE user_id = ? ORDER BY created_at DESC LIMIT 120",
                (user_id,),
            ).fetchall()
        }
    now = time.time()
    scored: list[tuple[float, sqlite3.Row]] = []
    for row in rows:
        visual = json.loads(row["visual_json"])
        text = json.loads(row["text_json"])
        semantic = 0.62 * cosine(user_visual, visual) + 0.38 * cosine(user_text, text)
        freshness = math.exp(-max(0, now - row["created_at"]) / (86400 * 45))
        portrait = min(1.0, row["height"] / max(row["width"], 1) / 1.6)
        seen_penalty = 0.75 if row["id"] in recent else 0.0
        cold_start = (0.18 * freshness + 0.08 * portrait) if interaction_count < 5 else 0
        exploration = random.uniform(-0.07, 0.07)
        score = semantic + cold_start + 0.10 * freshness + exploration - seen_penalty
        scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    selected: list[dict[str, Any]] = []
    account_counts: Counter[str] = Counter()
    for score, row in scored:
        account = row["account_name"] or "未知来源"
        if account_counts[account] >= max(2, limit // 3):
            continue
        account_counts[account] += 1
        selected.append(
            {
                "id": row["id"],
                "image_url": f"/media/{row['id']}",
                "article_title": row["article_title"],
                "account_name": row["account_name"],
                "article_url": row["article_url"],
                "width": row["width"],
                "height": row["height"],
                "score": round(score, 4),
            }
        )
        if len(selected) >= limit:
            break
    return selected


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return INDEX_HTML


@app.get("/health")
def health() -> dict[str, Any]:
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM images WHERE status='approved'").fetchone()["n"]
    return {"ok": True, "approved_images": count}


@app.get("/api/feed")
def feed(user_id: str = Query(min_length=1, max_length=80), limit: int = Query(12, ge=1, le=30)) -> dict[str, Any]:
    return {"items": ranked_feed(user_id, limit)}


@app.post("/api/interactions")
def interaction(payload: InteractionRequest) -> dict[str, bool]:
    try:
        weight = event_weight(payload.event, payload.value)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    with db() as conn:
        row = conn.execute("SELECT visual_json, text_json FROM images WHERE id = ?", (payload.image_id,)).fetchone()
        if not row:
            raise HTTPException(404, "图片不存在")
        conn.execute(
            "INSERT INTO interactions(user_id, image_id, event, value, created_at) VALUES (?, ?, ?, ?, ?)",
            (payload.user_id, payload.image_id, payload.event, payload.value, int(time.time())),
        )
    update_profile(payload.user_id, json.loads(row["visual_json"]), json.loads(row["text_json"]), weight)
    return {"ok": True}


def ingest_article(
    article_url: str,
    *,
    image_status: str = "approved",
    expected_account: str = "",
    expected_biz: str = "",
) -> dict[str, Any]:
    article_url = validate_article_url(article_url)
    imported: list[int] = []
    skipped = 0
    errors: list[str] = []
    with httpx.Client(timeout=httpx.Timeout(20, connect=8), follow_redirects=False) as client:
        html, _ = fetch_limited(client, article_url, MAX_ARTICLE_BYTES)
        title, account, images = parse_article(html, article_url)
        account_biz = extract_account_biz(html)
        if expected_account and account != expected_account:
            raise ValueError(f"公众号名称不匹配：期望 {expected_account}，实际 {account or '未知'}")
        if expected_biz and account_biz != expected_biz:
            raise ValueError("公众号唯一标识不匹配")
        for item in images:
            try:
                content, _ = fetch_limited(client, item["url"], MAX_IMAGE_BYTES, article_url)
                image_id = save_image(
                    content,
                    item["url"],
                    article_url,
                    title,
                    account,
                    item["alt"],
                    status=image_status,
                )
                if image_id is None:
                    skipped += 1
                else:
                    imported.append(image_id)
            except Exception as exc:
                skipped += 1
                if len(errors) < 5:
                    errors.append(str(exc))
    return {
        "url": article_url,
        "title": title,
        "account": account,
        "account_biz": account_biz,
        "found": len(images),
        "imported": len(imported),
        "skipped": skipped,
        "ids": imported,
        "status": image_status,
        "errors": errors,
    }


@app.post("/api/ingest")
def ingest(payload: IngestRequest) -> dict[str, Any]:
    if not payload.rights_confirmed:
        raise HTTPException(400, "请先确认拥有采集与展示授权")
    if not payload.adult_confirmed:
        raise HTTPException(400, "请先确认来源内容中的人物均为成年人")
    try:
        return ingest_article(payload.url, image_status="approved")
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"公众号文章获取失败：{exc}") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/media/{image_id}")
def media(image_id: int):
    with db() as conn:
        row = conn.execute("SELECT local_path, mime_type, status FROM images WHERE id = ?", (image_id,)).fetchone()
    if not row or row["status"] != "approved":
        raise HTTPException(404, "图片不存在")
    path = (MEDIA_DIR / row["local_path"]).resolve()
    if MEDIA_DIR not in path.parents or not path.is_file():
        raise HTTPException(404, "图片文件不存在")
    return FileResponse(path, media_type=row["mime_type"], headers={"Cache-Control": "public, max-age=604800"})


@app.get("/api/moderation/pending")
def pending_images(limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    with db() as conn:
        rows = conn.execute(
            """SELECT id, article_title, account_name, article_url, width, height, created_at
            FROM images WHERE status='pending' ORDER BY id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return {
        "items": [
            {
                **dict(row),
                "preview_url": f"/api/moderation/media/{row['id']}",
            }
            for row in rows
        ]
    }


@app.get("/api/moderation/media/{image_id}")
def moderation_media(image_id: int):
    with db() as conn:
        row = conn.execute(
            "SELECT local_path, mime_type, status FROM images WHERE id = ?", (image_id,)
        ).fetchone()
    if not row or row["status"] not in {"pending", "approved"}:
        raise HTTPException(404, "图片不存在")
    path = (MEDIA_DIR / row["local_path"]).resolve()
    if MEDIA_DIR not in path.parents or not path.is_file():
        raise HTTPException(404, "图片文件不存在")
    return FileResponse(path, media_type=row["mime_type"], headers={"Cache-Control": "no-store"})


@app.patch("/api/images/{image_id}/moderation")
def moderate(image_id: int, payload: ModerationRequest) -> dict[str, bool]:
    if payload.status not in {"approved", "pending", "blocked"}:
        raise HTTPException(400, "status 必须是 approved、pending 或 blocked")
    with db() as conn:
        cursor = conn.execute("UPDATE images SET status = ? WHERE id = ?", (payload.status, image_id))
        if cursor.rowcount == 0:
            raise HTTPException(404, "图片不存在")
    return {"ok": True}


INDEX_HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>MuseFlow · 沉浸式图片流</title>
<style>
:root{color-scheme:dark;--glass:rgba(16,16,18,.58);--line:rgba(255,255,255,.15);--accent:#ff3b73}
*{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#050506;color:#fff;font-family:Inter,"PingFang SC","Microsoft YaHei",sans-serif}
button,input{font:inherit}.feed{height:100dvh;overflow-y:auto;scroll-snap-type:y mandatory;scroll-behavior:smooth;overscroll-behavior-y:contain;scrollbar-width:none}.feed::-webkit-scrollbar{display:none}
.card{height:100dvh;scroll-snap-align:start;scroll-snap-stop:always;position:relative;display:grid;place-items:center;background:#08080a;isolation:isolate}.card img{width:100%;height:100%;object-fit:contain;background:#08080a}.card:before{content:"";position:absolute;inset:0;background:var(--bg) center/cover;filter:blur(34px) brightness(.42);transform:scale(1.08);z-index:-1}.shade{position:absolute;inset:0;background:linear-gradient(180deg,rgba(0,0,0,.24),transparent 24%,transparent 55%,rgba(0,0,0,.82))}
.brand{position:fixed;z-index:20;left:max(18px,env(safe-area-inset-left));top:max(16px,env(safe-area-inset-top));display:flex;align-items:center;gap:10px;font-weight:800;letter-spacing:.4px;text-shadow:0 2px 18px #000}.brand i{width:11px;height:11px;border-radius:50%;background:linear-gradient(135deg,#ff7a45,var(--accent));box-shadow:0 0 20px var(--accent)}
.meta{position:absolute;left:max(18px,env(safe-area-inset-left));right:92px;bottom:max(22px,calc(env(safe-area-inset-bottom) + 18px));z-index:3;text-shadow:0 2px 10px #000}.account{font-size:16px;font-weight:750}.title{font-size:14px;line-height:1.55;margin-top:7px;color:rgba(255,255,255,.88);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}.source{display:inline-flex;color:#fff;text-decoration:none;font-size:12px;margin-top:10px;padding:7px 10px;border:1px solid var(--line);border-radius:999px;background:rgba(0,0,0,.28);backdrop-filter:blur(12px)}
.actions{position:absolute;right:max(14px,env(safe-area-inset-right));bottom:max(28px,calc(env(safe-area-inset-bottom) + 24px));z-index:4;display:flex;flex-direction:column;gap:15px}.action{width:54px;height:54px;border:1px solid var(--line);border-radius:50%;background:var(--glass);color:#fff;font-size:21px;display:grid;place-items:center;cursor:pointer;backdrop-filter:blur(16px);transition:.18s transform,.18s background}.action:hover{transform:scale(1.08)}.action.liked{background:var(--accent);border-color:transparent}.label{font-size:10px;text-align:center;margin-top:-10px;color:#ddd}
.admin{position:fixed;z-index:30;right:max(16px,env(safe-area-inset-right));top:max(14px,env(safe-area-inset-top));border:1px solid var(--line);border-radius:999px;padding:9px 13px;background:var(--glass);color:#fff;cursor:pointer;backdrop-filter:blur(15px)}dialog{border:1px solid var(--line);border-radius:24px;background:#151518;color:#fff;width:min(540px,calc(100% - 28px));padding:24px;box-shadow:0 28px 90px #000}dialog::backdrop{background:rgba(0,0,0,.72);backdrop-filter:blur(7px)}dialog h2{margin:0 0 8px}dialog p{color:#aaa;font-size:13px;line-height:1.65}dialog input[type=url]{width:100%;border:1px solid #34343a;border-radius:13px;padding:13px;background:#0c0c0e;color:#fff;outline:none}.checks{display:grid;gap:10px;margin:15px 0;color:#ddd;font-size:13px}.dialog-actions{display:flex;justify-content:flex-end;gap:10px}.btn{border:0;border-radius:12px;padding:10px 16px;cursor:pointer}.primary{background:linear-gradient(135deg,#ff6b45,var(--accent));color:#fff;font-weight:700}.secondary{background:#29292e;color:#fff}.status{min-height:22px;margin-top:12px;font-size:13px;color:#ffc56b}
.empty{height:100dvh;display:grid;place-items:center;text-align:center;padding:28px}.empty div{max-width:510px}.empty h1{font-size:clamp(34px,7vw,70px);margin:0;background:linear-gradient(135deg,#fff,#ff789d);-webkit-background-clip:text;color:transparent}.empty p{color:#999;line-height:1.8}.empty button{margin-top:10px}
@media(min-width:900px){.card img{width:min(56.25vh,45vw);border-left:1px solid #16161a;border-right:1px solid #16161a}.meta{left:calc(50% - min(28.125vh,22.5vw) + 20px);right:calc(50% - min(28.125vh,22.5vw) + 90px)}.actions{right:calc(50% - min(28.125vh,22.5vw) + 16px)}}
</style></head>
<body>
<div class="brand"><i></i>MuseFlow</div><button class="admin" onclick="openImport()">＋ 导入</button><main id="feed" class="feed"></main>
<dialog id="importDialog"><h2>导入公众号图片</h2><p>仅导入你拥有采集、存储和展示授权的公开公众号文章。系统不会绕过登录、验证码或访问限制。</p><input id="articleUrl" type="url" placeholder="https://mp.weixin.qq.com/s/..."><div class="checks"><label><input id="rights" type="checkbox"> 我确认拥有采集与展示授权</label><label><input id="adult" type="checkbox"> 我确认来源中的人物均为成年人</label></div><div class="dialog-actions"><button class="btn secondary" onclick="importDialog.close()">取消</button><button class="btn primary" onclick="ingest()">开始导入</button></div><div id="status" class="status"></div></dialog>
<script>
const feed=document.querySelector('#feed'),importDialog=document.querySelector('#importDialog');
const userId=localStorage.museflowUser||(localStorage.museflowUser='u_'+crypto.randomUUID());let loading=false,seen=new Set(),observer;
function openImport(){importDialog.showModal()}
async function api(path,options={}){const r=await fetch(path,{headers:{'Content-Type':'application/json',...(options.headers||{})},...options});if(!r.ok)throw new Error((await r.json().catch(()=>({}))).detail||`请求失败 ${r.status}`);return r.json()}
function card(item){const el=document.createElement('section');el.className='card';el.dataset.id=item.id;el.style.setProperty('--bg',`url("${item.image_url}")`);el.innerHTML=`<img src="${item.image_url}" alt="${esc(item.article_title||'图片')}" loading="eager"><div class="shade"></div><div class="meta"><div class="account">@${esc(item.account_name||'授权来源')}</div><div class="title">${esc(item.article_title||'未命名文章')}</div><a class="source" href="${esc(item.article_url)}" target="_blank" rel="noopener noreferrer">查看原文 ↗</a></div><div class="actions"><button class="action like" aria-label="喜欢">♥</button><div class="label">喜欢</div><button class="action skip" aria-label="不感兴趣">×</button><div class="label">不喜欢</div></div>`;el.querySelector('.like').onclick=e=>{e.currentTarget.classList.toggle('liked');track(item.id,'like',1)};el.querySelector('.skip').onclick=()=>{track(item.id,'dislike',1);el.nextElementSibling?.scrollIntoView({behavior:'smooth'})};return el}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function load(){if(loading)return;loading=true;try{const data=await api(`/api/feed?user_id=${encodeURIComponent(userId)}&limit=16`);const fresh=data.items.filter(x=>!seen.has(x.id));fresh.forEach(x=>{seen.add(x.id);feed.appendChild(card(x))});if(!feed.children.length)feed.innerHTML=`<div class="empty"><div><h1>你的灵感流</h1><p>还没有已审核图片。点击右上角“导入”，提交你拥有授权的公众号公开文章链接。完成导入后，视觉、文字语义与浏览行为会共同驱动推荐。</p><button class="btn primary" onclick="openImport()">导入第一篇文章</button></div></div>`;observe()}catch(e){console.error(e)}finally{loading=false}}
function observe(){observer?.disconnect();observer=new IntersectionObserver(entries=>entries.forEach(entry=>{if(entry.isIntersecting&&entry.intersectionRatio>.75){const el=entry.target;el.dataset.enter=Date.now();if(el===feed.lastElementChild)load()}else if(entry.target.dataset.enter){const sec=(Date.now()-Number(entry.target.dataset.enter))/1000;track(Number(entry.target.dataset.id),'view',sec);delete entry.target.dataset.enter}}),{root:feed,threshold:[.25,.75]});document.querySelectorAll('.card').forEach(x=>observer.observe(x))}
function track(image_id,event,value){if(!image_id)return;fetch('/api/interactions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:userId,image_id,event,value}),keepalive:true}).catch(()=>{})}
async function ingest(){const status=document.querySelector('#status');status.textContent='正在读取并分析图片，请稍候…';try{const result=await api('/api/ingest',{method:'POST',body:JSON.stringify({url:document.querySelector('#articleUrl').value,rights_confirmed:document.querySelector('#rights').checked,adult_confirmed:document.querySelector('#adult').checked})});status.textContent=`完成：发现 ${result.found} 张，收录 ${result.imported} 张，跳过 ${result.skipped} 张。`;if(result.imported){feed.innerHTML='';seen.clear();await load();setTimeout(()=>importDialog.close(),900)}}catch(e){status.textContent=e.message}}
feed.addEventListener('wheel',e=>{if(Math.abs(e.deltaY)<20)return;e.preventDefault();const cards=[...document.querySelectorAll('.card')],current=cards.findIndex(x=>Math.abs(x.getBoundingClientRect().top)<innerHeight*.5),next=Math.max(0,Math.min(cards.length-1,current+(e.deltaY>0?1:-1)));cards[next]?.scrollIntoView({behavior:'smooth'})},{passive:false});
addEventListener('keydown',e=>{if(['ArrowDown','PageDown',' '].includes(e.key))feed.scrollBy({top:innerHeight,behavior:'smooth'});if(['ArrowUp','PageUp'].includes(e.key))feed.scrollBy({top:-innerHeight,behavior:'smooth'})});load();
</script></body></html>'''
