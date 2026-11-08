"""Raise the front descriptor limit before exec, without native unsafe code."""
import os
import resource
import sys

cwd = sys.argv.pop(1)
required = int(sys.argv[1])
_, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
if hard != resource.RLIM_INFINITY and hard < required:
    sys.exit(f"RLIMIT_NOFILE hard limit {hard} below required {required}")
resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
os.environ["BRIGADE_HUB_LIMIT_PID"] = str(os.getpid())
os.chdir(cwd)
# argv[2] is the image to run (a /proc/self/fd path when the caller kept a
# descriptor open), argv[3] the argv[0] it keeps.
os.execv(sys.argv[2], sys.argv[3:])
