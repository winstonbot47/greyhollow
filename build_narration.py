#!/usr/bin/env python3
"""Pre-render Winston's narration for Greyhollow (spec: specs/winston-narration.md).

What it does
  1. Pulls the game script and the <script id="narr-text"> block out of index.html and runs
     them in Node (no hand-copied text). A seeded simulator plays the game many times as both
     characters and records every clip narrClips() asks for: scene titles and paragraphs,
     "What do you do?", numbered choice lines, and the outcome after a pick. The page builds
     its clips with the same function, so the hashes match by construction.
  2. Names each clip audio/<sha1(text)[:16]>.mp3. A file that already exists is never
     re-rendered or re-billed (the cache is the hash).
  3. Prints the ElevenLabs remaining character quota first, then refuses to bill more than
     --budget characters (default 130000) or more than the remaining quota unless --yes
     (over budget) or --fit (render the most-heard clips that fit, skip the rest).
  4. Renders with the same voice and settings as ARC's /api/tts and writes audio/manifest.json.

Usage
  python build_narration.py --dry-run          # counts only, no API calls except the quota read
  python build_narration.py                    # render whatever is missing (guards apply)
  python build_narration.py --fit              # render what fits in budget/quota, most-heard first
  python build_narration.py --prune            # also delete mp3s no clip uses any more

The key comes from ELEVENLABS_API_KEY only and is never printed.
"""
import argparse, hashlib, json, os, re, subprocess, sys, tempfile, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "index.html")
AUDIO = os.path.join(HERE, "audio")
MANIFEST = os.path.join(AUDIO, "manifest.json")

