import fcntl
import sys
import os

if len(sys.argv) != 3:
    print("Usage: sudo python3 reset_usb.py <BUS> <DEVICE>")
    print("Example: sudo python3 reset_usb.py 001 005")
    sys.exit(1)

bus, dev = sys.argv[1].zfill(3), sys.argv[2].zfill(3)
device_path = f"/dev/bus/usb/{bus}/{dev}"

# This is the raw ioctl command code for a USB hardware reset in Linux
USBDEVFS_RESET = 21780 

try:
    print(f"Attempting to reset {device_path}...")
    fd = os.open(device_path, os.O_WRONLY)
    fcntl.ioctl(fd, USBDEVFS_RESET, 0)
    os.close(fd)
    print("Success! The device has been reset.")
except PermissionError:
    print("Error: You must run this script with sudo!")
except Exception as e:
    print(f"Failed to reset device: {e}")
