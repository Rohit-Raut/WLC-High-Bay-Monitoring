#!/usr/bin/env python3
"""
emergency_erase.py - FORCE ERASE counter regardless of archive status

⚠️  DANGER: This erases ALL data from the counter WITHOUT checking archive!
    Use this ONLY when you want to completely wipe the counter and start fresh.

Use cases:
  - You know the counter has stale/corrupted data
  - You're starting a completely fresh installation
  - The counter has data from testing/debugging that you don't want

DO NOT use this for normal operation! Use particle_plus.py --trim instead.
"""

import sys
from pymodbus.client import ModbusTcpClient

# Counter connection details (modify if different)
COUNTER_IP = '10.66.66.68'
COUNTER_PORT = 502

def get_record_count(client):
    """Read how many records are in counter memory."""
    try:
        # Read holding registers (not input registers!)
        # Address 8000, 2 registers (32-bit value)
        result = client.read_holding_registers(address=8000, count=2)
        if hasattr(result, 'isError') and result.isError():
            print(f"  Modbus error reading counter: {result}")
            return None
        if hasattr(result, 'registers') and len(result.registers) >= 2:
            # Decode 32-bit unsigned value from 2 registers
            high = result.registers[0]
            low = result.registers[1]
            count = (high << 16) | low
            return count
    except Exception as e:
        print(f"  Exception reading counter: {e}")
        return None
    return 0

def erase_counter(client):
    """
    Erase ALL data from counter memory.
    Magic value 0x9559 to register 8004 triggers the erase.
    """
    print("\n⚡ Sending erase command to counter...")
    try:
        client.write_registers(address=8004, values=[0x9559])
        import time
        time.sleep(3)  # Give counter time to erase

        remaining = get_record_count(client)
        if remaining is None:
            print("  ✗ Could not verify erase (read failed)")
            return False

        if remaining == 0:
            print(f"  ✓ Counter erased successfully! (0 records remaining)")
            return True
        else:
            print(f"  ⚠ Erase may have failed ({remaining} records still present)")
            return False

    except Exception as e:
        print(f"  ✗ Erase failed: {e}")
        return False

def main():
    print("╔═══════════════════════════════════════════════════════════════════╗")
    print("║           ⚠️  EMERGENCY COUNTER ERASE  ⚠️                          ║")
    print("╚═══════════════════════════════════════════════════════════════════╝")
    print()
    print("This script will ERASE ALL DATA from the particle counter.")
    print("It does NOT check if data is saved to archive first!")
    print()
    print(f"Target: {COUNTER_IP}:{COUNTER_PORT}")
    print()

    # Connect
    print("Connecting to counter...")
    client = ModbusTcpClient(COUNTER_IP, port=COUNTER_PORT, timeout=5)
    if not client.connect():
        print("✗ Connection failed!")
        print("\nPossible issues:")
        print("  - Counter is offline or unreachable")
        print("  - IP address is wrong")
        print("  - Network connectivity issue")
        return 1

    print("✓ Connected successfully\n")

    try:
        # Check current record count
        count = get_record_count(client)
        if count is None:
            print("✗ Could not read counter record count!")
            return 1

        print(f"Current records in counter: {count}")

        if count == 0:
            print("\n✓ Counter is already empty - nothing to erase")
            return 0

        # Show warning
        print("\n" + "="*67)
        print("⚠️  WARNING: YOU ARE ABOUT TO DELETE ALL DATA FROM THE COUNTER!")
        print("="*67)
        print(f"\nThis will erase {count} records from counter memory.")
        print("These records will be PERMANENTLY DELETED from the counter.")
        print()
        print("Make sure you have already:")
        print("  1. Synced data to archive (if you need it)")
        print("  2. Verified archive has the data you want to keep")
        print()
        print("This is typically used when:")
        print("  - Counter has stale data from days/weeks ago")
        print("  - You're starting a completely fresh installation")
        print("  - Counter has test/debug data you don't need")
        print()

        # Require exact confirmation
        print("Type the following EXACTLY to confirm erase:")
        print("  DELETE ALL DATA")
        print()
        response = input("Confirmation: ").strip()

        if response != "DELETE ALL DATA":
            print("\n❌ Cancelled (confirmation did not match)")
            print("   Counter was NOT erased")
            return 0

        # Perform erase
        print()
        success = erase_counter(client)

        if success:
            print("\n" + "="*67)
            print("✅ COUNTER SUCCESSFULLY ERASED")
            print("="*67)
            print()
            print("Next steps:")
            print("  1. The counter will now start collecting NEW data")
            print("  2. Start monitoring: python3 particle_plus.py --all")
            print("  3. New data will have today's timestamps")
            print()
            print("Note: sync_state will be reset automatically on next sync")
            return 0
        else:
            print("\n" + "="*67)
            print("❌ ERASE FAILED OR INCOMPLETE")
            print("="*67)
            print()
            print("Troubleshooting:")
            print("  - Try running this script again")
            print("  - Check counter display/logs for errors")
            print("  - Power cycle the counter if necessary")
            print("  - Contact support if issue persists")
            return 1

    finally:
        client.close()
        print()

if __name__ == '__main__':
    sys.exit(main())
