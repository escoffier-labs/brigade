"""Raise the front descriptor limit before exec, without native unsafe code."""
import os
import resource
import sys

required = int(sys.argv[1])
_, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
if hard != resource.RLIM_INFINITY and hard < required:
    sys.exit(f"RLIMIT_NOFILE hard limit {hard} below required {required}")
resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
os.environ["BRIGADE_HUB_LIMIT_PID"] = str(os.getpid())
os.execv(sys.argv[2], sys.argv[2:])
