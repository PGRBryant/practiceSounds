#!/usr/bin/env python3
"""
Discord Soundboard Generator — lab edition
==========================================
Generates 20 meme-grade soundboard clips with the ElevenLabs API:
16 AI sound effects + 4 voice lines (Jake, Reece, and two more for the boys).

Every final clip fits Discord's soundboard hard caps:
  * max 5.2 seconds   (verified with ffprobe; auto-squeezed if a hair over)
  * max 512 KB        (verified on disk)
  * MP3 (192 kbps)

Production pipeline (per sound), adapted from a field-tested handoff doc:
  1. LAB      generate N takes (default 2), requesting lossless PCM first and
              falling back mp3_192 -> mp3_128 only if the plan gates it.
              All intermediate work stays lossless WAV; ONE encode at the end.
  2. MEASURE  ffprobe duration + astats RMS / peak / tail level per take.
              Never assume the API returned the length you asked for.
  3. PICK     score takes (fullness, duration fit, honest tail) and record
              the reasoning in discord_soundboard/report.txt.
  4. INSTALL  trim silence at the edges (tight for punchy one-shots, gentle
              for musical tails and slow builds), micro-fades on every cut,
              loudness handling, final 192k MP3 encode, verify limits.

Loudness doctrine (the RAW LAW):
  * voice lines  -> two-pass ffmpeg loudnorm in LINEAR mode to -14 LUFS
  * sound FX     -> NO loudness targeting; transparent fixed-dB peak lift
                    to -1 dB, boost capped at +12 dB so quiet-by-design
                    material stays quiet. Attenuation is uncapped.

USAGE (no key in this file — safe to commit)
-----
In a repo: pair with .github/workflows/soundboard.yml and add your key as a
repo Actions secret named ELEVENLABS_API_KEY (name must match EXACTLY —
a guessed secret name means an empty key and a bare 401).

Locally:
    pip install requests            # ffmpeg also required for the full pipeline
    export ELEVENLABS_API_KEY=sk_...
    python soundboard_generator.py                   # everything, 2 takes each
    python soundboard_generator.py --takes 1         # thrift mode (half the credits)
    python soundboard_generator.py --only phonk_drop # reroll one sound
    python soundboard_generator.py --list-voices     # voices on your account

Without ffmpeg the script degrades gracefully: single take, direct mp3_128
from the API, size check only.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run:  pip install requests")

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip()
if not API_KEY:
    sys.exit(
        "No API key found. Set the ELEVENLABS_API_KEY environment variable.\n"
        "On GitHub: repo Settings -> Secrets and variables -> Actions ->\n"
        "New repository secret, named exactly ELEVENLABS_API_KEY."
    )

BASE_URL = "https://api.elevenlabs.io/v1"
OUT_DIR = Path("discord_soundboard")
LAB_DIR = Path("_lab")                    # scratch takes; never committed
REPORT = OUT_DIR / "report.txt"

DISCORD_MAX_SECONDS = 5.2
DISCORD_MAX_BYTES = 512 * 1024
FINAL_BITRATE = "192k"                    # 5.2 s @ 192 kbps ~= 125 KB — plenty of headroom

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))

# Lossless-first for TTS; fall back only if the plan gates a format.
# The sound-generation endpoint is different: its PCM comes back STEREO
# (measured — every SFX decoded as mono ran exactly 2x the requested length,
# slowed and pitched an octave down). MP3 frames self-describe channel count
# and sample rate, so SFX requests use MP3 and dodge the guesswork entirely.
CHAINS = {
    "tts": ["pcm_44100", "mp3_44100_192", "mp3_44100_128"] if HAVE_FFMPEG else ["mp3_44100_128"],
    "sfx": ["mp3_44100_192", "mp3_44100_128"],
}
_fmt_idx = {"tts": 0, "sfx": 0}

# Premade ElevenLabs voices. If any ID errors on your account,
# run --list-voices and swap in one you like.
VOICES = {
    "adam":   "pNInz6obpgDQGcFmaJgB",  # deep American male -> movie-trailer guy
    "callum": "N2lVS1w4EtoT3dr4eOWO",  # gravelly + intense -> esports caster
    "lily":   "pFZP5JQG7iQjIQuC4Bku",  # British female     -> furious mum energy
}

# Meme performances want exaggeration; a narrator's 0.5/0.25 would flatten them.
DRAMATIC = {"stability": 0.30, "similarity_boost": 0.75, "style": 0.85, "use_speaker_boost": True}
ANGRY    = {"stability": 0.25, "similarity_boost": 0.75, "style": 0.90, "use_speaker_boost": True}
DEADPAN  = {"stability": 0.95, "similarity_boost": 0.75, "style": 0.05, "use_speaker_boost": True}

# ---------------------------------------------------------------------------
# THE SOUNDS
# trim="tight"  -> punchy one-shot: cut hard to the transient
# trim="gentle" -> musical build/reverb tail: only true silence is removed,
#                  so quiet intros and dying tails survive (they're the point)
# ---------------------------------------------------------------------------
SOUNDS = [
    # ---------- FAST + ANNOYING ----------
    dict(name="airhorn_triple", kind="sfx", seconds=1.8, trim="tight",
         prompt="Extremely loud party airhorn, three rapid blasts back to back, slightly distorted, hype DJ energy"),
    dict(name="vine_boom", kind="sfx", seconds=1.0, trim="gentle",
         prompt="One single deep dramatic bass boom with heavy sub rumble, tight and punchy, comedic dramatic sting"),
    dict(name="metal_pipe", kind="sfx", seconds=2.0, trim="tight",
         prompt="Heavy metal pipe dropped onto concrete, extremely loud clang, bounces and clatters to a stop"),
    dict(name="fart_reverb", kind="sfx", seconds=2.5, trim="gentle",
         prompt="Comically long wet fart with enormous cathedral reverb echo trailing off"),
    dict(name="wrong_buzzer", kind="sfx", seconds=1.2, trim="tight",
         prompt="Harsh game show wrong-answer buzzer, abrasive double buzz"),
    dict(name="seductive_sax", kind="sfx", seconds=2.5, trim="tight",
         prompt="Short smooth seductive saxophone riff, cheesy romantic lounge sting"),
    dict(name="angry_goose", kind="sfx", seconds=1.8, trim="tight",
         prompt="Furious goose honking aggressively three times with wings flapping"),
    dict(name="red_alert", kind="sfx", seconds=2.0, trim="tight",
         prompt="Submarine dive klaxon alarm, two loud urgent blasts, emergency red alert"),
    dict(name="glass_cat", kind="sfx", seconds=2.2, trim="tight",
         prompt="Window glass shattering loudly, followed immediately by a startled cat yowl"),
    dict(name="bruh", kind="tts", voice="adam", settings=DEADPAN, trim="tight",
         text="bruh."),

    # ---------- EPIC ----------
    dict(name="braam_trailer", kind="sfx", seconds=3.0, trim="gentle",
         prompt="Massive cinematic movie-trailer brass braam hit with deep sub-bass boom and long tail"),
    dict(name="heavenly_choir", kind="sfx", seconds=4.0, trim="gentle",
         prompt="Angelic choir swelling on a glorious major chord, heavens opening, shimmering and holy"),
    dict(name="epic_riser", kind="sfx", seconds=4.5, trim="gentle",
         prompt="Orchestral riser building unbearable tension, then exploding into a huge epic impact hit"),
    dict(name="victory_fanfare", kind="sfx", seconds=3.5, trim="gentle",
         prompt="Triumphant brass victory fanfare, champions celebration, confetti energy"),
    dict(name="phonk_drop", kind="sfx", seconds=5.0, trim="tight",
         prompt="Aggressive drift phonk beat drop, Memphis cowbell melody, distorted 808 bass, night car drift energy"),
    dict(name="boss_battle", kind="sfx", seconds=5.0, trim="gentle",
         prompt="Ominous final-boss battle music sting, pounding taiko drums, epic menacing choir stabs"),
    dict(name="sad_violin", kind="sfx", seconds=4.0, trim="gentle",
         prompt="Melodramatic weeping solo violin phrase, over-the-top tragic soap opera moment"),

    # ---------- THE BOYS ----------
    dict(name="jake_trailer", kind="tts", voice="adam", settings=DRAMATIC, trim="tight",
         text="In a world... there was only one man... JAKE."),
    dict(name="reece_mum", kind="tts", voice="lily", settings=ANGRY, trim="tight",
         text="REECE! Get down here RIGHT NOW! I will NOT ask you again!"),
    dict(name="jungle_diff", kind="tts", voice="callum", settings=DRAMATIC, trim="tight",
         text="Ladies and gentlemen... JUNGLE DIFF!"),
]

# ---------------------------------------------------------------------------
# SMALL TOOLS
# ---------------------------------------------------------------------------
def run(cmd: list) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {p.stderr[-400:]}")
    return p


def ffprobe_duration(path: Path) -> float:
    p = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)])
    return float(p.stdout.strip())


_ASTAT = re.compile(r"(Peak level dB|RMS level dB):\s*(-?[\d.]+|-inf)")

def _astats(path: Path, extra_filter: str = "") -> dict:
    af = (extra_filter + "," if extra_filter else "") + "astats"
    p = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-af", af,
                        "-f", "null", "-"], capture_output=True, text=True)
    overall = p.stderr[p.stderr.rfind("Overall"):]
    vals = {"Peak level dB": -99.0, "RMS level dB": -99.0}
    for key, val in _ASTAT.findall(overall):
        vals[key] = -99.0 if val == "-inf" else float(val)
    return vals


def measure(path: Path) -> dict:
    """duration + overall peak/RMS + RMS of the final 150 ms (tail honesty)."""
    dur = ffprobe_duration(path)
    whole = _astats(path)
    tail = _astats(path, extra_filter=f"atrim=start={max(0.0, dur - 0.15):.3f}")
    return {"dur": dur, "peak": whole["Peak level dB"],
            "rms": whole["RMS level dB"], "tail": tail["RMS level dB"]}

# ---------------------------------------------------------------------------
# API CALLS (lossless-first with plan-gate fallback)
# ---------------------------------------------------------------------------
def eleven_post(url: str, payload: dict, kind: str) -> tuple:
    """POST to ElevenLabs. Returns (audio_bytes, format_used).
    Falls down the kind's format chain if a format is gated; retries once on 429."""
    chain = CHAINS[kind]
    while True:
        fmt = chain[_fmt_idx[kind]]
        fell_back = False
        for attempt in (1, 2):
            r = requests.post(
                url,
                params={"output_format": fmt},
                headers={"xi-api-key": API_KEY, "Content-Type": "application/json"},
                json=payload,
                timeout=180,
            )
            if r.status_code == 200:
                return r.content, fmt
            if r.status_code == 429 and attempt == 1:
                print("      rate limited, waiting 15s...")
                time.sleep(15)
                continue
            body = r.text[:300]
            gated = r.status_code in (400, 401, 403) and (
                "output_format" in body or "subscription" in body or "upgrade" in body.lower()
            )
            if gated and _fmt_idx[kind] < len(chain) - 1:
                _fmt_idx[kind] += 1
                print(f"      {fmt} gated by plan -> falling back to {chain[_fmt_idx[kind]]}")
                fell_back = True
                break
            raise RuntimeError(f"HTTP {r.status_code}: {body}")
        if not fell_back:
            raise RuntimeError("request retry loop exhausted")