# identical to pitwall-mk2/server.py api_tts()
VOICE = "NFG5qt843uXKj4pFvR7C"
MODEL = "eleven_turbo_v2_5"
SETTINGS = {"stability": 0.5, "similarity_boost": 0.75, "speed": 1.1}
FORMAT = "mp3_44100_64"

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
  const units = new Map();   // text -> {ids:Set, hits, first}
  let order = 0, errors = [];
  const record = () => {
    for (const c of narrClips()) {
      let u = units.get(c.text);
      if (!u) { u = { ids: new Set(), hits: 0, first: order++ }; units.set(c.text, u); }
      u.ids.add(c.id + '|' + S.role); u.hits++;
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
  // spot-check the page's sha1 against Python's (Python re-verifies every unit)
  const out = [...units.entries()].map(([text, u]) => ({ text, ids: [...u.ids].sort(), hits: u.hits, first: u.first, sha1: narrSha1(text) }));
  process.stdout.write(JSON.stringify({ nodes: Object.keys(N), seen: [...nodesSeen], units: out, errors: errors.slice(0, 20), errorCount: errors.length, runs: RUNS }));
})();
"""


def page_scripts():
    html = open(PAGE, encoding="utf-8").read()
    game = re.search(r"<script>\s*(.*?)</script>", html, re.S)
    narr = re.search(r'<script id="narr-text">(.*?)</script>', html, re.S)
    if not game or not narr:
        sys.exit("index.html: could not find the game script and the narr-text block")
    return game.group(1), narr.group(1)


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
    for u in data["units"]:
        h = hashlib.sha1(u["text"].encode("utf-8")).hexdigest()
        if h != u["sha1"]:
            sys.exit(f"sha1 mismatch between page JS and Python for: {u['text'][:60]!r}")
        u["hash"] = h[:16]
    return data


def api_key():
    k = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not k:
        sys.exit("ELEVENLABS_API_KEY is not set")
    return k


def quota(key):
    req = urllib.request.Request("https://api.elevenlabs.io/v1/user/subscription", headers={"xi-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            s = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return None, f"subscription read failed: HTTP {e.code}"
    except Exception as e:
        return None, f"subscription read failed: {type(e).__name__}"
    used, limit = s.get("character_count", 0), s.get("character_limit", 0)
    reset = s.get("next_character_count_reset_unix")
    when = time.strftime("%Y-%m-%d", time.localtime(reset)) if reset else "unknown"
    return limit - used, f"ElevenLabs quota: {limit - used:,} characters remaining ({used:,} of {limit:,} used, tier {s.get('tier','?')}, resets {when})"


def tts(key, text):
    body = json.dumps({"text": text, "model_id": MODEL, "voice_settings": SETTINGS}).encode()
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE}?output_format={FORMAT}",
        data=body, headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            msg = e.read()[:300].decode("utf-8", "replace")
            if e.code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(4 * (attempt + 1)); continue
            raise RuntimeError(f"ElevenLabs HTTP {e.code}: {msg}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < 3:
                time.sleep(4 * (attempt + 1)); continue
            raise RuntimeError(f"ElevenLabs network error: {type(e).__name__}") from None


def write_manifest(units, data):
    rendered = {}
    for u in units:
        f = os.path.join(AUDIO, u["hash"] + ".mp3")
        if os.path.exists(f) and os.path.getsize(f) > 0:
            rendered[u["hash"]] = {"file": u["hash"] + ".mp3", "chars": len(u["text"]), "bytes": os.path.getsize(f),
                                   "ids": u["ids"][:6]}
    man = {
        "voice": VOICE, "model": MODEL, "settings": SETTINGS, "format": FORMAT,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "scenes": len(data["nodes"]), "clips_known": len(units), "clips_rendered": len(rendered),
        "units": rendered,
    }
    tmp = MANIFEST + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=0, sort_keys=True)
    os.replace(tmp, MANIFEST)
    return man


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="count clips and characters; no rendering")
    ap.add_argument("--budget", type=int, default=130000, help="max characters to bill this run (default 130000)")
    ap.add_argument("--yes", action="store_true", help="allow a run over --budget")
    ap.add_argument("--fit", action="store_true", help="render the most-heard clips that fit budget and quota")
    ap.add_argument("--reserve", type=int, default=5000,
                    help="characters --fit leaves on the plan for ARC's live /api/tts (default 5000)")
    ap.add_argument("--runs", type=int, default=1500, help="simulated playthroughs used to find clips")
    ap.add_argument("--prune", action="store_true", help="delete mp3s no current clip uses")
    ap.add_argument("--list", metavar="FILE", help="write every clip (text, ids, hits, hash) to FILE as JSON")
    a = ap.parse_args()

    key = api_key()
    remaining, line = quota(key)
    print(line)

    data = collect(a.runs)
    # scenes and choices first (the spec's core), then outcomes; most-heard first within each
    rank = {"scene": 0, "ask": 0, "choice": 1, "outcome": 2}
    kind = lambda u: u["ids"][0].split("|")[0].split(":")[0]
    units = sorted(data["units"], key=lambda u: (rank.get(kind(u), 3), -u["hits"], u["first"]))
    if a.list:
        with open(a.list, "w", encoding="utf-8") as f:
            json.dump(units, f, ensure_ascii=False, indent=1)
    os.makedirs(AUDIO, exist_ok=True)
    have = {u["hash"] for u in units if os.path.exists(os.path.join(AUDIO, u["hash"] + ".mp3"))}
    todo = [u for u in units if u["hash"] not in have]
    total = sum(len(u["text"]) for u in units)
    pending = sum(len(u["text"]) for u in todo)
    scene_ids = {i.split(":")[1] for u in units for i in u["ids"] if i.startswith("scene:")}
    missing_scenes = [n for n in data["nodes"] if n not in scene_ids]

    print(f"Scenes on the page: {len(data['nodes'])}; scenes with narration clips: {len(scene_ids)}"
          + (f"; never reached: {', '.join(missing_scenes)}" if missing_scenes else ""))
    kinds = {}
    for u in units:
        k = kind(u)
        kinds.setdefault(k, [0, 0]); kinds[k][0] += 1; kinds[k][1] += len(u["text"])
    print(f"Simulated playthroughs: {data['runs']} ({data['errorCount']} errors)")
    for e in data["errors"][:5]:
        print("  error:", e)
    print("Clips by kind: " + "; ".join(f"{k} {n} clips / {c:,} chars" for k, (n, c) in sorted(kinds.items())))
    print(f"Distinct clips: {len(units)}; total characters: {total:,}")
    print(f"Already rendered (cache hits): {len(have)}; to render: {len(todo)} clips / {pending:,} characters")

    if a.prune:
        keep = {u["hash"] + ".mp3" for u in units}
        gone = [f for f in os.listdir(AUDIO) if f.endswith(".mp3") and f not in keep]
        if a.dry_run:
            print(f"--prune would delete {len(gone)} unused mp3s")
        else:
            for f in gone:
                os.remove(os.path.join(AUDIO, f))
            print(f"Pruned {len(gone)} unused mp3s")

    if a.dry_run:
        print("Dry run: nothing rendered, no characters billed.")
        return 0

    cap = a.budget
    if remaining is not None:
        cap = min(cap, max(0, remaining - a.reserve)) if a.fit else cap
    if pending > a.budget and not (a.yes or a.fit):
        print(f"REFUSED: {pending:,} characters is over the --budget of {a.budget:,}. Re-run with --yes or --fit.")
        return 2
    if remaining is not None and pending > remaining and not a.fit:
        print(f"REFUSED: {pending:,} characters is more than the {remaining:,} left on the ElevenLabs plan. Re-run with --fit.")
        return 2
    if a.yes and not a.fit:
        cap = pending

    billed, done, skipped = 0, 0, []
    for u in todo:
        n = len(u["text"])
        if billed + n > cap:
            skipped.append(u); continue
        audio = tts(key, u["text"])
        if not audio or len(audio) < 512:
            raise RuntimeError(f"tiny/empty audio for clip {u['ids'][0]}")
        with open(os.path.join(AUDIO, u["hash"] + ".mp3"), "wb") as f:
            f.write(audio)
        billed += n; done += 1
        if done % 25 == 0:
            write_manifest(units, data)
            print(f"  rendered {done}/{len(todo)} clips, {billed:,} characters")
    man = write_manifest(units, data)
    mb = sum(v["bytes"] for v in man["units"].values()) / 1e6
    print(f"Rendered {done} clips, billed {billed:,} characters; skipped {len(skipped)} clips / "
          f"{sum(len(u['text']) for u in skipped):,} characters (they fall back to the browser voice).")
    print(f"Manifest: {man['clips_rendered']} of {man['clips_known']} clips have audio; audio total {mb:.1f} MB")
    _, line = quota(key)
    print(line.replace("quota:", "quota after:"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
