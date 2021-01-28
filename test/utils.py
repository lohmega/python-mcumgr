
import sys
import os

MCUMGR_CMD = ["mcumgr"]
def use_repo_sources(yes):
    """ hack! should use pyenv but lazy """
    global MCUMGR_CMD
    if yes:
        # use local relative to this dir
        this_dir = os.path.dirname(os.path.realpath(__file__))
        import_dir = os.path.realpath(os.path.join(this_dir, "../"))
        sys.path.insert(0, import_dir)
        py_cli = os.path.join(import_dir, "cli.py")
        MCUMGR_CMD = ["python3", py_cli]
    else:
        MCUMGR_CMD = ["mcumgr"]
