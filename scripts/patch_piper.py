"""Make wyoming-piper start on Windows (idempotent).

wyoming_piper/__main__.py calls loop.add_signal_handler(), which raises NotImplementedError on
Windows, so the server dies right after "Ready". Wrap those calls the same way the wyoming
library itself does. Re-run after upgrading wyoming-piper.
"""
import importlib.util
import pathlib
import re
import sys

spec = importlib.util.find_spec("wyoming_piper")
if spec is None or not spec.submodule_search_locations:
    sys.exit("wyoming_piper is not installed in this environment")
main = pathlib.Path(list(spec.submodule_search_locations)[0]) / "__main__.py"
src = main.read_text(encoding="utf-8")

MARK = "# patched for Windows"
if MARK in src:
    print(f"already patched: {main}")
    sys.exit(0)

pattern = re.compile(r"^(?P<i>[ \t]*)(?P<lines>loop\.add_signal_handler\(signal\.SIGINT[^\n]*\n"
                     r"(?P=i)loop\.add_signal_handler\(signal\.SIGTERM[^\n]*\n)", re.M)
m = pattern.search(src)
if not m:
    if "add_signal_handler" not in src:
        print(f"no signal handlers found; nothing to patch: {main}")
        sys.exit(0)
    sys.exit(f"unexpected layout in {main}; patch it by hand (wrap add_signal_handler in try/except NotImplementedError)")

i = m.group("i")
body = "".join(f"{i}    {line.strip()}\n" for line in m.group("lines").splitlines())
patched = f"{i}try:  {MARK}: add_signal_handler raises NotImplementedError there\n{body}{i}except NotImplementedError:\n{i}    pass\n"
main.write_text(src[:m.start()] + patched + src[m.end():], encoding="utf-8")
print(f"patched: {main}")
