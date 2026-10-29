#!/usr/bin/env python3
"""/patch across SEVERAL JACK servers (orc1_web.py, 2026-10-19).

    python3 _test_patch_servers.py [-v]

A module moved to a card's own jackd (orc1-jackd@<card>) used to vanish from
the grid. Now every list covers every running server; a per-card port is
named "<server>::<client>:<port>", wires never cross servers, and a per-card
server that cannot be listed is "unknown", never aged toward a prune.
CONNECTING a module to another server's ports MOVES the module there (its old
wires and its startup auto-wire are dropped); only a module can move.

SAFETY: jack is never run. _jack, _jackd_servers, _patch_run,
_assign_module_card (the sudo helper) and subprocess.run are faked; the saved
patch and the module-outputs file are temp files; every global is restored.
"""

import json, os, shutil, subprocess, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import orc1_web as W                                        # noqa: E402
import orc1_config as _cfg_suite                              # noqa: E402

# THE WHOLE RUN WRITES ONLY TO TEMP FILES, set here at import, before any test.
# 2026-10-19: a MUTATION run of this suite (refusal disabled, test not yet
# sandboxed) POSTed a fake connect that went through and wrote a bogus wire
# into the REAL ~/.orc1_patch.json and drum="none" into the REAL
# ~/.orc1_module_outputs.json (= drums silent at their next restart). A test
# must be safe even when the code under it is broken: that is what a mutation
# run IS. __main__ checks both real files are byte-identical afterwards.
REAL_PATCH_FILE = W.PATCH_FILE
REAL_MO_FILE = _cfg_suite.MODULE_OUT_FILE
_SANDBOX = tempfile.mkdtemp(prefix="patchsrv_suite_")
W.PATCH_FILE = os.path.join(_SANDBOX, "patch.json")
_cfg_suite.MODULE_OUT_FILE = os.path.join(_SANDBOX, "mo.json")

VERBOSE = "-v" in sys.argv
PASS = FAIL = 0
EXPECTED = 54


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        if VERBOSE:
            print("  ok   %s" % name)
    else:
        FAIL += 1
        print("  FAIL %s%s" % (name, ("  -- " + str(detail)) if detail else ""))


def lsp_ports(ports):
    """jack_lsp -t -p text for [(name, 'in'|'out')]."""
    out = []
    for n, d in ports:
        out += [n, "\tproperties: %s,physical,terminal," %
                ("input" if d == "in" else "output"),
                "\t32 bit float mono audio"]
    return "\n".join(out) + "\n"


def lsp_conns(conns):
    """jack_lsp -c text: each port, then what it is connected to."""
    by = {}
    for a, b in conns:
        by.setdefault(a, []).append(b)
        by.setdefault(b, []).append(a)
    out = []
    for p, others in by.items():
        out.append(p)
        out += ["   " + o for o in others]
    return "\n".join(out) + "\n"


MAIN = [("system:capture_1", "out"), ("system:playback_1", "in"),
        ("ORC1-Drum:out_L", "out"), ("ORC1-Drum:out_R", "out")]
PIS = [("system:capture_1", "out"), ("system:playback_1", "in"),
       ("system:playback_2", "in"), ("system:playback_3", "in"),
       ("system:playback_4", "in"), ("ORC1-Mono:out_L", "out")]