def gen_sfx(prompt: str, seconds: float) -> tuple:
    return eleven_post(f"{BASE_URL}/sound-generation",
                       {"text": prompt, "duration_seconds": seconds, "prompt_influence": 0.4},
                       kind="sfx")


def gen_tts(text: str, voice_id: str, settings: dict) -> tuple:
    return eleven_post(f"{BASE_URL}/text-to-speech/{voice_id}",
                       {"text": text, "model_id": "eleven_multilingual_v2",
                        "voice_settings": settings},
                       kind="tts")


def list_voices() -> None:
    r = requests.get(f"{BASE_URL}/voices", headers={"xi-api-key": API_KEY}, timeout=30)
    r.raise_for_status()
    for v in r.json().get("voices", []):
        print(f"  {v['voice_id']}  {v['name']}")

# ---------------------------------------------------------------------------
# AUDIO PIPELINE (everything lossless WAV until the single final encode)
# ---------------------------------------------------------------------------
def bytes_to_wav(raw: bytes, fmt: str, out: Path) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-y"]
    if fmt.startswith("pcm_"):
        cmd += ["-f", "s16le", "-ar", fmt.split("_")[1], "-ac", "1"]
    cmd += ["-i", "pipe:0", str(out)]
    p = subprocess.run(cmd, input=raw, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"decode failed: {p.stderr[-300:].decode(errors='replace')}")


