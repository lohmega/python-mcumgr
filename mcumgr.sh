#!/bin/sh
#
# wrapper to run cli.py in this dir and not the installed one, without
# modifications to pythons `sys.path` in source.  for development and
# debug/test. (something about virtualenv...)
#
# note: could also run as module `python3 -m bblogger.cli` but that only works
# with correct PWD.


# `realpath` and `readlink -f` not on MacOS :(
_realpath()
{
    python3 -c "import os; print(os.path.realpath('$1'))"
}

# Absolute path to this script
SCRIPT=$(_realpath "$0")
# Absolute path this script is in
SCRIPTPATH=$(dirname "$SCRIPT")

PYTHONPATH="$SCRIPTPATH:$PYTHONPATH" 

# you can verify import paths with the following command
# PYTHONPATH="$PYTHONPATH" python3 -v $MCUMGR -h 2>&1 

echo "PYTHONPATH: $PYTHONPATH" 1>&2

MCUMGR="$SCRIPTPATH/mcumgr/cli.py"
PYTHONPATH="$PYTHONPATH" python3 $MCUMGR "$@"


