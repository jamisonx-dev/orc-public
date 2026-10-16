"""
orc1_routes.py — MIDI routing engine for ORC-1.

Handles EXTERNAL message routing on top of orc1.py's built-in channel
processing.  It does not replace the chord engine, control handler, or
synth bus routing — those stay in orc1.py.  This engine decides what goes
out the physical DIN/USB ports.

Design
------
orc1.py calls:
    outputs = router.process(msg, source_port_name)

Returns:
    None                       no rules matched → use default passthrough
    []                         rules matched   → message suppressed
    [(port_key, msg), ...]     rules matched   → send to these ports

CH14 is always forwarded by orc1.py regardless of what this engine returns
(the eavesdrop contract — ORC-1 may process CH14 messages but never blocks
them from reaching whatever else is listening on that channel).

Rules come from two sources:
    1. MIDI_REMAP in orc1_config.py  — static, always active
    2. ~/orc1_routes.json            — per-device profiles, auto-activated
                                       when the matching port appears

Rule format (dict):
    in_port     which input the message must come from        default: "any"
                  "any"        every input
                  "usb" / "din" the input KIND
                  "self"       only the device this profile belongs to (its
                               port_match) -- a rule under the MPC profile that
                               should hear ONLY the MPC
                  other text   a device-name substring ("BeatStep", "MPC") --
                               matched against the real input port name
                (2026-09-24: before this, orc1.py passed only the kind, so every
                USB device shared the one "usb" label and a rule could not single
                one out.)
    stage       "post" | "pre" (pre = claim before channel dispatch)  default: "post"
    in_ch       1-16, [1,3,5], or "any"              default: "any"
    in_type     "note" | "cc" | "pc" | "any"         default: "any"
    match       [min, max] for note/cc range          default: None (all)
    out_port    port key ("pisound", "mac", …)        default: "pisound"
    out_ch      1-16 or "same"                        default: "same"
    transform   None | "bank_align" | "transpose"     default: None
    semitones   int (used with "transpose")           default: 0
    action      "forward" | "consume"                 default: "forward"

Built-in transforms
    bank_align   renumber notes so match[0] maps to note 36 (SP404 banks)
    transpose    shift note by rule["semitones"]

Example — SP404 bank routing from a controller on CH1:
    {"in_ch": 1, "in_type": "note", "match": [36, 51], "out_port": "pisound", "out_ch": 1},
    {"in_ch": 1, "in_type": "note", "match": [52, 67], "out_port": "pisound", "out_ch": 2, "transform": "bank_align"},
    {"in_ch": 1, "in_type": "note", "match": [68, 83], "out_port": "pisound", "out_ch": 3, "transform": "bank_align"},
    {"in_ch": 1, "in_type": "note", "match": [84, 99], "out_port": "pisound", "out_ch": 4, "transform": "bank_align"},

Integration in orc1.py  (the only changes needed there)
---------------------------------------------------------
Near the top, after port_outs is built:

    from orc1_routes import RoutingEngine, CONSUMED_INTERNALLY
    router = RoutingEngine(cfg, port_outs)

In the hot-plug scanner, when ports change:

    for new_port in appeared:  router.device_connected(new_port)
    for old_port in gone:      router.device_disconnected(old_port)

In the message handler, after internal processing, replace the old
passthrough block with:

    ch = msg.channel + 1
    if ch == 14:                        # eavesdrop: always forward
        send_to("pisound", msg)
        return
    outputs = router.process(msg, source_port_name)
    if outputs is not None:
        for pk, om in outputs:
            send_to(pk, om)
    elif ch not in CONSUMED_INTERNALLY: # default passthrough
        send_to("pisound", msg)

CONSUMED_INTERNALLY should be built from your existing special channels
(NOTE_INPUT_CH, CH_CTRL, MELODY_CH, and the synth bus channels).
"""

import json
import os
import threading

try:
    import mido
except ImportError:
    mido = None


ROUTES_PATH = os.path.expanduser("~/orc1_routes.json")


