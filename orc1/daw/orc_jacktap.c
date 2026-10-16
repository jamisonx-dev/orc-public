/* orc_jacktap -- a listen-only JACK client for the VISUALS renderer.
 *
 * WHY C: the renderer (orc_visuals.py) is Python doing numpy + OpenGL work.
 * A Python JACK callback needs the GIL every period, so a busy renderer
 * would make the callback late, and a late client makes the WHOLE graph miss
 * its deadline -- skipback included (the 2026-09-29 xrun hunt). Here the
 * realtime callback only copies each period into shared memory: no locks,
 * no allocation, no syscalls, no Python. The renderer reads that memory.
 *
 * It never outputs audio (input ports only), so it cannot change the sound.
 *
 * Shared memory /dev/shm/orc1_vistap (struct tap_hdr, then float32 frames,
 * interleaved stereo, a ring of CAP frames):
 *   wpos   frames written since start; written AFTER the period's samples
 *          with release ordering, so a reader that sees wpos sees the data
 *   mono   CLOCK_MONOTONIC seconds of the newest period (freshness)
 *   conn   how many of our 2 ports have a source (0 = nothing to hear)
 *   alive  1 while running, 0 after a clean exit
 * A reader copies the newest N frames, then re-reads wpos: if the writer
 * lapped the copied span (CAP = 16384 frames = 341 ms at 48 kHz), it drops
 * that copy.
 *
 * Source: ~/.orc1_vistap, line 1 = the port for LEFT, line 2 = RIGHT (blank
 * = the left one again, for a mono source). Missing file = system:capture_1
 * + _2. "ring" on line 1 = connect nothing (the renderer then falls back to
 * skipback's RAM ring). Re-read every 0.5 s, and a source that vanishes
 * (a module host restarting) is re-connected when it comes back.
 *
 * Stop it with SIGTERM/SIGINT: it closes its JACK client cleanly. Killing a
 * JACK client (SIGKILL) can xrun the graph (MEASURED 2026-09-29, Mixxx).
 *
 * Build: daw/jacktap_build.sh (gcc, -ljack). Run by orc1-vistap.service.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <jack/jack.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define SHM_NAME "/orc1_vistap"
#define CAP 16384u                  /* frames, power of two */
#define CH 2

struct tap_hdr {                    /* 64 bytes; orc_visuals.py TapShm reads it */
    char     magic[8];              /* "ORCTAP1\0" */
    uint32_t rate;
    uint32_t channels;
    uint32_t cap;
    uint32_t period;
    uint64_t wpos;
    double   mono;
    uint32_t conn;
    uint32_t alive;
    uint32_t xruns;
    uint32_t pid;
    uint32_t pad[2];
};

_Static_assert(sizeof(struct tap_hdr) == 64, "tap_hdr is read as 64 bytes");

static struct tap_hdr *hdr;
static float *ring;
static jack_port_t *in[CH];
static jack_client_t *client;
static volatile sig_atomic_t running = 1;
static volatile sig_atomic_t server_gone;

static void on_signal(int sig) { (void)sig; running = 0; }
static void on_shutdown(void *arg) { (void)arg; server_gone = 1; running = 0; }
static int on_xrun(void *arg) { (void)arg; hdr->xruns++; return 0; }

static int process(jack_nframes_t n, void *arg)
{
    (void)arg;
    const jack_default_audio_sample_t *a = jack_port_get_buffer(in[0], n);
    const jack_default_audio_sample_t *b = jack_port_get_buffer(in[1], n);
    uint64_t w = hdr->wpos;
    for (jack_nframes_t i = 0; i < n; i++) {
        float *f = ring + ((w + i) & (CAP - 1)) * CH;
        f[0] = a[i];
        f[1] = b[i];
    }
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);   /* vDSO: no syscall */
    hdr->mono = ts.tv_sec + ts.tv_nsec * 1e-9;
    hdr->period = n;
    __atomic_store_n(&hdr->wpos, w + n, __ATOMIC_RELEASE);
    return 0;
}

/* ---- source file ---------------------------------------------------- */

static char want[CH][256];          /* "" = nothing */

static void read_source(const char *path)
{
    char l1[256] = "", l2[256] = "";
    FILE *f = fopen(path, "r");
    if (!f) {
        strcpy(want[0], "system:capture_1");
        strcpy(want[1], "system:capture_2");
        return;
    }
    if (!fgets(l1, sizeof l1, f)) l1[0] = 0;
    if (!fgets(l2, sizeof l2, f)) l2[0] = 0;
    fclose(f);
    l1[strcspn(l1, "\r\n")] = 0;
    l2[strcspn(l2, "\r\n")] = 0;
    if (!strcmp(l1, "ring")) l1[0] = 0;
    strcpy(want[0], l1);
    strcpy(want[1], l2[0] ? l2 : l1);
}