TRIM_FILTERS = {
    # head cut, then (reversed) tail cut + 60ms hsin fade-out, then 12ms fade-in
    "tight": ("silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.02,"
              "areverse,silenceremove=start_periods=1:start_threshold=-58dB:start_silence=0.10,"
              "afade=t=in:d=0.06:curve=hsin,areverse,afade=t=in:d=0.012"),
    # only true digital silence goes; ramps-from-nothing and dying tails survive
    "gentle": ("silenceremove=start_periods=1:start_threshold=-70dB:start_silence=0.05,"
               "areverse,silenceremove=start_periods=1:start_threshold=-70dB:start_silence=0.05,"
               "afade=t=in:d=0.06:curve=hsin,areverse,afade=t=in:d=0.012"),
}


def apply_filter(src: Path, dst: Path, af: str) -> None:
    run(["ffmpeg", "-hide_banner", "-y", "-i", str(src), "-af", af, str(dst)])


def loudnorm_two_pass(src: Path, dst: Path, target_i: float = -14.0) -> str:
    """Voice mastering: two-pass loudnorm in LINEAR mode (single-pass pumps)."""
    base = f"loudnorm=I={target_i}:TP=-1.5:LRA=11"
    p = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(src),
                        "-af", base + ":print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = json.loads(p.stderr[p.stderr.rfind("{"):])
    if m.get("input_i") in (None, "-inf"):        # measurement failed -> peak lift
        return peak_lift(src, dst)
    apply_filter(src, dst,
                 base + f":linear=true:measured_I={m['input_i']}:measured_TP={m['input_tp']}"
                        f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}"
                        f":offset={m['target_offset']}")
    return f"loudnorm 2-pass linear -> {target_i} LUFS (was {m['input_i']} LUFS)"


