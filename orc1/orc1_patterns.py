# ═══════════════════════════════════════════════════════════════════════════════
# ORC-1 PATTERNS v9
# Add/edit patterns here. Restart orc1.py to apply.
#
# STEP FORMAT:
#   tones   chord tone indices (0=root 1=3rd 2=5th 3=7th 4=9th)
#           -1 = bass (one octave below root)
#           list = step chord  e.g. [0,1,2] plays root+3rd+5th simultaneously
#   oct     octave offset (-3 to +3)
#   vel     velocity multiplier (0.0–1.5, relative to input velocity)
#   gate    duration as fraction of step length (0.1=staccato  >1.0=legato)
#   rest    True = silent step
#   tie     True = no new attack; previous legato note continues
#           Only audible when the preceding step has gate > 1.0
#   prob    0.0–1.0 probability of firing (default 1.0 = always)
#           Values < 1.0 create generative patterns that shift on each loop
#
# PATTERN FIELDS:
#   description       human-readable summary
#   feel              category tag (used for display/filtering)
#   swing             default swing amount 0.0–0.5 (applied by ArpEngine)
#   recommended_rate  suggested subdivision string e.g. "1/16", "1/8", "1/4"
#                     (informational — web UI can suggest or auto-apply)
# ═══════════════════════════════════════════════════════════════════════════════


def s(tones, oct=0, vel=1.0, gate=0.8, prob=1.0):
    """Normal step."""
    if isinstance(tones, int): tones = [tones]
    return {"tones":tones,"oct":oct,"vel":vel,"gate":gate,
            "rest":False,"tie":False,"prob":prob}

def r():
    """Rest step."""
    return {"tones":[0],"oct":0,"vel":0.0,"gate":0.0,"rest":True,"tie":False,"prob":1.0}

def t(gate=0.8):
    """Tie step — no new attack. Preceding step should have gate > 1.0."""
    return {"tones":[0],"oct":0,"vel":1.0,"gate":gate,"rest":False,"tie":True,"prob":1.0}


FF=1.0; MF=0.8; MP=0.65; P=0.5; PP=0.35; ACC=1.2


