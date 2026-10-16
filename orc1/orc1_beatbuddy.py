"""
orc1_beatbuddy.py -- THE MIND (BeatBuddy foot-drummer state machine), ORC-native.

This is a FAITHFUL PORT of the BeatBuddy firmware song player, taken directly from
the open-source BeatBuddy Manager player source:

    openbbm-src/BBManagerLean/src/player/songPlayer.cpp  (the state machine)
    openbbm-src/BBManagerLean/src/player/songPlayer.h    (states / part-id defines)
    openbbm-src/BBManagerLean/src/player/button.h         (pedal events)

owner's directive (2026-09-14): "I want the logic to be what the beatbuddy does.
just straight up copied." So the grammar here is NOT feel-tuned -- it mirrors the C
state machine one-for-one:

    STOPPED --(pedal press/release)--> INTRO --> PLAYING_MAIN_TRACK (loops)
    tap  (press+quick release)  -> DRUMFILL: play a fill on the bar, RETURN to part
    hold (press + long-press)   -> TRANSITION fill loops; RELEASE -> advance to next part
    double/multi-tap            -> OUTRO (if present) else stop, on the bar
    pause / unpause             -> PAUSE_REQUEST toggle

AUTOPILOT is intentionally DORMANT. All 244 factory songs carry NO autopilot data
(autoPilotDataOffset == 0 in every one), so in the real firmware ``APPtr`` is always
NULL for this content and every autopilot branch is dead. We keep ``APPtr = None`` and
the AP branches are guarded exactly as in C (never taken). Enabling AP later = model
the AUTOPILOT_AutoPilotDataStruct and port CheckAndCountBeat / fillAPIndex / the cue
branches. Marked ``# AP:`` at each site.

This module is PURE LOGIC + a .BBS loader: no jack, no mido, no rig. The brain drives
it (Milestone 2): one ORC MIDI-clock pulse -> process_song(ratio, TICKS_PER_CLOCK),
and playDrumsetNote -> a MIDI note on the BBKit host's channel. Output goes through an
injected ``emit`` callback so the whole thing is unit-testable offline.

Tick model: BeatBuddy tracks are 480 tpqn; the firmware ticks the player 24x per
quarter (a MIDI clock pulse) with nTick = 480/24 = 20 -- the literal ``20`` hardcoded
in IntroPart's pickup math. So TICKS_PER_CLOCK = 20 and one MIDI clock pulse == one
process_song(ratio, 20) call. That keeps our timing identical to the hardware.
"""

import struct
import json
import threading
from functools import wraps


# ---------------------------------------------------------------------------
# Enums (values match songPlayer.h so status/debug line up with the firmware)
# ---------------------------------------------------------------------------

# SongPlayer_PlayerStatus
NO_SONG_LOADED = 0
STOPPED = 1
PAUSED = 2
INTRO = 3
PLAYING_MAIN_TRACK = 4
PLAYING_MAIN_TRACK_TO_END = 5
NO_FILL_TRAN = 6
NO_FILL_TRAN_QUITTING = 7
NO_FILL_TRAN_CANCEL = 8
OUTRO = 9
OUTRO_WAITING_TRIG = 10
OUTRO_CANCELED = 11
TRANFILL_WAITING_TRIG = 12
TRANFILL_ACTIVE = 13
TRANFILL_QUITING = 14
TRANFILL_CANCEL = 15
DRUMFILL_WAITING_TRIG = 16
DRUMFILL_ACTIVE = 17
SINGLE_TRACK_PLAYER = 18

STATUS_NAME = {
    NO_SONG_LOADED: "NO_SONG_LOADED", STOPPED: "STOPPED", PAUSED: "PAUSED",
    INTRO: "INTRO", PLAYING_MAIN_TRACK: "PLAYING_MAIN_TRACK",
    PLAYING_MAIN_TRACK_TO_END: "PLAYING_MAIN_TRACK_TO_END",
    NO_FILL_TRAN: "NO_FILL_TRAN", NO_FILL_TRAN_QUITTING: "NO_FILL_TRAN_QUITTING",
    NO_FILL_TRAN_CANCEL: "NO_FILL_TRAN_CANCEL", OUTRO: "OUTRO",
    OUTRO_WAITING_TRIG: "OUTRO_WAITING_TRIG", OUTRO_CANCELED: "OUTRO_CANCELED",
    TRANFILL_WAITING_TRIG: "TRANFILL_WAITING_TRIG", TRANFILL_ACTIVE: "TRANFILL_ACTIVE",
    TRANFILL_QUITING: "TRANFILL_QUITING", TRANFILL_CANCEL: "TRANFILL_CANCEL",
    DRUMFILL_WAITING_TRIG: "DRUMFILL_WAITING_TRIG", DRUMFILL_ACTIVE: "DRUMFILL_ACTIVE",
    SINGLE_TRACK_PLAYER: "SINGLE_TRACK_PLAYER",
}

# SongFlags_t (request flags)
REQUEST_DONE = 0
START_REQUEST = 1
EXTERNAL_START_REQUEST = 2
STOP_REQUEST = 3
KILL_REQUEST = 4
DRUMFILL_REQUEST = 5
TRANFILL_REQUEST = 6
TRANFILL_QUIT_REQUEST = 7
TRANFILL_CANCEL_REQUEST = 8
OUTRO_CANCEL_REQUEST = 9
PAUSE_REQUEST = 10
SWAP_TO_OUTRO_REQUEST = 11

# Part IDs (passed to the sound engine; identify which section a note came from)
NO_PART_ID = 0
MAIN_PART_ID = 1
DRUM_FILL_ID = 2
TRAN_FILL_ID = 3
INTR_FILL_ID = 4
OUTR_FILL_ID = 5

# PLAYER_actions (unpause modes)
START_FILL = "START_FILL"
START_TRANSITION = "START_TRANSITION"

# Button events (from button.h; only the pedal + foot-switch ones matter here)
BE_PEDAL_PRESS = "PEDAL_PRESS"
BE_PEDAL_RELEASE = "PEDAL_RELEASE"
BE_PEDAL_LONG_PRESS = "PEDAL_LONG_PRESS"
BE_PEDAL_MULTI_TAP = "PEDAL_MULTI_TAP"
BE_FOOT_PRIMARY_PRESS = "FOOT_PRIMARY_PRESS"
BE_FOOT_SECONDARY_PRESS = "FOOT_SECONDARY_PRESS"

# Footswitch actions
ACTION_NONE = "ACTION_NONE"
ACTION_SPECIAL_EFFECT = "ACTION_SPECIAL_EFFECT"
ACTION_PAUSE_UNPAUSE = "ACTION_PAUSE_UNPAUSE"
ACTION_OUTRO = "ACTION_OUTRO"
ACTION_MAIN_PEDAL = "ACTION_MAIN_PEDAL"

POST_EVENT_MAX_TICK = 200

# 480 tpqn / 24 PPQN MIDI clock = 20 player ticks per clock pulse (the firmware's
# hardcoded IntroPart magic number). One clock pulse == process_song(ratio, 20).
TICKS_PER_CLOCK = 20

# multi-tap detect window (ms), from SongPlayer_ButtonCallback
MULTI_TAP_WINDOW_MS = 350


# ---------------------------------------------------------------------------
# Data model -- the .BBS structures the player walks
# ---------------------------------------------------------------------------

class Track:
    """MIDIPARSER_MidiTrack -- one drum pattern (main loop, fill, transition...)."""
    __slots__ = ("event", "nTick", "barLength", "timeSigNum", "timeSigDen",
                 "trigPos", "pickupNotesLength", "tpqn", "index")

    def __init__(self):
        self.event = []            # list of (tick, note, vel); vel 0 == note-off
        self.nTick = 0
        self.barLength = 480
        self.timeSigNum = 4
        self.timeSigDen = 4
        self.trigPos = 0
        self.pickupNotesLength = 0
        self.tpqn = 480
        self.index = 0             # mutable playback cursor into event[]


class SongPart:
    """SONG_SongPartStruct."""
    __slots__ = ("mainLoopIndex", "transFillIndex", "drumFillIndex", "nDrumFill",
                 "shuffleFlag", "loopCount", "effectName")

    def __init__(self):
        self.mainLoopIndex = -1
        self.transFillIndex = -1
        self.drumFillIndex = [-1] * 8
        self.nDrumFill = 0
        self.shuffleFlag = 0
        self.loopCount = 0
        self.effectName = ""


class Song:
    """SONG_SongStruct."""
    __slots__ = ("bpm", "loopSong", "nPart", "drumsetName", "intro", "outro",
                 "part", "tracks", "name", "_has_autopilot")

    def __init__(self):
        self.bpm = 120
        self.loopSong = 0
        self.nPart = 0
        self.drumsetName = ""
        self.intro = SongPart()
        self.outro = SongPart()
        self.part = []             # list[SongPart]
        self.tracks = []           # list[Track], indexed by *Index fields
        self.name = ""


# ---------------------------------------------------------------------------
# .BBS loader -- ports SongPlayer_loadSong (+ read()/adjust_*)
# ---------------------------------------------------------------------------

_PART_FMT = "<IiHHIIIii8i16sIIII"          # 96 bytes  (see bbs_parse.py / spec §5)
_PART_SZ = struct.calcsize(_PART_FMT)
_TRK_HDR_FMT = "<IIiiiBBBBIiiIIII"         # 52 bytes
_TRK_HDR_SZ = struct.calcsize(_TRK_HDR_FMT)