class Fake:
    """Two servers: main (None) and 'pisound'. `down` = servers whose
    jack_lsp returns nothing (unreachable)."""

    def __init__(self, down=(), main_conns=(), pis_conns=()):
        self.down = set(down)
        self.conns = {None: list(main_conns), "pisound": list(pis_conns)}
        self.ports = {None: list(MAIN), "pisound": list(PIS)}
        self.calls = []

    def jack(self, args, server=None):
        self.calls.append((tuple(args), server))
        if server in self.down:
            return ""
        if args[:2] == ["jack_lsp", "-c"]:
            return lsp_conns(self.conns[server])
        return lsp_ports(self.ports[server])

    def run(self, tool, a, b):
        """Stands in for _patch_run: edits the fake graph, same-server only."""
        sa, ba = W._port_server(a)
        sb, bb = W._port_server(b)
        if sa != sb:
            return "different JACK servers"
        c = self.conns[sa]
        if tool == "jack_connect":
            if (ba, bb) not in c:
                c.append((ba, bb))
        elif (ba, bb) in c:
            c.remove((ba, bb))
        return None

    def move(self, client, to, autowire=("system:playback_1", "system:playback_2")):
        """What a host restart onto server `to` does: its ports leave the old
        server (with their wires) and it self-wires to its startup pair."""
        for srv in self.ports:
            self.ports[srv] = [x for x in self.ports[srv]
                               if not x[0].startswith(client + ":")]
            self.conns[srv] = [w for w in self.conns[srv]
                               if not w[0].startswith(client + ":")]
        self.ports[to] += [(client + ":out_L", "out"), (client + ":out_R", "out")]
        if autowire:
            self.conns[to] += [(client + ":out_L", autowire[0]),
                               (client + ":out_R", autowire[1])]

    def servers(self):
        return [(None, "L12"), ("pisound", "pisound")]


def with_fake(fake, fn):
    saved = (W._jack, W._jackd_servers)
    W._jack, W._jackd_servers = fake.jack, fake.servers
    try:
        return fn()
    finally:
        W._jack, W._jackd_servers = saved
        W._patch_unreachable.clear()


def test_ports_cover_every_server():
    f = Fake()
    names = with_fake(f, lambda: [p["name"] for p in W._patch_ports()])
    check("main ports keep their plain names", "ORC1-Drum:out_L" in names, names)
    check("a per-card server's ports are prefixed",
          "pisound::ORC1-Mono:out_L" in names, names)
    check("each server listed through its own JACK_DEFAULT_SERVER",
          {s for _a, s in f.calls} == {None, "pisound"}, f.calls)


def test_main_down_means_no_graph():
    f = Fake(down={None})
    ports = with_fake(f, W._patch_ports)
    check("main unreachable = [] even with a per-card server up", ports == [], ports)


def test_card_server_down_is_unknown():
    f = Fake(down={"pisound"})
    def run():
        ports = W._patch_ports()
        return [p["name"] for p in ports], set(W._patch_unreachable)
    names, bad = with_fake(f, run)
    check("an unreachable per-card server is left out",
          not any(n.startswith("pisound::") for n in names) and
          "ORC1-Drum:out_L" in names, names)
    check("and noted as unreachable", bad == {"pisound"}, bad)


def test_conns_per_server():
    f = Fake(main_conns=[("ORC1-Drum:out_L", "system:playback_1")],
             pis_conns=[("ORC1-Mono:out_L", "system:playback_2")])
    conns = with_fake(f, lambda: W._patch_conns())
    check("main wires plain", ("ORC1-Drum:out_L", "system:playback_1") in conns, conns)
    check("per-card wires prefixed on both ends",
          ("pisound::ORC1-Mono:out_L", "pisound::system:playback_2") in conns, conns)
    check("no wire listed twice", len(conns) == 2, conns)


def test_run_routes_and_refuses():
    real = subprocess.run
    seen = []

    class R:
        returncode, stderr = 0, b""

    def fake_run(args, **k):
        seen.append((list(args), (k.get("env") or {}).get("JACK_DEFAULT_SERVER")))
        return R()
    subprocess.run = fake_run
    try:
        err = W._patch_run("jack_connect", "pisound::ORC1-Mono:out_L",
                           "pisound::system:playback_1")
        check("per-card connect succeeds", err is None, err)
        check("per-card connect uses BARE names on that server",
              seen and seen[-1] == (["jack_connect", "ORC1-Mono:out_L",
                                     "system:playback_1"], "pisound"), seen)
        n = len(seen)
        err = W._patch_run("jack_connect", "ORC1-Drum:out_L",
                           "pisound::system:playback_1")
        check("a cross-server wire is refused with a message",
              err and "different JACK servers" in err, err)
        check("and never reaches jack", len(seen) == n, seen)
        W._patch_run("jack_disconnect", "ORC1-Drum:out_L", "system:playback_1")
        check("main wires run with this process's own env",
              seen[-1] == (["jack_disconnect", "ORC1-Drum:out_L",
                            "system:playback_1"], None), seen[-1])
    finally:
        subprocess.run = real