PATTERNS = {


    # ── Directional ───────────────────────────────────────────────────────────


    "UP": {
        "description": "Simple ascending",
        "feel": "directional", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [s(0), s(1), s(2), s(3)],
    },
    "DOWN": {
        "description": "Simple descending",
        "feel": "directional", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [s(3), s(2), s(1), s(0)],
    },
    "UP_DOWN": {
        "description": "Ascending then descending",
        "feel": "directional", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [s(0),s(1),s(2),s(3),s(3),s(2),s(1),s(0)],
    },
    "UP_DOWN_2": {
        "description": "Up then down, no repeated top/bottom",
        "feel": "directional", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [s(0),s(1),s(2),s(3),s(2),s(1)],
    },
    "UP_OCT": {
        "description": "Ascending across two octaves",
        "feel": "directional", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [s(0),s(1),s(2),s(3),s(0,oct=1),s(1,oct=1),s(2,oct=1),s(3,oct=1)],
    },
    "DOWN_OCT": {
        "description": "Descending across two octaves",
        "feel": "directional", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [s(3,oct=1),s(2,oct=1),s(1,oct=1),s(0,oct=1),s(3),s(2),s(1),s(0)],
    },
    "UP_DOWN_OCT": {
        "description": "Two octave ascent then descent",
        "feel": "directional", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [
            s(0),s(1),s(2),s(3),s(0,oct=1),s(1,oct=1),s(2,oct=1),s(3,oct=1),
            s(3,oct=1),s(2,oct=1),s(1,oct=1),s(0,oct=1),s(3),s(2),s(1),s(0),
        ],
    },
    "MIRROR": {
        "description": "Bass to top and back — symmetrical arc",
        "feel": "directional", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [s(-1,vel=FF),s(0),s(1),s(2),s(3),s(2),s(1),s(0)],
    },


    # ── Legato / Tied ─────────────────────────────────────────────────────────
    # Ties only work when the preceding step has gate > 1.0.
    # The tie step prevents a new attack while the previous note still sounds.


    "LEGATO_UP": {
        "description": "Smooth ascending — each note held past the barline",
        "feel": "legato", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [
            s(0,gate=1.6), t(),
            s(1,gate=1.6), t(),
            s(2,gate=1.6), t(),
            s(3,gate=1.6), t(),
        ],
    },
    "LEGATO_DRIFT": {
        "description": "Wide slow legato — long overlapping notes",
        "feel": "legato", "swing": 0.0, "recommended_rate": "1/4",
        "steps": [
            s(0,vel=MP,gate=3.5), t(), t(),
            s(2,vel=P, gate=3.5), t(), t(),
            s(1,vel=MP,gate=3.5), t(), t(),
            s(3,vel=P, gate=3.5), t(), t(),
        ],
    },


    # ── Strum / Sweep ─────────────────────────────────────────────────────────


    "STRUM_UP": {
        "description": "Rapid upward strum — velocity ramp up",
        "feel": "strum", "swing": 0.0, "recommended_rate": "1/16",
        "steps": [
            s(0, vel=0.5, gate=1.6),
            s(1, vel=0.65, gate=1.4),
            s(2, vel=0.8, gate=1.2),
            s(3, vel=1.0, gate=1.0),
        ],
    },
    "STRUM_DOWN": {
        "description": "Rapid downward strum — velocity ramp down",
        "feel": "strum", "swing": 0.0, "recommended_rate": "1/16",
        "steps": [
            s(3, vel=1.0, gate=1.0),
            s(2, vel=0.8, gate=1.2),
            s(1, vel=0.65, gate=1.4),
            s(0, vel=0.5, gate=1.6),
        ],
    },
    "STRUM_UPDOWN": {
        "description": "Up strum then down strum",
        "feel": "strum", "swing": 0.0, "recommended_rate": "1/16",
        "steps": [
            s(0,vel=0.5,gate=1.6), s(1,vel=0.7,gate=1.4),
            s(2,vel=0.85,gate=1.2), s(3,vel=1.0,gate=1.0),
            s(3,vel=1.0,gate=1.0), s(2,vel=0.85,gate=1.2),
            s(1,vel=0.7,gate=1.4), s(0,vel=0.5,gate=1.6),
        ],
    },
    "SWEEP_UP": {
        "description": "Velocity sweep ascending — builds energy",
        "feel": "sweep", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [
            s(0,oct=-1,vel=PP,gate=0.9),
            s(0, vel=P, gate=0.9),
            s(1, vel=MP,gate=0.9),
            s(2, vel=MF,gate=0.9),
            s(3, vel=FF,gate=0.9),
            s(2,oct=1, vel=FF,gate=1.2),
        ],
    },
    "SWEEP_DOWN": {
        "description": "Velocity sweep descending — releases energy",
        "feel": "sweep", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [
            s(2,oct=1, vel=FF,gate=1.2),
            s(3, vel=FF,gate=0.9),
            s(2, vel=MF,gate=0.9),
            s(1, vel=MP,gate=0.9),
            s(0, vel=P, gate=0.9),
            s(0,oct=-1,vel=PP,gate=0.9),
        ],
    },
    "SPREAD_STRUM": {
        "description": "Wide strum — bass to high with big velocity arc",
        "feel": "strum", "swing": 0.0, "recommended_rate": "1/16",
        "steps": [
            s(-1, vel=FF, gate=1.8),
            s(0, vel=MF, gate=1.6),
            s(1, vel=MP, gate=1.4),
            s(2, vel=MP, gate=1.2),
            s(3, vel=MF, gate=1.0),
            s(2,oct=1, vel=FF, gate=0.9),
        ],
    },


    # ── Skip patterns ─────────────────────────────────────────────────────────


    "SKIP_1": {
        "description": "Root 5th 3rd 7th",
        "feel": "rhythmic", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [s(0),s(2),s(1),s(3)],
    },
    "SKIP_2": {
        "description": "Root 3rd root 5th bouncing",
        "feel": "rhythmic", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [s(0),s(1),s(0),s(2),s(0),s(1),s(0),s(3)],
    },
    "PINCH": {
        "description": "Outside in — bass+7th converging",
        "feel": "rhythmic", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [s(-1),s(3),s(0),s(2),s(-1),s(3),s(1),s(2)],
    },
    "BOUNCE": {
        "description": "Root octave bounce with inner voices",
        "feel": "rhythmic", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [
            s(0),s(0,oct=1),s(1),s(0,oct=1),
            s(2),s(0,oct=1),s(3),s(0,oct=1),
        ],
    },
    "FUGUE": {
        "description": "Bass and upper voice alternating in counterpoint",
        "feel": "rhythmic", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [
            s(-1,vel=FF,gate=0.9), s(2,vel=MP,gate=0.7),
            s(0, vel=MF,gate=0.9), s(3,vel=MP,gate=0.7),
            s(-1,vel=FF,gate=0.9), s(1,vel=MP,gate=0.7),
            s(0, vel=MF,gate=0.9), s(2,vel=P, gate=0.7),
        ],
    },


    # ── Rhythmic ──────────────────────────────────────────────────────────────


    "PUMP": {
        "description": "Four on the floor, accent on 1",
        "feel": "rhythmic", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [
            s(0,vel=ACC,gate=0.9), s(1,vel=MF,gate=0.7),
            s(2,vel=MF,gate=0.7),  s(3,vel=MF,gate=0.7),
        ],
    },
    "OFFBEAT": {
        "description": "Syncopated — notes off the beat",
        "feel": "rhythmic", "swing": 0.0, "recommended_rate": "1/16",
        "steps": [r(),s(0,vel=FF),r(),s(2,vel=MF),r(),s(1,vel=FF),r(),s(3,vel=MP)],
    },
    "STUTTER": {
        "description": "Double hit on root with skip",
        "feel": "rhythmic", "swing": 0.0, "recommended_rate": "1/16",
        "steps": [
            s(0,vel=ACC,gate=0.4),s(0,vel=MP,gate=0.4),s(2,vel=MF,gate=0.8),r(),
            s(0,vel=ACC,gate=0.4),s(0,vel=MP,gate=0.4),s(3,vel=MF,gate=0.8),r(),
        ],
    },
    "TRESILLO": {
        "description": "3+3+2 feel over 8 steps",
        "feel": "rhythmic", "swing": 0.0, "recommended_rate": "1/16",
        "steps": [s(0,vel=ACC),r(),r(),s(2,vel=MF),r(),r(),s(1,vel=MF),r()],
    },
    "GALLOP": {
        "description": "Triplet gallop — long-short-short feel",
        "feel": "rhythmic", "swing": 0.0, "recommended_rate": "1/16",
        "steps": [
            s(0,vel=ACC,gate=1.6), s(1,vel=MP,gate=0.4), s(0,vel=MF,gate=0.4),
            s(0,vel=ACC,gate=1.6), s(2,vel=MP,gate=0.4), s(0,vel=MF,gate=0.4),
        ],
    },
    "REGGAE": {
        "description": "Skank — chord stabs on the offbeats",
        "feel": "rhythmic", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [
            r(), s([0,1,2],vel=MF,gate=0.3),
            r(), s([0,1,2],vel=MF,gate=0.3),
        ],
    },


    # ── Afro-Latin rhythms ────────────────────────────────────────────────────


    "CLAVE": {
        "description": "Son clave 3-2 over 16 steps",
        "feel": "latin", "swing": 0.0, "recommended_rate": "1/16",
        "steps": [
            s(0,vel=ACC), r(), r(), s(2,vel=MF), r(),
            r(), r(), s(1,vel=MF), r(), r(),
            s(0,vel=ACC), r(), r(), s(3,vel=MF),
            r(), r(),
        ],
    },
    "CLAVE_23": {
        "description": "Son clave 2-3 — reverse of CLAVE",
        "feel": "latin", "swing": 0.0, "recommended_rate": "1/16",
        "steps": [
            s(0,vel=ACC), r(), r(), s(2,vel=MF),
            r(), r(), r(), r(),
            s(1,vel=MF), r(), r(), s(0,vel=ACC),
            r(), r(), s(3,vel=MF), r(),
        ],
    },
    "HABANERA": {
        "description": "Habanera/tango rhythm — syncopated dip",
        "feel": "latin", "swing": 0.0, "recommended_rate": "1/16",
        "steps": [
            s(-1,vel=FF,gate=0.9), r(),
            s(0, vel=MF,gate=0.6), s(1,vel=MP,gate=0.6),
            r(),
            s(0, vel=FF,gate=0.8), r(),
            s(-1,vel=MF,gate=0.7),
        ],
    },
    "BOSSA": {
        "description": "Bossa nova feel — bass and chord interlocking",
        "feel": "latin", "swing": 0.15, "recommended_rate": "1/16",
        "steps": [
            s(-1,vel=FF,gate=0.8), r(), s([0,2],vel=MP,gate=0.4), r(),
            r(),                   r(), s([1,2],vel=MF,gate=0.4), r(),
            s(-1,vel=MF,gate=0.8), r(), s([0,2],vel=MP,gate=0.4), r(),
            s(0, vel=MF,gate=0.6), r(), s([1,3],vel=MP,gate=0.4), r(),
        ],
    },


    # ── Chord stabs ───────────────────────────────────────────────────────────


    "STAB": {
        "description": "Full chord stab on beat 1",
        "feel": "stab", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [s([0,1,2,3],vel=FF,gate=0.5),r(),r(),r()],
    },
    "STAB_2": {
        "description": "Chord stabs on 1 and 3",
        "feel": "stab", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [s([0,1,2,3],vel=FF,gate=0.5),r(),s([0,1,2,3],vel=MF,gate=0.4),r()],
    },
    "STAB_OFFBEAT": {
        "description": "Chord stabs on 2 and 4",
        "feel": "stab", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [r(),s([0,1,2,3],vel=FF,gate=0.5),r(),s([0,1,2,3],vel=MF,gate=0.4)],
    },
    "MIXED": {
        "description": "Bass note then chord stab alternating",
        "feel": "stab", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [
            s(-1,vel=FF,gate=0.9), s([0,1,2],vel=MF,gate=0.5),
            s(-1,vel=MF,gate=0.7), s([1,2,3],vel=MF,gate=0.5),
        ],
    },


    # ── Classical ─────────────────────────────────────────────────────────────


    "ALBERTI": {
        "description": "Alberti bass — low mid high mid",
        "feel": "classical", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [s(0,vel=MF),s(2,vel=MP),s(1,vel=MP),s(2,vel=MP)],
    },
    "ALBERTI_BASS": {
        "description": "Alberti with bass note below",
        "feel": "classical", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [s(-1,vel=FF),s(2,vel=MP),s(1,vel=MP),s(2,vel=MP)],
    },
    "WALTZ": {
        "description": "3/4 waltz — bass on 1, chord on 2 and 3",
        "feel": "classical", "swing": 0.0, "recommended_rate": "1/4",
        "steps": [s(-1,vel=FF,gate=0.9),s([1,2],vel=MP,gate=0.5),s([1,2],vel=P,gate=0.5)],
    },


    # ── Jazz ──────────────────────────────────────────────────────────────────


    "JAZZ_UP": {
        "description": "Jazz ascending with swing",
        "feel": "jazz", "swing": 0.2, "recommended_rate": "1/8",
        "steps": [
            s(0,vel=MF,gate=0.7), s(1,vel=MP,gate=0.6),
            s(2,vel=MF,gate=0.7), s(3,vel=ACC,gate=0.9),
            s(2,vel=MP,gate=0.6), s(1,vel=MP,gate=0.6),
        ],
    },
    "COMPING": {
        "description": "Jazz comping rhythm with swing",
        "feel": "jazz", "swing": 0.25, "recommended_rate": "1/8",
        "steps": [
            s([0,1,2],vel=MF,gate=0.4), r(),
            r(), s([1,2,3],vel=MP,gate=0.3),
            s([0,2],vel=MF,gate=0.5), r(),
            r(), r(),
        ],
    },
    "BEBOP": {
        "description": "Bebop 8th note run",
        "feel": "jazz", "swing": 0.2, "recommended_rate": "1/8",
        "steps": [
            s(0),s(1),s(2),s(3),
            s(2,oct=1),s(1,oct=1),s(2),s(1),
        ],
    },


    # ── Ambient ───────────────────────────────────────────────────────────────


    "PEDAL": {
        "description": "Root drone — single note sustained, no movement",
        "feel": "ambient", "swing": 0.0, "recommended_rate": "1/4",
        "steps": [s(0, vel=MF, gate=3.8)],
    },
    "DRIFT": {
        "description": "Slow wide intervals, long gates",
        "feel": "ambient", "swing": 0.0, "recommended_rate": "1/4",
        "steps": [
            s(0,vel=MP,gate=1.8), s(2,oct=1,vel=P,gate=1.8),
            s(1,vel=MP,gate=1.8), s(3,oct=1,vel=P,gate=1.8),
        ],
    },
    "SHIMMER": {
        "description": "High register gentle arpeggio",
        "feel": "ambient", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [
            s(0,oct=1,vel=P,gate=1.5), s(2,oct=1,vel=PP,gate=1.5),
            s(1,oct=1,vel=P,gate=1.5), s(3,oct=2,vel=PP,gate=1.5),
            s(2,oct=1,vel=P,gate=1.5), s(0,oct=1,vel=PP,gate=1.5),
        ],
    },
    "PULSE": {
        "description": "Slow root pulse with occasional upper note",
        "feel": "ambient", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [
            s(0,vel=MP,gate=0.9), s(0,vel=P,gate=0.9), s(2,oct=1,vel=PP,gate=1.2),
            s(0,vel=MP,gate=0.9), s(0,vel=P,gate=0.9), s(1,oct=1,vel=PP,gate=1.2),
            s(0,vel=MP,gate=0.9), s(0,vel=P,gate=0.9),
        ],
    },


    # ── Bass-forward ──────────────────────────────────────────────────────────


    "BASS_WALK": {
        "description": "Walking bass feel",
        "feel": "bass", "swing": 0.15, "recommended_rate": "1/8",
        "steps": [s(-1,vel=FF,gate=0.9),s(0,vel=MF,gate=0.7),s(1,vel=MP,gate=0.7),s(2,vel=MF,gate=0.7)],
    },
    "BASS_CHORD": {
        "description": "Bass then upper chord alternating",
        "feel": "bass", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [
            s(-1,vel=FF,gate=0.8), s([1,2,3],vel=MF,gate=0.5),
            s(-1,vel=MF,gate=0.7), s([0,2,3],vel=MP,gate=0.5),
        ],
    },


    # ── Polyrhythmic ──────────────────────────────────────────────────────────


    "FIVE": {
        "description": "5-step — phases against 4/4",
        "feel": "polyrhythm", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [s(0,vel=ACC),s(1),s(2),s(1),s(3)],
    },
    "THREE": {
        "description": "3-step — phases against 4/4",
        "feel": "polyrhythm", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [s(0,vel=ACC),s(2),s(1)],
    },
    "SEVEN": {
        "description": "7-step — dense phasing",
        "feel": "polyrhythm", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [s(0,vel=ACC),s(1),s(2),s(0),s(3),s(1),s(2)],
    },
    "SIX": {
        "description": "6-step — 3/4 or triplet overlay",
        "feel": "polyrhythm", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [s(0,vel=ACC),s(1),s(2),s(0,vel=MF),s(3),s(2)],
    },


    # ── Probabilistic / Generative ────────────────────────────────────────────
    # These patterns sound different on each repetition.
    # prob=1.0 = always fires. prob=0.5 = fires ~half the time.


    "GHOST": {
        "description": "Probabilistic — shifting ghosted pattern, never repeats exactly",
        "feel": "generative", "swing": 0.0, "recommended_rate": "1/16",
        "steps": [
            s(0, vel=MF, gate=0.6, prob=0.95),
            s(1, vel=P,  gate=0.5, prob=0.4),
            s(2, vel=MP, gate=0.6, prob=0.7),
            s(1, vel=P,  gate=0.4, prob=0.3),
            s(0, vel=MF, gate=0.6, prob=0.9),
            s(3, vel=P,  gate=0.5, prob=0.25),
            s(2, vel=MP, gate=0.6, prob=0.6),
            s(0, vel=P,  gate=0.4, prob=0.5),
        ],
    },
    "SPARSE": {
        "description": "Mostly silence with occasional accent — unpredictable",
        "feel": "generative", "swing": 0.0, "recommended_rate": "1/8",
        "steps": [
            s(0, vel=MF, gate=1.2, prob=0.7),
            r(),
            s(2, vel=P,  gate=0.8, prob=0.35),
            r(),
            s(1, vel=MP, gate=1.0, prob=0.5),
            r(),
            s(0, vel=MF, gate=0.9, prob=0.6),
            s(3, vel=PP, gate=0.7, prob=0.2),
        ],
    },
    "DICE": {
        "description": "Every step probabilistic — maximum generativity",
        "feel": "generative", "swing": 0.0, "recommended_rate": "1/16",
        "steps": [
            s(0, vel=ACC, gate=0.7, prob=0.8),
            s(2, vel=MF,  gate=0.5, prob=0.45),
            s(1, vel=MP,  gate=0.6, prob=0.55),
            s(3, vel=MF,  gate=0.5, prob=0.35),
            s(0, vel=MF,  gate=0.7, prob=0.7),
            s(2, vel=P,   gate=0.5, prob=0.4),
            s(1, vel=MP,  gate=0.6, prob=0.5),
            s(3, vel=P,   gate=0.4, prob=0.3),
        ],
    },

}