def _cstr(b):
    return b.split(b"\x00", 1)[0].decode("latin1", "replace")


def _parse_part(d, o):
    (nDrumFill, _bpmDelta, _repeatFlag, shuffleFlag, _tsNum, _tsDen, _tickPerBar,
     mainLoopIndex, transFillIndex, *rest) = struct.unpack_from(_PART_FMT, d, o)
    drumFills = list(rest[0:8])
    effectName = rest[8]
    _effVol, loopCount, _r2, _r3 = rest[9:13]
    p = SongPart()
    p.nDrumFill = nDrumFill
    p.shuffleFlag = shuffleFlag
    p.mainLoopIndex = mainLoopIndex
    p.transFillIndex = transFillIndex
    p.drumFillIndex = drumFills
    p.loopCount = loopCount
    p.effectName = _cstr(effectName)
    return p


def _parse_track(d, base, item_off):
    o = base + item_off
    (_fmt, _nTrack, nTick, pickup, _unused, tsN, tsD, _n32, _mclk, tpqn,
     barLen, trigPos, _bpm, index, _r1, _r2) = struct.unpack_from(_TRK_HDR_FMT, d, o)
    ne = struct.unpack_from("<I", d, o + _TRK_HDR_SZ)[0]
    evbase = o + _TRK_HDR_SZ + 4
    t = Track()
    t.nTick = nTick
    t.pickupNotesLength = pickup
    t.timeSigNum = tsN or 4
    t.timeSigDen = tsD or 4
    t.tpqn = tpqn or 480
    t.barLength = barLen
    t.trigPos = trigPos
    t.index = index
    ev = t.event
    for i in range(ne):
        tick, _typ, note, vel, _pad = struct.unpack_from("<iBBBB", d, evbase + i * 8)
        ev.append((tick, note, vel))
    return t


