#!/usr/bin/env python3
"""Generate genre mashup pitches with AI titles, prompts, posters, and a gallery page.

The pipeline runs the LLM (title + Z-Image prompt) and ComfyUI (image render)
concurrently — while ComfyUI renders row N's poster, the LLM is already drafting
row N+1's title/prompt. Each completed row gets written to the CSV and the HTML
gallery, so a partial run is still useful.

Requires:
    - llama.cpp server (default http://127.0.0.1:9503)
    - ComfyUI server with the Z-Image art workflow loaded (default 127.0.0.1:8188)
    - websocket-client and pillow installed (use the realOrAi venv if you have one)

Usage:
    python generate_mashups.py <count> [output.csv]
                               [--llm-server URL] [--comfy-server HOST:PORT]
                               [--images-dir DIR] [--html FILE] [--no-images]

Example:
    python generate_mashups.py 30
    python generate_mashups.py 5 --no-images       # CSV only, no posters
    /path/to/realOrAi/tools/.venv/bin/python generate_mashups.py 50
"""

import argparse
import concurrent.futures
import copy
import csv
import json
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from html import escape as html_escape
from pathlib import Path

GENRES = {
    "Horror": [
        "Folk Horror (rural cult vibes)",
        "Body Horror (Cronenberg-core)",
        "Found Footage",
        "Cosmic / Lovecraftian",
        "Slasher (masked maniac)",
        "Haunted Appliance",
        "Suburban Satanic Panic",
        "Eco-Horror (the trees are mad)",
        "Dental Horror",
    ],
    "Romance": [
        "Regency Bodice-Ripper",
        "Enemies-to-Lovers",
        "Monster Romance (he is a kraken)",
        "Mafia Romance",
        "Small-Town Christmas Romance",
        "Time-Travel Romance",
        "Workplace Slow-Burn",
        "Marriage of Convenience",
        "Forbidden Beekeeper Romance",
    ],
    "Sci-Fi": [
        "Cyberpunk (neon and noodles)",
        "Solarpunk (gay agrarian future)",
        "Space Western",
        "Hard Sci-Fi (engineers explaining things)",
        "Biopunk (squelchy laboratory)",
        "Retrofuturism (1962's tomorrow)",
        "Dying Earth / Far Future",
        "First Contact Bureaucracy",
        "Mundane Apocalypse",
    ],
    "Fantasy": [
        "Grimdark (everyone's sad and damp)",
        "Cozy Fantasy (tea and dragons)",
        "Urban Fantasy",
        "Sword & Sorcery",
        "Portal Fantasy",
        "Magical Academia",
        "Flintlock Fantasy (muskets + magic)",
        "Mythic Retelling",
        "Bureaucratic Fantasy (the Wizard's HR dept)",
    ],
    "Mystery": [
        "Cozy Village Whodunit",
        "Hardboiled Detective",
        "Locked Room Mystery",
        "Nordic Noir (everyone is cold)",
        "Amateur Sleuth (a baker did it)",
        "Forensic Procedural",
        "Conspiracy Thriller",
        "Cold Case Podcast Mystery",
        "Cryptozoological Investigation",
    ],
    "Comedy": [
        "Cringe Mockumentary",
        "Screwball",
        "Stoner Comedy",
        "Satire (eat the rich)",
        "Workplace Comedy",
        "Gross-Out",
        "Surreal Anti-Humor",
        "Farce (so many doors)",
        "Wholesome Himbo Comedy",
    ],
    "Drama": [
        "Prestige Misery Drama",
        "Coming-of-Age",
        "Legal Drama",
        "Medical Drama (very sweaty)",
        "Family Saga",
        "Sports Underdog",
        "Period Piece (corsets, scandals)",
        "Slow Cinema (a man stares at a lake)",
        "Restaurant Kitchen Drama",
    ],
    "Action": [
        "Heist",
        "Spy Thriller",
        "Martial Arts",
        "Disaster Movie",
        "Revenge Thriller (he killed my dog)",
        "Buddy Cop",
        "Military Sci-Fi",
        "Parkour Chase Movie",
        "Vehicular Mayhem",
    ],
    "Documentary-style": [
        "True Crime (let me look at this map)",
        "Nature Documentary (gentle British narrator)",
        "Sports Doc",
        "Cult Exposé",
        "Food Porn Doc",
        "Music Bio-Doc",
        "Conspiracy Doc",
        "Reality Competition",
    ],
    "Weird Niche": [
        "Liminal Space Horror",
        "Hauntology (lost media core)",
        "Hopepunk",
        "Dieselpunk",
        "Mall Mythology",
        "Y2K Techno-Paranoia",
        "Gentle Apocalypse",
        "Bardcore Medieval",
        "Backrooms Bureaucracy",
    ],
    "Western": [
        "Acid Western (peyote-fueled)",
        "Spaghetti Western",
        "Weird West (cowboys + monsters)",
        "Modern Neo-Western",
        "Revisionist Western",
    ],
    "Musical": [
        "Jukebox Musical",
        "Rock Opera",
        "Sad Indie Folk Musical",
        "Crime Musical (everyone sings about taxes)",
        "Surrealist Dance Musical",
    ],
}

