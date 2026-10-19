#!/usr/bin/env python3
"""Build Winston's narration list for Greyhollow (v2, spec: specs/v3-on-demand.md).

What it does
  1. Pulls the game script and the <script id="narr-text"> block out of index.html and runs
     them in Node (no hand-copied text). A seeded simulator plays the game many times as both
     characters and records every clip narrParts() asks for: one whole-scene clip per view
     (title, paragraphs, "What do you do?", the numbered choices, joined with eleven_v3 audio
     tags), one outcome clip per pick, one clip each for the hub and the role screen. The page
     builds its clips with the same function, so the ids match by construction; this script
     also recomputes every id in Python and stops on any difference.
  2. Writes narration.json beside index.html:
       { clips: { <id>: { text, chars, kind, scene } }, model, voice, settings, built }
     id = sha1(model|voice|settings|text)[:16]. ARC serves GET /api/games/narrate/<id> for
     these ids only, renders a clip on eleven_v3 the first time anyone asks for it and caches
     it. Nothing is rendered here by default and no API key is needed.
  3. Optional pre-warm (off by default; not used now): --prewarm URL asks that ARC for the
     most-heard clips that fit --budget (estimated billed characters, 0.4 per character), or
     the quota left minus --reserve with --fit. ARC's own quota and daily-cap guards still apply.

Usage
  python build_narration.py --dry-run          # counts only; writes nothing, calls nothing
  python build_narration.py                    # write narration.json
  python build_narration.py --prewarm https://winston.tail05945a.ts.net --budget 2000

The v1 per-paragraph clips in audio/ (manifest.json) stay as the page's fallback; this script
no longer writes them.
"""
import argparse, hashlib, json, math, os, re, subprocess, sys, tempfile, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "index.html")
OUT = os.path.join(HERE, "narration.json")

# the approved v3 request (audio-test/render_test.py V3) = pitwall-mk2/server.py GN_* constants
MODEL = "eleven_v3"
VOICE = "NFG5qt843uXKj4pFvR7C"
SETTINGS = "stability=0.5,similarity_boost=0.75,style=0.3,speed=1.0,use_speaker_boost=true,output_format=mp3_44100_128"
MAX_CHARS = 4500
BILL_RATE = 0.4          # billed credits per character seen on eleven_v3 (audio-test: 0.37-0.40)

HARNESS = r"""
// stub just enough browser for the game script to run headless
const store = {};
const el = () => ({ innerHTML: '', textContent: '', querySelectorAll: () => [], querySelector: () => null,
                    addEventListener(){}, appendChild(){}, getBoundingClientRect: () => ({ top: 0 }) });
const els = {};
globalThis.document = { getElementById: id => (els[id] = els[id] || el()), querySelector: () => null,
                        querySelectorAll: () => [], addEventListener(){}, createElement: el };
globalThis.window = globalThis; globalThis.scrollTo = () => {}; globalThis.scrollY = 0;
globalThis.localStorage = { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } };
"""