def _adjust_length(t):
    """Pad a main-loop track up to a whole number of bars (ports adjust_length)."""
    if t is None:
        return
    per_beat = 4 * 480 // t.timeSigDen
    if per_beat <= 0:
        return
    if (t.nTick // per_beat) % t.timeSigNum:
        n_missing = t.timeSigNum - ((t.nTick // per_beat) % t.timeSigNum)
        t.nTick += n_missing * per_beat


def _adjust_trig_length(t):
    """Set the trigger position to 70% of a bar (ports adjust_trig_length)."""
    if t is None:
        return
    t.trigPos = int(0.70 * float(t.barLength))


def load_bbs(path):
    """Read a factory .BBS file into a Song (ports SongPlayer_loadSong)."""
    with open(path, "rb") as fh:
        d = fh.read()
    return parse_bbs_bytes(d, name=path)


def parse_bbs_bytes(d, name=""):
    if d[:4] != b"BBSF":
        raise ValueError("not a BBSF song file")

    ot = struct.unpack_from("<20I", d, 32)
    (_thisOff, _metaOff, songOff, tracksDataOff, _tracksMetaOff, tracksIdxOff,
     autoOff, _r2, _r3, _r4, _thisSz, _metaSz, _songSz, tracksIdxSz,
     _tracksMetaSz, _tracksDataSz, _autoSz, _r6, _r7, _r8) = ot

    loopSong, bpm, nPart, _res = struct.unpack_from("<IIII", d, songOff)
    drmName = _cstr(struct.unpack_from("<16s", d, songOff + 16)[0])

    song = Song()
    song.name = name
    song.bpm = bpm
    song.loopSong = loopSong
    song.nPart = nPart
    song.drumsetName = drmName
    song.intro = _parse_part(d, songOff + 32)
    song.outro = _parse_part(d, songOff + 32 + _PART_SZ)
    song.part = [_parse_part(d, songOff + 32 + 2 * _PART_SZ + i * _PART_SZ)
                 for i in range(nPart)]

    # track index table: nIdx entries of 32 bytes, first u32 = dataOffset
    n_idx = tracksIdxSz // 32
    idx = [struct.unpack_from("<8I", d, tracksIdxOff + i * 32)[0] for i in range(n_idx)]
    song.tracks = [_parse_track(d, tracksDataOff, idx[t]) for t in range(n_idx)]

    # --- ports the adjust_* pass in SongPlayer_loadSong ---
    def mt(i):
        return song.tracks[i] if 0 <= i < len(song.tracks) else None

    _adjust_trig_length(mt(song.intro.mainLoopIndex))
    for p in song.part:
        _adjust_trig_length(mt(p.mainLoopIndex))
        _adjust_length(mt(p.mainLoopIndex))
        for j in range(p.nDrumFill):
            _adjust_trig_length(mt(p.drumFillIndex[j]))
        _adjust_trig_length(mt(p.transFillIndex))
    _adjust_trig_length(mt(song.outro.mainLoopIndex))

    # AP: autoOff is 0 for all factory content, so no AutopilotDataStruct is read.
    song._has_autopilot = (autoOff != 0)  # informational only
    return song


# ---------------------------------------------------------------------------
# Song-IR loader -- builds the SAME Song/SongPart/Track objects as the .BBS
# loader from a clean JSON IR (see tools/slate2song.py). This is how non-factory
# content (Slate grooves, custom songs) becomes playable: the mind never knows
# whether a song came from a .BBS or an IR.  IR shape (orc-song-ir/1):
#   {name,bpm,ts,loopSong, intro:{main}, outro:{main},
#    parts:[{name,main,trans,fills:[...]}], tracks:[{barLength,ts,tpqn,nTick,events}]}
# where every *ref* is an index into tracks[] (null == none), events are
# [tick,note,vel] (vel 0 == note-off), notes already in the kit's GM space.
# ---------------------------------------------------------------------------

def _refi(x):
    return int(x) if x is not None else -1


def build_song_from_ir(ir, name=""):
    song = Song()
    song.name = name or ir.get("name", "")
    song.bpm = int(ir.get("bpm", 120))
    song.loopSong = int(ir.get("loopSong", 0))
    song.drumsetName = ir.get("kit", "") or ""

    for tr in ir.get("tracks", []):
        t = Track()
        t.event = [(int(a), int(b), int(c)) for a, b, c in tr.get("events", [])]
        t.barLength = int(tr.get("barLength", 480))
        tsn = tr.get("ts", [4, 4])
        t.timeSigNum = int(tsn[0]) or 4
        t.timeSigDen = int(tsn[1]) or 4
        t.tpqn = int(tr.get("tpqn", 480))
        t.nTick = int(tr.get("nTick", max((e[0] for e in t.event), default=0)))
        song.tracks.append(t)

    def part_from(pd):
        p = SongPart()
        p.mainLoopIndex = _refi(pd.get("main"))
        p.transFillIndex = _refi(pd.get("trans"))
        fills = [i for i in (_refi(x) for x in pd.get("fills", [])) if i >= 0][:8]
        p.nDrumFill = len(fills)
        p.drumFillIndex = (fills + [-1] * 8)[:8]
        p.effectName = pd.get("name", "")
        return p

    song.intro = part_from(ir.get("intro", {}))
    song.outro = part_from(ir.get("outro", {}))
    song.part = [part_from(pd) for pd in ir.get("parts", [])]
    song.nPart = len(song.part)

    def mt(i):
        return song.tracks[i] if 0 <= i < len(song.tracks) else None

    _adjust_trig_length(mt(song.intro.mainLoopIndex))
    for p in song.part:
        _adjust_trig_length(mt(p.mainLoopIndex))
        _adjust_length(mt(p.mainLoopIndex))
        for j in range(p.nDrumFill):
            _adjust_trig_length(mt(p.drumFillIndex[j]))
        _adjust_trig_length(mt(p.transFillIndex))
    _adjust_trig_length(mt(song.outro.mainLoopIndex))
    song._has_autopilot = False
    return song


def load_ir(path):
    """Read an orc-song-ir/1 JSON file into a Song."""
    with open(path, encoding="utf-8") as fh:
        ir = json.load(fh)
    return build_song_from_ir(ir, name=path)


# ---------------------------------------------------------------------------
# The state machine -- ports songPlayer.cpp
# ---------------------------------------------------------------------------

def _serialized(method):
    """Serialize a mind entry point on the instance lock.

    The BeatBuddy firmware runs its button callback and per-tick processSong in
    ONE loop -- they never overlap. In the ORC the mind is driven by up to three
    threads (the arp clock tick, the web/cmd socket, and the foot-pedal input), so
    a lock is needed to restore that single-threaded serialization; without it a
    STOP / song-load racing a tick can null CurrPart mid-processSong and crash the
    tick (silently -- the arp swallows subscriber exceptions). RLock: the
    convenience verbs call button_callback and the clock tick's poll() re-enters
    via button_callback, all on one thread. Read-only getters are NOT wrapped."""
    @wraps(method)
    def _wrap(self, *a, **k):
        with self._lock:
            return method(self, *a, **k)
    return _wrap


class BeatBuddyMind:
    """Faithful port of the BeatBuddy firmware song player.

    Wire it up by passing an ``emit`` callable; it is called for every note the
    firmware would send to its sound engine:

        emit(note, vel, delay, ratio, part_id, playing_pickup)

    ``vel == 0`` is a note-off (BeatBuddy drum voices are one-shots + choke groups,
    so the ORC kit host ignores these -- but they are emitted faithfully).
    """

    def __init__(self, emit=None):
        self._lock = threading.RLock()  # serializes the 3 driving threads
        self._recover_count = 0          # times the invariant net stopped a strand
        self.emit = emit if emit is not None else (lambda *a, **k: None)
        # ports SongPlayer_init
        self.PlayerStatus = NO_SONG_LOADED
        self.LastPlayerStatus = NO_SONG_LOADED
        self.UnPausedPlayerStatus = NO_SONG_LOADED
        self.PausedPlayerStatus = NO_SONG_LOADED
        self.RequestFlag = REQUEST_DONE

        self.PedalPressFlag = 0
        self.PedalPresswDrumFillFlag = 0
        self.TransPedalPressFlag = False
        self.WasPausedFlag = 0
        self.MultiTapCounter = 0
        self.WasLongPressed = 0
        self.DelayStartCmd = 0
        self.LastMultiTapTime = 0

        self.PrimaryFootSwitchPlayingAction = ACTION_SPECIAL_EFFECT
        self.PrimaryFootSwitchStoppedAction = ACTION_SPECIAL_EFFECT
        self.SecondaryFootSwitchPlayingAction = ACTION_SPECIAL_EFFECT
        self.SecondaryFootSwitchStoppedAction = ACTION_SPECIAL_EFFECT

        self.MainUnpauseModeTapToFill = START_FILL
        self.MainUnpauseModeHoldToTransition = START_TRANSITION
        self.ActivePauseEnable = 1        # DEFAULT_ACTIVE_PAUSE
        self.TrippleTapEnable = 1         # DEFAULT_TRIPLE_TAP_STOP
        self.StartBeatOnPress = 0
        self.SendStopOnPause = 0
        self.SendStopOnEnd = 0

        self.CurrSong = None
        self.CurrPart = None
        self.APPtr = None                 # always None for factory content (see header)

        self.MasterTick = 0
        self.TmpMasterPartTick = 0
        self.PartIndex = 0
        self.DrumFillIndex = 0
        self.BeatCounter = 0
        self.NextPartNumber = 0

        # AP-flavoured trims (inert without AP data, kept for structural parity)
        self.currentLoopTick = -1
        self.newEnd = 0
        self.addedTick = 0
        self.playingPickUp = False
        self.AutopilotAction = False
        self.AutopilotCueFill = False

        # sync ticks
        self.PartStopSyncTick = 0
        self.PartStopPickUpSyncTickLength = 0
        self.DrumFillStartSyncTick = 0
        self.DrumFillPickUpSyncTickLength = 0
        self.TranFillStartSyncTick = 0
        self.TranFillStopSyncTick = 0
        self.TranFillPickUpSyncTickLength = 0

    # ---- MAIN_LOOP_PTR / TRANS_FILL_PTR / DRUM_FILL_PTR / *_TRACK_PTR macros ----
    def _trk(self, i):
        if self.CurrSong is None or i < 0 or i >= len(self.CurrSong.tracks):
            return None
        return self.CurrSong.tracks[i]

    def _main_loop_ptr(self, part):
        return self._trk(part.mainLoopIndex) if part else None

    def _trans_fill_ptr(self, part):
        return self._trk(part.transFillIndex) if part else None

    def _drum_fill_ptr(self, part, index):
        if not part or index < 0 or index >= len(part.drumFillIndex):
            return None
        return self._trk(part.drumFillIndex[index])

    def _intro_track_ptr(self, song):
        return self._trk(song.intro.mainLoopIndex) if song else None

    def _outro_track_ptr(self, song):
        return self._trk(song.outro.mainLoopIndex) if song else None

    # ---- firmware hooks that are no-ops in this port ----
    def _special_effect_manager(self):
        pass

    def _uart_send_stop(self):
        pass

    def _uart_send_start(self):
        pass

    def _uart_send_next_part(self, part_num):
        pass

    def _gui_force_refresh(self):
        pass

    def _tempo_start_with_int(self):
        pass

    def _play_special_effect(self, *a):
        pass

    # AP helpers -- unreachable while APPtr is None (see header)
    def _fill_ap_index(self):
        pass

    def _next_ap_index(self):
        return 0

    # ---------------------------------------------------------------- lifecycle
    @_serialized
    def load_song(self, song):
        """Load a Song object (from load_bbs). Ports the tail of SongPlayer_loadSong."""
        self.CurrSong = song
        self._reset_song_position()
        return 1

    def load_bbs(self, path):
        return self.load_song(load_bbs(path))

    def load_ir(self, path):
        return self.load_song(load_ir(path))

    def _reset_song_position(self):
        self._fill_ap_index()
        self.DrumFillIndex = self._next_ap_index() if self.APPtr else 0
        self.PartIndex = 0
        self.MasterTick = 0
        self.CurrPart = None
        self.PlayerStatus = STOPPED if self.CurrSong is not None else NO_SONG_LOADED
        # A freshly-loaded song must NOT inherit a pending request or a
        # half-finished pedal gesture from the previous song. The firmware only
        # loads from a quiescent UI; the ORC can load mid-play over the cmd
        # socket, so a stale DRUMFILL_REQUEST/TRANFILL_REQUEST left set here would
        # be applied by the next tick against CurrPart=None and crash. Clear it,
        # exactly as _stop_song does on a stop.
        self.RequestFlag = REQUEST_DONE
        self.NextPartNumber = 0
        self.PedalPressFlag = 0
        self.PedalPresswDrumFillFlag = 0
        self.TransPedalPressFlag = False
        self.WasLongPressed = 0
        self.MultiTapCounter = 0

    def _reset_beat_counter(self):
        self.BeatCounter = 0

    def get_player_status(self):
        return (self.PlayerStatus, self.PartIndex, self.DrumFillIndex)

    def status_name(self):
        return STATUS_NAME.get(self.PlayerStatus, "?")

    def is_playing(self):
        return self.PlayerStatus not in (NO_SONG_LOADED, STOPPED)

    @_serialized
    def force_stop(self):
        self._stop_song()

    # ---------------------------------------------------------- external entry
    @_serialized
    def external_start(self):
        if self.PlayerStatus == STOPPED and self.CurrSong is not None:
            self.RequestFlag = EXTERNAL_START_REQUEST
            self._tempo_start_with_int()

    @_serialized
    def external_stop(self):
        self._stop_song()

    @_serialized
    def external_pause(self):
        self._pause_unpause_handler()

    @_serialized
    def external_drumfill(self):
        if self.PlayerStatus == PLAYING_MAIN_TRACK and self.RequestFlag == REQUEST_DONE:
            self.RequestFlag = DRUMFILL_REQUEST

    @_serialized
    def external_outro(self):
        if self.RequestFlag != REQUEST_DONE:
            return
        st = self.PlayerStatus
        if st == INTRO or (self.WasPausedFlag and st == PLAYING_MAIN_TRACK):
            self.RequestFlag = SWAP_TO_OUTRO_REQUEST
        elif st in (PLAYING_MAIN_TRACK, NO_FILL_TRAN, NO_FILL_TRAN_QUITTING,
                    TRANFILL_WAITING_TRIG, TRANFILL_ACTIVE, TRANFILL_QUITING,
                    DRUMFILL_WAITING_TRIG, DRUMFILL_ACTIVE):
            self.RequestFlag = STOP_REQUEST

    @_serialized
    def external_transition(self, part_number):
        """part_number: 0 = quit transition; N = go to part N; >nPart = next part."""
        if self.RequestFlag != REQUEST_DONE:
            return
        st = self.PlayerStatus
        if part_number == 0 and st in (NO_FILL_TRAN, TRANFILL_WAITING_TRIG, TRANFILL_ACTIVE):
            self.RequestFlag = TRANFILL_QUIT_REQUEST
        elif part_number > 0:
            if st in (PAUSED, DRUMFILL_WAITING_TRIG, DRUMFILL_ACTIVE, PLAYING_MAIN_TRACK):
                self.NextPartNumber = part_number
                self.RequestFlag = TRANFILL_REQUEST
            elif st == STOPPED:
                self.NextPartNumber = part_number
                self.external_start()

    # -------------------------------------------------- getters (bar/beat/tempo)
    def get_bar_length(self):
        s, p = self.CurrSong, self.CurrPart
        if s is not None:
            if p is not None and self._main_loop_ptr(p):
                return self._main_loop_ptr(p).barLength
            if self._intro_track_ptr(s):
                return self._intro_track_ptr(s).barLength
            if s.part and self._main_loop_ptr(s.part[0]):
                return self._main_loop_ptr(s.part[0]).barLength
        return 480

    def get_time_signature(self):
        s, p = self.CurrSong, self.CurrPart
        if s is not None:
            t = None
            if p is not None:
                t = self._main_loop_ptr(p)
            elif self._intro_track_ptr(s):
                t = self._intro_track_ptr(s)
            elif s.part:
                t = self._main_loop_ptr(s.part[0])
            if t:
                return (t.timeSigNum, t.timeSigDen)
        return (4, 4)

    def get_tempo(self):
        if self.CurrSong is not None and self.CurrPart is not None:
            return self.CurrSong.bpm
        return 0

    def get_beat_in_bar(self):
        """Ports SongPlayer_getBeatInbar (returns -2 when not applicable)."""
        p = self.CurrPart
        if p is not None:
            ml = self._main_loop_ptr(p)
            st = self.PlayerStatus
            if st in (NO_SONG_LOADED, STOPPED):
                return -2
            if st == INTRO and ml:
                offset = ml.barLength - (ml.nTick % ml.barLength)
                return ((self.MasterTick + offset) //
                        (ml.barLength // ml.timeSigNum)) % ml.timeSigNum
            if st == PAUSED and ml:
                if not self.ActivePauseEnable:
                    return -2
                return (self.MasterTick // (ml.barLength // ml.timeSigNum)) % ml.timeSigNum
            if st in (PLAYING_MAIN_TRACK_TO_END, PLAYING_MAIN_TRACK, DRUMFILL_WAITING_TRIG,
                      TRANFILL_WAITING_TRIG, NO_FILL_TRAN, NO_FILL_TRAN_QUITTING,
                      NO_FILL_TRAN_CANCEL, OUTRO, OUTRO_CANCELED, OUTRO_WAITING_TRIG) and ml:
                return (self.MasterTick // (ml.barLength // ml.timeSigNum)) % ml.timeSigNum
            if st in (TRANFILL_ACTIVE, TRANFILL_QUITING, TRANFILL_CANCEL):
                tf = self._trans_fill_ptr(p)
                if tf:
                    return (self.MasterTick // (tf.barLength // tf.timeSigNum)) % tf.timeSigNum
            if st == DRUMFILL_ACTIVE:
                df = self._drum_fill_ptr(p, self.DrumFillIndex)
                if df:
                    return (self.MasterTick // (df.barLength // df.timeSigNum)) % df.timeSigNum
            return -2
        return -2

    # ---------------------------------------------------------- track playback
    def _track_play(self, track, start_tick, end_tick, ratio, manual_offset, part_id):
        """Ports TrackPlay -- emit every event in [start_tick, end_tick)."""
        if track is None:
            return
        self.playingPickUp = start_tick < 0

        if track.index >= len(track.event):
            track.index = len(track.event) - 1
        if not track.event or start_tick > track.event[-1][0]:
            return
        if start_tick < track.event[track.index][0]:
            track.index = 0
        # advance to the right position
        while track.event[track.index][0] < start_tick:
            track.index += 1
            if track.index >= len(track.event):
                return
        # play everything before end_tick
        while track.event[track.index][0] < end_tick:
            tick, note, vel = track.event[track.index]
            delay = ratio * float(manual_offset + tick - start_tick)
            self.emit(note, vel, delay, ratio, part_id, self.playingPickUp)
            track.index += 1
            if track.index >= len(track.event):
                return
        # pick-up-note beat-counter adjust (AP-flavoured; inert without pickups)
        if (self.DrumFillPickUpSyncTickLength > 0
                and track.event[track.index][0] >= 0
                and self.PlayerStatus != DRUMFILL_WAITING_TRIG):
            self.TmpMasterPartTick -= self.DrumFillPickUpSyncTickLength
            self.DrumFillPickUpSyncTickLength = 0
            if self.playingPickUp:
                self.currentLoopTick = 0

    # ---------------------------------------------------------- sync-tick math
    @staticmethod
    def _calc_tranfill_quit_sync_tick(tick_pos, tick_per_bar, play_for=1):
        return (play_for + (tick_pos // tick_per_bar)) * tick_per_bar

    @staticmethod
    def _calc_start_bar_sync_tick(tick_pos, tick_per_bar, bar_trigger_limit):
        if (tick_pos % tick_per_bar) <= bar_trigger_limit:
            return (0 + (tick_pos // tick_per_bar)) * tick_per_bar
        return (1 + (tick_pos // tick_per_bar)) * tick_per_bar

    def _is_end_of_track(self, pos, part):
        return pos > self._main_loop_ptr(part).nTick

    # ---------------------------------------------------------- part movers
    def _next_part(self):
        self.TmpMasterPartTick = 0
        self.MasterTick = 0
        self.BeatCounter = 0
        self.WasPausedFlag = 0
        s = self.CurrSong
        if s.nPart > 0:
            if (self.NextPartNumber > 0 and self.NextPartNumber <= s.nPart
                    and self.PedalPresswDrumFillFlag != 1):
                self.PartIndex = self.NextPartNumber - 1
            elif self.PedalPresswDrumFillFlag != 1:
                self.PartIndex = (self.PartIndex + 1) % s.nPart
            self.NextPartNumber = 0
            self.CurrPart = s.part[self.PartIndex]
            self.PlayerStatus = PLAYING_MAIN_TRACK
            self.BeatCounter = 0
        else:
            self._stop_song()
            return
        if self.CurrPart.shuffleFlag:
            if self.CurrPart.nDrumFill != 0:
                self.DrumFillIndex = _rand() % self.CurrPart.nDrumFill
        else:
            self._fill_ap_index()
            self.DrumFillIndex = self._next_ap_index() if self.APPtr else 0
        self._uart_send_next_part(self.PartIndex + 1)

    def _outro_part(self):
        self.TmpMasterPartTick = 0
        self.MasterTick = 0
        self.BeatCounter = 0

    def _swap_to_outro(self):
        self.DrumFillIndex = 0
        self.PartStopSyncTick = 0
        if self._outro_track_ptr(self.CurrSong):
            self.CurrPart = self.CurrSong.outro
            self.MasterTick %= self._main_loop_ptr(self.CurrPart).barLength
            self.PlayerStatus = OUTRO
        else:
            self._stop_song()
        self._special_effect_manager()

    def _first_part(self):
        self.TmpMasterPartTick = 0
        self.MasterTick = 0
        self.WasPausedFlag = 0
        self.BeatCounter = 0
        s = self.CurrSong
        if s.nPart > 0:
            if self.NextPartNumber > 0 and self.NextPartNumber <= s.nPart:
                self.PartIndex = self.NextPartNumber - 1
            else:
                self.PartIndex = 0
            self.NextPartNumber = 0
            self.CurrPart = s.part[self.PartIndex]
        else:
            self._stop_song()
            return
        if self.CurrPart is not None and self.CurrPart.nDrumFill != 0:
            if self.CurrPart.shuffleFlag:
                self.DrumFillIndex = _rand() % self.CurrPart.nDrumFill
            else:
                self.DrumFillIndex = self._next_ap_index() if self.APPtr else 0
        self.PlayerStatus = PLAYING_MAIN_TRACK
        self.BeatCounter = 0

    def _intro_part(self):
        self.DrumFillIndex = 0
        self.MasterTick = 0
        self.WasPausedFlag = 0
        self.PartIndex = 0
        s = self.CurrSong
        if self._intro_track_ptr(s) is not None:
            self.PlayerStatus = INTRO
            self.CurrPart = s.intro
            ml = self._main_loop_ptr(self.CurrPart)
            if ml.pickupNotesLength:
                if ml.pickupNotesLength % 20:
                    self.MasterTick = 0 - ((1 + ml.pickupNotesLength // 20) * 20)
                else:
                    self.MasterTick = 0 - ((0 + ml.pickupNotesLength // 20) * 20)
            else:
                self.MasterTick = 0
        elif s.part:
            if self.NextPartNumber > 0 and self.NextPartNumber <= s.nPart:
                self.PartIndex = self.NextPartNumber - 1
            else:
                self.PartIndex = 0
            self.NextPartNumber = 0
            self.CurrPart = s.part[self.PartIndex]
            if self.CurrPart.shuffleFlag:
                if self.CurrPart.nDrumFill != 0:
                    self.DrumFillIndex = _rand() % self.CurrPart.nDrumFill
            else:
                self._fill_ap_index()
                self.DrumFillIndex = self._next_ap_index() if self.APPtr else 0
            self._special_effect_manager()
            self.PlayerStatus = PLAYING_MAIN_TRACK
        else:
            self._stop_song()          # no intro and no parts -> nothing to play
            return
        # Some factory .BBS are empty placeholders (nPart>=1 but every track index
        # is -1). A part with no resolvable main loop is not playable: stop cleanly
        # instead of entering a PLAYING state that would crash this same tick's
        # renderer on ml.nTick. The firmware assumes non-empty songs; the ORC lets
        # a user pick any file in the songs dir, so we guard it.
        ml = self._main_loop_ptr(self.CurrPart)
        if ml is None:
            self._stop_song()
            return
        ml.index = 0

    def _same_part(self, next_drumfill):
        self.TmpMasterPartTick = 0
        self.MasterTick = 0
        self.WasPausedFlag = 0
        if self.PedalPresswDrumFillFlag == 1:
            self.MasterTick = 0
            self._reset_beat_counter()
            self.PedalPresswDrumFillFlag = 0
            self.PlayerStatus = PLAYING_MAIN_TRACK
        else:
            if next_drumfill and self.CurrPart is not None and self.CurrPart.nDrumFill != 0:
                if self.CurrPart.shuffleFlag:
                    self.DrumFillIndex = _rand() % self.CurrPart.nDrumFill
                else:
                    self.DrumFillIndex = (self._next_ap_index() if self.APPtr
                                          else (self.DrumFillIndex + 1) % self.CurrPart.nDrumFill)
            self.CurrPart = self.CurrSong.part[self.PartIndex]
            self.PlayerStatus = PLAYING_MAIN_TRACK

    def _stop_song(self):
        # The resume branch ("cancel the pending drumfill, keep playing") is only
        # valid with a live part. The firmware guarantees that because its button
        # events and ticks interleave in one loop; the ORC fires discrete gestures
        # from the web/cmd socket with no tick guaranteed between them, so the
        # flag can outlive the part -- guard on CurrPart so a stale flag can't
        # resume "playing" onto a null part (which crashed the next tick).
        if (self.PedalPresswDrumFillFlag == 1 and self.MultiTapCounter == 0
                and self.CurrPart is not None):
            self.MasterTick = 0
            self._reset_beat_counter()
            self.PedalPresswDrumFillFlag = 0
            self.PlayerStatus = PLAYING_MAIN_TRACK
        else:
            self.DrumFillIndex = 0
            self.PartIndex = 0
            self.MasterTick = 0
            self.BeatCounter = 0
            self.NextPartNumber = 0
            self.RequestFlag = REQUEST_DONE
            self.PedalPresswDrumFillFlag = 0   # a full stop clears the pending flag
            self.MultiTapCounter = 0           # (the firmware leaves these; the ORC
            self.TransPedalPressFlag = False   #  can strand them across gestures)
            self.PlayerStatus = STOPPED if self.CurrSong is not None else NO_SONG_LOADED
            self.CurrPart = None
            self._special_effect_manager()
            if self.SendStopOnEnd:
                self._uart_send_stop()
            self._gui_force_refresh()

    def _pause_unpause_handler(self):
        if self.PlayerStatus not in (STOPPED, NO_SONG_LOADED):
            self.RequestFlag = PAUSE_REQUEST

    # =====================================================================
    # process_song -- the per-tick engine (ports SongPlayer_processSong)
    # =====================================================================
    @_serialized
    def process_song(self, ratio=1.0, nTick=TICKS_PER_CLOCK):
        if self.PlayerStatus == NO_SONG_LOADED:
            self.MasterTick += nTick
            return

        # INVARIANT NET: an active (non-stopped, non-paused) status must always
        # have a live CurrPart whose main loop resolves. In the firmware that is
        # guaranteed by its single-loop execution; the ORC drives the mind from
        # three threads and via discrete web gestures, so a pathological order
        # could still strand a playing status onto a null/dangling part. Rather
        # than crash the clock tick (silently -- the arp swallows it) we recover
        # to a clean STOP. This never fires in legitimate play; _recover_count
        # tracks it so a real strand is not hidden.
        if self.PlayerStatus not in (STOPPED, PAUSED):
            if self.CurrPart is None or self._main_loop_ptr(self.CurrPart) is None:
                self._recover_count += 1
                self.PedalPresswDrumFillFlag = 0
                self.MultiTapCounter = 0
                self._stop_song()          # takes the full-stop branch -> STOPPED
                self.MasterTick += nTick
                return

        self.AutopilotCueFill = False
        # AP: the whole autopilot cue block runs only when APPtr != None.
        # All factory songs have no AP data, so this is skipped (see header).

        if self.PlayerStatus == PAUSED:
            if self.RequestFlag == START_REQUEST:
                if self.MainUnpauseModeTapToFill == START_FILL:
                    if self.PausedPlayerStatus in (
                            PLAYING_MAIN_TRACK, DRUMFILL_ACTIVE, DRUMFILL_WAITING_TRIG,
                            TRANFILL_WAITING_TRIG, TRANFILL_ACTIVE, TRANFILL_CANCEL,
                            TRANFILL_QUITING, NO_FILL_TRAN, NO_FILL_TRAN_CANCEL,
                            NO_FILL_TRAN_QUITTING):
                        if self.CurrPart.nDrumFill != 0:
                            if self.ActivePauseEnable:
                                self.MasterTick %= self._main_loop_ptr(self.CurrPart).nTick
                            else:
                                self.MasterTick = 0
                            self.RequestFlag = DRUMFILL_REQUEST
                        else:
                            self._same_part(False)
                    else:
                        self._same_part(False)
                else:
                    self._intro_part()
            elif self.RequestFlag == PAUSE_REQUEST:
                self.PlayerStatus = self.UnPausedPlayerStatus
                if self.ActivePauseEnable:
                    self.MasterTick %= self._main_loop_ptr(self.CurrPart).nTick
                else:
                    self.MasterTick = 0
                self._special_effect_manager()
                self.WasPausedFlag = 1
                self.RequestFlag = REQUEST_DONE

        if (self.RequestFlag in (START_REQUEST, EXTERNAL_START_REQUEST)
                and self.PlayerStatus == STOPPED):
            self._intro_part()

        if self.RequestFlag == PAUSE_REQUEST:
            test_master_tick = self.MasterTick
            test_tmp_tick = self.TmpMasterPartTick
            self.PausedPlayerStatus = self.PlayerStatus
            st = self.PlayerStatus

            if st == PLAYING_MAIN_TRACK:
                self._same_part(False)
                self.UnPausedPlayerStatus = PLAYING_MAIN_TRACK
                self.PlayerStatus = PAUSED
                if not self.ActivePauseEnable and self.SendStopOnPause:
                    self._uart_send_stop()
            elif st in (DRUMFILL_ACTIVE, DRUMFILL_WAITING_TRIG):
                self._same_part(True)
                self.UnPausedPlayerStatus = PLAYING_MAIN_TRACK
                self.PlayerStatus = PAUSED
                if not self.ActivePauseEnable and self.SendStopOnPause:
                    self._uart_send_stop()
            elif st in (TRANFILL_CANCEL, NO_FILL_TRAN_CANCEL):
                self._same_part(False)
                self.UnPausedPlayerStatus = PLAYING_MAIN_TRACK
                self.PlayerStatus = PAUSED
                if not self.ActivePauseEnable and self.SendStopOnPause:
                    self._uart_send_stop()
            elif st in (TRANFILL_WAITING_TRIG, TRANFILL_ACTIVE, TRANFILL_QUITING,
                        NO_FILL_TRAN, NO_FILL_TRAN_QUITTING):
                self._next_part()
                self.UnPausedPlayerStatus = PLAYING_MAIN_TRACK
                self.PlayerStatus = PAUSED
                if not self.ActivePauseEnable and self.SendStopOnPause:
                    self._uart_send_stop()
            elif st == PLAYING_MAIN_TRACK_TO_END:
                self._stop_song()
                self._special_effect_manager()
            elif st == OUTRO_WAITING_TRIG:
                self._outro_part()
                self.UnPausedPlayerStatus = OUTRO
                self.PlayerStatus = PAUSED
                self.PartStopSyncTick = 0
                if not self.ActivePauseEnable and self.SendStopOnPause:
                    self._uart_send_stop()
            elif st == INTRO:
                self._first_part()
                self.UnPausedPlayerStatus = self.PlayerStatus
                self.PlayerStatus = PAUSED
                if not self.ActivePauseEnable and self.SendStopOnPause:
                    self._uart_send_stop()
            elif st == OUTRO:
                self.DrumFillIndex = 0
                self._same_part(False)
                self.UnPausedPlayerStatus = self.PlayerStatus
                self.PlayerStatus = PAUSED
                if not self.ActivePauseEnable and self.SendStopOnPause:
                    self._uart_send_stop()
            elif st == OUTRO_CANCELED:
                self._same_part(False)
                self.UnPausedPlayerStatus = PLAYING_MAIN_TRACK
                self.PlayerStatus = PAUSED
                if not self.ActivePauseEnable and self.SendStopOnPause:
                    self._uart_send_stop()
            # STOPPED / NO_SONG_LOADED / default: nothing

            if self.ActivePauseEnable and self.PlayerStatus == PAUSED:
                self.MasterTick = test_master_tick
                self.TmpMasterPartTick = test_tmp_tick
            self._gui_force_refresh()
        else:
            rf = self.RequestFlag
            if rf == DRUMFILL_REQUEST:
                if self.CurrPart.nDrumFill > 0:
                    ml = self._main_loop_ptr(self.CurrPart)
                    self.DrumFillStartSyncTick = self._calc_start_bar_sync_tick(
                        self.MasterTick, ml.barLength,
                        ml.barLength if self.AutopilotCueFill else ml.trigPos)
                    df = self._drum_fill_ptr(self.CurrPart, self.DrumFillIndex)
                    if df.nTick < ml.barLength:
                        self.DrumFillStartSyncTick += ml.barLength - (df.nTick % ml.barLength)
                    if df.event[0][0] < 0:
                        self.DrumFillPickUpSyncTickLength = df.event[0][0] * -1
                    else:
                        self.DrumFillPickUpSyncTickLength = 0
                    # AP: shuffle/next-index selection block runs only with APPtr; skipped.
                    self.PlayerStatus = DRUMFILL_WAITING_TRIG
            elif rf == TRANFILL_REQUEST:
                tf = self._trans_fill_ptr(self.CurrPart)
                ml = self._main_loop_ptr(self.CurrPart)
                # AP: the "APPtr && playFor>=1" branch is skipped; factory content
                # takes the plain "else if (TRANS_FILL_PTR)" path.
                if tf:
                    if tf.pickupNotesLength:
                        if tf.pickupNotesLength % nTick:
                            self.TranFillPickUpSyncTickLength = (1 + tf.pickupNotesLength // nTick) * nTick
                        else:
                            self.TranFillPickUpSyncTickLength = (0 + tf.pickupNotesLength // nTick) * nTick
                    else:
                        self.TranFillPickUpSyncTickLength = 0
                    if self.PlayerStatus == PAUSED:
                        if not self.ActivePauseEnable:
                            self.MasterTick = tf.nTick - self.TranFillPickUpSyncTickLength
                            self.TranFillStartSyncTick = self.MasterTick
                        else:
                            self.TranFillStartSyncTick = self._calc_start_bar_sync_tick(
                                self.MasterTick, ml.barLength,
                                ml.barLength if self.AutopilotCueFill else ml.trigPos)
                            self.TranFillStartSyncTick -= self.TranFillPickUpSyncTickLength
                    else:
                        self.TranFillStartSyncTick = self._calc_start_bar_sync_tick(
                            self.MasterTick, ml.barLength,
                            ml.barLength if self.AutopilotCueFill else ml.trigPos)
                        self.TranFillStartSyncTick -= self.TranFillPickUpSyncTickLength
                    if tf.nTick < tf.barLength:
                        self.TranFillStartSyncTick += ml.barLength - (tf.nTick % tf.barLength)
                    self.PlayerStatus = TRANFILL_WAITING_TRIG
                else:
                    self.PlayerStatus = NO_FILL_TRAN
            elif rf == TRANFILL_QUIT_REQUEST:
                ml = self._main_loop_ptr(self.CurrPart)
                if self.PlayerStatus in (TRANFILL_WAITING_TRIG, TRANFILL_ACTIVE):
                    self.TranFillStopSyncTick = self._calc_tranfill_quit_sync_tick(
                        self.MasterTick, ml.barLength)
                    # AP: the playFor>1 adjustments are skipped (no AP).
                    self.PlayerStatus = TRANFILL_QUITING
                else:
                    self.PartStopSyncTick = self._calc_tranfill_quit_sync_tick(
                        self.MasterTick, ml.barLength)
                    self.PlayerStatus = NO_FILL_TRAN_QUITTING
            elif rf == TRANFILL_CANCEL_REQUEST:
                if self.PlayerStatus == TRANFILL_QUITING:
                    self.PlayerStatus = TRANFILL_CANCEL
                elif self.PlayerStatus == NO_FILL_TRAN_QUITTING:
                    self.PlayerStatus = NO_FILL_TRAN_CANCEL
            elif rf == STOP_REQUEST:
                ml = self._main_loop_ptr(self.CurrPart)
                outro = self._outro_track_ptr(self.CurrSong)
                if outro is not None:
                    self.PartStopSyncTick = self._calc_start_bar_sync_tick(
                        self.MasterTick, ml.barLength,
                        ml.barLength if self.AutopilotCueFill else ml.trigPos)
                    if outro.nTick < ml.barLength:
                        self.PartStopSyncTick += ml.barLength - (outro.nTick % ml.barLength)
                    if outro.pickupNotesLength:
                        if outro.pickupNotesLength % nTick:
                            self.PartStopPickUpSyncTickLength = (1 + outro.pickupNotesLength // nTick) * nTick
                        else:
                            self.PartStopPickUpSyncTickLength = (0 + outro.pickupNotesLength // nTick) * nTick
                    else:
                        self.PartStopPickUpSyncTickLength = 0
                    self.PartStopSyncTick -= self.PartStopPickUpSyncTickLength
                    self.PlayerStatus = OUTRO_WAITING_TRIG
                else:
                    self.PartStopSyncTick = self._calc_tranfill_quit_sync_tick(
                        self.MasterTick, ml.barLength)
                    self.PlayerStatus = PLAYING_MAIN_TRACK_TO_END
            elif rf == SWAP_TO_OUTRO_REQUEST:
                self._swap_to_outro()
            elif rf == OUTRO_CANCEL_REQUEST:
                if self.PlayerStatus == PLAYING_MAIN_TRACK_TO_END:
                    self.PlayerStatus = PLAYING_MAIN_TRACK
                elif self.PlayerStatus == OUTRO:
                    self.PlayerStatus = OUTRO_CANCELED
                elif self.PlayerStatus == OUTRO_WAITING_TRIG:
                    self.PlayerStatus = PLAYING_MAIN_TRACK

        self.RequestFlag = REQUEST_DONE
        self.TmpMasterPartTick = self.MasterTick + nTick

        # ---- Trigger Manager (waiting-for-bar states) ----
        st = self.PlayerStatus
        if st == DRUMFILL_WAITING_TRIG:
            if self.TmpMasterPartTick <= self.DrumFillStartSyncTick:
                ml = self._main_loop_ptr(self.CurrPart)
                self._track_play(ml, self.MasterTick, self.TmpMasterPartTick, ratio, 0, MAIN_PART_ID)
                if self.TmpMasterPartTick >= ml.nTick:
                    self.DrumFillStartSyncTick -= ml.nTick
                    self.TmpMasterPartTick = 0
            else:
                self.PlayerStatus = DRUMFILL_ACTIVE
        elif st == TRANFILL_WAITING_TRIG:
            if self.TmpMasterPartTick <= self.TranFillStartSyncTick:
                ml = self._main_loop_ptr(self.CurrPart)
                self._track_play(ml, self.MasterTick, self.TmpMasterPartTick, ratio, 0, MAIN_PART_ID)
                if self.TmpMasterPartTick >= ml.nTick:
                    self.TranFillStartSyncTick -= ml.nTick
                    self.TmpMasterPartTick = 0
            else:
                self.PlayerStatus = TRANFILL_ACTIVE
        elif st == OUTRO_WAITING_TRIG:
            if self.TmpMasterPartTick <= self.PartStopSyncTick:
                ml = self._main_loop_ptr(self.CurrPart)
                self._track_play(ml, self.MasterTick, self.TmpMasterPartTick, ratio, 0, MAIN_PART_ID)
                if self.TmpMasterPartTick >= ml.nTick:
                    self.PartStopSyncTick -= ml.nTick
                    self.TmpMasterPartTick = 0
            else:
                self.PlayerStatus = OUTRO
                self.CurrPart = self.CurrSong.outro

        # ---- Player state renderers ----
        st = self.PlayerStatus
        if st == INTRO:
            ml = self._main_loop_ptr(self.CurrPart)
            self._track_play(ml, self.MasterTick, self.TmpMasterPartTick, ratio, 0, INTR_FILL_ID)
            if self.TmpMasterPartTick >= ml.nTick:
                self._track_play(ml, ml.nTick, ml.nTick + POST_EVENT_MAX_TICK, ratio, nTick, INTR_FILL_ID)
                self.TmpMasterPartTick = 0
                self._fill_ap_index()
                self.DrumFillIndex = self._next_ap_index() if self.APPtr else 0
                if 0 < self.NextPartNumber <= self.CurrSong.nPart:
                    self.PartIndex = self.NextPartNumber - 1
                else:
                    self.PartIndex = 0
                self.NextPartNumber = 0
                self.CurrPart = self.CurrSong.part[self.PartIndex]
                self.PlayerStatus = PLAYING_MAIN_TRACK
                self.BeatCounter = 0
                self._special_effect_manager()
        elif st == DRUMFILL_ACTIVE:
            df = self._drum_fill_ptr(self.CurrPart, self.DrumFillIndex)
            base = self.DrumFillStartSyncTick + self.DrumFillPickUpSyncTickLength
            self._track_play(df, self.MasterTick - base, self.TmpMasterPartTick - base,
                             ratio, 0, DRUM_FILL_ID)
            if self.TmpMasterPartTick - base >= df.nTick:
                self._track_play(df, df.nTick, df.nTick + POST_EVENT_MAX_TICK, ratio, nTick, DRUM_FILL_ID)
                if self.PedalPresswDrumFillFlag != 0:
                    self._reset_beat_counter()
                    self.PedalPresswDrumFillFlag = 0
                self._same_part(True)
                self._special_effect_manager()
        elif st == TRANFILL_ACTIVE:
            tf = self._trans_fill_ptr(self.CurrPart)
            base = self.TranFillStartSyncTick + self.TranFillPickUpSyncTickLength
            self._track_play(tf, self.MasterTick - base, self.TmpMasterPartTick - base,
                             ratio, 0, TRAN_FILL_ID)
            if self.TmpMasterPartTick >= tf.nTick + self.TranFillStartSyncTick:
                self.TranFillStartSyncTick += tf.nTick
        elif st in (TRANFILL_CANCEL, TRANFILL_QUITING):
            tf = self._trans_fill_ptr(self.CurrPart)
            if self.TmpMasterPartTick <= self.TranFillStartSyncTick:
                ml = self._main_loop_ptr(self.CurrPart)
                self._track_play(ml, self.MasterTick, self.TmpMasterPartTick, ratio, 0, MAIN_PART_ID)
            else:
                base = self.TranFillStartSyncTick + self.TranFillPickUpSyncTickLength
                self._track_play(tf, self.MasterTick - base, self.TmpMasterPartTick - base,
                                 ratio, 0, TRAN_FILL_ID)
            if self.TmpMasterPartTick >= (tf.nTick + self.TranFillStartSyncTick
                                          + self.TranFillPickUpSyncTickLength):
                self.TranFillStartSyncTick += tf.nTick
            if self.TmpMasterPartTick >= self.TranFillStopSyncTick:
                self._track_play(tf, tf.nTick, tf.nTick + POST_EVENT_MAX_TICK, ratio, nTick, TRAN_FILL_ID)
                if self.PlayerStatus == TRANFILL_CANCEL:
                    self._same_part(0)
                else:
                    self._next_part()
                    self.PedalPresswDrumFillFlag = 0
                self._special_effect_manager()
        elif st == NO_FILL_TRAN:
            ml = self._main_loop_ptr(self.CurrPart)
            self._track_play(ml, self.MasterTick, self.TmpMasterPartTick, ratio, 0, MAIN_PART_ID)
            if self.TmpMasterPartTick >= ml.nTick:
                self.TmpMasterPartTick = 0
        elif st == PLAYING_MAIN_TRACK:
            if self.DelayStartCmd:
                self._uart_send_start()
                self.DelayStartCmd = 0
            ml = self._main_loop_ptr(self.CurrPart)
            self._track_play(ml, self.MasterTick, self.TmpMasterPartTick, ratio, 0, MAIN_PART_ID)
            # AP: the BeatCounter-based pedal-press restart never fires (BeatCounter
            # stays 0 without AP); the restart happens via SamePart instead.
            if (self.PedalPresswDrumFillFlag == 1 and self.BeatCounter > 1
                    and (self.BeatCounter - 1) % ml.timeSigNum == 0):
                self.MasterTick = 0
                self._reset_beat_counter()
                self.PedalPresswDrumFillFlag = 0
                self.PlayerStatus = PLAYING_MAIN_TRACK
            if self.TmpMasterPartTick >= ml.nTick:
                self._same_part(0)
                self._special_effect_manager()
            elif self._is_end_of_track(self.TmpMasterPartTick, self.CurrPart):
                self._same_part(2)
                self._special_effect_manager()
        elif st == NO_FILL_TRAN_CANCEL:
            ml = self._main_loop_ptr(self.CurrPart)
            self._track_play(ml, self.MasterTick, self.TmpMasterPartTick, ratio, 0, MAIN_PART_ID)
            if self.TmpMasterPartTick >= self.PartStopSyncTick:
                self._same_part(False)
                self._special_effect_manager()
        elif st == NO_FILL_TRAN_QUITTING:
            ml = self._main_loop_ptr(self.CurrPart)
            self._track_play(ml, self.MasterTick, self.TmpMasterPartTick, ratio, 0, MAIN_PART_ID)
            if (self.PedalPresswDrumFillFlag != 0 and self.BeatCounter > 1
                    and (self.BeatCounter - 1) % ml.timeSigNum == 0):
                self.MasterTick = 0
                self._reset_beat_counter()
                self.PedalPresswDrumFillFlag = 0
                self.PlayerStatus = PLAYING_MAIN_TRACK
            elif self.PartIndex < self.CurrSong.nPart - 1:
                if self.TmpMasterPartTick >= self.PartStopSyncTick:
                    self._next_part()
                    self.PedalPresswDrumFillFlag = 0
                    self._special_effect_manager()
        elif st == PLAYING_MAIN_TRACK_TO_END:
            ml = self._main_loop_ptr(self.CurrPart)
            self._track_play(ml, self.MasterTick, self.TmpMasterPartTick, ratio, 0, MAIN_PART_ID)
            if self.TmpMasterPartTick >= self.PartStopSyncTick:
                if self.TransPedalPressFlag:
                    self._next_part()
                    self.TransPedalPressFlag = False
                else:
                    self._stop_song()
                self._special_effect_manager()
        elif st == OUTRO:
            ml = self._main_loop_ptr(self.CurrPart)
            base = self.PartStopSyncTick + self.PartStopPickUpSyncTickLength
            self._track_play(ml, self.MasterTick - base, self.TmpMasterPartTick - base,
                             ratio, 0, OUTR_FILL_ID)
            if self.TmpMasterPartTick >= ml.nTick + base:
                self._track_play(ml, ml.nTick, ml.nTick + POST_EVENT_MAX_TICK, ratio, nTick, OUTR_FILL_ID)
                self.PedalPresswDrumFillFlag = 0
                self._stop_song()
        elif st == OUTRO_CANCELED:
            ml = self._main_loop_ptr(self.CurrPart)
            base = self.PartStopSyncTick + self.PartStopPickUpSyncTickLength
            self._track_play(ml, self.MasterTick - base, self.TmpMasterPartTick - base,
                             ratio, 0, OUTR_FILL_ID)
            if self.TmpMasterPartTick >= ml.nTick + base:
                self._track_play(ml, ml.nTick, ml.nTick + POST_EVENT_MAX_TICK, ratio, nTick, OUTR_FILL_ID)
                self._same_part(False)
                self._special_effect_manager()
        elif st in (PAUSED, STOPPED):
            pass

        self.MasterTick = self.TmpMasterPartTick
        self.LastPlayerStatus = self.PlayerStatus
        self._gui_force_refresh()

    # =====================================================================
    # button_callback -- ports SongPlayer_ButtonCallback + per-state handlers
    # =====================================================================
    @_serialized
    def button_callback(self, event, time_ms=0):
        if event == BE_PEDAL_PRESS:
            self.MultiTapCounter = 0
            self.WasPausedFlag = False
            self.PedalPressFlag = True
            self.WasLongPressed = False
        elif event == BE_PEDAL_RELEASE:
            if not self.PedalPressFlag:
                return
        elif event == BE_PEDAL_LONG_PRESS:
            self.WasLongPressed = True
            if not self.PedalPressFlag:
                return
        elif event == BE_PEDAL_MULTI_TAP:
            if self.MultiTapCounter:
                if (time_ms - self.LastMultiTapTime) < MULTI_TAP_WINDOW_MS:
                    if self.TrippleTapEnable:
                        self._stop_song()
            else:
                if self.WasLongPressed:
                    event = BE_PEDAL_PRESS
                    self.MultiTapCounter = 0
                    self.WasPausedFlag = False
                    self.PedalPressFlag = True
                    self.WasLongPressed = False
            self.LastMultiTapTime = time_ms
            self.MultiTapCounter += 1
        elif event == BE_FOOT_SECONDARY_PRESS:
            if self.PlayerStatus != NO_SONG_LOADED:
                if self.PlayerStatus != STOPPED:
                    if self.SecondaryFootSwitchPlayingAction == ACTION_PAUSE_UNPAUSE:
                        self.PedalPressFlag = 0
                        self._pause_unpause_handler()
                    elif self.SecondaryFootSwitchPlayingAction == ACTION_SPECIAL_EFFECT:
                        self._play_special_effect(100, False)
                    elif self.SecondaryFootSwitchPlayingAction == ACTION_OUTRO:
                        event = BE_PEDAL_MULTI_TAP
                else:
                    if self.SecondaryFootSwitchStoppedAction == ACTION_SPECIAL_EFFECT:
                        self._play_special_effect(100, False)
        elif event == BE_FOOT_PRIMARY_PRESS:
            if self.PlayerStatus != NO_SONG_LOADED:
                if self.PlayerStatus != STOPPED:
                    if self.PrimaryFootSwitchPlayingAction == ACTION_PAUSE_UNPAUSE:
                        self.PedalPressFlag = 0
                        self._pause_unpause_handler()
                    elif self.PrimaryFootSwitchPlayingAction == ACTION_SPECIAL_EFFECT:
                        self._play_special_effect(100, False)
                    elif self.PrimaryFootSwitchPlayingAction == ACTION_OUTRO:
                        event = BE_PEDAL_MULTI_TAP
                else:
                    if self.PrimaryFootSwitchStoppedAction == ACTION_SPECIAL_EFFECT:
                        self._play_special_effect(100, False)

        if self.MultiTapCounter > 1:
            return

        handler = self._STATE_BUTTON_HANDLERS.get(self.PlayerStatus)
        if handler is not None:
            handler(self, event)

    # ------ per-state button handlers (port the *_ButtonHandler functions) ------
    def _bh_no_song_loaded(self, event):
        pass

    def _bh_stopped(self, event):
        if event == BE_PEDAL_PRESS:
            if self.StartBeatOnPress and self.PedalPressFlag:
                self.RequestFlag = START_REQUEST
                self.PedalPressFlag = 0
                self._special_effect_manager()
                self._tempo_start_with_int()
        elif event == BE_PEDAL_RELEASE:
            if not self.StartBeatOnPress and self.PedalPressFlag:
                self.RequestFlag = START_REQUEST
                self._special_effect_manager()
                self._tempo_start_with_int()

    def _bh_paused(self, event):
        if event == BE_PEDAL_PRESS:
            if self.StartBeatOnPress:
                self.RequestFlag = START_REQUEST
                self._special_effect_manager()
                self._tempo_start_with_int()
                self.PedalPressFlag = 0
        elif event == BE_PEDAL_RELEASE:
            if not self.StartBeatOnPress:
                self.RequestFlag = START_REQUEST
                self._special_effect_manager()
                self._tempo_start_with_int()
        elif event == BE_PEDAL_LONG_PRESS:
            if self.MainUnpauseModeHoldToTransition == START_TRANSITION:
                self.NextPartNumber = 0
                self.RequestFlag = TRANFILL_REQUEST
            else:
                self.PedalPressFlag = 0
                self._stop_song()
        elif event == BE_PEDAL_MULTI_TAP:
            self._stop_song()

    def _bh_intro(self, event):
        if event == BE_PEDAL_MULTI_TAP:
            self.RequestFlag = SWAP_TO_OUTRO_REQUEST
            if not self.TrippleTapEnable:
                self.MultiTapCounter = 0

    def _bh_playing_main_track(self, event):
        if event == BE_PEDAL_RELEASE:
            self.RequestFlag = DRUMFILL_REQUEST
            self.PedalPresswDrumFillFlag = 1
        elif event == BE_PEDAL_LONG_PRESS:
            self.NextPartNumber = 0
            self.RequestFlag = TRANFILL_REQUEST
            self.TransPedalPressFlag = True
        elif event == BE_PEDAL_MULTI_TAP:
            if self.WasPausedFlag:
                self.RequestFlag = SWAP_TO_OUTRO_REQUEST
            else:
                self.RequestFlag = STOP_REQUEST
                self.PedalPressFlag = 0
                if not self.TrippleTapEnable:
                    self.MultiTapCounter = 0

    def _bh_playing_main_track_to_end(self, event):
        if event == BE_PEDAL_RELEASE:
            self.RequestFlag = OUTRO_CANCEL_REQUEST
            self.PedalPresswDrumFillFlag = 1

    def _bh_no_fill_tran(self, event):
        if event == BE_PEDAL_RELEASE:
            self.RequestFlag = TRANFILL_QUIT_REQUEST
        elif event == BE_PEDAL_MULTI_TAP:
            self.RequestFlag = STOP_REQUEST
            self.PedalPressFlag = 0
            if not self.TrippleTapEnable:
                self.MultiTapCounter = 0

    def _bh_no_fill_tran_quitting(self, event):
        if event == BE_PEDAL_RELEASE:
            if not self.WasLongPressed and self.MultiTapCounter == 0:
                self._bh_playing_main_track(event)
            else:
                self.RequestFlag = TRANFILL_CANCEL_REQUEST
        elif event == BE_PEDAL_MULTI_TAP:
            self.RequestFlag = STOP_REQUEST
            self.PedalPressFlag = 0
            if not self.TrippleTapEnable:
                self.MultiTapCounter = 0

    def _bh_no_fill_tran_cancel(self, event):
        pass

    def _bh_tranfill_waiting_trig(self, event):
        if event == BE_PEDAL_RELEASE:
            self.RequestFlag = TRANFILL_QUIT_REQUEST
        elif event == BE_PEDAL_MULTI_TAP:
            self.RequestFlag = STOP_REQUEST
            self.PedalPressFlag = 0
            if not self.TrippleTapEnable:
                self.MultiTapCounter = 0

    def _bh_tranfill_active(self, event):
        if event == BE_PEDAL_RELEASE:
            self.RequestFlag = TRANFILL_QUIT_REQUEST
        elif event == BE_PEDAL_MULTI_TAP:
            self.RequestFlag = STOP_REQUEST
            self.PedalPressFlag = 0
            if not self.TrippleTapEnable:
                self.MultiTapCounter = 0

    def _bh_tranfill_quiting(self, event):
        if event in (BE_PEDAL_MULTI_TAP, BE_PEDAL_RELEASE):
            if not self.WasLongPressed and self.MultiTapCounter == 0:
                self._bh_playing_main_track(event)
            else:
                self.RequestFlag = TRANFILL_CANCEL_REQUEST
                self.WasLongPressed = True

    def _bh_tranfill_cancel(self, event):
        pass

    def _bh_drumfill_waiting_trig(self, event):
        if event == BE_PEDAL_LONG_PRESS:
            self.NextPartNumber = 0
            self.RequestFlag = TRANFILL_REQUEST
        elif event == BE_PEDAL_MULTI_TAP:
            self.RequestFlag = STOP_REQUEST
            self.PedalPressFlag = 0
            if not self.TrippleTapEnable:
                self.MultiTapCounter = 0

    def _bh_drumfill_active(self, event):
        if event == BE_PEDAL_LONG_PRESS:
            self.RequestFlag = TRANFILL_REQUEST
        elif event == BE_PEDAL_MULTI_TAP:
            self.RequestFlag = STOP_REQUEST
            self.PedalPressFlag = 0
            if not self.TrippleTapEnable:
                self.MultiTapCounter = 0
        elif event == BE_PEDAL_RELEASE:
            self.RequestFlag = DRUMFILL_REQUEST
            self.PedalPresswDrumFillFlag = 1

    def _bh_outro(self, event):
        if event in (BE_PEDAL_PRESS, BE_PEDAL_MULTI_TAP):
            self.RequestFlag = OUTRO_CANCEL_REQUEST
            self.PedalPressFlag = False
            self.WasLongPressed = True

    def _bh_outro_waiting_trig(self, event):
        if event in (BE_PEDAL_PRESS, BE_PEDAL_MULTI_TAP):
            self.PedalPressFlag = False
            self.WasLongPressed = True
            self.RequestFlag = OUTRO_CANCEL_REQUEST
            if not self.TrippleTapEnable:
                self.MultiTapCounter = 0

    def _bh_outro_canceled(self, event):
        pass

    # =====================================================================
    # High-level convenience API (cmd socket / web / pedal driver)
    # =====================================================================
    @_serialized
    def tap(self, time_ms=0):
        """A quick tap: press + short release. In main = drumfill; stopped = start."""
        self.button_callback(BE_PEDAL_PRESS, time_ms)
        self.button_callback(BE_PEDAL_RELEASE, time_ms)

    @_serialized
    def hold(self, time_ms=0):
        """Press + hold (long-press) -> transition fill begins looping."""
        self.button_callback(BE_PEDAL_PRESS, time_ms)
        self.button_callback(BE_PEDAL_LONG_PRESS, time_ms)

    @_serialized
    def release(self, time_ms=0):
        """Release after a hold -> quit the transition (advance to next part)."""
        self.button_callback(BE_PEDAL_RELEASE, time_ms)

    @_serialized
    def double_tap(self, time_ms=0):
        """Double tap -> outro (if present) else stop, on the bar.

        A real double tap feeds the callback PRESS, RELEASE, MULTI_TAP as one
        atomic burst (no process_song between): the PRESS clears any stale
        WasLongPressed, and the MULTI_TAP lands while still in the current
        playing state -> STOP_REQUEST / SWAP_TO_OUTRO_REQUEST. The transient
        DRUMFILL_REQUEST the RELEASE sets is overwritten by the MULTI_TAP since
        no tick runs in between -- exactly as on the hardware.
        """
        self.button_callback(BE_PEDAL_PRESS, time_ms)
        self.button_callback(BE_PEDAL_RELEASE, time_ms)
        self.button_callback(BE_PEDAL_MULTI_TAP, time_ms)

    @_serialized
    def start(self):
        """Start the song from STOPPED (skip the pedal press/release detail)."""
        if self.PlayerStatus == STOPPED:
            self.RequestFlag = START_REQUEST

    @_serialized
    def stop(self):
        self._stop_song()

    @_serialized
    def pause(self):
        self.external_pause()

    def accent(self, note=None, vel=127):
        """One-shot accent hit -- emitted immediately (not tied to the grammar)."""
        if note is None:
            return
        self.emit(note, vel, 0.0, 1.0, NO_PART_ID, False)


def _rand():
    import random
    return random.randint(0, 1 << 30)


# state -> button handler dispatch table (built after methods exist)
BeatBuddyMind._STATE_BUTTON_HANDLERS = {
    NO_SONG_LOADED: BeatBuddyMind._bh_no_song_loaded,
    STOPPED: BeatBuddyMind._bh_stopped,
    PAUSED: BeatBuddyMind._bh_paused,
    INTRO: BeatBuddyMind._bh_intro,
    PLAYING_MAIN_TRACK: BeatBuddyMind._bh_playing_main_track,
    PLAYING_MAIN_TRACK_TO_END: BeatBuddyMind._bh_playing_main_track_to_end,
    DRUMFILL_WAITING_TRIG: BeatBuddyMind._bh_drumfill_waiting_trig,
    DRUMFILL_ACTIVE: BeatBuddyMind._bh_drumfill_active,
    TRANFILL_WAITING_TRIG: BeatBuddyMind._bh_tranfill_waiting_trig,
    TRANFILL_ACTIVE: BeatBuddyMind._bh_tranfill_active,
    TRANFILL_QUITING: BeatBuddyMind._bh_tranfill_quiting,
    TRANFILL_CANCEL: BeatBuddyMind._bh_tranfill_cancel,
    NO_FILL_TRAN: BeatBuddyMind._bh_no_fill_tran,
    NO_FILL_TRAN_QUITTING: BeatBuddyMind._bh_no_fill_tran_quitting,
    NO_FILL_TRAN_CANCEL: BeatBuddyMind._bh_no_fill_tran_cancel,
    OUTRO: BeatBuddyMind._bh_outro,
    OUTRO_WAITING_TRIG: BeatBuddyMind._bh_outro_waiting_trig,
    OUTRO_CANCELED: BeatBuddyMind._bh_outro_canceled,
}
