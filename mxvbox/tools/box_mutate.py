# -*- coding: utf-8 -*-
"""box_mutate.py -- mutation-test box code ON THE BOX (Python 2.7 there): break one thing at a time in a
COPY of the staged files and confirm a suite goes red. The project rule (CLAUDE.md): MUTATION-TEST
EVERY FIX; a mutation that "passes" may simply not have applied, so each line says changed=True/False.

  1. stage your files:   bash mxvbox/tools/box_deploy.sh test      (-> /storage/orc/gpu_stage)
  2. write the mutations as JSON: [[file, exact old text, new text, label, test file], ...]
     (old text must occur EXACTLY once; files are compared with \\n line ends)
  3. run, from orc/ on the PC:
     cat muts.json | ssh pibox@pibox.local 'A=$(python3 -c "import json;print(json.load(open(\\"/dev/shm/orc1_mxvbox_status.json\\")).get(\\"addr\\",\\"\\"))"); ssh -o HostName=$A mxvbox "cat > /tmp/muts.json"'
     cat mxvbox/tools/box_mutate.py | ssh pibox@pibox.local '<same $A> ; ssh -o HostName=$A mxvbox "python - /tmp/muts.json"'
Every line should end in "N passed, M failed" with M > 0. Writing the JSON with the Write tool (not a
shell heredoc) avoids the backslash mangling CLAUDE.md warns about.
"""
from __future__ import print_function
import json, os, shutil, subprocess, sys

SRC = "/storage/orc/gpu_stage"
muts = json.load(open(sys.argv[1]))
for f, a, b, name, test in muts:
    d = "/tmp/mut"
    shutil.rmtree(d, True)
    shutil.copytree(SRC, d)
    for x in [x for x in os.listdir(d) if x.endswith((".pyc", ".pyo"))]:
        os.remove(os.path.join(d, x))
    s = open(os.path.join(d, f)).read().replace("\r\n", "\n")
    changed = s.count(a) == 1
    open(os.path.join(d, f), "w").write(s.replace(a, b))
    out = subprocess.Popen(["nice", "-n", "10", "python", "-B", os.path.join(d, test)],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT).communicate()[0]
    last = [l for l in out.splitlines() if "passed" in l]
    print("%-24s changed=%s  %s" % (name, changed, last[-1] if last else out[-300:]))
shutil.rmtree("/tmp/mut", True)
