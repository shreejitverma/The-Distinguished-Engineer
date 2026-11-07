import sys

sys.stdout.write("PING: py_req\n")
# Note: no sys.stdout.flush() to test default buffering
resp = sys.stdin.readline()
if resp:
    sys.stderr.write(f"CLIENT_GOT: {resp}")
    sys.exit(0)
sys.exit(1)
