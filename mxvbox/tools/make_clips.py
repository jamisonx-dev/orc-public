"""Add video clips to the MXV box's own clip LIBRARY, on its card (PC side; ffmpeg + ssh).

    py make_clips.py VIDEO [VIDEO ...] [--seconds 12] [--replace] [--dry-run]
    py make_clips.py --list
    (or drag videos onto "Box clips.bat")

For each video:
  - the first SECONDS at 15 fps, scaled to FILL 640x360 (centre crop: portrait clips are
    cropped, not letterboxed), contrast/colour pushed up -> clip.etc: COMPRESSED frames (ETC1,
    the box GPU's own format: 4x the old picture in half its RAM; 2026-11-02, owner: "let's go
    new"). Needs the etcpak package (pip install etcpak); without it the old kind is made:
    clip.raw, raw BGRA frames at 320x180.
  - meta.json (name, frames, bytes)
  No blur copies: since 2026-11-02 the box's GPU makes every blur live, from the clip itself.
into /storage/orc/lib/<name>/ on the BOX (sent through the Pi: the box is only on the
Pi's eth0). A clip of the same name is replaced. The box notices within ~5 s, adds it to
its index (lib/index.json) and copies clips from its card into its RAM slots itself while
it plays (owner: "i like the idea of the box being able to handle itself").
Limits: the box's card (a 12 s clip is ~21 MB compressed, ~42 MB raw), and one clip must fit a RAM slot (MAX_CLIP:
~50 s).
"""
import argparse, json, os, re, shutil, subprocess, tarfile, tempfile, time

W, H, FPS = 320, 180, 15               # the old raw clips
EW, EH = 640, 360                      # compressed clips: the box GPU's surface (gpu.Etc)
MAX_CLIP = 180 * 1024 * 1024           # one clip must fit a box slot
PI = "pibox@pibox.local"
LIB = "/storage/orc/lib"               # on the box
LOOK = "eq=contrast=1.35:saturation=1.6"


def fill(w, h):
    return "scale=%d:%d:force_original_aspect_ratio=increase:flags=area,crop=%d:%d" % (w, h, w, h)


FILL = fill(W, H)


def etc_encoder():
    """etcpak's ETC1 encoder, or None when it isn't installed. It is handed the BGRA bytes as
    they are: the box's GPU then sees them exactly like a raw clip (measured on the box)."""
    try:
        import etcpak
    except ImportError:
        return None
    return etcpak.compress_etc1_rgb


def slug(path):
    s = re.sub(r"[^a-z0-9]+", "_", os.path.splitext(os.path.basename(path))[0].lower()).strip("_")
    return s[:40] or "clip"


def ffmpeg(src, seconds, vf, out):
    r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", src, "-t", str(seconds), "-an",
                        "-vf", vf, "-f", "rawvideo", out], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
        raise SystemExit("ffmpeg could not read %s:\n%s" % (src, r.stderr.strip()[-600:]))


def ffmpeg_etc(src, seconds, out, encode):
    """ffmpeg's EW x EH BGRA frames, each compressed as it comes, into out. Returns the frames."""
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", src, "-t", str(seconds), "-an",
                          "-vf", "fps=%d,%s,%s,format=bgra" % (FPS, fill(EW, EH), LOOK), "-f", "rawvideo", "-"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    fs, n = EW * EH * 4, 0
    with open(out, "wb") as f:
        while True:
            fr = p.stdout.read(fs)
            if len(fr) < fs:
                break
            f.write(encode(fr, EW, EH))
            n += 1
    err = p.stderr.read().decode(errors="replace")
    p.wait()
    if p.returncode != 0 or n == 0:
        raise SystemExit("ffmpeg could not read %s:\n%s" % (src, err.strip()[-600:]))
    return n


def build_one(src, seconds, folder, encode=None):
    """One video -> folder/clip.etc (or clip.raw without etcpak, or with encode=False) + meta.json.
    Returns (frames, size)."""
    os.makedirs(folder)
    if encode is None:
        encode = etc_encoder()
        if encode is None:
            print("   (etcpak is not installed: making the old 320x180 kind; pip install etcpak for sharp clips)")
    if encode:
        sharp = os.path.join(folder, "clip.etc")
        frames = ffmpeg_etc(src, seconds, sharp, encode)
    else:
        sharp = os.path.join(folder, "clip.raw")
        ffmpeg(src, seconds, "fps=%d,%s,%s,format=bgra" % (FPS, FILL, LOOK), sharp)
        frames = os.path.getsize(sharp) // (W * H * 4)
    size = os.path.getsize(sharp)
    with open(os.path.join(folder, "meta.json"), "w") as f:
        json.dump({"name": os.path.basename(src), "frames": frames,
                   "bytes": size, "added": int(time.time())}, f)
    return frames, size


