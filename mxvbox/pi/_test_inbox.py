"""_test_inbox.py -- the Samba box inbox's logic (python3, Pi or PC; no box, no ffmpeg).

    python3 _test_inbox.py

HOME / USERPROFILE sandboxed before import; conversion, sending and the card check are
fakes; a temp folder stands in for the share.
"""
import os, sys, tempfile

SANDBOX = tempfile.mkdtemp(prefix="mxvbox_inbox_test_")
os.environ["HOME"] = os.environ["USERPROFILE"] = SANDBOX
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import inbox as I                       # noqa: E402

EXPECTED = 20
passed = failed = 0


def check(cond, label, extra=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("  FAIL:", label, extra)


check(I.INBOX.startswith(SANDBOX), "the inbox path is in the sandbox", I.INBOX)
root = os.path.join(SANDBOX, "share")
I.folders(root)
check(all(os.path.isdir(os.path.join(root, d)) for d in ("done", "failed"))
      and not os.path.exists(os.path.join(root, "blur")), "the share's folders exist (no blur\\ any more)")


def drop(rel, size):
    p = os.path.join(root, rel)
    with open(p, "wb") as f:
        f.write(b"v" * size)
    return p


seen = {}
a = drop("skull.mp4", 100)
b = drop("pills.MOV", 100)
drop("notes.txt", 10)
drop("~$temp.mp4", 10)
check(I.ready_files(root, seen) == [], "a file seen for the first time is not taken (it may still be copying)")
with open(a, "ab") as f:
    f.write(b"more")                    # still growing
got = I.ready_files(root, seen)
check(got == [b], "the next scan takes only files whose size stopped changing", got)
got = I.ready_files(root, seen)
check(sorted(got) == sorted([a, b]), "once stable, both are taken; non-videos and temp files never", got)

calls = []
ok_convert = lambda src, secs, folder: (os.makedirs(folder) or calls.append(("conv", secs))) or (180, 50 * 2 ** 20)
ok_send = lambda folder, name: calls.append(("send", name))
roomy = lambda: 5 * 2 ** 30
msg = I.handle(a, convert=ok_convert, send=ok_send, free=roomy, root=root)
check(msg.startswith("added skull") and calls == [("conv", I.SECONDS), ("send", "skull")]
      and os.path.exists(os.path.join(root, "done", "skull.mp4")) and not os.path.exists(a),
      "a good video: converted, sent to the box, the original moved to done\\", (msg, calls))
check("added skull" in open(os.path.join(root, "STATUS.txt")).read(), "STATUS.txt says what happened")


def bad_convert(src, secs, folder):
    raise SystemExit("ffmpeg could not read it")


msg = I.handle(b, convert=bad_convert, send=ok_send, free=roomy, root=root)
why = os.path.join(root, "failed", "pills.MOV.why.txt")
check(msg.startswith("failed") and os.path.exists(why) and "ffmpeg could not read" in open(why).read(),
      "an unreadable video goes to failed\\ with a .why.txt", msg)
c = drop("big.mp4", 10)
msg = I.handle(c, convert=lambda s, t, f: (os.makedirs(f), (900, I.MC.MAX_CLIP + 1))[1],
               send=ok_send, free=roomy, root=root)
check(msg.startswith("failed") and "too big" in open(os.path.join(root, "failed", "big.mp4.why.txt")).read(),
      "a clip too big for a box slot is refused (failed\\, with the reason)")
d = drop("later.mp4", 10)


def down(folder, name):
    raise OSError("box not answering")


try:
    I.handle(d, convert=ok_convert, send=down, free=roomy, root=root)
    raised = False
except OSError:
    raised = True
check(raised and os.path.exists(d), "the box is off: the video STAYS in the inbox and is retried later")
# box_free with no answer (2026-11-03 review): it raised IndexError / TimeoutExpired, so handle()
# moved a good video to failed/. Now every no-answer is an OSError, checked BEFORE converting.
real_run = I.subprocess.run


class _R(object):
    returncode, stdout = 255, ""


def no_answer(*a, **k):
    return _R()


def hang(*a, **k):
    raise I.subprocess.TimeoutExpired("ssh", 20)


class _Empty(object):
    returncode, stdout = 0, ""


kinds = []
for fake in (no_answer, hang, lambda *a, **k: _Empty()):
    I.subprocess.run = fake
    try:
        I.box_free()
    except OSError:
        kinds.append("OSError")
    except Exception as x:
        kinds.append(type(x).__name__)
I.subprocess.run = real_run
check(kinds == ["OSError"] * 3, "the box not answering (no reply / a timeout / an empty reply) is an OSError", kinds)
f2 = drop("later.mp4", 5)
del calls[:]
try:
    I.handle(f2, convert=ok_convert, send=ok_send, free=lambda: (_ for _ in ()).throw(OSError("off")), root=root)
except OSError:
    pass
check(os.path.exists(f2) and not calls, "box off: the video stays, and is not converted for nothing", calls)
e = drop("full.mp4", 10)
msg = I.handle(e, convert=ok_convert, send=ok_send, free=lambda: 100 * 2 ** 20, root=root)
check(msg.startswith("failed") and "card is too full" in open(os.path.join(root, "failed", "full.mp4.why.txt")).read(),
      "the box's card is full: refused with the reason")
dup = drop("skull.mp4", 5)
I.move(dup, os.path.join(root, "done"))
check(len([n for n in os.listdir(os.path.join(root, "done")) if n.startswith("skull")]) == 2,
      "a second file of the same name in done\\ does not overwrite the first")

# ---- "on the box\": one file per clip; deleting a file deletes the clip ---------------
box = [{"id": "aa", "name": "aa.mp4", "frames": 30}, {"id": "bb", "name": "bb.mp4", "frames": 15}]
gone = []
idx = lambda: [c for c in box if c["id"] not in gone]
dele = lambda cid: gone.append(cid)
folder = os.path.join(root, I.ON_BOX)
check(I.sync_on_box(root, idx, dele) == [] and sorted(os.listdir(folder)) == ["aa.txt", "bb.txt"]
      and "Delete this file" in open(os.path.join(folder, "aa.txt")).read(),
      "first sync: a file per clip on the box, nothing deleted")
os.remove(os.path.join(folder, "aa.txt"))
check(I.sync_on_box(root, idx, dele) == ["aa"] and gone == ["aa"] and os.listdir(folder) == ["bb.txt"],
      "deleting a clip's file deletes that clip from the box")
box.append({"id": "cc", "name": "cc.mp4", "frames": 45})
I.sync_on_box(root, idx, dele)
check(sorted(os.listdir(folder)) == ["bb.txt", "cc.txt"] and gone == ["aa"],
      "a clip added to the box (by the inbox or the tool) gets its file; nothing else deleted")
gone.append("bb")                                    # deleted elsewhere (the BOX tab)
I.sync_on_box(root, idx, dele)
check(os.listdir(folder) == ["cc.txt"] and gone == ["aa", "bb"], "a clip deleted elsewhere: its file is tidied away")
import shutil                                        # noqa: E402
shutil.rmtree(folder)
check(I.sync_on_box(root, idx, dele) == [] and os.listdir(folder) == ["cc.txt"],
      "the whole folder deleted: recreated, NOT read as 'delete everything'")
try:
    I.delete_on_box("../etc")
    bad = False
except ValueError:
    bad = True
check(bad, "a clip id that is not a plain slug is refused (no path tricks)")

print("%d passed, %d failed  (expected %d)" % (passed, failed, EXPECTED))
if passed + failed != EXPECTED:
    print("COUNT DRIFT: ran %d checks, EXPECTED = %d" % (passed + failed, EXPECTED))
sys.exit(0 if failed == 0 and passed + failed == EXPECTED else 1)
