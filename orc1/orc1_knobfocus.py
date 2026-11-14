"""orc1_knobfocus -- a bank of knobs that FOLLOWS the voice you are playing.

Any controller's endless knobs (the BeatStep Pro's 16 encoders by default) are
claimed by device name + channel + CC. Each knob is a SLOT on a PAGE; the page
belongs to the module in FOCUS, and focus follows what you play: the poly's page
when you play the poly, the drums' page (of the drum voice you hit) when you hit
a drum. A knob turn moves that module's REAL parameter CC from its current value,
so nothing jumps when focus changes.

Stdlib only: the brain feeds it messages and gives it a send function.

Focus rules ("follow" mode):
  * a note-on reaching the synth bus on a module's channel can move focus there;
  * only if that module was QUIET for `quiet_s` before this note (a loop that
    never stops doesn't keep grabbing the knobs back), and
  * never while you are turning knobs (`hold_s` after the last turn);
  * modules in `ignore` never take focus; a module with no page never does.
  The drum VOICE inside the drums page follows the same rules, per voice.
"lock" mode: focus stays on `locked` (and `drum_voice` if set) until changed.

Values: every CC the brain sends to the synth bus is observed, so the current
value of each parameter is known whoever moved it (Fire, web, a scene). At
focus change, values the hosts saved (~/.orc1_*_params.json) fill in anything
not seen in the last few seconds. A parameter nobody has ever set starts at 64.

Knob encodings (`encoding`):
  "offset64"  64 = no move, 65 = +1, 63 = -1   (Arturia "Relative #1")
  "twos"      1 = +1, 127 = -1                 (two's complement)
  "signmag"   1 = +1, 65 = -1                  (Mackie / MCU style)
  "abs"       plain 0-127 knobs, with PICKUP: a knob takes over only once it
              passes the parameter's current value (no jumps)

MCU surfaces: any input port listed in `mcu_ports` is read as Mackie Control
(orc1_mcu): V-pots 1-8 = slots 1-8 (relative), faders 1-8 = slots 9-16.
"""

import json
import os
import threading
import time

import orc1_mcu as _mcu

N_SLOTS = 16
CONF_FILE = "~/.orc1_knobfocus.json"

# Files the module hosts keep their live values in (orc1_synth.py *_PARAMS_FILE).
PARAM_FILES = {
    "poly": "~/.orc1_poly_params.json",
    "mono": "~/.orc1_mono_params.json",
    "chip": "~/.orc1_chip_params.json",
    "drums": "~/.orc1_drum_params.json",
    "bbkit": "~/.orc1_bbkit_params.json",
}
BBKIT_STATE = "~/.orc1_bbkit.json"         # the host's last-selected kit {"kit": folder}
KITS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kits")
try:                                        # the shared table (orc1_config is numpy-free)
    import orc1_config as _cfg
    BB_CC = dict(_cfg.BBKIT_SHAPE_CC)
    BB_DEFAULT = dict(_cfg.BBKIT_SHAPE_DEFAULT)
except Exception:                           # pragma: no cover
    BB_CC, BB_DEFAULT = {}, {}
BB_CC_NAME = {v: k for k, v in BB_CC.items()}
BB_KIT_KEYS = ("kit_tune", "kit_cutoff")
# BBKit page: top row = the drum you hit, bottom row = kit-wide + common drums' levels
BB_FOCUS_PARAMS = [("level", "level"), ("tune", "tune"), ("attack", "attack"),
                   ("decay", "decay"), ("pan", "pan"), ("cutoff", "cutoff"),
                   ("reso", "reso")]
BB_LEVEL_ROW = [36, 38, 42, 46, 49, 51, 39]      # GM kick, snare, CH, OH, crash, ride, clap
# chip module -> its voice key in the chip file (= cfg.MODULE_VOICE)
CHIP_VOICE = {"chip_p1": "pulse1", "chip_p2": "pulse2", "chip_tri": "triangle",
              "chip_noise": "noise", "chip_noise2": "noise2", "chip_pcm": "pcm",
              "gb_wave": "wave"}