# ══════════════════════════════════════════════════════════════════════════════
# Transforms
# ══════════════════════════════════════════════════════════════════════════════

def _bank_align(msg, rule):
    """Renumber notes so match[0] maps to note 36 — SP404 bank boundary."""
    if msg.type not in ('note_on', 'note_off') or not mido:
        return msg
    lo       = rule.get('match', [36, 127])[0]
    new_note = 36 + (msg.note - lo)
    if not (0 <= new_note <= 127):
        return None   # out of range — drop
    return msg.copy(note=new_note)


def _transpose(msg, rule):
    """Shift note pitch by rule['semitones']."""
    if msg.type not in ('note_on', 'note_off') or not mido:
        return msg
    new_note = msg.note + int(rule.get('semitones', 0))
    if not (0 <= new_note <= 127):
        return None
    return msg.copy(note=new_note)


_TRANSFORMS = {
    'bank_align': _bank_align,
    'transpose':  _transpose,
}


# ══════════════════════════════════════════════════════════════════════════════
# Rule matching & application
# ══════════════════════════════════════════════════════════════════════════════

# Keys that make a dict a routing rule. An entry with none of them is an annotation
# ({"_comment": ...}), and {"_disabled": {...}} is a parked example; both appear in
# the starter routes file. Without this check they fell through every filter below
# (all defaults = "any"), so each one forwarded EVERY message to pisound whenever
# its profile was active -- 6x DIN spam, echoing DIN input back out the DIN.
_RULE_KEYS = ('in_port', 'in_ch', 'in_type', 'match', 'out_port', 'out_ch',
              'transform', 'action', 'stage')

def is_rule(rule):
    """True for a real routing rule, False for a comment / parked placeholder."""
    return (isinstance(rule, dict) and '_disabled' not in rule
            and any(k in rule for k in _RULE_KEYS))


def _source_ok(in_port, source_kind, source_name, own_match):
    """Does the message's input satisfy the rule's in_port? (see module doc)"""
    ip = str(in_port).strip().lower()
    if ip in ('', 'any'):
        return True
    if ip in ('usb', 'din'):
        return ip in (source_kind or '').lower()
    name = (source_name or '').lower()
    if ip == 'self':
        # Static / always-on rules have no device of their own -> never 'self'.
        return bool(own_match) and own_match.lower() in name
    return ip in name


def _matches(rule, msg, source_port, source_name='', own_match=None):
    """Return True if rule applies to this message.

    source_port = input KIND ("usb"/"din"); source_name = the real input port
    name; own_match = port_match of the profile the rule belongs to (None for a
    static rule)."""
    if not is_rule(rule):
        return False
    # Disabled rules are skipped — the UI toggles this flag
    if not rule.get('enabled', True):
        return False
    if not hasattr(msg, 'channel'):
        return False

    # ── Port ─────────────────────────────────────────────────────────────────
    if not _source_ok(rule.get('in_port', 'any'), source_port, source_name, own_match):
        return False

    # ── Channel ──────────────────────────────────────────────────────────────
    in_ch = rule.get('in_ch', 'any')
    if in_ch != 'any':
        ch = msg.channel + 1
        if isinstance(in_ch, list):
            if ch not in in_ch:
                return False
        elif ch != int(in_ch):
            return False

    # ── Message type ─────────────────────────────────────────────────────────
    in_type = rule.get('in_type', 'any')
    if in_type != 'any':
        if   in_type == 'note' and msg.type not in ('note_on', 'note_off'):
            return False
        elif in_type == 'cc'   and msg.type != 'control_change':
            return False
        elif in_type == 'pc'   and msg.type != 'program_change':
            return False

    # ── Value range ───────────────────────────────────────────────────────────
    match = rule.get('match')
    if match is not None:
        lo, hi = int(match[0]), int(match[1])
        if msg.type in ('note_on', 'note_off'):
            if not (lo <= msg.note <= hi):
                return False
        elif msg.type == 'control_change':
            if not (lo <= msg.control <= hi):
                return False
        elif msg.type == 'program_change':
            if not (lo <= msg.program <= hi):
                return False

    return True


