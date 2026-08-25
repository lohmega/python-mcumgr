#!/bin/sh
# Run the tests that need no hardware.
#
# note: could also run as module `python3 -m mcumgr.cli` but that only works
# with correct PWD.

_realpath()
{
    python3 -c "import os; print(os.path.realpath('$1'))"
}

SCRIPT=$(_realpath "$0")
THIS_DIR=$(dirname "$SCRIPT")
BASE_DIR=$(_realpath "$THIS_DIR/../")

PYTHON=${PYTHON:-python3}
rc=0

for t in test_image.py test_mgmt.py test_mgmt_image.py test_transport_serial.py test_transport_ble.py test_smp_proxy.py test_ble_compat.py test_mgmt_proxy_ble.py; do
    echo "=== $t ==="
    PYTHONPATH="$BASE_DIR:$PYTHONPATH" "$PYTHON" "$THIS_DIR/$t" || rc=1
done

exit $rc
