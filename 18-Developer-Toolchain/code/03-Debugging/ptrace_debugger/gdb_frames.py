# gdb_frames.py - print GDB's own unwind of the current stop as one line of function names.
# run.sh loads it with `gdb -x` after GDB stops on `break *leaf_compute`, the same instruction
# where minidbg stops, so the two backtraces can be compared name for name.
import gdb

names = []
frame = gdb.newest_frame()
while frame is not None:
    name = frame.name() or "??"
    names.append(name)
    if name == "main":
        break
    frame = frame.older()
print("GDB frames: " + " ".join(names))