# CCs the chip engine keeps ONE value for across its voices (mirrors
# orc1_synth._CHIP_ENGINE_RING_CCS / _CHIP_ENGINE_CCS; 'wave' only shares ring mod)
CHIP_ENGINE_RING = frozenset({12, 14, 15})
CHIP_ENGINE_CCS = frozenset({8, 9, 10, 11, 70, 71, 74, 75, 85})

FRESH_S = 10.0     # a value seen this recently beats the hosts' saved file

DEFAULTS = {
    "enabled": True,
    "device": "BeatStep Pro",          # input port name contains this
    "channel": 8,                      # 1-based
    "ccs": list(range(20, 36)),        # knob 1..16 send these CCs
    "encoding": "offset64",
    "step": 1,                         # value change per relative tick
    "mode": "follow",                  # follow | lock
    "locked": None,                    # module name in lock mode
    "drum_voice": None,                # None = follow; a voice name = stay on it
    "quiet_s": 2.0,
    "hold_s": 1.5,
    "ignore": [],                      # modules that never take focus
    "mcu_ports": [],                   # input ports read as Mackie Control
    "pages": {},                       # module -> 16 slots, overrides the defaults
}

# ---- default pages ----------------------------------------------------------
# A slot is [cc, "Name"], or None (knob does nothing). Knobs 1-8 = top row,
# 9-16 = bottom row on the BeatStep Pro. Every CC here is checked against the
# module's own CC handler by _test_knobfocus (no slot may point at nothing).
_ENV = [[8, "Attack"], [9, "Decay"], [10, "Sustain"], [11, "Release"]]
_RING = [[12, "Ring depth"], [14, "Ring ratio"], [15, "Ring freq"]]
_FILT = [[74, "Cutoff"], [71, "Resonance"], [70, "Filter mode"]]


def _chip_page(fourth):
    return (_FILT + [fourth, [75, "Octave"]] + _RING
            + _ENV + [[13, "Level"], [5, "Glide"], [77, "Vib depth"], [78, "Vib rate"]])


DEFAULT_PAGES = {
    "poly": _FILT + [[1, "Waveform"], [75, "Octave"]] + _RING
            + _ENV + [[13, "Level"], None, None, None],
    "mono": _FILT + [[1, "Waveform"], [75, "Octave"]] + _RING
            + _ENV + [[13, "Level"], [5, "Glide"], [19, "Osc gain"], None],
    "chip_p1": _chip_page([1, "Duty"]),
    "chip_p2": _chip_page([1, "Duty"]),
    "chip_tri": _chip_page([86, "Pan"]),
    "chip_noise": _chip_page([3, "Noise mode"]),
    "chip_noise2": _chip_page([3, "Noise mode"]),
    "gb_wave": [[1, "Wave preset"], [86, "Pan"], [90, "Slide amt"], [110, "Slide time"]] + _RING
               + [[111, "Trem depth"]]
               + _ENV + [[7, "Volume"], [5, "Glide"], [77, "Vib depth"], [78, "Vib rate"]],
}
# drums: top row = the focused drum voice's own params, bottom row = 8 levels
DRUM_FOCUS_PARAMS = ["level", "tune", "attack", "decay", "filter_freq", "filter_res"]
DRUM_LEVEL_ROW = ["bass_drum", "snare", "closed_hat", "open_hat",
                  "clap", "rimshot", "low_tom", "cymbal"]
_DRUM_SHORT = {"bass_drum": "Kick", "snare": "Snare", "closed_hat": "CH",
               "open_hat": "OH", "clap": "Clap", "rimshot": "Rim", "low_tom": "LTom",
               "mid_tom": "MTom", "hi_tom": "HTom", "cymbal": "Cym", "cowbell": "Bell"}


def _read_json(path):
    try:
        with open(os.path.expanduser(path)) as f:
            return json.load(f)
    except Exception:
        return None


def _int_keys(d):
    out = {}
    for k, v in (d or {}).items():
        try:
            out[int(k)] = int(v)
        except (TypeError, ValueError):
            pass
    return out