SIM = r"""
;(() => {
  let seed = 20261018;
  const rnd = () => { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed / 0x80000000; };
  const pickOf = a => a[Math.floor(rnd() * a.length)];
  const clips = new Map();   // id -> {text, kind, scene, part, of, roles:Set, hits, first}
  let order = 0, errors = [];
  const record = () => {
    for (const p of narrParts()) {
      let u = clips.get(p.id);
      if (!u) { u = { text: p.text, kind: p.kind, scene: p.scene, part: p.part, of: p.of, roles: new Set(), hits: 0, first: order++ }; clips.set(p.id, u); }
      u.roles.add(S.role || '-'); u.hits++;
    }
  };
  const nodesSeen = new Set();
  const RUNS = +process.env.NARR_RUNS || 1500;
  for (let run = 0; run < RUNS; run++) {
    const role = run % 2 ? 'witch' : 'witcher';
    try {
      S = fresh(); S.node = 'world'; render(); record();
      choose(0); record();                                   // "Got it. Who am I playing?"
      if (S.node === 'world') { advance(); record(); }
      S.role = role; S.eye = W() ? 1 : 2; S.node = 'prologue'; render(); record();
      let steps = 0;
      while (steps++ < 400) {
        nodesSeen.add(S.node);
        const n = N[S.node];
        if (rnd() < 0.01 && S.node !== 'world') { S._return = S.node; S.node = 'world'; S.chosen = null; render(); record(); choose(0); record(); if (S.chosen !== null) { advance(); record(); } continue; }
        if (n.hub) {
          const left = QUESTS.filter(q => !S.done[q.id]);
          if (!left.length) break;
          const q = rnd() < 0.1 ? pickOf(QUESTS) : pickOf(left);
          startQuest(q.id); record(); continue;
        }
        if (n.end) { advance(); record(); continue; }
        if (S.chosen !== null) { advance(); record(); continue; }
        const vc = visibleChoices(n).filter(c => !c.disabled);
        if (!vc.length) { errors.push('dead end at ' + S.node); break; }
        choose(pickOf(vc)._i); record();
      }
      if (run % 7 === 0) { S._return = S.node; S.node = 'world'; S.chosen = null; render(); record(); choose(0); record(); }
    } catch (e) { errors.push(String(e && e.stack || e).slice(0, 300)); }
  }
  const out = [...clips.entries()].map(([id, u]) => ({ id, text: u.text, kind: u.kind, scene: u.scene, part: u.part, of: u.of,
    roles: [...u.roles].sort(), hits: u.hits, first: u.first }));
  process.stdout.write(JSON.stringify({ nodes: Object.keys(N), seen: [...nodesSeen], clips: out, errors: errors.slice(0, 20),
    errorCount: errors.length, runs: RUNS, v3: NARR_V3, max: NARR_MAX }));
})();
"""


def page_scripts():
    html = open(PAGE, encoding="utf-8").read()
    game = re.search(r"<script>\s*(.*?)</script>", html, re.S)
    narr = re.search(r'<script id="narr-text">(.*?)</script>', html, re.S)
    if not game or not narr:
        sys.exit("index.html: could not find the game script and the narr-text block")
    return game.group(1), narr.group(1)


def clip_id(text):
    return hashlib.sha1(f"{MODEL}|{VOICE}|{SETTINGS}|{text}".encode("utf-8")).hexdigest()[:16]


def collect(runs):
    game, narr = page_scripts()
    src = HARNESS + "\n" + game + "\n;\n" + narr + "\n" + SIM
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(src)
        path = f.name
    try:
        env = dict(os.environ, NARR_RUNS=str(runs))
        env.pop("ELEVENLABS_API_KEY", None)  # node never needs it
        r = subprocess.run(["node", path], capture_output=True, text=True, encoding="utf-8", env=env, timeout=600)
    finally:
        os.unlink(path)
    if r.returncode != 0:
        sys.exit("node harness failed:\n" + r.stderr[-2000:])
    data = json.loads(r.stdout)
    v3 = data["v3"]
    if (v3["model"], v3["voice"], v3["settings"], data["max"]) != (MODEL, VOICE, SETTINGS, MAX_CHARS):
        sys.exit(f"page NARR_V3/NARR_MAX {v3} {data['max']} differ from this script's constants")
    for c in data["clips"]:
        if clip_id(c["text"]) != c["id"]:
            sys.exit(f"id mismatch between page JS and Python for: {c['text'][:60]!r}")
        if len(c["text"]) > MAX_CHARS:
            sys.exit(f"clip over {MAX_CHARS} characters ({len(c['text'])}) in scene {c['scene']}")
    return data


