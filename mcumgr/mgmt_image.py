# mcumgr Image management group

from . import smp
from .mgmt import MgmtGrpBase, MgmtGrpEndpoint
from .smp import MgmtEndpointError


# Image Management Command IDs
IMG_MGMT_ID_STATE = 0
IMG_MGMT_ID_UPLOAD = 1
IMG_MGMT_ID_FILE = 2
IMG_MGMT_ID_CORELIST = 3
IMG_MGMT_ID_CORELOAD = 4
IMG_MGMT_ID_ERASE = 5


class MgmtGrpImage(MgmtGrpBase):
    """Image Management Group (MGMT_GROUP_ID.IMAGE = 1)

    Provides firmware image management operations including:
    - Reading image state (active, pending, confirmed images)
    - Uploading firmware images
    - Erasing image slots
    - Testing and confirming images for boot
    """

    nh_group = smp.MGMT_GROUP_ID.IMAGE

    def __init__(self, transport):
        super().__init__(transport)
        self.mh_state = MgmtGrpEndpoint(transport, self.nh_group, IMG_MGMT_ID_STATE)
        self.mh_upload = MgmtGrpEndpoint(transport, self.nh_group, IMG_MGMT_ID_UPLOAD)
        self.mh_erase = MgmtGrpEndpoint(transport, self.nh_group, IMG_MGMT_ID_ERASE)


    def get_state(self):
        """
        Returns: Example
{'images': [{'slot': 0, 'version': '0.4.2', 'hash': b'y\x9bL(\x81\xd6\x87 Jck\xb5#\xc7\x89\x8c@\xea\x0f\x1a[\x19\x9d\xbd\x1f7\xb1\t\x84\xb9$\xb7', 'bootable': True, 'pending': False, 'confirmed': True, 'active': True, 'permanent': False}, {'slot': 1, 'version': '0.4.1', 'hash': b'\x05\x11\r9\xe2\xcc\x89\x84V[\xcb\xe6\xbeo3\xed\x18P\xcb\x07Y\x99\\\x7f=W\x9c\x05\x01T\xd5;', 'bootable': True, 'pending': False, 'confirmed': False, 'active': False, 'permanent': False}], 'splitStatus': 0}

        """
        return self.mh_state.mh_read()

    def upload(self, file_path, slot=0, progress_callback=None):
        """
        Upload a firmware image to the device.

        Args:
            file_path: Path to the binary image file to upload
            slot: Target slot number (default: 0)
            progress_callback: Optional callback function(offset, total_size, rate_kbps)
                             called after each chunk upload

        Returns:
            dict: Final upload response

        Raises:
            FileNotFoundError: If image file doesn't exist
            MgmtEndpointError: If upload fails with error code
        """
        import os
        import time

        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Image file not found: {file_path}")

        file_size = os.path.getsize(file_path)
        offset = 0
        chunk_size = 512  # Start with conservative chunk size
        start_time = time.time()

        with open(file_path, 'rb') as f:
            while offset < file_size:
                # Read chunk from file
                f.seek(offset)
                data = f.read(chunk_size)

                if not data:
                    break

                # Build upload request payload
                payload = {
                    "off": offset,
                    "data": data
                }

                # First chunk includes total image size
                if offset == 0:
                    payload["len"] = file_size
                    payload["image"] = slot  # Target slot

                # Send chunk
                response = self.mh_upload.mh_write(payload)

                # Check for errors
                if "rc" in response and response["rc"] != 0:
                    rc = response["rc"]
                    rsn = response.get("rsn")
                    raise MgmtEndpointError(f"Upload failed at offset {offset}", rc=rc, rsn=rsn)

                # Update offset from response
                if "off" in response:
                    new_offset = response["off"]

                    # Adjust chunk size based on response (adaptive)
                    if new_offset == 0:
                        # First response - start small
                        chunk_size = 32
                    else:
                        # Increase chunk size for better throughput
                        # But leave headroom for SMP overhead
                        chunk_size = min(1024, chunk_size + 256)

                    offset = new_offset
                else:
                    # No offset in response, increment by data sent
                    offset += len(data)

                # Progress callback
                if progress_callback:
                    elapsed = time.time() - start_time
                    rate_kbps = (offset / elapsed / 1024) if elapsed > 0 else 0
                    progress_callback(offset, file_size, rate_kbps)

                # Check if upload is complete
                if offset >= file_size:
                    break

        return response
