"""The box inbox (Pi, python3; service orc1-boxinbox): drag videos into a Samba share,
they end up on the MXV box's card (owner 2026-10-29: "we could do drag and drop samba").

    \\\\pibox\\boxinbox\\            drop videos here -> clips (every blur is the box's GPU, live)
    done\\  failed\\  STATUS.txt      what happened (failed\\ gets a .why.txt next to the file)
    on the box\\                    one .txt per clip on the box: DELETE one = delete that clip

A file is taken once its size stopped changing (a Samba copy still in progress is left
alone), converted with the same code as the PC tool (mxvbox/tools/make_clips.py: 320x180,
15 fps, fills the frame, 12 s), sent to the box's library (/storage/orc/lib) at 3 MB/s,
and the original moved to done\\. The box picks the new clip up by itself within seconds.

The service runs at idle CPU + IO priority: conversion only ever uses what nothing else
wants; skipback's recorder (SCHED_FIFO) is far above it.
"""
import io, json, os, shutil, subprocess, sys, tarfile, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import make_clips as MC                     # noqa: E402  the PC tool's converter, shared

INBOX = os.path.expanduser("~/box_inbox")
VIDEO = (".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".gif", ".mpg", ".mpeg", ".wmv")
SECONDS = 12.0
RATE = 3000000
BOX_LIB = "/storage/orc/lib"
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
       "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3", "mxvbox"]
BOX_STATUS = "/dev/shm/orc1_mxvbox_status.json"     # the conductor writes the box's address here


def ssh(status=BOX_STATUS):
    """SSH, at the address the conductor uses for the box (not mxvbox.local: mDNS drops out)."""
    try:
        with open(status) as f:
            addr = json.load(f).get("addr")
        if addr and all(c in "0123456789." for c in addr):
            return SSH[:-1] + ["-o", "HostName=%s" % addr, "mxvbox"]
    except (OSError, ValueError):
        pass
    return list(SSH)
SCAN_S = 10.0
_seen = {}                                  # path -> size at the last scan


def folders(root=INBOX):
    for sub in ("", "done", "failed"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)


def ready_files(root=INBOX, seen=None):
    """[path] of videos whose size did not change since the last scan."""
    seen = _seen if seen is None else seen
    out, now_seen = [], {}
    try:
        names = sorted(os.listdir(root))
    except OSError:
        names = []
    for n in names:
        p = os.path.join(root, n)
        if n.startswith((".", "~$")) or not n.lower().endswith(VIDEO) or not os.path.isfile(p):
            continue
        size = os.path.getsize(p)
        now_seen[p] = size
        if size > 0 and seen.get(p) == size:
            out.append(p)
    seen.clear()
    seen.update(now_seen)
    return out


class _Paced(object):
    def __init__(self, raw, rate):
        self.raw, self.rate, self.t0, self.n = raw, float(rate), time.time(), 0

    def write(self, b):
        for i in range(0, len(b), 65536):
            chunk = b[i:i + 65536]
            self.raw.write(chunk)
            self.n += len(chunk)
            ahead = self.n / self.rate - (time.time() - self.t0)
            if ahead > 0:
                time.sleep(ahead)
        return len(b)


def box_free():
    """Free bytes on the box's storage. Any way of not getting an answer is an OSError (= the box is
    off: the video waits in the inbox). Until 2026-11-03 an empty answer or a timeout raised something
    else, and a video that had converted fine was moved to failed/."""
    try:
        r = subprocess.run(ssh() + ["df -k /storage | tail -1"], capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            raise OSError("the box did not answer (ssh %d)" % r.returncode)
        return int(r.stdout.split()[3]) * 1024
    except subprocess.TimeoutExpired:
        raise OSError("the box did not answer in 20 s")
    except (IndexError, ValueError):
        raise OSError("the box gave no free-space answer")


def send_to_box(folder, name, rate=RATE):
    """folder (clip.raw [+ clip_yL.raw] + meta.json) -> the box's /storage/orc/lib/<name>."""
    remote = "mkdir -p {lib} && cd {lib} && rm -rf {n} && tar xf -".format(lib=BOX_LIB, n=name)
    p = subprocess.Popen(ssh() + [remote], stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=_Paced(p.stdin, rate), mode="w|") as tar:
            tar.add(folder, arcname=name)
    finally:
        try:
            p.stdin.close()
        except (OSError, ValueError):
            pass
        err = p.stderr.read()
        try:
            p.wait(timeout=60)
        except subprocess.TimeoutExpired:
            p.kill()
            raise OSError("the box stopped answering while sending")
    if p.returncode != 0:
        raise OSError("send to the box failed: %s" % err.decode(errors="replace").strip()[-200:])


def unique_name(path):
    return MC.slug(path)


def move(src, dest_dir, note=None):
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(src))
    if os.path.exists(dest):
        base, ext = os.path.splitext(dest)
        dest = "%s_%d%s" % (base, int(time.time()), ext)
    shutil.move(src, dest)
    if note:
        with open(dest + ".why.txt", "w") as f:
            f.write(note + "\n")
    return dest