def _apply(rule, msg, port_outs):
    """
    Apply a matched rule.
    Returns [] to suppress, [(port_key, msg)] to forward, or None if the
    rule's output port is unavailable (treat as no-match, try next rule).
    """
    if rule.get('action') == 'consume':
        return []

    out_port = rule.get('out_port', 'pisound')
    out_ch   = rule.get('out_ch', 'same')

    # Check port exists
    if out_port not in port_outs:
        return None   # port unavailable

    # Apply transform
    out_msg  = msg
    tf_name  = rule.get('transform')
    if tf_name and tf_name in _TRANSFORMS:
        out_msg = _TRANSFORMS[tf_name](out_msg, rule)
        if out_msg is None:
            return []   # transform dropped the message

    # Apply channel remap
    if out_ch != 'same' and mido and hasattr(out_msg, 'channel'):
        try:
            out_msg = out_msg.copy(channel=int(out_ch) - 1)
        except Exception:
            pass

    return [(out_port, out_msg)]


# ══════════════════════════════════════════════════════════════════════════════
# RoutingEngine
# ══════════════════════════════════════════════════════════════════════════════

class RoutingEngine:
    """
    MIDI routing engine.  Thread-safe.

    Instantiate once at startup:
        router = RoutingEngine(cfg, port_outs)

    Call on every incoming external message:
        outputs = router.process(msg, source_port_name)

    Call when ports appear / disappear (e.g. from the hot-plug scanner):
        router.device_connected("SP-404mk2 MIDI 1")
        router.device_disconnected("SP-404mk2 MIDI 1")

    Web UI can call get_state() / upsert_device() / delete_device() / reload().
    """

    def __init__(self, cfg, port_outs):
        self._lock      = threading.RLock()
        self._port_outs = port_outs   # live reference to orc1.py's port dict
        self._static    = []
        self._devices   = {}          # {key: {description, port_match, rules, active}}
        self._path      = ROUTES_PATH

        # Load static rules from config
        raw = getattr(cfg, 'MIDI_REMAP', [])
        self._static = [r for r in raw if isinstance(r, dict)]
        if self._static:
            print(f"[router] {len(self._static)} static rule(s) loaded from config")

        # Load per-device profiles
        self._load_file()

    # ── Core routing ──────────────────────────────────────────────────────────

    def process(self, msg, source_port='', stage='post', source_name=''):
        """
        Route one message.

        Returns:
          None                — no rules matched; use default passthrough
          []                  — rules matched; suppress the message
          [(port_key, msg)]   — rules matched; send to these destinations

        stage: which pass to evaluate. Only rules whose 'stage' equals this are
        considered; a rule with no 'stage' key defaults to 'post', so existing
        rules are unchanged.
          'post' (default) — the classic tail-of-_route_message pass: runs only
                             on channels the engine did NOT already claim.
          'pre'            — rules that PREEMPT channel dispatch. A pre rule can
                             claim ANY channel (even a module/reserved one), so
                             nothing 'owns' a channel — routing is a choice, not
                             ownership (owner's principle). See _route_message.
        source_port is the input KIND ("usb"/"din"); source_name is the real
        input port name, used by in_port "self" / a device name.
        """
        if not hasattr(msg, 'channel'):
            return None   # sysex / clock — not our concern

        with self._lock:
            # (rule, owning profile's port_match) -- a static rule owns no device.
            rules = [(r, None) for r in self._static
                     if isinstance(r, dict) and r.get('stage', 'post') == stage]
            for dev in self._devices.values():
                if dev.get('active'):
                    own = None if dev.get('always') else dev.get('port_match')
                    rules.extend((r, own) for r in dev.get('rules', [])
                                 if isinstance(r, dict)
                                 and r.get('stage', 'post') == stage)

        outputs   = None
        any_match = False

        for rule, own in rules:
            if not _matches(rule, msg, source_port, source_name, own):
                continue
            result = _apply(rule, msg, self._port_outs)
            if result is None:
                continue   # port unavailable — rule didn't fire
            any_match = True
            if outputs is None:
                outputs = []
            outputs.extend(result)

        # any_match but outputs is [] means "consume" rules fired
        # any_match but outputs is None — shouldn't happen; treat as no-match
        return outputs if any_match else None

    # ── Device lifecycle ──────────────────────────────────────────────────────

    def device_connected(self, port_name):
        """Activate matching device profiles when a port appears."""
        activated = []
        with self._lock:
            for key, dev in self._devices.items():
                pm = dev.get('port_match', key)
                if pm.lower() in port_name.lower() and not dev.get('active'):
                    dev['active'] = True
                    activated.append(key)
        for k in activated:
            print(f"[router] profile active: {k}  (matched '{port_name}')")

    def device_disconnected(self, port_name):
        """Deactivate matching device profiles when a port disappears."""
        deactivated = []
        with self._lock:
            for key, dev in self._devices.items():
                if dev.get('always'):
                    continue          # always-on profiles never switch off
                pm = dev.get('port_match', key)
                if pm.lower() in port_name.lower() and dev.get('active'):
                    dev['active'] = False
                    deactivated.append(key)
        for k in deactivated:
            print(f"[router] profile inactive: {k}")

    def active_devices(self):
        """Return list of currently active device profile keys."""
        with self._lock:
            return [k for k, v in self._devices.items() if v.get('active')]

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load_file(self):
        """Load ~/orc1_routes.json.  Creates an annotated default if missing."""
        try:
            with open(self._path) as fh:
                data = json.load(fh)
            count = 0
            with self._lock:
                for key, dev in data.get('devices', {}).items():
                    # "always": a GLOBAL profile, active whatever is plugged in
                    # (2026-09-24). Others activate on device_connected().
                    always = bool(dev.get('always', False))
                    self._devices[key] = {
                        'description': dev.get('description', ''),
                        'port_match':  dev.get('port_match', key),
                        'rules':       dev.get('rules', []),
                        'always':      always,
                        'active':      always,
                    }
                    count += 1
            print(f"[router] {count} device profile(s) loaded from {self._path}")
        except FileNotFoundError:
            self._write_default()
        except Exception as e:
            print(f"[router] routes load error: {e}")

    def _write_default(self):
        """Write an annotated starter routes file."""
        default = {
            "version":  1,
            "_comment": (
                "ORC-1 MIDI routing profiles. "
                "Each device profile activates automatically when its port appears. "
                "Edit freely — live-reload via web UI or restart orc1.py."
            ),
            "devices": {
                "SP-404mk2": {
                    "description": "SP-404mk2 bank A-D pad routing (uncomment rules to use)",
                    "port_match":  "SP-404",
                    "rules": [
                        {"_comment": "Bank routing example — in_ch is whatever your controller sends on"},
                        {"_comment": "Uncomment and adjust to taste:"},
                        {"_disabled": {"in_ch": 1, "in_type": "note", "match": [36,  51], "out_port": "pisound", "out_ch": 1}},
                        {"_disabled": {"in_ch": 1, "in_type": "note", "match": [52,  67], "out_port": "pisound", "out_ch": 2, "transform": "bank_align"}},
                        {"_disabled": {"in_ch": 1, "in_type": "note", "match": [68,  83], "out_port": "pisound", "out_ch": 3, "transform": "bank_align"}},
                        {"_disabled": {"in_ch": 1, "in_type": "note", "match": [84,  99], "out_port": "pisound", "out_ch": 4, "transform": "bank_align"}}
                    ]
                },
                "MPC1000": {
                    "description": "MPC1000 JJOS multitimbral (add rules as needed)",
                    "port_match":  "MPC",
                    "rules": []
                }
            }
        }
        try:
            with open(self._path, 'w') as fh:
                json.dump(default, fh, indent=2)
            with self._lock:
                for key, dev in default['devices'].items():
                    self._devices[key] = {
                        'description': dev.get('description', ''),
                        'port_match':  dev.get('port_match', key),
                        'rules':       [r for r in dev.get('rules', [])
                                        if isinstance(r, dict) and '_disabled' not in r
                                        and not r.get('_comment')],
                        'active': False,
                    }
            print(f"[router] created starter routes file: {self._path}")
        except Exception as e:
            print(f"[router] could not write routes file: {e}")

    def reload(self):
        """Hot-reload routes file.  Active device state is preserved."""
        with self._lock:
            was_active = {k for k, v in self._devices.items() if v.get('active')}
            self._devices.clear()
        self._load_file()
        with self._lock:
            for k in was_active:
                if k in self._devices:
                    self._devices[k]['active'] = True
        print(f"[router] reloaded  ({len(was_active)} device(s) re-activated)")

    def save(self):
        """Write current device profiles to disk."""
        with self._lock:
            data = {
                "version": 1,
                "devices": {
                    k: dict({
                        "description": v.get('description', ''),
                        "port_match":  v.get('port_match', k),
                        "rules":       v.get('rules', []),
                    }, **({"always": True} if v.get('always') else {}))
                    for k, v in self._devices.items()
                }
            }
        try:
            with open(self._path, 'w') as fh:
                json.dump(data, fh, indent=2)
        except Exception as e:
            print(f"[router] save error: {e}")

    # ── Web UI API ─────────────────────────────────────────────────────────────

    def get_state(self):
        """Serialisable snapshot for the web UI."""
        with self._lock:
            return {
                'static_rules': self._static[:],
                'devices': {
                    k: {
                        'description': v.get('description', ''),
                        'port_match':  v.get('port_match', k),
                        'rules':       v.get('rules', []),
                        'always':      v.get('always', False),
                        'active':      v.get('active', False),
                    }
                    for k, v in self._devices.items()
                }
            }

    def set_static_rules(self, rules):
        """Replace static rules live (from web UI config editor)."""
        with self._lock:
            self._static = [r for r in rules if isinstance(r, dict)]

    def upsert_device(self, key, description='', port_match=None, rules=None):
        """Add or update a device profile (from web UI)."""
        with self._lock:
            existing = self._devices.get(key, {})
            self._devices[key] = {
                'description': description or existing.get('description', ''),
                'port_match':  port_match  or existing.get('port_match', key),
                'rules':       rules if rules is not None else existing.get('rules', []),
                'always':      existing.get('always', False),
                'active':      existing.get('active', False),
            }
        self.save()

    def delete_device(self, key):
        """Remove a device profile (from web UI)."""
        with self._lock:
            self._devices.pop(key, None)
        self.save()


