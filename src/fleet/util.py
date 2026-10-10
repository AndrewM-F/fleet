import json
import os
import subprocess


def atomic(path, value, indent=2):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=indent) + "\n")
    os.replace(tmp, path)


def command(args, **kw):
    # Never let Claude append an inherited maintenance script to its task prompt.
    kw.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(args, capture_output=True, text=True, timeout=15,
                          check=True, **kw).stdout
