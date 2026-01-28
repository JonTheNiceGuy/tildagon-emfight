import asyncio
import app
from micropython import const

from events.input import Buttons, BUTTON_TYPES
from app_components import clear_background

# BLE event constants
_IRQ_SCAN_RESULT = const(5)
_IRQ_SCAN_DONE = const(6)


class EMFightApp(app.App):
    def __init__(self):
        super().__init__()
        self.button_states = Buttons(self)
        self.beacons = []
        self.status = "Press A to scan"
        self.scan_requested = False
        self.scan_complete = False
        self._ble = None

    def _irq(self, event, data):
        """Handle BLE IRQ events."""
        if event == _IRQ_SCAN_RESULT:
            addr_type, addr, adv_type, rssi, adv_data = data
            try:
                # Try to decode name from advertising data
                name = self._decode_name(adv_data) or "?"
                addr_str = ":".join("{:02x}".format(b) for b in bytes(addr))

                if not any(b["addr"] == addr_str for b in self.beacons):
                    self.beacons.append({
                        "name": name,
                        "rssi": rssi,
                        "addr": addr_str,
                    })
                    print(f"[EMFight] Found: {name} ({rssi}dBm)")
            except Exception as e:
                print(f"[EMFight] Parse error: {e}")

        elif event == _IRQ_SCAN_DONE:
            print("[EMFight] Scan complete")
            self.scan_complete = True

    def _decode_name(self, adv_data):
        """Decode device name from advertising data."""
        i = 0
        while i + 1 < len(adv_data):
            length = adv_data[i]
            if length == 0:
                break
            ad_type = adv_data[i + 1]
            if ad_type == 0x09:  # Complete local name
                return bytes(adv_data[i + 2:i + 1 + length]).decode("utf-8")
            i += 1 + length
        return None

    async def run(self, render_update):
        """Main async loop."""
        while True:
            if self.button_states.get(BUTTON_TYPES["CANCEL"]):
                self.button_states.clear()
                self._cleanup_ble()
                self.minimise()
                return

            if self.button_states.get(BUTTON_TYPES["UP"]):
                self.button_states.clear()
                self.scan_requested = True

            if self.button_states.get(BUTTON_TYPES["DOWN"]):
                self.button_states.clear()
                self.beacons = []
                self.status = "Cleared"

            if self.scan_requested:
                self.scan_requested = False
                await self._do_scan()

            await render_update()

    async def _do_scan(self):
        """Perform BLE scan using low-level API."""
        print("[EMFight] Starting scan...")
        self.status = "Init..."
        self.beacons = []
        self.scan_complete = False

        try:
            import gc
            gc.collect()
            print(f"[EMFight] Free mem: {gc.mem_free()}")

            print("[EMFight] Importing bluetooth...")
            import bluetooth

            if self._ble is None:
                print("[EMFight] Creating BLE instance...")
                self._ble = bluetooth.BLE()

            print(f"[EMFight] BLE active? {self._ble.active()}")

            if not self._ble.active():
                print("[EMFight] Setting IRQ handler...")
                self._ble.irq(self._irq)
                print("[EMFight] Activating BLE...")
                self._ble.active(True)
                print("[EMFight] BLE activated")
                await asyncio.sleep(0.3)

            self.status = "Scanning..."
            print("[EMFight] Starting gap_scan...")
            # gap_scan(duration_ms, interval_us, window_us)
            self._ble.gap_scan(2000, 30000, 30000)

            # Wait for scan to complete
            timeout = 30  # 3 seconds max
            while not self.scan_complete and timeout > 0:
                await asyncio.sleep(0.1)
                timeout -= 1

            print(f"[EMFight] Found {len(self.beacons)} devices")
            self.status = f"Found {len(self.beacons)}"

        except Exception as e:
            print(f"[EMFight] Error: {e}")
            self.status = f"Err: {str(e)[:15]}"

    def _cleanup_ble(self):
        """Clean up BLE on exit."""
        if self._ble:
            try:
                print("[EMFight] Stopping BLE...")
                self._ble.gap_scan(None)  # Stop scanning
                self._ble.active(False)
            except:
                pass

    def draw(self, ctx):
        """Draw the UI."""
        clear_background(ctx)
        ctx.save()

        ctx.rgb(1, 1, 1)
        ctx.font_size = 18
        ctx.text_align = ctx.CENTER
        ctx.move_to(0, -100).text("EMFight")

        ctx.font_size = 14
        ctx.rgb(0.8, 0.8, 0.2)
        ctx.move_to(0, -75).text(self.status)

        ctx.font_size = 11
        ctx.rgb(0.7, 0.7, 0.7)
        y_pos = -45
        for beacon in self.beacons[:5]:
            name = beacon["name"][:8]
            rssi = beacon["rssi"]
            ctx.move_to(0, y_pos).text(f"{name} {rssi}")
            y_pos += 20

        ctx.font_size = 11
        ctx.rgb(0.5, 0.5, 0.5)
        ctx.move_to(0, 90).text("A:Scan D:Clear F:Exit")

        ctx.restore()


__app_export__ = EMFightApp