CHARACTERS = [
    "Disgraced Sommelier", "Sentient Roomba", "Retired Assassin",
    "Conspiracy-Theorist Librarian", "Goth Accountant", "Himbo Pirate",
    "Tax-Dodging Witch", "Time-Traveling Plumber", "Cursed Beauty Pageant Winner",
    "Bog Witch", "Disillusioned Cult Defector", "Call Center Vampire",
    "Talking Horse", "Reluctant Middle Manager", "Former Child Star",
    "Doomsday Prepper Grandma", "Legalese-Speaking Ghost", "Influencer Exorcist",
    "Spare Royal Twin", "Mall Cop", "Anxious Cryptozoologist", "Gardening AI",
    "Fading Magician's Assistant", "Mediocre Knight", "Superhero Therapist",
    "Stand-Up Plague Doctor", "Disgraced Olympic Curler", "Union-Rep Fairy Godmother",
    "Cowboy Astronaut", "Reformed Demon", "Substitute Teacher",
    "Polite Cannibal Food Critic", "Anxious Werewolf", "Ex-Pop-Star Goat Farmer",
    "Underworld Bureaucrat", "Failed Wizard", "Disgruntled Tooth Fairy",
    "Sentient Houseplant", "Medieval Town Crier", "Murder-Hobo Adventurer",
    "Mediocre Oracle", "Burned-Out Knight", "Rideshare-Driving Centaur",
    "Theater Kid Detective", "Failed Mall Santa", "Disgraced Bee Inspector",
    "HOA-President Lich", "Reluctant Pope", "Off-Brand Superhero",
    "Suburban Dad Necromancer", "Gen-Z Pirate", "Kindergarten Teacher Spy",
    "Sentient Vending Machine", "Aging Boy Band Member", "Renaissance Fair Champion",
    "DMV Clerk Demigod",
]

QUIRKS = [
    "with a heart of gold", "seeking redemption", "with PTSD",
    "who runs a bakery", "who has not paid her taxes",
    "with strong opinions about jazz", "in a polycule",
    "doing court-ordered community service", "going through a messy divorce",
    "training for a marathon", "with prophetic dreams", "with crippling anxiety",
    "who just wants to garden", "with a podcast nobody listens to",
    "haunted by a single regret", "who is allergic to their own job",
    "writing a memoir", "with three ex-husbands and a parrot",
    "who learned everything from YouTube", "secretly running a Ponzi scheme",
    "raising a teenager alone", "who only speaks in movie quotes",
    "competing on a reality show", "with a vendetta against a specific seagull",
    "in their flop era", "who pivoted to crypto last year", "trying veganism",
    "in witness protection", "with a doctorate in medieval poetry",
    "who is also a twin (it's relevant)", "running for local office",
    "stuck in a time loop", "with a court-mandated emotional support animal",
    "going through perimenopause", "newly sober and rage-y about it",
    "who just inherited a haunted mansion", "afraid of birds (specifically)",
    "moonlighting as a wedding singer", "with one eye and a grudge",
    "who has been replaced by a doppelgänger and nobody noticed",
    "currently being sued", "trying to win back their ex",
    "secretly the chosen one (don't tell them)", "with golden retriever energy",
    "who has seen things, man", "in deep with the wrong people",
    "doing a TED Talk circuit", "raising bees as therapy",
    "who never finished their PhD", "with a mysterious birthmark",
    "currently ghosting their family group chat", "two weeks from retirement",
    "obsessed with a single Wikipedia article", "running a failing food truck",
    "who married into a cult by accident", "with a rival for some reason",
]