class MoveRig:
    """A fake graph + fake assign helper + temp saved patch and module-outputs
    file, around a POST to /api/patch."""

    def __init__(self, fake):
        self.f = fake
        self.assigned = []
        self.tmp = tempfile.mkdtemp(prefix="patchmove_")

    def __enter__(self):
        import orc1_config as cfg
        self.cfg = cfg
        self.saved = (W._jack, W._jackd_servers, W._patch_run,
                      W._assign_module_card, W._module_current_card,
                      W.PATCH_FILE, cfg.MODULE_OUT_FILE, W.time.sleep)
        W._jack, W._jackd_servers = self.f.jack, self.f.servers
        W._patch_run = self.f.run
        W.PATCH_FILE = os.path.join(self.tmp, "saved.json")
        cfg.MODULE_OUT_FILE = os.path.join(self.tmp, "mo.json")
        W.time.sleep = lambda s: None
        self.card = {}

        def assign(module, card):
            self.assigned.append((module, card))
            self.card[module] = card
            self.f.move(cfg.MODULE_JACK_NAMES[module],
                        None if card == "main" else card)
            return True, "ok"
        W._assign_module_card = assign
        W._module_current_card = lambda k: self.card.get(k, "main")
        return self

    def __exit__(self, *a):
        (W._jack, W._jackd_servers, W._patch_run, W._assign_module_card,
         W._module_current_card, W.PATCH_FILE, self.cfg.MODULE_OUT_FILE,
         W.time.sleep) = self.saved
        W._patch_unreachable.clear()
        W._patch_seen_up.clear()
        W._patch_last_error.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False

    def post(self, body):
        r = W.app.test_client().post("/api/patch", json=body)
        return r.status_code, (r.get_json() or {})


def test_connect_across_servers_moves_the_module():
    f = Fake(main_conns=[("ORC1-Drum:out_L", "system:playback_1")])
    with MoveRig(f) as rig:
        W._patch_saved_write([W._patch_entry("ORC1-Drum:out_L",
                                             "system:playback_1")])
        code, body = rig.post({"op": "connect",
                               "src": ["ORC1-Drum:out_L", "ORC1-Drum:out_R"],
                               "dst": ["pisound::system:playback_3",
                                       "pisound::system:playback_4"]})
        check("a module wired to another server's ports is MOVED, not refused",
              code == 200 and body.get("ok"), (code, body))
        check("through the assign helper, to that card",
              rig.assigned == [("drum", "pisound")], rig.assigned)
        check("the response says so", body.get("moved") ==
              {"module": "drum", "to": "pisound"}, body.get("moved"))
        conns = set(map(tuple, W._patch_conns()))
        want = {("pisound::ORC1-Drum:out_L", "pisound::system:playback_3"),
                ("pisound::ORC1-Drum:out_R", "pisound::system:playback_4")}
        check("it ends up wired ONLY where it was pointed (startup auto-wire "
              "to 1/2 cut)", {c for c in conns if "ORC1-Drum" in c[0]} == want,
              conns)
        saved = {(e["src"], e["dst"]) for e in W._patch_saved_load()}
        check("its old saved wire on the main server is forgotten",
              ("ORC1-Drum:out_L", "system:playback_1") not in saved, saved)
        check("the new wires are saved", want <= saved, saved)
        check("the new pair is its startup pair on that server",
              rig.cfg.load_module_out("drum") == "out-3-4",
              rig.cfg.load_module_out("drum"))


def test_move_back_to_main():
    f = Fake()
    f.move("ORC1-Mono", "pisound")
    with MoveRig(f) as rig:
        rig.card["mono"] = "pisound"
        code, body = rig.post({"op": "connect",
                               "src": ["pisound::ORC1-Mono:out_L"],
                               "dst": ["system:playback_1"]})
        check("a per-card module wired to a main port moves to main",
              code == 200 and rig.assigned == [("mono", "main")], (code, body))
        conns = set(map(tuple, W._patch_conns()))
        check("and is wired there, its auto-wire cut",
              {c for c in conns if "ORC1-Mono" in c[0]} ==
              {("ORC1-Mono:out_L", "system:playback_1")}, conns)


