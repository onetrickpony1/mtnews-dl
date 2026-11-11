"""Keep every test away from the real per-user data folders (browser, link record, archive
allowance). Individual tests may still point these somewhere more specific."""
import os
import tempfile

_DATA = tempfile.mkdtemp(prefix="mtnews-dl-tests-")
for _tool in ("TOM_DL", "MTNEWS_DL", "MTTD_DL", "TALKMT_DL"):
    os.environ.setdefault(_tool + "_DATA", os.path.join(_DATA, _tool.lower()))
    os.environ.setdefault(_tool + "_CONFIG", os.path.join(_DATA, _tool.lower() + "-config"))