PITCH_TEMPLATES = [
    "It's {g1} meets {g2}... but the protagonist is a {c} {q}.",
    "Imagine if a {g1} show had a baby with a {g2} miniseries, and that baby grew up to be a {c} {q}.",
    "Set in a world where {g1} and {g2} coexist uneasily, our hero — a {c} {q} — must save... something. Probably a town.",
    "Pitch: {g1} x {g2}. Tone: dread, but make it horny. Lead: a {c} {q}.",
    "A {c} {q} stumbles into a {g1} conspiracy that can only be solved using the rules of {g2}.",
    "Picture a {g1} setting, but everyone behaves like they're in a {g2}. Our reluctant hero: a {c} {q}.",
    "Logline: When a {c} {q} discovers their quiet life is actually a {g1}, they must master the genre conventions of {g2} to survive.",
    "Three-act structure: {g1} in act one, {g2} by act three, and a {c} {q} sobbing into a microwave dinner the whole time.",
    "Cold open: a {c} {q}, alone, in the rain. The genre? {g1}. The vibe? Unmistakably {g2}. Cue title card.",
]


def generate_mashup():
    major1 = random.choice(list(GENRES.keys()))
    major2 = random.choice([m for m in GENRES if m != major1])
    sub1 = random.choice(GENRES[major1])
    sub2 = random.choice(GENRES[major2])
    character = random.choice(CHARACTERS)
    quirk = random.choice(QUIRKS)
    template = random.choice(PITCH_TEMPLATES)
    pitch = template.format(g1=sub1, g2=sub2, c=character, q=quirk)
    return {
        "genre_1_major": major1,
        "genre_1_sub": sub1,
        "genre_2_major": major2,
        "genre_2_sub": sub2,
        "character": character,
        "quirk": quirk,
        "pitch": pitch,
    }


SYSTEM_PROMPT = """You are a creative director writing absurd, funny film concepts.

For each mashup you receive, invent:
1. A punchy, ridiculous fake film title (3-7 words). It should feel like a real movie title — sometimes with a colon and subtitle. No emojis.
2. A 3-5 sentence streaming-service-style synopsis (a "logline-plus") that sells the film. Make it funny — lean into the absurd genre clash and protagonist quirk. Hint at stakes, central conflict, and tone, but stay tight. Roughly 50-100 words.
3. A detailed prompt for the Z-Image stable diffusion model to generate a movie poster.

Rules for the image prompt:
- Use long, descriptive, complete sentences (not comma-separated tags). Z-Image responds well to natural language.
- Describe the scene, the central character, their costume and expression, the setting, the lighting, color palette, and visual mood.
- Explicitly include the film title as visible text on the poster, e.g.: large bold movie title text reading "THE TITLE HERE" across the top.
- Optionally include a tagline as visible text underneath.
- Aim for 80-150 words. Cinematic, painterly, vivid.
- Do NOT mention real actors or real franchises.

Think as much as you want first. Then output your final answer wrapped in EXACTLY these three XML tags, on their own lines, with nothing else inside them:

<title>The Film Title Goes Here</title>
<synopsis>The 3-5 sentence funny streaming-style synopsis goes here.</synopsis>
<positive>The full Z-Image prompt goes here as one or more long sentences, including the title in quotes as visible poster text.</positive>

The tags must appear after any thinking. Do not nest tags. Do not add attributes."""