def test_only_modules_move():
    f = Fake()
    with MoveRig(f) as rig:
        code, body = rig.post({"op": "connect", "src": ["system:capture_1"],
                               "dst": ["pisound::system:playback_1"]})
        check("a card's own port can't move: 400 with the reason",
              code == 400 and "not a module output" in body.get("error", ""),
              (code, body))
        check("and nothing was moved", rig.assigned == [], rig.assigned)


def _with_rates(rates, fn):
    """Stub the card -> server-rate lookup for a test."""
    saved = W._card_rate
    W._card_rate = lambda card: rates.get(card)
    try:
        return fn()
    finally:
        W._card_rate = saved


def test_bbkit_moves_to_a_48k_server():
    import orc1_config as cfg
    jk = cfg.MODULE_JACK_NAMES["bbkit"]
    f = Fake()
    f.ports[None] += [(jk + ":out_L", "out"), (jk + ":out_R", "out")]
    with MoveRig(f) as rig:
        code, body = _with_rates({"pisound": 48000}, lambda: rig.post({
            "op": "connect", "src": [jk + ":out_L", jk + ":out_R"],
            "dst": ["pisound::system:playback_1",
                    "pisound::system:playback_2"]}))
        check("BBKit moves to a 48 kHz server (owner: put the BeatBuddy on "
              "the pisound)", code == 200 and rig.assigned == [("bbkit", "pisound")],
              (code, body, rig.assigned))


def test_bbkit_refused_on_another_rate():
    import orc1_config as cfg
    jk = cfg.MODULE_JACK_NAMES["bbkit"]
    f = Fake()
    f.ports[None].append((jk + ":out_L", "out"))
    with MoveRig(f) as rig:
        code, body = _with_rates({"pisound": 44100}, lambda: rig.post({
            "op": "connect", "src": [jk + ":out_L"],
            "dst": ["pisound::system:playback_1"]}))
        check("BBKit to a 44.1 kHz server is refused with the reason, nothing moved",
              code == 400 and "only runs at 48000 Hz" in body.get("error", "")
              and "44100" in body.get("error", "") and rig.assigned == [],
              (code, body, rig.assigned))
        check("other modules are not rate-limited",
              W._card_rate_refusal("mono", "pisound") is None)


def test_picker_lists_per_module():
    saved = (W._assignable_cards, W._module_current_card, W._card_rate,
             W._jackd_servers)
    W._assignable_cards = lambda: ["main", "pisound", "M3"]
    W._module_current_card = lambda k: "main"
    W._card_rate = lambda c: {"pisound": 48000, "M3": 44100}.get(c)
    W._jackd_servers = lambda: [(None, "L12")]
    try:
        st = W.app.test_client().get("/api/module_card").get_json()
    finally:
        (W._assignable_cards, W._module_current_card, W._card_rate,
         W._jackd_servers) = saved
    mods = {m["key"]: m for m in st.get("modules", [])}
    check("BBKit is in the MODULE SERVERS picker", "bbkit" in mods, list(mods))
    check("its choices are only the 48 kHz cards",
          mods.get("bbkit", {}).get("cards") == ["main", "pisound"],
          mods.get("bbkit"))
    check("the other voices still see every card",
          mods.get("mono", {}).get("cards") == ["main", "pisound", "M3"],
          mods.get("mono"))