# ── Pattern order ─────────────────────────────────────────────────────────────
# Explicit ordering for the CC-sweep and pattern browser.
# Any patterns present in PATTERNS but absent from this list are
# automatically appended alphabetically at the end — new patterns
# never silently disappear from the sweep.

PATTERN_ORDER = [
    "UP","DOWN","UP_DOWN","UP_DOWN_2","UP_OCT","DOWN_OCT","UP_DOWN_OCT","MIRROR",
    "LEGATO_UP","LEGATO_DRIFT",
    "STRUM_UP","STRUM_DOWN","STRUM_UPDOWN","SWEEP_UP","SWEEP_DOWN","SPREAD_STRUM",
    "SKIP_1","SKIP_2","PINCH","BOUNCE","FUGUE",
    "PUMP","OFFBEAT","STUTTER","TRESILLO","GALLOP","REGGAE",
    "CLAVE","CLAVE_23","HABANERA","BOSSA",
    "STAB","STAB_2","STAB_OFFBEAT","MIXED",
    "ALBERTI","ALBERTI_BASS","WALTZ",
    "JAZZ_UP","COMPING","BEBOP",
    "PEDAL","DRIFT","SHIMMER","PULSE",
    "BASS_WALK","BASS_CHORD",
    "FIVE","THREE","SEVEN","SIX",
    "GHOST","SPARSE","DICE",
]

# Catch any patterns added to PATTERNS but not listed above
_unlisted = [k for k in PATTERNS if k not in PATTERN_ORDER]
if _unlisted:
    PATTERN_ORDER = PATTERN_ORDER + sorted(_unlisted)
