#!/usr/bin/env python3
"""
Discord Soundboard Generator — lab edition
==========================================
Generates 203 meme-grade soundboard clips with the ElevenLabs API:
29 AI sound effects + 174 voice lines (Jake, Reece, and the whole lobby).

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
    "george": "JBFqnCBsd6RMkjVDRZzb",  # posh British male  -> weary condescension
    "charlie": "IKne3meq5aSn9XLyUdCD", # casual Aussie male -> copium merchant
    # round 9 v2 cast (from this account's catalog, voices.json)
    "harry":   "SOYHLrjzK2X1ezoPC6cr",  # fierce warrior, rough  -> screams excuses
    "liam":    "TX3LPaxmHKxFdv7VOQHJ",  # energetic creator      -> the gamer himself
    "laura":   "FGY2WhTYpPnrIDTdsKH5",  # quirky, sassy          -> mouse-slipped energy
    "jessica": "cgSgspJ2msm6clMCkdW9",  # playful, bright        -> cheerleader / meltdown
    "bill":    "pqHfZKP75CvOlQylNhV4",  # wise old man           -> grandpa excuses
    "daniel":  "onwK4e9ZLuTAKqWW03F9",  # steady British broadcaster
    "alice":   "Xb7hH8MSUJpSbSDYk0k2",  # clear British educator -> polite verdicts
    "chris":   "iP95p4xoKVk53GoZ742B",  # down-to-earth American -> losing it quietly
    "brian":   "nPczCjzI2devNBz1zQrb",  # deep, resonant         -> movie trailer #2
    "oliver":  "jfIS2w2yJi0grJZPyEsk",  # deep gravel Brit       -> HUZZAH
    "will":    "bIHbv24MWmeRgasZH58o",  # relaxed optimist       -> "look at us, legends"
    "river":   "SAz9YHcvj6GT2YYXdXww",  # neutral, calm          -> "it's giving... eighth"
    "sarah":   "EXAVITQu4vr4xnSDxMaL",  # mature, confident      -> diva energy
    "eric":    "cjVigY5qzO86Huf0OWal",  # smooth, trustworthy    -> the smooth one
}

# Meme performances want exaggeration; a narrator's 0.5/0.25 would flatten them.
DRAMATIC = {"stability": 0.30, "similarity_boost": 0.75, "style": 0.85, "use_speaker_boost": True}
ANGRY    = {"stability": 0.25, "similarity_boost": 0.75, "style": 0.90, "use_speaker_boost": True}
DEADPAN  = {"stability": 0.95, "similarity_boost": 0.75, "style": 0.05, "use_speaker_boost": True}
CASUAL   = {"stability": 0.45, "similarity_boost": 0.75, "style": 0.45, "use_speaker_boost": True}

# ---------------------------------------------------------------------------
# THE SOUNDS
# trim="tight"  -> punchy one-shot: cut hard to the transient
# trim="gentle" -> musical build/reverb tail: only true silence is removed,
#                  so quiet intros and dying tails survive (they're the point)
# trim="none"   -> quiet-by-design (crickets): nothing removed, micro-fades only
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

    # ---------- HIGH ENERGY (round 2) ----------
    dict(name="doubled_damage", kind="tts", voice="callum", settings=DRAMATIC, trim="tight",
         text="I just DOUBLED your damage. AGAIN!"),
    dict(name="reece_tank", kind="tts", voice="adam", settings=ANGRY, trim="tight",
         text="REECE! I'M A TANK! I'M A TAAAANK!"),
    dict(name="jake_why_bad", kind="tts", voice="lily", settings=DRAMATIC, trim="tight",
         text="Jaaaake... why so baaaaad?"),
    dict(name="missed_everything", kind="tts", voice="callum", settings=DRAMATIC, trim="tight",
         text="Ladies and gentlemen... he missed... EVERYTHING!"),
    dict(name="minus_aura", kind="tts", voice="adam", settings=DEADPAN, trim="tight",
         text="Minus one thousand aura."),
    dict(name="who_pinged", kind="tts", voice="adam", settings=ANGRY, trim="tight",
         text="WHO pinged me?! WHO! PINGED! ME!"),
    dict(name="no_flash_no_hope", kind="tts", voice="callum", settings=DRAMATIC, trim="tight",
         text="No flash! No ult! NO HOPE!"),
    dict(name="character_dev", kind="tts", voice="adam", settings=DEADPAN, trim="tight",
         text="It's not a loss. It's character development."),
    dict(name="in_this_economy", kind="tts", voice="lily", settings=ANGRY, trim="tight",
         text="Zero and seven?! In THIS economy?!"),

    # ---------- ROUND 3: LOL + GAMING CULTURE ----------
    dict(name="question_mark_ping", kind="tts", voice="lily", settings=DRAMATIC, trim="tight",
         text="Question mark? ...Question mark?! QUESTION MARK?!"),
    dict(name="surrender_at_15", kind="tts", voice="callum", settings=DRAMATIC, trim="tight",
         text="Surrender at FIFTEEN?! Are you SERIOUS right now?!"),
    dict(name="report_support", kind="tts", voice="adam", settings=ANGRY, trim="tight",
         text="Report support! Report support! REPORT! SUPPORT!"),
    dict(name="skill_issue", kind="tts", voice="adam", settings=DEADPAN, trim="tight",
         text="Skill issue."),
    dict(name="one_v_nine", kind="tts", voice="callum", settings=DRAMATIC, trim="tight",
         text="It's a ONE... versus... NINE!"),
    dict(name="rage_quit_keyboard", kind="sfx", seconds=4.0, trim="tight",
         prompt="Furious mechanical keyboard mashing, then a violent desk slam and a chair toppling over"),
    dict(name="legendary_drop", kind="sfx", seconds=3.0, trim="gentle",
         prompt="Video game legendary loot drop, magical rising sparkle shimmer into a triumphant golden chime hit"),
    dict(name="game_over_8bit", kind="sfx", seconds=3.5, trim="gentle",
         prompt="Retro 8-bit chiptune game over jingle, sad descending square-wave melody, original arcade style"),

    # ---------- ROUND 3: GENERAL CHAOS ----------
    dict(name="record_scratch", kind="sfx", seconds=1.5, trim="tight",
         prompt="Vinyl record scratch, abrupt needle rip across the record, freeze-frame moment"),
    dict(name="crickets", kind="sfx", seconds=4.5, trim="none",
         prompt="Awkward silence, crickets chirping quietly at night, one distant single cough"),
    dict(name="slide_whistle_fail", kind="sfx", seconds=3.0, trim="tight",
         prompt="Cartoon slide whistle descending, a long pathetic fall, ending in a small dull thud"),
    dict(name="bonk", kind="sfx", seconds=1.0, trim="tight",
         prompt="Cartoon bonk, hollow wooden knock on a head, single comical hit"),
    dict(name="clown_honk", kind="sfx", seconds=3.5, trim="tight",
         prompt="Circus clown bicycle horn honking twice, then a short goofy carnival calliope sting"),
    dict(name="machine_gun_fart", kind="sfx", seconds=3.0, trim="tight",
         prompt="Rapid-fire machine gun fart burst, comedic, ending with one long squeaky one"),
    dict(name="dial_up_modem", kind="sfx", seconds=5.0, trim="tight",
         prompt="1990s dial-up modem connecting, screeching handshake tones and static, harsh and nostalgic"),
    dict(name="sad_airhorn", kind="sfx", seconds=3.0, trim="gentle",
         prompt="Airhorn that starts loud and hype then deflates, drooping down in pitch and dying pathetically"),

    # ---------- ROUND 4: THINGS LEAGUE PLAYERS SAY TO EACH OTHER ----------
    dict(name="roy_walks_in", kind="tts", voice="callum", settings=DRAMATIC, trim="tight",
         text="AND ROY WALKS INTO FIVE PEOPLE! WHY, ROY?! WHYYY?!"),
    dict(name="roy_dead_talking", kind="tts", voice="callum", settings=DRAMATIC, trim="tight",
         text="Roy has been dead for forty seconds... and he is STILL... TALKING."),
    dict(name="roy_this_summer", kind="tts", voice="adam", settings=DRAMATIC, trim="tight",
         text="This summer... Roy... finally buys a ward."),
    dict(name="roy_stole_my_kill", kind="tts", voice="lily", settings=ANGRY, trim="tight",
         text="ROY! That was MY kill! MINE! I HAD it!"),
    dict(name="jake_ill_carry", kind="tts", voice="charlie", settings=CASUAL, trim="tight",
         text="Jake said he'd carry, mate. ...Jake is zero and eight."),
    dict(name="blanch_ward", kind="tts", voice="adam", settings=ANGRY, trim="tight",
         text="BLANCH! Buy a ward! ONE ward! I am BEGGING you!"),
    dict(name="who_is_blanch", kind="tts", voice="george", settings=DEADPAN, trim="tight",
         text="Sorry... who is Blanch?"),
    dict(name="blanch_alt_tabbed", kind="tts", voice="george", settings=CASUAL, trim="tight",
         text="Blanch, are you alt-tabbed again? ...Blanch. We can hear the YouTube."),
    dict(name="blanch_nice_ult", kind="tts", voice="lily", settings=DRAMATIC, trim="tight",
         text="Niiiice ult, Blanch. Stunning. Truly. Nobody was there."),
    dict(name="reece_not_tilted", kind="tts", voice="adam", settings=ANGRY, trim="tight",
         text="I'M NOT TILTED! REECE IS NOT TILTED! WHO SAID TILTED?!"),
    dict(name="reece_one_more_game", kind="tts", voice="adam", settings=DRAMATIC, trim="tight",
         text="It's 3 AM. Reece says one more game. It is never... one more game."),
    dict(name="not_feeding_scaling", kind="tts", voice="charlie", settings=CASUAL, trim="tight",
         text="Nah nah nah, I'm not feeding, Reece. I'm scaling. Trust."),
    dict(name="jake_and_reece_bots", kind="tts", voice="george", settings=DEADPAN, trim="tight",
         text="Jake and Reece are not bots. Bots would have warded."),
    dict(name="jake_i_got_this", kind="tts", voice="adam", settings=DRAMATIC, trim="tight",
         text="Jake said... I got this. ...Jake did not got this."),
    dict(name="jake_flash", kind="tts", voice="callum", settings=DRAMATIC, trim="tight",
         text="Jake has flash! Jake has— he flashed into the WALL. Into the wall."),
    dict(name="jake_mother_phone", kind="tts", voice="lily", settings=ANGRY, trim="tight",
         text="JAKE! Your mother is on the phone! No, I don't CARE that it's ranked!"),

    # ---------- ROUND 5: ARENA ----------
    dict(name="get_out_of_the_fire", kind="tts", voice="adam", settings=ANGRY, trim="tight",
         text="JAKE! THE FIRE! GET OUT OF THE FIRE! WHY ARE YOU STANDING IN THE FIRE?!"),
    dict(name="prismatic_bread", kind="tts", voice="callum", settings=DRAMATIC, trim="tight",
         text="PRISMATIC! PRISMATIC! ...and Roy took the bread one."),
    dict(name="who_picks_that", kind="tts", voice="george", settings=DEADPAN, trim="tight",
         text="Blanch. Who picks that augment? Genuinely. Who?"),
    dict(name="eighth_of_eight", kind="tts", voice="adam", settings=DEADPAN, trim="tight",
         text="Eighth place. Out of eight."),
    dict(name="crowd_favorite", kind="tts", voice="lily", settings=DRAMATIC, trim="tight",
         text="Oooh, Reece picked the Crowd Favorite. The crowd... was wrong."),
    dict(name="guest_executed", kind="tts", voice="callum", settings=DRAMATIC, trim="tight",
         text="Roy is at ONE HP! And the Guest of Honor— OH! EXECUTED! Roy has been EXECUTED!"),
    dict(name="corner_carry", kind="tts", voice="charlie", settings=CASUAL, trim="tight",
         text="Nah, Roy carried. I just stood in the corner, mate. Second place, baby."),
    dict(name="bravery_blanch", kind="tts", voice="george", settings=CASUAL, trim="tight",
         text="Blanch queued Bravery. Blanch has no idea what any of his buttons do."),
    dict(name="eat_the_flower", kind="tts", voice="lily", settings=ANGRY, trim="tight",
         text="JAKE! Stop eating the FLOWERS and FIGHT!"),
    dict(name="ring_closing", kind="tts", voice="callum", settings=DRAMATIC, trim="tight",
         text="The ring is CLOSING! The ring is CLOSING! And Jake... is still reading his augment."),
    dict(name="pick_the_augment", kind="tts", voice="adam", settings=ANGRY, trim="tight",
         text="PICK! PICK THE AUGMENT! ROY! THE TIMER! PIIIICK!"),
    dict(name="one_other_person", kind="tts", voice="adam", settings=DEADPAN, trim="tight",
         text="It's a two v two, Reece. There is exactly one other person to blame."),
    dict(name="flee_toward_enemy", kind="tts", voice="charlie", settings=CASUAL, trim="tight",
         text="Blanch pressed Flee, mate... toward the enemy. Bold."),
    dict(name="guest_did_more", kind="tts", voice="george", settings=DEADPAN, trim="tight",
         text="The Guest of Honor did more damage than Blanch. He is a GUEST."),
    dict(name="flash_last_round", kind="tts", voice="callum", settings=DRAMATIC, trim="tight",
         text="Jake has flash! No— he used it LAST round. Jake does NOT have flash."),
    dict(name="not_that_one", kind="tts", voice="lily", settings=ANGRY, trim="tight",
         text="Not THAT one— Reece! Reece, not that— ...you took it. You took it."),

    # ---------- ROUND 6: THE DAY JOBS ----------
    # Roy: a pitcher living in the mountains of Quincy
    dict(name="roy_no_hitter", kind="tts", voice="callum", settings=DRAMATIC, trim="tight",
         text="Roy threw a NO-HITTER! In Arena. Zero damage. A no-hitter."),
    dict(name="quincy_trailer", kind="tts", voice="adam", settings=DRAMATIC, trim="tight",
         text="From the mountains of Quincy... comes a man... who has never hit a Q."),
    dict(name="roy_bears_wifi", kind="tts", voice="lily", settings=ANGRY, trim="tight",
         text="Roy's lagging AGAIN! Roy! Tell the BEARS to get OFF the WIFI!"),
    # Blanch: Roy's coach
    dict(name="blanch_explains_roy", kind="tts", voice="george", settings=DEADPAN, trim="tight",
         text="Blanch is Roy's coach. ...That explains Roy."),
    dict(name="blanch_huddle", kind="tts", voice="adam", settings=DRAMATIC, trim="tight",
         text="Huddle up! Roy, stop crying. Jake, put the flowers down. Reece— where's Reece?"),
    dict(name="blanch_timeout", kind="tts", voice="callum", settings=DRAMATIC, trim="tight",
         text="Blanch called a TIMEOUT! There are NO TIMEOUTS in Arena, Blanch!"),
    dict(name="coach_whistle", kind="sfx", seconds=2.0, trim="tight",
         prompt="Shrill coach's whistle, three sharp piercing blasts, gym class energy"),
    # Jake: nursing school in Bismarck, North Dakota
    dict(name="jake_nursing_heal", kind="tts", voice="adam", settings=DEADPAN, trim="tight",
         text="Jake is in nursing school. And still. Will not. Heal."),
    dict(name="jake_code_blue", kind="tts", voice="callum", settings=DRAMATIC, trim="tight",
         text="CODE BLUE! CODE BLUE! Reece is flatlining and Nurse Jake... is buying an anvil."),
    dict(name="jake_bismarck_cold", kind="tts", voice="charlie", settings=CASUAL, trim="tight",
         text="It's minus thirty in Bismarck, mate. Jake's hands are frozen. Every game. Even in July."),
    dict(name="jake_clinical_6am", kind="tts", voice="lily", settings=ANGRY, trim="tight",
         text="JAKE! You have a clinical at SIX A.M.! Why are you QUEUEING?!"),
    # Reece: San Diego
    dict(name="reece_sunny_inside", kind="tts", voice="george", settings=DEADPAN, trim="tight",
         text="Seventy-two and sunny in San Diego. Reece is inside. Reece has been inside for six years."),
    dict(name="reece_burrito", kind="tts", voice="adam", settings=ANGRY, trim="tight",
         text="Reece is eating a California burrito MID-FIGHT! Reece! REECE! Put it DOWN!"),
    dict(name="reece_beach_eighth", kind="tts", voice="charlie", settings=CASUAL, trim="tight",
         text="Reece could be at the beach right now, mate. Instead he's eighth. Out of eight."),
    # the whole squad
    dict(name="four_friends_trailer", kind="tts", voice="adam", settings=DRAMATIC, trim="tight",
         text="Four friends. Two time zones. Zero wins."),
    dict(name="scouting_report", kind="tts", voice="george", settings=DEADPAN, trim="tight",
         text="Coach Blanch's scouting report: Roy can't hit. Jake won't heal. Reece is at the beach."),

    # ---------- ROUND 7: JAKE vs REECE, RAZZING EDITION ----------
    dict(name="reece_trust_me", kind="tts", voice="charlie", settings=CASUAL, trim="tight",
         text="Reece said 'trust me.' ...Never trust Reece, mate. Never."),
    dict(name="jake_did_not_heal", kind="tts", voice="adam", settings=DRAMATIC, trim="tight",
         text="Jake said... I'll heal you. ...Jake did not heal you."),
    dict(name="reece_not_on_it", kind="tts", voice="adam", settings=DRAMATIC, trim="tight",
         text="Reece said... I'm on it. ...Reece was not on it. Reece was dead."),
    dict(name="one_brain_cell", kind="tts", voice="george", settings=DEADPAN, trim="tight",
         text="Jake and Reece share one brain cell. Today it's Reece's turn. He's not using it."),
    dict(name="blame_both", kind="tts", voice="charlie", settings=CASUAL, trim="tight",
         text="Reece blames Jake. Jake blames Reece. I blame both of you, mate."),
    dict(name="both_one_hp", kind="tts", voice="callum", settings=DRAMATIC, trim="tight",
         text="Jake's one HP! Reece is one HP! ...They're hugging. Both dead."),
    dict(name="trust_the_process", kind="tts", voice="charlie", settings=CASUAL, trim="tight",
         text="Trust the process, Reece. ...The process is Jake. ...We're cooked, mate."),
    dict(name="carried_by_jake", kind="tts", voice="george", settings=DEADPAN, trim="tight",
         text="Reece got carried by Jake. Jake. Let that sink in."),
    dict(name="nobody_playing", kind="tts", voice="adam", settings=DEADPAN, trim="tight",
         text="Jake is thinking. Reece is also thinking. Nobody is playing."),
    dict(name="reece_said_easy", kind="tts", voice="adam", settings=DRAMATIC, trim="tight",
         text="Reece said... easy. ...It was not easy. It was eighth."),
    dict(name="reece_pinged_shop", kind="tts", voice="lily", settings=ANGRY, trim="tight",
         text="'I pinged,' says Reece. Reece pinged the SHOP, Jake! He pinged the SHOP!"),
    dict(name="duo_is_dead", kind="tts", voice="callum", settings=DRAMATIC, trim="tight",
         text="Jake and Reece! The DUO! The DYNAM— they're dead. The duo is dead."),
    dict(name="jake_locked_in", kind="tts", voice="charlie", settings=CASUAL, trim="tight",
         text="Jake's locked in, mate. Locked in. ...Jake's alt-tabbed. Jake's on YouTube."),
    dict(name="jake_my_bad", kind="tts", voice="adam", settings=DEADPAN, trim="tight",
         text="Jake said my bad. That's the sixth my bad. Jake. Stop being bad."),
    dict(name="custody_kill", kind="tts", voice="george", settings=DEADPAN, trim="tight",
         text="Jake and Reece have one kill between them. Shared. Like a custody arrangement."),
    dict(name="cant_carry_conversation", kind="tts", voice="charlie", settings=CASUAL, trim="tight",
         text="Carry me, Jake. ...Jake can't carry a conversation, mate."),

    # ---------- ROUND 8: ANONYMOUS RAZZING (fire at whoever just died) ----------
    dict(name="cant_carry_convo", kind="tts", voice="charlie", settings=CASUAL, trim="tight",
         text="Carry you? ...You can't carry a conversation, mate."),
    dict(name="custody_brain_cell", kind="tts", voice="george", settings=DEADPAN, trim="tight",
         text="You have one kill between you. One brain cell. Like a custody arrangement."),
    dict(name="you_did_not_got_this", kind="tts", voice="adam", settings=DRAMATIC, trim="tight",
         text="You said, I got this. ...You did not got this."),
    dict(name="trusted_you_last_round", kind="tts", voice="charlie", settings=CASUAL, trim="tight",
         text="Trust you? I trusted you last round, mate. Look at me. I'm dead."),
    dict(name="you_are_the_process", kind="tts", voice="george", settings=DEADPAN, trim="tight",
         text="Trust the process. ...You ARE the process. That's the problem."),
    dict(name="same_sentence", kind="tts", voice="adam", settings=DEADPAN, trim="tight",
         text="You said you'd carry. You're zero and eight. Those are the same sentence now."),
    dict(name="sixth_my_bad", kind="tts", voice="adam", settings=DEADPAN, trim="tight",
         text="That's your sixth my bad. At some point it stops being an accident."),
    dict(name="dying_slower", kind="tts", voice="charlie", settings=CASUAL, trim="tight",
         text="You're not scaling, mate. You're just dying slower."),
    dict(name="outplayed_by_ring", kind="tts", voice="callum", settings=DRAMATIC, trim="tight",
         text="OUTPLAYED! ...by the Ring of Fire. The ring outplayed him."),
    dict(name="try_playing", kind="tts", voice="adam", settings=DEADPAN, trim="tight",
         text="You're thinking. Great. Try playing."),
    dict(name="here_for_the_snacks", kind="tts", voice="george", settings=DEADPAN, trim="tight",
         text="The Guest of Honor did more than you. He's a guest. He's here for the snacks."),
    dict(name="big_orange_circle", kind="tts", voice="lily", settings=ANGRY, trim="tight",
         text="You're in the fire AGAIN! It's ORANGE! It's a big ORANGE circle!"),
    dict(name="apology_accepted", kind="tts", voice="adam", settings=DEADPAN, trim="tight",
         text="Apology accepted. Play better."),
    dict(name="spectator_energy", kind="tts", voice="george", settings=DEADPAN, trim="tight",
         text="You have spectator energy. You're playing, technically. But spectator energy."),
    dict(name="specifically_you", kind="tts", voice="charlie", settings=CASUAL, trim="tight",
         text="We're cooked. Not because of them. Because of you. Specifically you."),
    # ---------- ROUND 9A: NOT MY FAULT (deflection at full volume) ----------
    dict(name="not_my_fault", kind="tts", voice="charlie", settings=ANGRY, trim="tight",
         text="I blame you. I blame him. I blame the lobby. NOT MY FAULT!"),
    dict(name="it_was_lag", kind="tts", voice="adam", settings=ANGRY, trim="tight",
         text="That was LAG! ...It wasn't lag. But it was LAG!"),
    dict(name="my_screen_froze", kind="tts", voice="lily", settings=ANGRY, trim="tight",
         text="My screen FROZE! It froze! ...It did not freeze. I panicked. NOT MY FAULT!"),
    dict(name="sun_in_my_eyes", kind="tts", voice="charlie", settings=CASUAL, trim="tight",
         text="The sun was in my eyes, mate. ...I'm indoors. Still counts. Not my fault."),
    dict(name="cat_on_keyboard", kind="tts", voice="george", settings=DEADPAN, trim="tight",
         text="The cat was on the keyboard. ...I don't have a cat. Not my fault."),
    dict(name="i_pinged_it", kind="tts", voice="callum", settings=DRAMATIC, trim="tight",
         text="I PINGED IT! I pinged it! ...I pinged the shop. But I PINGED!"),
    dict(name="augment_was_bait", kind="tts", voice="charlie", settings=ANGRY, trim="tight",
         text="The augment was BAIT, mate! Bait! ...Fine, I took the bait. Not my fault it was bait!"),
    dict(name="mouse_slipped", kind="tts", voice="lily", settings=ANGRY, trim="tight",
         text="My MOUSE slipped! It slipped! Four times! In a ROW! NOT MY FAULT!"),

    # ---------- ROUND 9B: HUZZAH (positive, for the one good round) ----------
    dict(name="huzzah", kind="tts", voice="george", settings=DRAMATIC, trim="tight",
         text="HUZZAH! A round! We won a ROUND! Somebody write this down!"),
    dict(name="first_place", kind="tts", voice="callum", settings=DRAMATIC, trim="tight",
         text="FIRST PLACE! FIRST PLACE! I have NEVER seen this! LADIES AND GENTLEMEN!"),
    dict(name="mvp_clip_that", kind="tts", voice="adam", settings=DRAMATIC, trim="tight",
         text="The MVP. The legend. The man of the hour. Somebody clip that."),
    dict(name="clip_it", kind="tts", voice="charlie", settings=DRAMATIC, trim="tight",
         text="CLIP IT! Clip it, mate! That's going on the WALL!"),
    dict(name="proud_of_you", kind="tts", voice="lily", settings=CASUAL, trim="tight",
         text="I'm so PROUD of you! Look at you! Playing the game! Like a champion!"),
    dict(name="no_notes", kind="tts", voice="george", settings=DEADPAN, trim="tight",
         text="Well played. Truly. No notes. ...One note. But well played."),
    dict(name="we_survived", kind="tts", voice="charlie", settings=CASUAL, trim="tight",
         text="We survived the fire, mate! Both of us! Alive! Look at us. Legends."),
    dict(name="crowd_cheer", kind="sfx", seconds=3.0, trim="gentle",
         prompt="Stadium crowd erupting in cheers, whistles and applause, huge celebration"),

    # ---------- ROUND 9 V2: same lines, new cast ----------
    dict(name="not_my_fault_v2", kind="tts", voice="harry", settings=ANGRY, trim="tight",
         text="I blame you. I blame him. I blame the lobby. NOT MY FAULT!"),
    dict(name="it_was_lag_v2", kind="tts", voice="liam", settings=ANGRY, trim="tight",
         text="That was LAG! ...It wasn't lag. But it was LAG!"),
    dict(name="my_screen_froze_v2", kind="tts", voice="laura", settings=ANGRY, trim="tight",
         text="My screen FROZE! It froze! ...It did not freeze. I panicked. NOT MY FAULT!"),
    dict(name="sun_in_my_eyes_v2", kind="tts", voice="bill", settings=CASUAL, trim="tight",
         text="The sun was in my eyes. ...I'm indoors. Still counts. Not my fault."),
    dict(name="cat_on_keyboard_v2", kind="tts", voice="daniel", settings=DEADPAN, trim="tight",
         text="The cat was on the keyboard. ...I don't have a cat. Not my fault."),
    dict(name="i_pinged_it_v2", kind="tts", voice="harry", settings=DRAMATIC, trim="tight",
         text="I PINGED IT! I pinged it! ...I pinged the shop. But I PINGED!"),
    dict(name="augment_was_bait_v2", kind="tts", voice="chris", settings=ANGRY, trim="tight",
         text="The augment was BAIT! Bait! ...Fine, I took the bait. Not my fault it was bait!"),
    dict(name="mouse_slipped_v2", kind="tts", voice="jessica", settings=ANGRY, trim="tight",
         text="My MOUSE slipped! It slipped! Four times! In a ROW! NOT MY FAULT!"),
    dict(name="huzzah_v2", kind="tts", voice="oliver", settings=DRAMATIC, trim="tight",
         text="HUZZAH! A round! We won a ROUND! Somebody write this down!"),
    dict(name="first_place_v2", kind="tts", voice="liam", settings=DRAMATIC, trim="tight",
         text="FIRST PLACE! FIRST PLACE! I have NEVER seen this! LADIES AND GENTLEMEN!"),
    dict(name="mvp_clip_that_v2", kind="tts", voice="brian", settings=DRAMATIC, trim="tight",
         text="The MVP. The legend. The man of the hour. Somebody clip that."),
    dict(name="clip_it_v2", kind="tts", voice="laura", settings=DRAMATIC, trim="tight",
         text="CLIP IT! Clip it! That's going on the WALL!"),
    dict(name="proud_of_you_v2", kind="tts", voice="jessica", settings=CASUAL, trim="tight",
         text="I'm so PROUD of you! Look at you! Playing the game! Like a champion!"),
    dict(name="no_notes_v2", kind="tts", voice="alice", settings=DEADPAN, trim="tight",
         text="Well played. Truly. No notes. ...One note. But well played."),
    dict(name="we_survived_v2", kind="tts", voice="will", settings=CASUAL, trim="tight",
         text="We survived the fire! Both of us! Alive! Look at us. Legends."),

    # ---------- ROUND 10: CHEEKY (camp reads + gamer double entendres, PG-13) ----------
    dict(name="the_audacity", kind="tts", voice="laura", settings=DRAMATIC, trim="tight",
         text="The audacity. The AUDACITY. To die like that. In front of me."),
    dict(name="girl_the_bread", kind="tts", voice="jessica", settings=DRAMATIC, trim="tight",
         text="Not the bread augment. Girl. GIRL. We talked about this."),
    dict(name="its_giving_eighth", kind="tts", voice="river", settings=DEADPAN, trim="tight",
         text="It's giving... spectator. It's giving... eighth place. It's giving up."),
    dict(name="mother_has_arrived", kind="tts", voice="sarah", settings=DRAMATIC, trim="tight",
         text="Mother has ARRIVED. ...Mother is dead. Mother had one HP the whole time."),
    dict(name="died_so_pretty", kind="tts", voice="will", settings=CASUAL, trim="tight",
         text="You died so pretty. Honestly. Gorgeous death. Ten out of ten."),
    dict(name="top_or_bottom_lane", kind="tts", voice="liam", settings=CASUAL, trim="tight",
         text="Top or bottom? ...LANE. Top or bottom LANE. Answer the question."),
    dict(name="so_thick", kind="tts", voice="eric", settings=CASUAL, trim="tight",
         text="Ooh, he's a tank. He's so THICK. So much health. Respect."),
    dict(name="flash_on_me", kind="tts", voice="jessica", settings=CASUAL, trim="tight",
         text="Flash on me. Flash ON me. ...No, the spell. Use the spell."),
    dict(name="went_in_raw", kind="tts", voice="harry", settings=DRAMATIC, trim="tight",
         text="He went in RAW! No wards! No vision! Just went in RAW!"),
    dict(name="ganked_from_behind", kind="tts", voice="laura", settings=DRAMATIC, trim="tight",
         text="Ganked from behind. AGAIN. And you LOVED it."),
    dict(name="ill_peel_for_you", kind="tts", voice="brian", settings=DRAMATIC, trim="tight",
         text="I'll peel for you, baby. I'll peel for you all night."),
    dict(name="basically_married", kind="tts", voice="alice", settings=DEADPAN, trim="tight",
         text="You two are duo queue. You're basically married. Now fight about it."),
    dict(name="little_bit_a_date", kind="tts", voice="will", settings=CASUAL, trim="tight",
         text="It's a two v two, not a date. ...It's a little bit a date."),
    dict(name="filthy_backdoor", kind="tts", voice="oliver", settings=DRAMATIC, trim="tight",
         text="That backdoor was FILTHY. Filthy. ...I'm blushing."),
    dict(name="hard_to_kill", kind="tts", voice="daniel", settings=DEADPAN, trim="tight",
         text="He's hard. To kill. He's hard to kill. Why is everyone laughing."),
    dict(name="kiss_kill", kind="tts", voice="liam", settings=CASUAL, trim="tight",
         text="Give me a kiss— a KILL. Give me a kill. ...Kiss also fine."),

    # ---------- ROUND 11: JAKE vs REECE, CHEEKY RIVALRY ----------
    dict(name="tension_open_window", kind="tts", voice="sarah", settings=DRAMATIC, trim="tight",
         text="The tension between Jake and Reece. Somebody open a window."),
    dict(name="reece_jealous", kind="tts", voice="laura", settings=DRAMATIC, trim="tight",
         text="Reece is jealous. Jake got a kill and Reece is JEALOUS. It's cute."),
    dict(name="jake_watching", kind="tts", voice="alice", settings=DEADPAN, trim="tight",
         text="Jake has been watching Reece die for three rounds. Watching. Not helping. Watching."),
    dict(name="enemies_to_lovers", kind="tts", voice="river", settings=DEADPAN, trim="tight",
         text="Jake and Reece. Enemies to lovers. Currently enemies. Currently losing."),
    dict(name="reece_would_die", kind="tts", voice="will", settings=CASUAL, trim="tight",
         text="Reece would die for Jake. He just did. Twice. Romantic."),
    dict(name="bridal_style", kind="tts", voice="brian", settings=DRAMATIC, trim="tight",
         text="Jake carried Reece. Bridal style. All the way to eighth place."),
    dict(name="pick_me_jake", kind="tts", voice="jessica", settings=DRAMATIC, trim="tight",
         text="Pick me, Jake! Pick ME! ...He picked the bread augment. Over Reece."),
    dict(name="divorce_round_four", kind="tts", voice="daniel", settings=DEADPAN, trim="tight",
         text="Jake and Reece are getting a divorce. Round four. Irreconcilable positioning."),
    dict(name="stole_his_heart", kind="tts", voice="liam", settings=CASUAL, trim="tight",
         text="Reece stole Jake's kill. And his heart. Mostly the kill."),
    dict(name="they_switch", kind="tts", voice="laura", settings=CASUAL, trim="tight",
         text="Jake's bottom lane. Reece is top. ...They switch every game. Don't make it weird."),
    dict(name="hugging_in_fire", kind="tts", voice="harry", settings=DRAMATIC, trim="tight",
         text="Reece FLASHED! Straight into Jake! On PURPOSE! They're HUGGING! In the FIRE!"),
    dict(name="sure_jake", kind="tts", voice="eric", settings=CASUAL, trim="tight",
         text="Jake says he hates Reece. Jake queues with Reece every night. Sure, Jake."),
    dict(name="just_kiss_or_play", kind="tts", voice="chris", settings=CASUAL, trim="tight",
         text="Jake and Reece are fighting in the call again. Just kiss. Or play. One of the two."),
    dict(name="the_way_reece_looks", kind="tts", voice="oliver", settings=DRAMATIC, trim="tight",
         text="The way Reece looks at Jake when Jake misses. Pure hatred. Pure... something."),
    dict(name="kiss_to_break_tie", kind="tts", voice="river", settings=DEADPAN, trim="tight",
         text="Jake: two kills. Reece: two kills. Tied. Kiss to break the tie."),
    dict(name="behind_the_shop", kind="tts", voice="liam", settings=CASUAL, trim="tight",
         text="Reece wants to one v one Jake. Behind the shop. ...It's not a fight, is it, Reece."),

    # ---------- ROUND 12: STATS (damage charts as a personality) ----------
    dict(name="damage_charts", kind="tts", voice="laura", settings=DRAMATIC, trim="tight",
         text="Damage charts."),
    dict(name="damage_doesnt_matter", kind="tts", voice="jessica", settings=DRAMATIC, trim="tight",
         text="Damage doesn't matter!"),
    dict(name="kda_doesnt_matter", kind="tts", voice="harry", settings=ANGRY, trim="tight",
         text="KDA DOESN'T MATTER!!"),
    dict(name="look_at_the_charts", kind="tts", voice="sarah", settings=DRAMATIC, trim="tight",
         text="Look at the CHARTS, sweetie. Look at them. Scroll down. Keep scrolling."),
    dict(name="most_damage_eighth", kind="tts", voice="river", settings=DEADPAN, trim="tight",
         text="Most damage. Eighth place. The numbers were beautiful. The result was not."),
    dict(name="kda_player", kind="tts", voice="laura", settings=CASUAL, trim="tight",
         text="Oh, he's a KDA player. Precious. Protecting that ratio like it's a skincare routine."),
    dict(name="numbers_dont_lie", kind="tts", voice="eric", settings=CASUAL, trim="tight",
         text="The numbers don't lie, honey. You do. But the numbers don't."),
    dict(name="stat_check", kind="tts", voice="alice", settings=DEADPAN, trim="tight",
         text="Stat check. Damage: fine. Kills: no. Deaths: many. Vibes: immaculate."),
    dict(name="serving_damage", kind="tts", voice="jessica", settings=DRAMATIC, trim="tight",
         text="Serving DAMAGE. Serving NUMBERS. Serving... zero kills. But serving."),
    dict(name="you_are_a_sponge", kind="tts", voice="daniel", settings=DEADPAN, trim="tight",
         text="Top of the damage taken chart. Congratulations. You are a sponge."),
    dict(name="stat_padding", kind="tts", voice="oliver", settings=DRAMATIC, trim="tight",
         text="Stat padding. In ARENA. Farming numbers while we BURN."),
    dict(name="screenshot_the_stats", kind="tts", voice="liam", settings=CASUAL, trim="tight",
         text="Screenshot the stats. Screenshot it. That's going in the group chat. Forever."),
    dict(name="kda_is_a_construct", kind="tts", voice="brian", settings=DRAMATIC, trim="tight",
         text="KDA... is a construct. Deaths... are a mindset. Eighth place... is a lifestyle."),
    dict(name="put_some_respect", kind="tts", voice="sarah", settings=DRAMATIC, trim="tight",
         text="She did the most damage. SHE did. Say it. Put some respect on it."),
    dict(name="damage_share", kind="tts", voice="laura", settings=DRAMATIC, trim="tight",
         text="Forty percent damage share. Forty. She's carrying this whole relationship."),
    dict(name="deaths_are_content", kind="tts", voice="will", settings=CASUAL, trim="tight",
         text="Nine deaths? That's not a stat. That's content."),
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
    # no trimming at all, micro-fades only: for sounds that are QUIET on purpose
    # (crickets sit below every silence threshold — the quiet IS the content)
    "none": "afade=t=in:d=0.012,areverse,afade=t=in:d=0.06:curve=hsin,areverse",
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
