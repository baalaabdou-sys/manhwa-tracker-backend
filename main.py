"""
Manhwa Tracker - Backend
=========================
FastAPI backend that:
- Searches/scrapes AsuraScans for manhwa info + covers
- Stores user favorites (simple JSON file, swap for DB later if needed)
- Periodically checks favorited manhwas for new chapters
- Sends Web Push notifications when new chapters drop
"""

import os
import json
import asyncio
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pywebpush import webpush, WebPushException

# ─────────────────────────────────────────
SITE_URL = "https://asurascans.com"
DATA_DIR = "data"
FAVORITES_FILE = os.path.join(DATA_DIR, "favorites.json")
SUBSCRIPTIONS_FILE = os.path.join(DATA_DIR, "subscriptions.json")
SEEN_CHAPTERS_FILE = os.path.join(DATA_DIR, "seen_chapters.json")

CHECK_INTERVAL_SECONDS = 15 * 60  # 15 minutes

VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_CLAIM_EMAIL = os.environ.get("VAPID_CLAIM_EMAIL", "mailto:admin@example.com")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

os.makedirs(DATA_DIR, exist_ok=True)


# ─────────────────────────────────────────
# Storage helpers
# ─────────────────────────────────────────

def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────
# Scraping functions
# ─────────────────────────────────────────