def drum_note_map(synth_src, overrides=None):
    """{note: drum voice}, read from orc1_synth.py's DEFAULT_DRUM_NOTE_MAP by
    source text (the brain can't import the numpy synth), plus the config's
    SYNTH["DRUM_NOTE_MAP"] overrides, exactly as orc1_synth builds DRUM_NOTE_MAP."""
    import ast
    out = {}
    try:
        with open(synth_src, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                    getattr(t, "id", None) == "DEFAULT_DRUM_NOTE_MAP" for t in node.targets):
                out = dict(ast.literal_eval(node.value))
                break
    except Exception:
        out = {}
    for k, v in (overrides or {}).items():
        try:
            out[int(k)] = v
        except (TypeError, ValueError):
            pass
    return out


def decode_relative(value, encoding):
    """One relative knob CC value -> signed ticks."""
    if encoding == "offset64":
        return value - 64
    if encoding == "twos":
        return value - 128 if value >= 64 else value
    if encoding == "signmag":
        return -(value & 0x3F) if value & 0x40 else value & 0x3F
    raise ValueError("not a relative encoding: %r" % (encoding,))


class KnobFocus(object):
    def __init__(self, send_cc, module_ch, module_at, drum_voice_of, drum_layout,
                 conf_file=CONF_FILE, param_files=None, now=time.monotonic,
                 log=lambda s: None, kits_dir=KITS_DIR, bbkit_state=BBKIT_STATE):
        """send_cc(channel 1-16, cc, value); module_ch(module) -> channel or None;
        module_at(channel) -> module or None; drum_voice_of(note) -> voice or None;
        drum_layout = cfg.DRUM_CC["LAYOUT"] {voice: {"start", "params"}}."""
        self._send = send_cc
        self._module_ch = module_ch
        self._module_at = module_at
        self._drum_voice_of = drum_voice_of
        self._layout = dict(drum_layout or {})
        self._conf_file = conf_file
        self._files = dict(PARAM_FILES if param_files is None else param_files)
        self._now = now
        self._log = log
        self._lock = threading.RLock()
        self.conf = dict(DEFAULTS)
        self.focus = None              # module in focus
        self.voice = None              # drum voice in focus (drums page)
        self.bb_note = None            # BBKit drum (note) in focus (bbkit page)
        self._kits_dir = kits_dir
        self._bb_state = bbkit_state
        self._bb_kit = None            # current kit folder (lazy: the host's state file)
        self._bb_target = None         # last "pick drum" CC seen on the bus
        self._bb_names = {}            # kit -> {note: instrument name}
        self._vals = {}                # value key -> (value, time seen)
        self._last_note = {}           # module or ("drum", voice) -> time
        self._last_turn = -1e9
        self._pickup = {}              # slot -> True once an abs knob has caught up
        self.load()

    # ---- config ---------------------------------------------------------------
    def load(self):
        raw = _read_json(self._conf_file) if self._conf_file else None
        with self._lock:
            self.conf = self._clean(raw or {})

    def _clean(self, raw):
        c = dict(DEFAULTS)
        for k in DEFAULTS:
            if k in raw:
                c[k] = raw[k]
        c["channel"] = max(1, min(16, int(c["channel"])))
        ccs = [int(x) for x in (c["ccs"] or [])][:N_SLOTS]
        c["ccs"] = ccs
        if c["encoding"] not in ("offset64", "twos", "signmag", "abs"):
            c["encoding"] = "offset64"
        if c["mode"] not in ("follow", "lock"):
            c["mode"] = "follow"
        c["step"] = max(1, int(c["step"]))
        c["ignore"] = [str(m) for m in (c["ignore"] or [])]
        c["mcu_ports"] = [str(p) for p in (c["mcu_ports"] or [])]
        c["pages"] = dict(c["pages"] or {})
        return c

    def save(self):
        if not self._conf_file:
            return
        path = os.path.expanduser(self._conf_file)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self.conf, f, indent=1)
            os.replace(tmp, path)
        except Exception as e:
            self._log("  ! knobfocus save failed: %s" % e)

    # ---- pages ------------------------------------------------------------------
    def page(self, module=None, voice=None):
        """The 16 slots of a module's page: [(value key, cc, name, pre) or None];
        pre = (cc, value) pairs sent first (the BBKit "pick drum" CC)."""
        module = self.focus if module is None else module
        if module is None:
            return [None] * N_SLOTS
        if module == "drums":
            return self._drum_page(self.voice if voice is None else voice)
        if module == "bbkit":
            return self._bbkit_page(self.bb_note if voice is None else voice)
        spec = self.conf["pages"].get(module) or DEFAULT_PAGES.get(module)
        if not spec:
            return [None] * N_SLOTS
        out = []
        for s in list(spec)[:N_SLOTS]:
            if s and len(s) >= 2:
                cc = int(s[0])
                out.append((self._key(module, cc), cc, str(s[1]), ()))
            else:
                out.append(None)
        return out + [None] * (N_SLOTS - len(out))

    def _drum_cc(self, voice, param):
        lay = self._layout.get(voice)
        if not lay or param not in lay["params"]:
            return None
        return lay["start"] + lay["params"].index(param)

    def _drum_page(self, voice):
        out = []
        short = _DRUM_SHORT.get(voice, voice or "")
        for p in DRUM_FOCUS_PARAMS:
            cc = self._drum_cc(voice, p) if voice else None
            out.append((("drums", cc), cc, "%s %s" % (short, p.replace("_", " ")), ())
                       if cc is not None else None)
        out += [None, None]
        for v in DRUM_LEVEL_ROW:
            cc = self._drum_cc(v, "level")
            out.append((("drums", cc), cc, "%s level" % _DRUM_SHORT.get(v, v), ())
                       if cc is not None else None)
        return out[:N_SLOTS]

    # ---- BBKit: kits, names, page ----------------------------------------------
    def _bb_kits(self):
        try:
            return sorted(d for d in os.listdir(self._kits_dir)
                          if os.path.isfile(os.path.join(self._kits_dir, d, "kit.json")))
        except Exception:
            return []

    def bb_kit(self):
        if self._bb_kit is None:
            st = _read_json(self._bb_state) or {}
            kits = self._bb_kits()
            k = st.get("kit")
            self._bb_kit = k if k in kits else (kits[0] if kits else "")
        return self._bb_kit

    def bb_names(self, kit=None):
        kit = self.bb_kit() if kit is None else kit
        if kit not in self._bb_names:
            m = _read_json(os.path.join(self._kits_dir, kit, "kit.json")) or {}
            names = {}
            for n, ins in (m.get("instruments") or {}).items():
                try:
                    names[int(n)] = str(ins.get("name") or "n%s" % n)
                except (TypeError, ValueError, AttributeError):
                    pass
            self._bb_names[kit] = names
        return self._bb_names[kit]

    def _bb_label(self, note, kit):
        if note is None:
            return ""
        return self.bb_names(kit).get(note, "Note %d" % note)

    def _bbkit_page(self, note):
        kit = self.bb_kit()
        out = []
        lab = self._bb_label(note, kit)[:12]
        for p, short in BB_FOCUS_PARAMS:
            cc = BB_CC.get(p)
            out.append((("bbkit", kit, note, cc), cc, "%s %s" % (lab, short), ((BB_CC["note"], note),))
                       if (note is not None and cc is not None) else None)
        for p, short in (("kit_tune", "Kit tune"), ("kit_cutoff", "Kit cutoff")):
            cc = BB_CC.get(p)
            out.append((("bbkit", kit, "kit", cc), cc, short, ()) if cc is not None else None)
        names = self.bb_names(kit)
        for n in BB_LEVEL_ROW:
            if names and n not in names:
                out.append(None)
                continue
            out.append((("bbkit", kit, n, BB_CC["level"]), BB_CC["level"],
                        "%s level" % self._bb_label(n, kit)[:10], ((BB_CC["note"], n),)))
        return out[:N_SLOTS]

    def _default(self, key):
        if key and key[0] == "bbkit":
            return BB_DEFAULT.get(BB_CC_NAME.get(key[3]), 64)
        return 64

    def has_page(self, module):
        if module == "drums":
            return bool(self._layout)
        if module == "bbkit":
            return bool(BB_CC) and bool(self._bb_kits())
        return bool(self.conf["pages"].get(module) or DEFAULT_PAGES.get(module))

    @staticmethod
    def _key(module, cc):
        """Value-cache key. Chip CCs the engine shares across voices share a key."""
        voice = CHIP_VOICE.get(module)
        if voice is not None:
            if cc in CHIP_ENGINE_RING or (cc in CHIP_ENGINE_CCS and voice != "wave"):
                return ("chip_engine", cc)
        return (module, cc)

    # ---- the hosts' saved values ------------------------------------------------
    def _saved_values(self, module):
        """{value key: value} from the file this module's host saves."""
        out = {}
        if module in ("poly", "mono"):
            for cc, v in _int_keys(_read_json(self._files.get(module, ""))).items():
                out[(module, cc)] = v
        elif module in CHIP_VOICE:
            d = _read_json(self._files.get("chip", "")) or {}
            for cc, v in _int_keys(d.get("_engine")).items():
                out[("chip_engine", cc)] = v
            for cc, v in _int_keys(d.get(CHIP_VOICE[module])).items():
                out.setdefault(self._key(module, cc), v)
        elif module == "drums":
            d = _read_json(self._files.get("drums", "")) or {}
            for voice, params in d.items():
                if not isinstance(params, dict):
                    continue
                for p, v in params.items():
                    cc = self._drum_cc(voice, p)
                    if cc is not None:
                        try:
                            out[("drums", cc)] = int(v)
                        except (TypeError, ValueError):
                            pass
        elif module == "bbkit":
            kit = self.bb_kit()
            d = ((_read_json(self._files.get("bbkit", "")) or {}).get("kits") or {}).get(kit) or {}
            for n, params in (d.get("notes") or {}).items():
                for p, v in (params or {}).items():
                    if p in BB_CC:
                        try:
                            out[("bbkit", kit, int(n), BB_CC[p])] = int(v)
                        except (TypeError, ValueError):
                            pass
            for p, v in (d.get("kit") or {}).items():
                if p in BB_CC:
                    try:
                        out[("bbkit", kit, "kit", BB_CC[p])] = int(v)
                    except (TypeError, ValueError):
                        pass
        return out

    def _refresh_from_files(self, module):
        now = self._now()
        for k, v in self._saved_values(module).items():
            have = self._vals.get(k)
            if have is None or now - have[1] > FRESH_S:
                self._vals[k] = (v, now - FRESH_S)   # file values never count as fresh

    # ---- focus --------------------------------------------------------------------
    def _set_focus(self, module, voice=None, why=""):
        if module != self.focus:
            self.focus = module
            self._refresh_from_files(module)
            if module == "drums" and voice is None:
                voice = self.voice or (DRUM_LEVEL_ROW[0] if self._layout else None)
            if module == "bbkit" and voice is None:
                voice = self.bb_note if self.bb_note is not None else BB_LEVEL_ROW[0]
        if voice is not None:
            if module == "bbkit":
                self.bb_note = int(voice)
            else:
                self.voice = voice
        self._pickup.clear()
        sub = ""
        if module == "drums" and self.voice:
            sub = " / " + self.voice
        elif module == "bbkit" and self.bb_note is not None:
            sub = " / %s (%d)" % (self._bb_label(self.bb_note, self.bb_kit()), self.bb_note)
        self._log("  [knobs] focus -> %s%s%s" % (module, sub, " (" + why + ")" if why else ""))

    def observe(self, msg):
        """Every message the brain sends to the synth bus (cheap; called per message)."""
        t = msg.type
        if t == "control_change":
            module = self._module_at(msg.channel + 1)
            if module is not None:
                with self._lock:
                    if module == "bbkit":
                        self._observe_bb_cc(msg.control, msg.value)
                    else:
                        self._vals[self._key(module, msg.control)] = (msg.value, self._now())
            return
        if t == "program_change":
            if self._module_at(msg.channel + 1) == "bbkit":
                kits = self._bb_kits()
                if kits:
                    with self._lock:
                        self._bb_kit = kits[msg.program % len(kits)]
                        if self.focus == "bbkit":
                            self._refresh_from_files("bbkit")
            return
        if t != "note_on" or msg.velocity == 0:
            return
        module = self._module_at(msg.channel + 1)
        if module is None:
            return
        with self._lock:
            now = self._now()
            c = self.conf
            quiet = c["quiet_s"]
            holding = now - self._last_turn < c["hold_s"]
            prev = self._last_note.get(module)
            self._last_note[module] = now
            voice = None
            vprev = None
            if module == "drums":
                voice = self._drum_voice_of(msg.note)
                if voice not in self._layout:
                    voice = None
                if voice is not None:
                    vprev = self._last_note.get(("drum", voice))
                    self._last_note[("drum", voice)] = now
            elif module == "bbkit":
                names = self.bb_names()
                voice = msg.note if (not names or msg.note in names) else None
                if voice is not None:
                    vprev = self._last_note.get(("bb", voice))
                    self._last_note[("bb", voice)] = now
            if c["mode"] != "follow" or holding or not c["enabled"]:
                return
            if module in c["ignore"] or not self.has_page(module):
                return
            if module != self.focus:
                if self.focus is None or prev is None or now - prev >= quiet:
                    self._set_focus(module, voice if (module != "drums" or c["drum_voice"] is None)
                                    else c["drum_voice"], "played")
                return
            if module == "drums" and voice and voice != self.voice \
                    and c["drum_voice"] is None and (vprev is None or now - vprev >= quiet):
                self._set_focus("drums", voice, "played")
            if module == "bbkit" and voice is not None and voice != self.bb_note \
                    and (vprev is None or now - vprev >= quiet):
                self._set_focus("bbkit", voice, "played")

    def _observe_bb_cc(self, cc, value):
        """A BBKit shaping CC on the bus (ours or anyone's): track the picked drum
        so the value lands under the right key."""
        name = BB_CC_NAME.get(cc)
        if name is None:
            return
        kit = self.bb_kit()
        if name == "note":
            self._bb_target = value
        elif name in BB_KIT_KEYS:
            self._vals[("bbkit", kit, "kit", cc)] = (value, self._now())
        elif self._bb_target is not None:
            self._vals[("bbkit", kit, self._bb_target, cc)] = (value, self._now())

    # ---- knobs ------------------------------------------------------------------
    def handle(self, msg, source_name=""):
        """True if this message is one of our knobs (consumed)."""
        c = self.conf
        if not c["enabled"]:
            return False
        if c["mcu_ports"] and any(p and p in source_name for p in c["mcu_ports"]):
            return self._handle_mcu(msg)
        if msg.type != "control_change" or msg.channel + 1 != c["channel"]:
            return False
        if not c["device"] or c["device"] not in source_name:
            return False
        try:
            slot = c["ccs"].index(msg.control)
        except ValueError:
            return False
        if c["encoding"] == "abs":
            self.set_slot_abs(slot, msg.value)
        else:
            self.turn(slot, decode_relative(msg.value, c["encoding"]))
        return True

    def _handle_mcu(self, msg):
        ev = _mcu.decode(msg)
        if ev is None:
            return False
        if ev[0] == "vpot":
            self.turn(ev[1], ev[2])
            return True
        if ev[0] == "fader" and ev[1] < 8:
            self.set_slot_abs(8 + ev[1], ev[2] >> 7, pickup=False)
            return True
        return False

    def _target(self, slot):
        slots = self.page()
        if not (0 <= slot < len(slots)) or slots[slot] is None:
            return None
        key, cc, _name, pre = slots[slot]
        ch = self._module_ch(self.focus)      # the page always belongs to the focus
        if ch is None:
            return None
        return key, cc, ch, pre

    def turn(self, slot, ticks):
        """A relative move of knob `slot` (0-15) by `ticks`."""
        with self._lock:
            self._last_turn = self._now()
            t = self._target(slot)
            if t is None or ticks == 0:
                return None
            key, cc, ch, pre = t
            cur = self._vals.get(key, (self._default(key), 0))[0]
            val = max(0, min(127, cur + ticks * self.conf["step"]))
            self._vals[key] = (val, self._now())
        if val != cur:
            for pcc, pv in pre:
                self._send(ch, pcc, pv)
            self._send(ch, cc, val)
        return val

    def set_slot_abs(self, slot, value, pickup=True):
        """An absolute knob / fader. With pickup it only takes over once it has
        reached the parameter's current value (so it never jumps)."""
        with self._lock:
            self._last_turn = self._now()
            t = self._target(slot)
            if t is None:
                return None
            key, cc, ch, pre = t
            cur = self._vals.get(key, (self._default(key), 0))[0]
            if pickup and not self._pickup.get(slot):
                if abs(value - cur) > 2:
                    return None
                self._pickup[slot] = True
            self._vals[key] = (value, self._now())
        for pcc, pv in pre:
            self._send(ch, pcc, pv)
        self._send(ch, cc, value)
        return value

    # ---- status / commands --------------------------------------------------------
    def names(self):
        return [(s[2] if s else "") for s in self.page()]

    def status(self):
        with self._lock:
            page = self.page()
            return {
                "ok": True, "focus": self.focus, "voice": self.voice,
                "bb_note": self.bb_note, "bb_kit": self.bb_kit() if self.has_page("bbkit") else None,
                "mode": self.conf["mode"], "enabled": self.conf["enabled"],
                "slots": [None if s is None else
                          {"cc": s[1], "name": s[2],
                           "value": self._vals.get(s[0], (self._default(s[0]) if s[0][0] == "bbkit"
                                                          else None, 0))[0]}
                          for s in page],
                "modules": sorted(m for m in set(DEFAULT_PAGES) | set(self.conf["pages"])
                                  | {"drums", "bbkit"} if self.has_page(m)),
                "drum_voices": [v for v in self._layout],
                "conf": dict(self.conf),
            }

    def command(self, cmd):
        """KNOBS_SET: {"focus": module, "voice": drum voice, "mode": follow|lock,
        "conf": {...partial config...}}. Saves the config when it changes."""
        with self._lock:
            changed = False
            if isinstance(cmd.get("conf"), dict):
                merged = dict(self.conf)
                merged.update(cmd["conf"])
                self.conf = self._clean(merged)
                changed = True
            if cmd.get("mode") in ("follow", "lock"):
                self.conf["mode"] = cmd["mode"]
                changed = True
            mod = cmd.get("focus")
            if mod:
                if not self.has_page(mod):
                    return {"ok": False, "error": "no knob page for %r" % mod}
                self._set_focus(mod, cmd.get("note") if mod == "bbkit" else cmd.get("voice"),
                                "picked")
                if self.conf["mode"] == "lock":
                    self.conf["locked"] = mod
                    changed = True
            elif cmd.get("voice") and self.focus == "drums":
                self._set_focus("drums", cmd["voice"], "picked")
            elif cmd.get("note") is not None and self.focus == "bbkit":
                self._set_focus("bbkit", cmd["note"], "picked")
            if self.conf["mode"] == "lock" and self.conf["locked"] and \
                    self.focus != self.conf["locked"]:
                self._set_focus(self.conf["locked"], why="locked")
            if changed:
                self.save()
        return self.status()

    def start(self):
        """At boot: lock mode restores its module."""
        if self.conf["mode"] == "lock" and self.conf["locked"] and \
                self.has_page(self.conf["locked"]):
            self._set_focus(self.conf["locked"], self.conf.get("drum_voice"), "locked")