def build_user_prompt(mashup):
    return (
        f"Genre 1: {mashup['genre_1_sub']} (a {mashup['genre_1_major']} subgenre)\n"
        f"Genre 2: {mashup['genre_2_sub']} (a {mashup['genre_2_major']} subgenre)\n"
        f"Protagonist: {mashup['character']} {mashup['quirk']}\n"
        f"Throwaway pitch (for inspiration only — do not just rewrite it): {mashup['pitch']}\n\n"
        f"Now produce the title, synopsis, and image prompt in the required tag format."
    )


TITLE_RE = re.compile(r"<title>\s*(.+?)\s*</title>", re.DOTALL | re.IGNORECASE)
SYNOPSIS_RE = re.compile(r"<synopsis>\s*(.+?)\s*</synopsis>", re.DOTALL | re.IGNORECASE)
POSITIVE_RE = re.compile(r"<positive>\s*(.+?)\s*</positive>", re.DOTALL | re.IGNORECASE)


def _post_chat(server_url, messages, timeout=600):
    payload = {
        "messages": messages,
        "temperature": 0.95,
        "top_p": 0.95,
        "max_tokens": 16384,
        "stream": False,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{server_url.rstrip('/')}/v1/chat/completions",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    msg = body["choices"][0]["message"]
    # Prefer the actual answer in `content`; fall back to reasoning_content if the
    # model only emitted tags inside its thinking stream.
    content = (msg.get("content") or "").strip()
    reasoning = (msg.get("reasoning_content") or "").strip()
    return content, reasoning


def _extract(text):
    """Return (title, synopsis, positive) using the LAST tag occurrence, so example
    quotations inside reasoning lose to the model's actual final answer."""
    title_matches = TITLE_RE.findall(text)
    syn_matches = SYNOPSIS_RE.findall(text)
    pos_matches = POSITIVE_RE.findall(text)
    if not title_matches or not syn_matches or not pos_matches:
        return None, None, None
    title = title_matches[-1].strip().strip('"').strip("'").strip()
    synopsis = syn_matches[-1].strip()
    positive = pos_matches[-1].strip()
    # Sanity checks
    if not title or "\n" in title or len(title) > 120 or len(title.split()) > 12:
        return None, None, None
    if len(synopsis) < 30 or len(synopsis) > 2000:
        return None, None, None
    if len(positive) < 50 or len(positive) > 4000:
        return None, None, None
    return title, synopsis, positive


def call_llama_server(server_url, mashup, max_attempts=3, timeout=300):
    """Call llama.cpp chat completions and extract <title>/<synopsis>/<positive> tags.

    Retries on missing/malformed tags up to max_attempts.
    Returns (title, synopsis, positive). Raises RuntimeError if all attempts fail.
    """
    last_error = None
    last_snippet = ""

    for attempt in range(1, max_attempts + 1):
        try:
            messages = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(mashup)},
            ]
            if attempt > 1:
                messages.append({
                    "role": "user",
                    "content": (
                        "Your previous response did not contain valid "
                        "<title>...</title>, <synopsis>...</synopsis>, and "
                        "<positive>...</positive> tags. Please respond again following "
                        "the exact tag format from the system instructions."
                    ),
                })

            content, reasoning = _post_chat(server_url, messages, timeout=timeout)
            title, synopsis, positive = _extract(content)
            if not (title and synopsis and positive):
                title, synopsis, positive = _extract(content + "\n" + reasoning)
            if title and synopsis and positive:
                return title, synopsis, positive
            last_error = "tags not found / failed validation"
            last_snippet = (content[-200:] or reasoning[-200:]).replace("\n", " ")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last_error = f"network: {e}"
        except Exception as e:
            last_error = f"unexpected: {e}"

    raise RuntimeError(f"{last_error} (last response snippet: {last_snippet!r})")


def format_eta(seconds):
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


# ----------------------------------------------------------------------------
# ComfyUI client (Z-Image art workflow)
# ----------------------------------------------------------------------------