def test_picker_post_refuses_wrong_rate():
    saved = (W._alsa_cards, W._assign_module_card, W._card_rate)
    called = []
    W._alsa_cards = lambda: [{"name": "M3"}, {"name": "pisound"}]
    W._assign_module_card = lambda m, c: called.append((m, c)) or (True, "")
    W._card_rate = lambda c: {"pisound": 48000, "M3": 44100}.get(c)
    try:
        c = W.app.test_client()
        r1 = c.post("/api/module_card", json={"module": "bbkit", "card": "M3"})
        r2 = c.post("/api/module_card", json={"module": "bbkit", "card": "pisound"})
    finally:
        (W._alsa_cards, W._assign_module_card, W._card_rate) = saved
    check("picker: BBKit to a 44.1 kHz card -> 400 with the reason",
          r1.status_code == 400 and "48000" in (r1.get_json() or {}).get("error", ""),
          (r1.status_code, r1.get_json()))
    check("picker: BBKit to the pisound goes through the helper",
          r2.status_code == 200 and called == [("bbkit", "pisound")],
          (r2.status_code, called))


def test_jack_hiccup_is_try_again():
    for down, src, dst in ((None, "ORC1-Drum:out_L", "system:playback_1"),
                           ("pisound", "pisound::ORC1-Mono:out_L",
                            "pisound::system:playback_1")):
        f = Fake(down={down})
        with MoveRig(f) as rig:
            code, body = rig.post({"op": "connect", "src": [src], "dst": [dst]})
        check("JACK not answering (%s) -> 503 try again, not 'ports do not exist'"
              % (down or "main"),
              code == 503 and "try again" in body.get("error", "")
              and rig.assigned == [], (code, body))


def test_bbkit_host_server_choice():
    import importlib
    B = importlib.import_module("orc1_bbkit_host")

    class Client:
        def __init__(self, name, servername=None):
            if servername == "gone":
                raise RuntimeError("no server")
            self.servername = servername
            self.samplerate = {None: B.SR, "pisound": 48000, "M3": 44100}[servername]
            self.closed = False

        def close(self):
            self.closed = True

    class J:
        pass
    J.Client = Client
    saved = os.environ.get("JACK_SERVER")
    try:
        res = {}
        for srv in (None, "pisound", "M3", "gone"):
            if srv is None:
                os.environ.pop("JACK_SERVER", None)
            else:
                os.environ["JACK_SERVER"] = srv
            res[srv] = B._open_client(J).servername
    finally:
        if saved is None:
            os.environ.pop("JACK_SERVER", None)
        else:
            os.environ["JACK_SERVER"] = saved
    check("BBKit host: no JACK_SERVER = the main server", res[None] is None, res)
    check("BBKit host: joins the server it was moved to (48 kHz)",
          res["pisound"] == "pisound", res)
    check("BBKit host: a server at another rate -> back to main",
          res["M3"] is None, res)
    check("BBKit host: an unreachable server -> main", res["gone"] is None, res)


def test_disconnect_across_servers_still_refused():
    f = Fake()
    with MoveRig(f) as rig:
        code, body = rig.post({"op": "disconnect", "src": ["ORC1-Drum:out_L"],
                               "dst": ["pisound::system:playback_1"]})
        check("a cross-server DISCONNECT moves nothing (400)",
              code == 400 and rig.assigned == [], (code, body))


def test_api_rows_labelled_by_server():
    f = Fake()
    saved = W._patch_card
    W._patch_card = lambda: "L12"
    try:
        d = with_fake(f, lambda: W.app.test_client().get("/api/patch").get_json())
    finally:
        W._patch_card = saved
    rows = {r["name"]: r for r in d["sources"] + d["destinations"]}
    p = rows.get("pisound::system:playback_1", {})
    check("a per-card system port is labelled by its card",
          p.get("label") == "pisound OUT 1", p)
    check("and carries its server", p.get("server") == "pisound", p)
    check("its group names the server",
          rows.get("pisound::ORC1-Mono:out_L", {}).get("client", "")
          .startswith("pisound server"), rows.get("pisound::ORC1-Mono:out_L"))
    check("main rows unchanged", rows.get("system:playback_1", {}).get("label")
          == "L12 OUT 1" and rows["system:playback_1"]["server"] == "", rows.get(
              "system:playback_1"))