def quote(cmd):
    """cmd as ONE single-quoted shell word (for the Pi to hand on to the box)."""
    return "'" + cmd.replace("'", "'\\''") + "'"


def ssh(cmd, data=None, timeout=60):
    """Run cmd ON THE BOX (via the Pi, which reaches it as `ssh mxvbox`)."""
    return subprocess.run(["ssh", "-o", "BatchMode=yes", PI, "ssh -o BatchMode=yes mxvbox " + quote(cmd)],
                          input=data, capture_output=True, timeout=timeout)


def box_free():
    r = ssh("df -k /storage | tail -1")
    try:
        return int(r.stdout.decode().split()[3]) * 1024
    except (IndexError, ValueError):
        raise SystemExit("could not read the box's free space (is it on? press its power button)")


def push(work, names, replace):
    """Send the built clip folders into the Pi's library (replacing same-named ones)."""
    clear = "rm -rf %s/* && " % LIB if replace else ""
    remote = ("%smkdir -p %s && cd %s && rm -rf %s && tar xf - && ls -d */ | wc -l"
              % (clear, LIB, LIB, " ".join(names)))
    p = subprocess.Popen(["ssh", "-o", "BatchMode=yes", PI, "ssh -o BatchMode=yes mxvbox " + quote(remote)],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with tarfile.open(fileobj=p.stdin, mode="w|") as tar:
        for n in names:
            tar.add(os.path.join(work, n), arcname=n)
    p.stdin.close()
    out, err = p.communicate(timeout=1800)
    if p.returncode != 0:
        raise SystemExit("copying to the box failed (is it on?):\n%s" % err.decode(errors="replace").strip()[-600:])
    return out.decode().strip()


def list_library():
    r = ssh('cd %s 2>/dev/null && for d in */; do cat "${d}meta.json"; echo; done' % LIB)
    rows = [json.loads(ln) for ln in r.stdout.decode().splitlines() if ln.strip()]
    for m in rows:
        print("  %-44s %5.1f s  %4.0f MB" % (m["name"], m["frames"] / float(FPS), m["bytes"] / 2**20))
    print("%d clips, %.0f MB in the box's library, %.0f MB free on its card"
          % (len(rows), sum(m["bytes"] for m in rows) / 2**20, box_free() / 2**20))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("videos", nargs="*")
    ap.add_argument("--seconds", type=float, default=12.0, help="how much of each video (default 12)")
    ap.add_argument("--replace", action="store_true", help="empty the library first")
    ap.add_argument("--dry-run", action="store_true", help="convert and check sizes, copy nothing")
    ap.add_argument("--list", action="store_true", help="show what is in the library")
    a = ap.parse_args()
    if a.list:
        list_library()
        return
    if not a.videos:
        ap.error("give one or more videos (or --list)")
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg is not on the PATH")
    for v in a.videos:
        if not os.path.isfile(v):
            raise SystemExit("not a file: %s" % v)
    work = tempfile.mkdtemp(prefix="boxclips_")
    try:
        names, seen = [], set()
        for src in a.videos:
            name = slug(src)
            while name in seen:
                name += "_"
            seen.add(name)
            print(os.path.basename(src), flush=True)
            frames, size = build_one(src, a.seconds, os.path.join(work, name))
            print("   %d frames (%.1f s), %.0f MB" % (frames, frames / float(FPS), size / 2**20), flush=True)
            if size > MAX_CLIP:
                raise SystemExit("TOO BIG for one box slot (%.0f MB, max %.0f): use --seconds lower"
                                 % (size / 2**20, MAX_CLIP / 2**20))
            names.append(name)
        if a.dry_run:
            print("dry run: nothing copied")
            return
        need = sum(os.path.getsize(os.path.join(dp, f)) for dp, _d, fs in os.walk(work) for f in fs)
        free = box_free()
        if not a.replace and need > free - 200 * 2**20:        # keep 200 MB spare on the card
            raise SystemExit("the box's card is too full: %.0f MB needed, %.0f MB free (use --replace "
                             "or fewer clips)" % (need / 2**20, free / 2**20))
        count = push(work, names, a.replace)
        print("done: %d added, the box's library now holds %s clips; it picks them up within seconds"
              % (len(names), count))
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