WORKFLOW_FILE = Path(__file__).parent / "comfy_art_workflow_api.json"
NODE_POSITIVE_PROMPT = "133"
NODE_NEGATIVE_PROMPT = "132"
NODE_LATENT = "130"
NODE_SEED = "509"
SEED_KEY = "noise_seed"
POSTER_W, POSTER_H = 1280, 1664  # 3:4 portrait, ~2 MP, multiples of 64


def _load_workflow():
    if not WORKFLOW_FILE.exists():
        raise FileNotFoundError(
            f"Workflow file not found: {WORKFLOW_FILE}. "
            "Copy comfy_art_workflow_api.json from the realOrAi/tools/workflows directory."
        )
    return json.loads(WORKFLOW_FILE.read_text())


def _patch_workflow(base, positive_prompt, seed):
    wf = copy.deepcopy(base)
    wf[NODE_POSITIVE_PROMPT]["inputs"]["text"] = positive_prompt
    wf[NODE_NEGATIVE_PROMPT]["inputs"]["text"] = "watermark, blurry, low quality, distorted text, misspelled text"
    wf[NODE_LATENT]["inputs"]["width"] = POSTER_W
    wf[NODE_LATENT]["inputs"]["height"] = POSTER_H
    wf[NODE_SEED]["inputs"][SEED_KEY] = seed
    return wf