def status(line, root=INBOX):
    try:
        with open(os.path.join(root, "STATUS.txt"), "w") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S  ") + line + "\r\n")   # CRLF: read in Windows Notepad
    except OSError:
        pass


def handle(path, convert=MC.build_one, send=send_to_box, free=box_free, root=INBOX):
    """One video: convert -> send -> done/ (or failed/ with the reason). Returns the outcome."""
    name = unique_name(path)
    work = tempfile.mkdtemp(prefix="boxinbox_")
    try:
        room = free()                              # box off = OSError now, before minutes of converting
        status("converting %s" % os.path.basename(path), root)
        try:
            frames, size = convert(path, SECONDS, os.path.join(work, name))
        except SystemExit as e:                    # the converter reports ffmpeg errors this way
            raise ValueError(str(e))
        if size > MC.MAX_CLIP:
            raise ValueError("too big for one box slot (%.0f MB, max %.0f): use a shorter clip"
                             % (size / 2 ** 20, MC.MAX_CLIP / 2 ** 20))
        if size > room - 200 * 2 ** 20:
            raise ValueError("the box's card is too full")
        status("sending %s to the box (%.0f MB)" % (name, size / 2 ** 20), root)
        send(os.path.join(work, name), name)
        move(path, os.path.join(root, "done"))
        msg = "added %s to the box (%.1f s)" % (name, frames / 15.0)
        status(msg + ". Drop more any time.", root)
        return msg
    except OSError as e:                           # the box is off / unreachable: try again later
        status("waiting for the box (%s); %s stays in the inbox" % (e, os.path.basename(path)), root)
        raise
    except Exception as e:                          # this file is the problem: move it aside
        move(path, os.path.join(root, "failed"), "%s: %s" % (type(e).__name__, e))
        status("FAILED %s: %s (moved to failed)" % (os.path.basename(path), e), root)
        return "failed %s: %s" % (name, e)
    finally:
        shutil.rmtree(work, ignore_errors=True)


ON_BOX = "on the box"                       # the share's view of the box's library
KNOWN = ".on_the_box.json"                  # ids we made a file for (so a MISSING file = deleted)


def box_index():
    """The box's own library index (id, name, frames), or raise OSError."""
    r = subprocess.run(ssh() + ["cat %s/index.json" % BOX_LIB], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        raise OSError("box not answering: %s" % r.stderr.strip()[-200:])
    return json.loads(r.stdout or "[]")


def delete_on_box(cid):
    if not cid.replace("_", "").isalnum():                 # ids are slugs: never a path
        raise ValueError("bad clip id %r" % cid)
    r = subprocess.run(ssh() + ["rm -rf %s/%s" % (BOX_LIB, cid)], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise OSError("could not delete %s on the box: %s" % (cid, r.stderr.strip()[-200:]))


def sync_on_box(root=INBOX, index=box_index, delete=delete_on_box):
    """Keep root/'on the box' = one small .txt per clip on the box. A file the user
    DELETED (one we made before) deletes that clip from the box (owner: "allow me to
    have some way of deleting videos from the list"). A missing FOLDER is recreated,
    never read as 'delete everything'. Returns the ids deleted."""
    folder = os.path.join(root, ON_BOX)
    known_path = os.path.join(root, KNOWN)
    try:
        known = set(json.load(open(known_path)))
    except (OSError, ValueError):
        known = set()
    if not os.path.isdir(folder):
        os.makedirs(folder)
        known = set()                                     # recreated: nothing counts as deleted
    clips = index()
    have = {}
    for n in os.listdir(folder):
        if n.endswith(".txt"):
            have[n[:-4]] = n
    deleted = []
    for c in clips:
        cid = c["id"]
        if cid in known and cid not in have:              # the user deleted its file
            delete(cid)
            deleted.append(cid)
    for c in clips:
        cid = c["id"]
        if cid in deleted or cid in have:
            continue
        with open(os.path.join(folder, cid + ".txt"), "w") as f:
            f.write("%s  %.1f s\r\n\r\nDelete this file to delete the clip from the box.\r\n"
                    % (c.get("name", cid), c.get("frames", 0) / 15.0))
    on_box = set(c["id"] for c in clips) - set(deleted)
    for cid, n in have.items():
        if cid not in on_box:                             # gone from the box (deleted elsewhere)
            try:
                os.remove(os.path.join(folder, n))
            except OSError:
                pass
    with open(known_path, "w") as f:
        json.dump(sorted(on_box), f)
    return deleted


def main():
    folders()
    status("watching. Drop videos here.")
    print("box inbox: watching %s" % INBOX, flush=True)
    while True:
        try:
            for path in ready_files():
                print("box inbox:", handle(path), flush=True)
            for cid in sync_on_box():
                print("box inbox: deleted %s from the box (its file was deleted)" % cid, flush=True)
                status("deleted %s from the box" % cid)
            pause = SCAN_S
        except OSError as e:
            print("box inbox: box unreachable (%s), retrying in 60 s" % e, flush=True)
            pause = 60.0
        except Exception as e:                        # never die: log, wait, carry on
            print("box inbox: error %s: %s" % (type(e).__name__, e), flush=True)
            pause = 30.0
        time.sleep(pause)


if __name__ == "__main__":
    main()