# ══════════════════════════════════════════════════════════════════════════════
# Helper for orc1.py integration
# ══════════════════════════════════════════════════════════════════════════════

def build_consumed_set(cfg):
    """
    Build the set of channels that orc1.py consumes internally and should
    NOT be echoed back to external ports.

    Call this in orc1.py after loading config:
        from orc1_routes import build_consumed_set
        CONSUMED_INTERNALLY = build_consumed_set(cfg)

    Channels in this set are silenced by the default passthrough path.
    CH14 is never in this set — it is always forwarded regardless.
    """
    synth_cfg = getattr(cfg, 'SYNTH', {})
    consumed = {
        getattr(cfg, 'NOTE_INPUT_CH', 12),  # chord engine
        getattr(cfg, 'CH_CTRL',       13),  # control
        getattr(cfg, 'MELODY_CH',     15),  # melody snap
        # Synth bus channels — consumed, sent to internal synth
        int(synth_cfg.get('CH_DRUM',      6)),
        int(synth_cfg.get('CH_MONO',      7)),
        int(synth_cfg.get('CH_POLY',      8)),
        int(synth_cfg.get('CH_CHIP',      9)),
        int(synth_cfg.get('CH_CHIP_P2',  10)),
        int(synth_cfg.get('CH_CHIP_TRI', 11)),
        int(synth_cfg.get('CH_CHIP_NS',  12)),
        int(synth_cfg.get('CH_CHIP_PCM', 16)),
    }
    return consumed