def peak_lift(src: Path, dst: Path, ceiling: float = -1.0, boost_cap: float = 12.0) -> str:
    """THE RAW LAW: no loudness targets for FX. Fixed-dB lift to the ceiling,
    boost capped so quiet-by-design stays quiet; attenuation uncapped."""
    peak = _astats(src)["Peak level dB"]
    gain = ceiling - peak
    if gain > 0:
        gain = min(gain, boost_cap)
    if abs(gain) < 0.3:
        shutil.copyfile(src, dst)
        return f"peak {peak:.1f} dB, no lift needed"
    apply_filter(src, dst, f"volume={gain:.2f}dB")
    return f"peak lift {gain:+.1f} dB (peak was {peak:.1f} dB, cap +{boost_cap:.0f})"


def encode_mp3(src: Path, dst: Path) -> None:
    run(["ffmpeg", "-hide_banner", "-y", "-i", str(src),
         "-c:a", "libmp3lame", "-b:a", FINAL_BITRATE, "-ar", "44100", str(dst)])

# ---------------------------------------------------------------------------
# LAB -> MEASURE -> PICK -> INSTALL
# ---------------------------------------------------------------------------
def score_take(m: dict, requested) -> float:
    s = m["rms"]                                   # fullness (dBFS, higher = fuller)
    if requested:                                  # duration fit (SFX only)
        s -= 3.0 * abs(m["dur"] - requested)
    if m["tail"] <= -50.0:                         # honest tail bonus
        s += 2.0
    return s


def generate_takes(s: dict, n: int, log: list):
    lab = LAB_DIR / s["name"]
    lab.mkdir(parents=True, exist_ok=True)
    takes = []
    for i in range(1, n + 1):
        try:
            if s["kind"] == "sfx":
                raw, fmt = gen_sfx(s["prompt"], s["seconds"])
            else:
                raw, fmt = gen_tts(s["text"], VOICES[s["voice"]], s["settings"])
        except Exception as e:
            log.append(f"    take {i}: FAILED ({e})")
            print(f"      take {i} failed: {e}")
            continue
        wav = lab / f"take{i}.wav"
        bytes_to_wav(raw, fmt, wav)
        m = measure(wav)
        m["path"], m["fmt"], m["i"] = wav, fmt, i
        m["score"] = score_take(m, s.get("seconds"))
        takes.append(m)
        log.append(f"    take {i} [{fmt}]: {m['dur']:.2f}s  rms {m['rms']:.1f}  "
                   f"peak {m['peak']:.1f}  tail {m['tail']:.1f}  -> score {m['score']:.1f}")
        time.sleep(1.2)
    if not takes:
        return None
    best = max(takes, key=lambda t: t["score"])
    log.append(f"    PICK: take {best['i']} (score {best['score']:.1f})")
    return best["path"]