def test_reconcile_never_ages_an_unreachable_server():
    tmp = tempfile.mkdtemp(prefix="patchsrv_")
    saved = (W.PATCH_FILE, W._patch_ports, W._patch_conns, W._patch_run)
    W.PATCH_FILE = os.path.join(tmp, "saved.json")
    try:
        wire = ("pisound::ORC1-Mono:out_L", "pisound::system:playback_2")
        W._patch_saved_write([W._patch_entry(*wire)])

        def ports():
            W._patch_unreachable.clear()
            W._patch_unreachable.add("pisound")
            return [{"name": "ORC1-Drum:out_L", "dir": "out", "audio": True}]
        W._patch_ports = ports
        W._patch_conns = lambda ports=None: []
        W._patch_run = lambda *a: "should not run"
        rep = W._patch_reconcile()
        e = W._patch_saved_load()
        check("its saved wire is kept", len(e) == 1 and
              (e[0]["src"], e[0]["dst"]) == wire, e)
        check("not aged toward a prune (missing_since stays None)",
              e and e[0]["missing_since"] is None, e)
        check("and reported pending", list(wire) in rep["pending"], rep)
        # Control: the same wire with the server simply GONE (not listed at
        # all, not unreachable) does start aging -- that is a real removal.
        W._patch_ports = lambda: [{"name": "ORC1-Drum:out_L", "dir": "out",
                                   "audio": True}]
        W._patch_unreachable.clear()
        W._patch_reconcile()
        e = W._patch_saved_load()
        check("control: a server that is gone does age",
              e and e[0]["missing_since"] is not None, e)
    finally:
        (W.PATCH_FILE, W._patch_ports, W._patch_conns, W._patch_run) = saved
        W._patch_unreachable.clear()
        W._patch_seen_up.clear()
        W._patch_last_error.clear()
        shutil.rmtree(tmp, ignore_errors=True)


def test_main_card_skips_named_servers():
    real = subprocess.run

    class R:
        stdout = ("ARGS\n/usr/bin/jackd -n pisound -R -dalsa -dhw:pisound -r 48000 -n 2\n"
                  "/usr/bin/jackd -R --port-max 256 -dalsa -dhw:L12 -r 48000 -n 2\n")
    subprocess.run = lambda *a, **k: R()
    try:
        check("_patch_card picks the UNNAMED server even when listed second",
              W._patch_card() == "L12", W._patch_card())
        check("_patch_servers = main + the named one",
              W._patch_servers() == [None, "pisound"], W._patch_servers())
    finally:
        subprocess.run = real


def _snap(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


if __name__ == "__main__":
    before = {p: _snap(p) for p in (REAL_PATCH_FILE, REAL_MO_FILE)}
    for t in (test_ports_cover_every_server, test_main_down_means_no_graph,
              test_card_server_down_is_unknown, test_conns_per_server,
              test_run_routes_and_refuses,
              test_connect_across_servers_moves_the_module,
              test_move_back_to_main, test_only_modules_move,
              test_bbkit_moves_to_a_48k_server,
              test_bbkit_refused_on_another_rate, test_picker_lists_per_module,
              test_picker_post_refuses_wrong_rate, test_jack_hiccup_is_try_again,
              test_bbkit_host_server_choice,
              test_disconnect_across_servers_still_refused,
              test_api_rows_labelled_by_server,
              test_reconcile_never_ages_an_unreachable_server,
              test_main_card_skips_named_servers):
        try:
            t()
        except Exception as e:
            check(t.__name__ + " raised", False, repr(e))
    check("the real saved patch was not touched",
          _snap(REAL_PATCH_FILE) == before[REAL_PATCH_FILE])
    check("the real module-outputs file was not touched",
          _snap(REAL_MO_FILE) == before[REAL_MO_FILE])
    check("every write went to the sandbox (not the real paths)",
          W.PATCH_FILE != REAL_PATCH_FILE and
          _cfg_suite.MODULE_OUT_FILE != REAL_MO_FILE)
    shutil.rmtree(_SANDBOX, ignore_errors=True)
    # +1 counts this check itself, so EXPECTED equals the printed total.
    check("every check ran (%d expected)" % EXPECTED,
          PASS + FAIL + 1 == EXPECTED, "counted %d" % (PASS + FAIL + 1))
    print("%d passed, %d failed" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)
