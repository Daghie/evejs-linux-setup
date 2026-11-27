# Runs a script under pythonw.exe (no console), logging stdout/stderr and the exit status to files.
import sys, runpy, traceback
log, script = sys.argv[1], sys.argv[2]
sys.argv = [script] + sys.argv[3:]
sys.stdout = sys.stderr = open(log, "w", buffering=1, encoding="utf-8")
sys.path.insert(0, script.rsplit("\\", 1)[0])
status = 0
try:
    runpy.run_path(script, run_name="__main__")
except SystemExit as e:
    status = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
except BaseException:
    traceback.print_exc(); status = 1
open(log + ".exit", "w").write(str(status))