def install(s: dict, take: Path, log: list):
    lab = take.parent
    trimmed = lab / "trimmed.wav"
    apply_filter(take, trimmed, TRIM_FILTERS[s["trim"]])

    mastered = lab / "mastered.wav"
    if s["kind"] == "tts":
        note = loudnorm_two_pass(trimmed, mastered)
    else:
        note = peak_lift(trimmed, mastered)
    log.append(f"    master: {note}")

    dur = ffprobe_duration(mastered)
    final_wav = mastered
    if dur > DISCORD_MAX_SECONDS - 0.1:            # squeeze, don't chop
        factor = dur / (DISCORD_MAX_SECONDS - 0.2)
        final_wav = lab / "squeezed.wav"
        apply_filter(mastered, final_wav, f"atempo={factor:.4f}")
        log.append(f"    squeeze: atempo x{factor:.3f} ({dur:.2f}s -> fits)")

    out = OUT_DIR / f"{s['name']}.mp3"
    encode_mp3(final_wav, out)                     # the one and only lossy encode
    final = {"dur": ffprobe_duration(out), "bytes": out.stat().st_size}
    return out, final


def raw_mode(s: dict, log: list):
    """No-ffmpeg fallback: one take, straight mp3_128 from the API."""
    try:
        if s["kind"] == "sfx":
            raw, _ = gen_sfx(s["prompt"], s["seconds"])
        else:
            raw, _ = gen_tts(s["text"], VOICES[s["voice"]], s["settings"])
    except Exception as e:
        log.append(f"    FAILED ({e})")
        print(f"      failed: {e}")
        return None
    out = OUT_DIR / f"{s['name']}.mp3"
    out.write_bytes(raw)
    time.sleep(1.2)
    return out, {"dur": len(raw) / 16000.0, "bytes": len(raw)}   # 128k CBR estimate

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main() -> None:
    args = sys.argv[1:]
    if "--list-voices" in args:
        list_voices()
        return

    takes_n = 2
    if "--takes" in args:
        takes_n = max(1, int(args[args.index("--takes") + 1]))
    only = None
    if "--only" in args:
        rest = args[args.index("--only") + 1:]
        only = {a for a in rest if not a.startswith("--") and not a.isdigit()}
        if not only:
            sys.exit("--only needs at least one sound name")

    if not HAVE_FFMPEG:
        print("NOTE: ffmpeg not found -> raw mode (single take, no mastering).")
        takes_n = 1

    OUT_DIR.mkdir(exist_ok=True)
    report, results, failures = [], [], []

    for s in SOUNDS:
        name, path = s["name"], OUT_DIR / f"{s['name']}.mp3"
        if only is not None and name not in only:
            continue
        if only is None and path.exists():
            print(f"[skip] {name} (exists — reroll with --only {name})")
            continue

        label = "SFX" if s["kind"] == "sfx" else "VOICE"
        print(f"[{label}] {name} ({takes_n} take{'s' if takes_n > 1 else ''})...")
        log = [f"{name}:"]

        if HAVE_FFMPEG:
            best = generate_takes(s, takes_n, log)
            if best is None:
                failures.append(name)
                report.extend(log + [""])
                continue
            out, final = install(s, best, log)
        else:
            r = raw_mode(s, log)
            if r is None:
                failures.append(name)
                report.extend(log + [""])
                continue
            out, final = r

        ok = final["bytes"] <= DISCORD_MAX_BYTES and final["dur"] <= DISCORD_MAX_SECONDS
        flag = "OK" if ok else "!! OVER DISCORD LIMIT"
        print(f"      -> {final['bytes']/1024:.0f} KB, {final['dur']:.2f}s  {flag}")
        log.append(f"    FINAL: {final['dur']:.2f}s, {final['bytes']/1024:.0f} KB  [{flag}]")
        report.extend(log + [""])
        results.append((name, final["bytes"], final["dur"], ok))

    # ---- summary + recorded reasoning ----
    print("\n" + "=" * 56)
    print(f"Done. {len(results)} installed, {len(failures)} failed.")
    for name, size, dur, ok in results:
        print(f"  {' ' if ok else '!'} {name:<18} {size/1024:>4.0f} KB  {dur:.2f}s")
    if failures:
        print(f"\nFailed (rerun with --only {' '.join(failures)})")
    if report:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        with REPORT.open("a") as f:
            f.write(f"==== run {stamp} | takes={takes_n} | formats: "
                    f"tts={CHAINS['tts'][_fmt_idx['tts']]}, sfx={CHAINS['sfx'][_fmt_idx['sfx']]} "
                    f"====\n" + "\n".join(report) + "\n")
        print(f"\nLab notes -> {REPORT}")
    print(f"Upload from ./{OUT_DIR}/ via Server Settings -> Soundboard -> Upload Sound")


if __name__ == "__main__":
    main()
