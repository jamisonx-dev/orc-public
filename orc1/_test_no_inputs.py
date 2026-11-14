"""The brain must keep running on a machine with NO MIDI input at all (2026-11-13).

It used to print "No MIDI inputs found." and exit 1: systemd restarted it every 5 s and each
restart stopped the six sound hosts (PartOf=orc1), so the web page's sequencer and arp had
nothing to play. Found by installing the public copy in a clean VM with no controller.
owner: "fix. it has an internal seq and arp".

Reads orc1.py as text / AST and runs run_all_inputs() alone with stubs: nothing is imported
from the brain, no MIDI port is opened, no file is written. Run anywhere: python3 _test_no_inputs.py
"""
import ast, os, sys

EXPECTED = 4
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = open(os.path.join(HERE, "orc1.py"), encoding="utf-8").read()
TREE = ast.parse(SRC)
passed = 0


def check(name, ok):
    global passed
    print(("  ok   " if ok else "  FAIL ") + name)
    passed += bool(ok)


def func(name):
    return next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == name)


main = func("main")
# the "no inputs at all" branch: the if whose test names every kind of input
want = {"din_in", "usb_ins", "mixtrack_port", "fire_port"}
branch = [n for n in main.body if isinstance(n, ast.If)
          and want <= {x.id for x in ast.walk(n.test) if isinstance(x, ast.Name)}]
check("main() still has ONE 'no MIDI inputs at all' branch", len(branch) == 1)
body = branch[0].body if branch else []
calls = [ast.unparse(c.func) for s in body for c in ast.walk(s) if isinstance(c, ast.Call)]
leaves = [s for b in body for s in ast.walk(b) if isinstance(s, (ast.Raise, ast.Return))]
check("that branch does not exit, raise or return (calls: %s)" % calls,
      bool(branch) and not leaves and not any(c in ("sys.exit", "exit", "quit", "os._exit") for c in calls))
check("that branch says so in the journal", "print" in calls)

# run_all_inputs with nothing to read must start no thread and just wait
started = []


class _Thread:
    def __init__(self, *a, **k): started.append((a, k))
    def start(self): pass


class _Time:
    @staticmethod
    def sleep(_):
        raise KeyboardInterrupt        # one pass of the wait loop is enough


ns = {"_pedal_ports": [], "threading": type("T", (), {"Thread": _Thread}), "time": _Time,
      "state": {"din_routing": "x"}, "print": lambda *a, **k: None}
exec(compile(ast.Module(body=[func("run_all_inputs")], type_ignores=[]), "orc1.py", "exec"), ns)
try:
    ns["run_all_inputs"](None, [], None, None)
    ok = not started
except BaseException as e:             # noqa: a crash here is the failure being tested for
    ok = False
    print("    run_all_inputs raised", repr(e))
check("run_all_inputs(no inputs) waits without starting a thread or failing", ok)

print(f"\n{passed}/{EXPECTED} passed")
sys.exit(0 if passed == EXPECTED else 1)