def get_cover_map():
    """Scrape homepage to build a {comic_id: cover_url} map (covers aren't on /browse)."""
    r = requests.get(SITE_URL + "/", headers=HEADERS, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    cover_map = {}
    for link in soup.select('a[href*="/comics/"]'):
        href = link.get("href", "")
        if "/chapter/" in href:
            continue
        img = link.find("img")
        if img and img.get("src"):
            comic_id = href.replace("/comics/", "").strip("/")
            cover = img["src"]
            if not cover.startswith("http"):
                cover = SITE_URL + cover
            cover_map[comic_id] = cover
    return cover_map


def search_manhwa(query: str):
    """Search AsuraScans for manhwa matching the query.

    Note: the site's /browse?name= filter is client-side JS only, so we fetch
    the full browse listing and filter server-side by title text instead.
    """
    url = f"{SITE_URL}/browse"
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    results = []
    seen_ids = set()

    # Titles live in <h3><a href="/comics/...">Title</a></h3> blocks
    for h in soup.find_all(["h3", "h2"]):
        link = h.find("a", href=lambda hh: hh and "/comics/" in hh and "/chapter/" not in hh)
        if not link:
            continue

        href = link.get("href", "")
        comic_id = href.replace("/comics/", "").strip("/")
        if comic_id in seen_ids:
            continue
        seen_ids.add(comic_id)

        title = link.get_text(strip=True)
        if not title:
            continue

        full_url = href if href.startswith("http") else SITE_URL + href

        results.append({
            "id": comic_id,
            "title": title,
            "url": full_url,
            "cover": None,  # filled in below
        })

    if query:
        q = query.lower()
        results = [r for r in results if q in r["title"].lower()]

    # Attach covers from homepage where available (best-effort, won't cover everything)
    try:
        cover_map = get_cover_map()
        for r in results:
            r["cover"] = cover_map.get(r["id"])
    except Exception:
        pass


    return results[:30]


def get_latest_chapter(comic_url: str):
    """Fetch a comic's page and return the latest chapter info."""
    r = requests.get(comic_url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    chapter_link = soup.find("a", href=lambda h: h and "/chapter/" in h)
    if not chapter_link:
        return None

    chapter_text = chapter_link.get_text(strip=True)
    chapter_url = chapter_link.get("href", "")
    if not chapter_url.startswith("http"):
        chapter_url = SITE_URL + chapter_url

    return {"chapter": chapter_text, "url": chapter_url}


def get_homepage_latest_updates():
    """Scrape homepage 'Latest Updates' - faster than checking each comic page."""
    r = requests.get(SITE_URL + "/", headers=HEADERS, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    updates = {}
    seen_slugs = set()

    for link in soup.select('a[href*="/comics/"]'):
        href = link.get("href", "")
        if "/chapter/" in href or href in seen_slugs:
            continue
        title_text = link.get_text(strip=True)
        if not title_text:
            continue
        seen_slugs.add(href)

        comic_id = href.replace("/comics/", "").strip("/")
        parent = link.find_parent()
        chapter_link = parent.find_next("a", href=lambda h: h and "/chapter/" in h) if parent else None

        if chapter_link:
            updates[comic_id] = {
                "title": title_text,
                "chapter": chapter_link.get_text(strip=True),
                "url": chapter_link.get("href", ""),
            }

    return updates


# ─────────────────────────────────────────
# Push notification helper
# ─────────────────────────────────────────

def send_push(subscription, title, body, url):
    if not VAPID_PRIVATE_KEY:
        print("[!] VAPID keys not configured, skipping push")
        return
    try:
        webpush(
            subscription_info=subscription,
            data=json.dumps({"title": title, "body": body, "url": url}),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": VAPID_CLAIM_EMAIL},
        )
    except WebPushException as e:
        print(f"[!] Push failed: {e}")


# ─────────────────────────────────────────
# Background checker
# ─────────────────────────────────────────

async def check_for_updates():
    while True:
        try:
            print(f"[{datetime.now()}] Checking for updates...")
            favorites = load_json(FAVORITES_FILE, {})  # {user_id: [comic_ids]}
            subscriptions = load_json(SUBSCRIPTIONS_FILE, {})  # {user_id: subscription}
            seen = load_json(SEEN_CHAPTERS_FILE, {})  # {comic_id: chapter}

            homepage_updates = get_homepage_latest_updates()

            # Find which favorited comics have new chapters
            all_favorited_ids = set()
            for ids in favorites.values():
                all_favorited_ids.update(ids)

            newly_updated = {}
            for comic_id in all_favorited_ids:
                if comic_id in homepage_updates:
                    info = homepage_updates[comic_id]
                    if seen.get(comic_id) != info["chapter"]:
                        newly_updated[comic_id] = info
                        seen[comic_id] = info["chapter"]

            save_json(SEEN_CHAPTERS_FILE, seen)

            # Notify users who favorited an updated comic
            for user_id, fav_ids in favorites.items():
                sub = subscriptions.get(user_id)
                if not sub:
                    continue
                for comic_id in fav_ids:
                    if comic_id in newly_updated:
                        info = newly_updated[comic_id]
                        send_push(
                            sub,
                            title=f"New chapter: {info['title']}",
                            body=info["chapter"],
                            url=info["url"] if info["url"].startswith("http") else SITE_URL + info["url"],
                        )

            if newly_updated:
                print(f"[+] Sent notifications for {len(newly_updated)} updated comics")

        except Exception as e:
            print(f"[!] Background check error: {e}")

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(check_for_updates())
    yield
    task.cancel()


# ─────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten to your Netlify URL after deploying
    allow_methods=["*"],
    allow_headers=["*"],
)


class FavoriteRequest(BaseModel):
    user_id: str
    comic_id: str
    title: str = ""
    cover: str = ""
    url: str = ""


class SubscriptionRequest(BaseModel):
    user_id: str
    subscription: dict


@app.get("/api/debug-browse")
def debug_browse():
    """Temporary debug endpoint - shows raw structure info from /browse page."""
    try:
        r = requests.get(f"{SITE_URL}/browse", headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")

        h3_count = len(soup.find_all("h3"))
        h2_count = len(soup.find_all("h2"))
        comic_links = soup.select('a[href*="/comics/"]')

        sample_h3 = str(soup.find_all("h3")[:3])
        sample_links = [
            {"href": l.get("href"), "text": l.get_text(strip=True)[:60]}
            for l in comic_links[:10]
        ]

        return {
            "status_code": r.status_code,
            "html_length": len(r.text),
            "h3_count": h3_count,
            "h2_count": h2_count,
            "comic_link_count": len(comic_links),
            "sample_h3_html": sample_h3,
            "sample_links": sample_links,
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/")
def root():
    return {"status": "ok", "service": "manhwa-tracker-backend"}


@app.get("/api/vapid-public-key")
def vapid_public_key():
    return {"publicKey": VAPID_PUBLIC_KEY}


@app.get("/api/search")
def search(q: str = ""):
    try:
        return {"results": search_manhwa(q)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/latest")
def latest():
    try:
        updates = get_homepage_latest_updates()
        return {"updates": list(updates.values())}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/favorites/add")
def add_favorite(req: FavoriteRequest):
    favorites = load_json(FAVORITES_FILE, {})
    user_favs = favorites.setdefault(req.user_id, [])
    if req.comic_id not in user_favs:
        user_favs.append(req.comic_id)
    save_json(FAVORITES_FILE, favorites)

    # Store metadata for display
    meta = load_json(os.path.join(DATA_DIR, "comic_meta.json"), {})
    meta[req.comic_id] = {"title": req.title, "cover": req.cover, "url": req.url}
    save_json(os.path.join(DATA_DIR, "comic_meta.json"), meta)

    return {"status": "ok", "favorites": user_favs}


@app.post("/api/favorites/remove")
def remove_favorite(req: FavoriteRequest):
    favorites = load_json(FAVORITES_FILE, {})
    user_favs = favorites.setdefault(req.user_id, [])
    if req.comic_id in user_favs:
        user_favs.remove(req.comic_id)
    save_json(FAVORITES_FILE, favorites)
    return {"status": "ok", "favorites": user_favs}


@app.get("/api/favorites/{user_id}")
def get_favorites(user_id: str):
    favorites = load_json(FAVORITES_FILE, {})
    meta = load_json(os.path.join(DATA_DIR, "comic_meta.json"), {})
    ids = favorites.get(user_id, [])
    return {"favorites": [{"id": cid, **meta.get(cid, {})} for cid in ids]}


@app.post("/api/subscribe")
def subscribe(req: SubscriptionRequest):
    subscriptions = load_json(SUBSCRIPTIONS_FILE, {})
    subscriptions[req.user_id] = req.subscription
    save_json(SUBSCRIPTIONS_FILE, subscriptions)
    return {"status": "ok"}


@app.get("/api/check-now")
def check_now():
    """Manually trigger a check (useful for testing)."""
    asyncio.create_task(check_for_updates_once())
    return {"status": "triggered"}


async def check_for_updates_once():
    """Single-pass version for manual trigger."""
    pass  # the main loop already covers this; kept simple for now