class ComfyClient:
    def __init__(self, server):
        # Accept "host:port" or "http://host:port"
        if "://" in server:
            server = urllib.parse.urlparse(server).netloc or server.split("://", 1)[1]
        self.server = server.rstrip("/")
        self.client_id = str(uuid.uuid4())

    def _queue(self, prompt, prompt_id):
        data = json.dumps({
            "prompt": prompt,
            "client_id": self.client_id,
            "prompt_id": prompt_id,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"http://{self.server}/prompt",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def _view(self, filename, subfolder, ftype):
        qs = urllib.parse.urlencode({"filename": filename, "subfolder": subfolder, "type": ftype})
        with urllib.request.urlopen(f"http://{self.server}/view?{qs}", timeout=120) as r:
            return r.read()

    def _history(self, prompt_id):
        with urllib.request.urlopen(f"http://{self.server}/history/{prompt_id}", timeout=30) as r:
            return json.loads(r.read())

    def generate(self, prompt):
        try:
            import websocket  # websocket-client
        except ImportError:
            raise RuntimeError(
                "websocket-client is required for ComfyUI. "
                "Run with the realOrAi venv or: pip install websocket-client"
            )
        ws = websocket.WebSocket()
        ws.connect(f"ws://{self.server}/ws?clientId={self.client_id}", timeout=30)
        try:
            prompt_id = str(uuid.uuid4())
            self._queue(prompt, prompt_id)
            while True:
                msg = ws.recv()
                if not isinstance(msg, str):
                    continue
                data = json.loads(msg)
                t = data.get("type")
                if t == "executing":
                    d = data["data"]
                    if d.get("node") is None and d.get("prompt_id") == prompt_id:
                        break
                elif t == "execution_error":
                    raise RuntimeError(f"Comfy execution error: {data.get('data')}")
        finally:
            ws.close()

        hist = self._history(prompt_id).get(prompt_id, {})
        for _node, output in hist.get("outputs", {}).items():
            for img in output.get("images", []) or []:
                return self._view(img["filename"], img.get("subfolder", ""), img.get("type", "output"))
        raise RuntimeError("No image in ComfyUI history output")


# ----------------------------------------------------------------------------
# HTML gallery
# ----------------------------------------------------------------------------

HTML_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Genre Masher: AI-Generated Pitches</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Abril+Fatface&family=Lora:ital,wght@0,400;0,600;1,400&family=Oswald:wght@600;700&display=swap" rel="stylesheet">
<style>
  * { box-sizing: border-box; }
  body {
    font-family: 'Lora', Georgia, 'Times New Roman', serif;
    background: #0e0a1a;
    color: #f4eafc;
    margin: 0; padding: 40px 20px;
  }
  h1 { font-family: 'Abril Fatface', 'Playfair Display', Georgia, serif;
       font-size: clamp(2.5rem, 6vw, 4.5rem); text-align: center; margin: 0 0 6px;
       text-shadow: 4px 4px 0 #000, -2px -2px 0 #ff006e; letter-spacing: 1px;
       font-weight: normal; }
  .tagline { text-align: center; opacity: 0.7; font-style: italic; margin-bottom: 50px;
             font-size: 1.1rem; }
  .grid { display: grid; gap: 48px; max-width: 1600px; margin: 0 auto;
          grid-template-columns: repeat(auto-fill, minmax(520px, 1fr)); }
  .card { background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.1);
          border-radius: 14px; overflow: hidden; display: flex; flex-direction: column;
          transition: transform 0.2s, box-shadow 0.2s; }
  .card:hover { transform: translateY(-4px); box-shadow: 0 14px 36px rgba(255,0,110,0.3); }
  .poster { width: 100%; aspect-ratio: 3/4; object-fit: cover; background: #1a0033;
            display: block; }
  .poster.missing { display: flex; align-items: center; justify-content: center;
                    color: rgba(255,255,255,0.3); font-style: italic; padding: 20px;
                    text-align: center; aspect-ratio: 3/4; }
  .meta { padding: 26px 30px 28px; }
  .title { font-family: 'Oswald', 'Helvetica Neue', sans-serif;
           font-size: 1.7rem; font-weight: 700; margin: 0 0 12px; line-height: 1.15;
           letter-spacing: 0.5px; text-transform: uppercase; }
  .badges { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 14px; }
  .badge { font-family: 'Oswald', sans-serif; font-size: 0.75rem; padding: 4px 12px;
           border-radius: 12px; background: rgba(255,190,11,0.2); color: #ffbe0b;
           letter-spacing: 1px; text-transform: uppercase; }
  .pitch { font-family: 'Lora', Georgia, serif; font-size: 1.05rem; line-height: 1.6;
           opacity: 0.92; margin: 0 0 14px; }
  .character { font-family: 'Lora', Georgia, serif; font-size: 0.95rem; opacity: 0.75;
                font-style: italic; }
  details { margin-top: 14px; font-size: 0.9rem; opacity: 0.65; }
  details summary { cursor: pointer; font-family: 'Oswald', sans-serif;
                    text-transform: uppercase; letter-spacing: 1px; font-size: 0.8rem; }
  details pre { white-space: pre-wrap; word-break: break-word; margin: 4px 0 12px;
                font-family: 'Lora', Georgia, serif; font-size: 0.9rem; line-height: 1.55; }
  .bts-label { font-family: 'Oswald', sans-serif; font-weight: bold; margin-top: 10px;
                opacity: 0.85; text-transform: uppercase; letter-spacing: 0.5px;
                font-size: 0.8rem; }
  footer { text-align: center; opacity: 0.55; margin-top: 60px; font-size: 0.9rem;
            font-style: italic; line-height: 1.7; }
  footer a { color: #ffbe0b; text-decoration: none;
              border-bottom: 1px dashed rgba(255,190,11,0.5); }
  footer a:hover { color: #fff; border-bottom-color: #fff; }
  .breadcrumb {
    position: absolute;
    top: 16px; left: 20px;
    font-size: 0.85rem;
    opacity: 0.65;
  }
  .breadcrumb a {
    color: #fff;
    text-decoration: none;
    border-bottom: 1px dashed rgba(255,255,255,0.4);
    margin-right: 14px;
  }
  .breadcrumb a:hover { opacity: 1; border-bottom-color: #fff; }
</style>
</head>
<body>
  <div class="breadcrumb">
    <a href="../">&larr; one-offs</a>
    <a href="index.html">&larr; play the game</a>
  </div>
  <h1>Genre Masher 3000</h1>
  <div class="tagline">AI-generated pitches that nobody asked for</div>
  <div class="grid">
"""

HTML_FOOT = """  </div>
  <footer>
    Generated by generate_mashups.py &middot; {count} pitches &middot; {timestamp}<br>
    Posters rendered with <a href="https://www.comfy.org/" target="_blank" rel="noopener">ComfyUI</a>
    using a Z-Image art workflow by
    <a href="https://www.patreon.com/c/aitrepreneur" target="_blank" rel="noopener">Aitrepreneur</a>.
  </footer>
</body>
</html>
"""


def write_gallery(html_path, rows, images_subdir):
    cards = []
    for row in rows:
        title = row.get("title") or "(untitled)"
        synopsis = row.get("synopsis") or ""
        seed_pitch = row.get("pitch") or ""
        g1 = row.get("genre_1_sub") or ""
        g2 = row.get("genre_2_sub") or ""
        character = row.get("character") or ""
        quirk = row.get("quirk") or ""
        image_prompt = row.get("image_prompt") or ""
        image_file = row.get("image_file") or ""

        if image_file:
            poster_html = (
                f'<img class="poster" loading="lazy" '
                f'src="{html_escape(images_subdir)}/{html_escape(image_file)}" '
                f'alt="{html_escape(title)}">'
            )
        else:
            poster_html = '<div class="poster missing">No poster generated</div>'

        body_text = synopsis or seed_pitch  # fall back to seed pitch if LLM failed

        cards.append(f"""    <div class="card">
      {poster_html}
      <div class="meta">
        <div class="title">{html_escape(title)}</div>
        <div class="badges">
          <span class="badge">{html_escape(g1)}</span>
          <span class="badge">x</span>
          <span class="badge">{html_escape(g2)}</span>
        </div>
        <p class="pitch">{html_escape(body_text)}</p>
        <div class="character">Starring: <b>{html_escape(character)}</b> {html_escape(quirk)}</div>
        <details><summary>Behind the scenes</summary>
          <div class="bts-label">Seed pitch:</div><pre>{html_escape(seed_pitch)}</pre>
          <div class="bts-label">Image prompt:</div><pre>{html_escape(image_prompt)}</pre>
        </details>
      </div>
    </div>""")

    html = HTML_HEAD + "\n".join(cards) + "\n" + HTML_FOOT.format(
        count=len(rows),
        timestamp=time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    html_path.write_text(html, encoding="utf-8")


# ----------------------------------------------------------------------------
# Pipeline
# ----------------------------------------------------------------------------

SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text, max_len=60):
    s = SLUG_RE.sub("-", text.lower()).strip("-")
    return (s or "untitled")[:max_len]


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("count", type=int, help="Number of mashups to generate")
    parser.add_argument("output", type=Path, nargs="?", default=Path("mashups.csv"),
                        help="Output CSV path (default: mashups.csv)")
    parser.add_argument("--llm-server", default="http://127.0.0.1:9503",
                        help="llama.cpp server URL (default: http://127.0.0.1:9503)")
    parser.add_argument("--comfy-server", default="127.0.0.1:8188",
                        help="ComfyUI server host:port (default: 127.0.0.1:8188)")
    parser.add_argument("--images-dir", type=Path, default=Path("images"),
                        help="Directory to write poster PNGs (default: ./images)")
    parser.add_argument("--html", type=Path, default=Path("mashups.html"),
                        help="Output HTML gallery path (default: mashups.html)")
    parser.add_argument("--no-images", action="store_true",
                        help="Skip ComfyUI image generation")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip LLM (no titles/prompts/images, just base mashups)")
    args = parser.parse_args()

    if args.count < 1:
        print("Error: count must be at least 1.", file=sys.stderr)
        sys.exit(1)

    do_images = not args.no_images and not args.no_llm

    fieldnames = ["genre_1_major", "genre_1_sub", "genre_2_major",
                  "genre_2_sub", "character", "quirk", "pitch",
                  "title", "synopsis", "image_prompt", "image_file"]

    print(f"Generating {args.count} mashup{'s' if args.count != 1 else ''}")
    print(f"  CSV:    {args.output}")
    print(f"  HTML:   {args.html}")
    if not args.no_llm:
        print(f"  LLM:    {args.llm_server}")
    if do_images:
        print(f"  Comfy:  {args.comfy_server}")
        print(f"  Images: {args.images_dir}/")
        args.images_dir.mkdir(parents=True, exist_ok=True)
    print()

    workflow_base = _load_workflow() if do_images else None
    comfy = ComfyClient(args.comfy_server) if do_images else None

    completed_rows = []
    failures = 0
    start = time.monotonic()

    def llm_task(idx):
        """Returns (mashup_dict, error_message_or_None)."""
        m = generate_mashup()
        m["title"] = ""
        m["synopsis"] = ""
        m["image_prompt"] = ""
        m["image_file"] = ""
        if args.no_llm:
            return m, None
        try:
            title, synopsis, image_prompt = call_llama_server(args.llm_server, m)
            m["title"] = title
            m["synopsis"] = synopsis
            m["image_prompt"] = image_prompt
            return m, None
        except Exception as e:
            return m, f"llm: {e}"

    def write_outputs():
        with args.output.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in completed_rows:
                writer.writerow({k: r.get(k, "") for k in fieldnames})
        write_gallery(args.html, completed_rows, args.images_dir.name)

    # Pipeline:
    #   While ComfyUI renders row N's poster, the LLM is already drafting row N+1.
    #   We use a single-worker pool for the LLM so we always have at most one
    #   LLM call in flight ahead of the current row.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as llm_pool:
        # Kick off the first LLM job
        next_future = llm_pool.submit(llm_task, 1) if args.count > 0 else None

        for i in range(1, args.count + 1):
            row_start = time.monotonic()

            # Wait for the LLM job for THIS row to finish.
            mashup, llm_err = next_future.result()
            llm_done = time.monotonic()

            # Immediately queue the LLM job for the NEXT row so it runs
            # alongside this row's image render.
            if i < args.count:
                next_future = llm_pool.submit(llm_task, i + 1)

            status_bits = []
            if llm_err:
                status_bits.append(llm_err[:50])

            # Image generation for this row (only if we have a usable prompt)
            img_time = 0.0
            if do_images and mashup.get("image_prompt"):
                img_start = time.monotonic()
                seed = random.randint(1, 2**31 - 1)
                wf = _patch_workflow(workflow_base, mashup["image_prompt"], seed)
                slug = f"{i:04d}-{slugify(mashup['title'] or 'untitled')}"
                fname = f"{slug}.png"
                dest = args.images_dir / fname
                try:
                    raw = comfy.generate(wf)
                    dest.write_bytes(raw)  # save PNG as-is, no transcoding
                    mashup["image_file"] = fname
                except Exception as e:
                    status_bits.append(f"img: {e}"[:50])
                img_time = time.monotonic() - img_start

            if not status_bits:
                status = "ok"
            else:
                failures += 1
                status = " | ".join(status_bits)

            completed_rows.append(mashup)
            write_outputs()  # rewrite CSV + HTML after every row

            elapsed = time.monotonic() - start
            row_time = time.monotonic() - row_start
            avg = elapsed / i
            remaining = avg * (args.count - i)

            llm_time = llm_done - row_start
            title_preview = (mashup.get("title") or "(no title)")[:48]
            timing = f"llm {llm_time:5.1f}s"
            if do_images:
                timing += f" img {img_time:5.1f}s"
            print(
                f"[{i:>4}/{args.count}] {timing}  total {row_time:5.1f}s  "
                f"avg {avg:5.1f}s  eta {format_eta(remaining):>7}  → {title_preview}  "
                f"[{status}]",
                flush=True,
            )

    total = time.monotonic() - start
    print()
    print(f"Done in {format_eta(total)}. {args.count - failures}/{args.count} succeeded.")
    print(f"  CSV:  {args.output}")
    print(f"  HTML: {args.html}")
    if failures:
        print(f"  ⚠  {failures} row{'s' if failures != 1 else ''} had errors.")


if __name__ == "__main__":
    main()