/* Make each of our ports fed by exactly want[c]. Runs on the main thread. */
static void apply_source(void)
{
    unsigned conn = 0;
    for (int c = 0; c < CH; c++) {
        const char *me = jack_port_name(in[c]);
        const char **cur = jack_port_get_all_connections(client, in[c]);
        int have = 0;
        for (int i = 0; cur && cur[i]; i++) {
            if (want[c][0] && !strcmp(cur[i], want[c]))
                have = 1;
            else
                jack_disconnect(client, cur[i], me);
        }
        if (cur) jack_free(cur);
        if (!have && want[c][0] && jack_port_by_name(client, want[c]))
            have = jack_connect(client, want[c], me) == 0;
        conn += have;
    }
    hdr->conn = conn;
}

int main(int argc, char **argv)
{
    const char *name = argc > 1 ? argv[1] : "orc1-vistap";
    char path[512];
    snprintf(path, sizeof path, "%s/.orc1_vistap",
             getenv("HOME") ? getenv("HOME") : "/home/pibox");

    size_t bytes = sizeof(struct tap_hdr) + (size_t)CAP * CH * sizeof(float);
    int fd = shm_open(SHM_NAME, O_CREAT | O_RDWR, 0644);
    if (fd < 0 || ftruncate(fd, bytes) < 0) {
        perror("orc_jacktap: shm");
        return 1;
    }
    void *mem = mmap(NULL, bytes, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    close(fd);
    if (mem == MAP_FAILED) {
        perror("orc_jacktap: mmap");
        return 1;
    }
    memset(mem, 0, bytes);
    hdr = mem;
    ring = (float *)(hdr + 1);
    memcpy(hdr->magic, "ORCTAP1", 8);
    hdr->channels = CH;
    hdr->cap = CAP;
    hdr->pid = (uint32_t)getpid();
    /* no page faults in the callback */
    if (mlockall(MCL_CURRENT | MCL_FUTURE) != 0)
        perror("orc_jacktap: mlockall (continuing)");

    jack_status_t st;
    client = jack_client_open(name, JackNoStartServer, &st);
    if (!client) {
        fprintf(stderr, "orc_jacktap: no JACK server (status 0x%x)\n", st);
        return 1;
    }
    in[0] = jack_port_register(client, "in_L", JACK_DEFAULT_AUDIO_TYPE, JackPortIsInput, 0);
    in[1] = jack_port_register(client, "in_R", JACK_DEFAULT_AUDIO_TYPE, JackPortIsInput, 0);
    if (!in[0] || !in[1]) {
        fprintf(stderr, "orc_jacktap: cannot register ports\n");
        jack_client_close(client);
        return 1;
    }
    hdr->rate = jack_get_sample_rate(client);
    jack_set_process_callback(client, process, NULL);
    jack_set_xrun_callback(client, on_xrun, NULL);
    jack_on_shutdown(client, on_shutdown, NULL);

    struct sigaction sa = {0};
    sa.sa_handler = on_signal;
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGHUP, &sa, NULL);

    if (jack_activate(client)) {
        fprintf(stderr, "orc_jacktap: cannot activate\n");
        jack_client_close(client);
        return 1;
    }
    hdr->alive = 1;
    printf("orc_jacktap: %s up, %u Hz, source file %s\n", name, hdr->rate, path);
    fflush(stdout);

    struct timespec last_m = {0, 0};
    int first = 1;
    while (running) {
        struct stat sb;
        int have = stat(path, &sb) == 0;
        if (first || (have && (sb.st_mtim.tv_sec != last_m.tv_sec ||
                               sb.st_mtim.tv_nsec != last_m.tv_nsec)) ||
            (!have && last_m.tv_sec)) {
            read_source(path);
            last_m = have ? sb.st_mtim : (struct timespec){0, 0};
            first = 0;
            printf("orc_jacktap: source L=%s R=%s\n",
                   want[0][0] ? want[0] : "(none)", want[1][0] ? want[1] : "(none)");
            fflush(stdout);
        }
        apply_source();
        usleep(500000);
    }

    hdr->alive = 0;
    if (!server_gone) {
        jack_deactivate(client);
        jack_client_close(client);
    }
    shm_unlink(SHM_NAME);
    printf("orc_jacktap: stopped%s\n", server_gone ? " (JACK went away)" : "");
    return server_gone ? 1 : 0;
}