def quota(key):
    req = urllib.request.Request("https://api.elevenlabs.io/v1/user/subscription", headers={"xi-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            s = json.loads(r.read())
    except Exception as e:
        return None, f"subscription read failed: {type(e).__name__}"
    used, limit = s.get("character_count", 0), s.get("character_limit", 0)
    return limit - used, f"ElevenLabs quota: {limit - used:,} characters remaining ({used:,} of {limit:,} used)"


def prewarm(base, clips, budget):
    """Ask ARC for clips (most-heard first) until the estimated billed characters would pass budget."""
    spent, done = 0, 0
    for c in clips:
        est = math.ceil(len(c["text"]) * BILL_RATE)
        if spent + est > budget:
            continue
        url = base.rstrip("/") + "/api/games/narrate/" + c["id"]
        for attempt in range(4):
            try:
                with urllib.request.urlopen(url, timeout=120) as r:
                    r.read()
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 3:
                    time.sleep(3); continue
                print(f"  stop: ARC answered {e.code} for {c['id']}")
                return done, spent
        spent += est; done += 1
    return done, spent


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="count clips and characters; write nothing")
    ap.add_argument("--runs", type=int, default=1500, help="simulated playthroughs used to find clips")
    ap.add_argument("--prewarm", metavar="ARC_URL", help="optional: ask this ARC to render clips now (bills)")
    ap.add_argument("--budget", type=int, default=0, help="--prewarm: max estimated billed characters (default 0)")
    ap.add_argument("--fit", action="store_true", help="--prewarm: budget = quota left minus --reserve")
    ap.add_argument("--reserve", type=int, default=5000, help="characters --fit leaves on the plan (default 5000)")
    a = ap.parse_args()

    data = collect(a.runs)
    clips = data["clips"]
    rank = {"role": 0, "scene": 0, "hub": 0, "outcome": 1}
    clips.sort(key=lambda c: (rank.get(c["kind"], 2), -c["hits"], c["first"]))
    scene_nodes = {c["scene"] for c in clips if c["kind"] in ("scene", "hub", "role")}
    missing = [n for n in data["nodes"] if n not in scene_nodes]
    split = sorted({c["scene"] for c in clips if c["of"] > 1})
    kinds = {}
    for c in clips:
        kinds.setdefault(c["kind"], [0, 0]); kinds[c["kind"]][0] += 1; kinds[c["kind"]][1] += len(c["text"])
    total = sum(len(c["text"]) for c in clips)
    big = max(clips, key=lambda c: len(c["text"]))

    print(f"Scenes on the page: {len(data['nodes'])}; scenes with a scene clip: {len(scene_nodes)}"
          + (f"; never reached: {', '.join(missing)}" if missing else ""))
    print(f"Simulated playthroughs: {data['runs']} ({data['errorCount']} errors)")
    for e in data["errors"][:5]:
        print("  error:", e)
    print(f"Ids: {len(clips)} computed in the page's JS, all {len(clips)} equal to Python's sha1(model|voice|settings|text)[:16]")
    print("Clips by kind: " + "; ".join(f"{k} {n} clips / {ch:,} chars" for k, (n, ch) in sorted(kinds.items())))
    print(f"Largest clip: {len(big['text']):,} chars ({big['kind']} {big['scene']}); cap {MAX_CHARS:,}; "
          f"scenes split into parts: {', '.join(split) if split else 'none'}")
    print(f"Total characters: {total:,}; estimated billed for the whole game at {BILL_RATE} per char: {math.ceil(total * BILL_RATE):,}")

    if a.dry_run:
        print("Dry run: narration.json not written, nothing rendered, no characters billed.")
        return 0

    man = {"model": MODEL, "voice": VOICE, "settings": SETTINGS, "built": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "clips": {c["id"]: {"text": c["text"], "chars": len(c["text"]), "kind": c["kind"], "scene": c["scene"]}
                     for c in sorted(clips, key=lambda c: c["id"])}}
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(man, f, ensure_ascii=False, indent=0, sort_keys=True)
    os.replace(tmp, OUT)
    print(f"Wrote {os.path.relpath(OUT, HERE)}: {len(clips)} clips, {os.path.getsize(OUT) / 1e3:.0f} KB")

    if a.prewarm:
        budget = a.budget
        if a.fit:
            k = os.environ.get("ELEVENLABS_API_KEY", "").strip()
            left, line = quota(k) if k else (None, "ELEVENLABS_API_KEY not set")
            print(line)
            budget = max(0, (left or 0) - a.reserve)
        n, spent = prewarm(a.prewarm, clips, budget)
        print(f"Pre-warm: asked ARC for {n} clips, about {spent:,} billed characters (budget {budget:,})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
