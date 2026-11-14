# Python 2.7 on the box: print the ON-SCREEN page of fb0 (raw BGRA) to stdout.
import fcntl, mmap, os, struct, sys
fd = os.open("/dev/fb0", os.O_RDONLY)
v = fcntl.ioctl(fd, 0x4600, b"\0" * 160)
w, h, xv, yv, xo, yo = struct.unpack("6I", v[:24])
line = struct.unpack("I", fcntl.ioctl(fd, 0x4602, b"\0" * 80)[44:48])[0]
m = mmap.mmap(fd, line * yv, prot=mmap.PROT_READ)
sys.stderr.write("%d %d %d\n" % (w, h, line))
sys.stdout.write(m[line * yo:line * (yo + h)])
