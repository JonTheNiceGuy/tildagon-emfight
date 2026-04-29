import asyncio
import utime
import ubinascii
import network
try:
    import urequests as requests
except ImportError:
    import requests
import settings
import app
from events.input import Buttons, BUTTON_TYPES

try:
    import os as _os
    _HAS_URANDOM = hasattr(_os, "urandom")
except ImportError:
    _HAS_URANDOM = False

# qr.py lives next to app.py inside whatever apps/<target> directory the
# deploy script chose. Relative import lets us find it regardless of the
# package name; the absolute fallback only matters when the module is run
# outside a package (test harness).
try:
    from .qr import encode as qr_encode
    _HAS_QR = True
except Exception:
    try:
        from qr import encode as qr_encode
        _HAS_QR = True
    except Exception:
        _HAS_QR = False

# ── Configuration ──────────────────────────────────────────────────────────────
DEFAULT_SERVER = "https://emfight.taila1639.ts.net"
_S_DEVICE_ID   = "emfight_device_id"
_S_TOKEN       = "emfight_token"
_S_SERVER      = "emfight_server"

HEARTBEAT_MS        = 25_000
POLL_MS             =  2_000
PAIR_POLL_MS        =  2_500   # while showing the pairing QR, check every 2.5s
ANIM_MS             =  1_500
FIGHT_EXPIRY_SECONDS =   300

# ── States ─────────────────────────────────────────────────────────────────────
(ST_INIT, ST_REGISTERING, ST_MENU,
 ST_INITIATING, ST_FIGHTING,
 ST_MATCHED,
 ST_SUBMITTING, ST_WAITING_RESULT,
 ST_ENTER_CODE, ST_JOINING,
 ST_ANIMATING, ST_RESULT,
 ST_PAIRING_FETCH, ST_PAIRING,
 ST_ERROR,
 ST_HOME, ST_LOADING_STATS,
 ST_SCOREBOARD, ST_LOADING_SCOREBOARD) = range(19)

MENU_TIMEOUT_MS     = 15_000   # auto-return from menu to home screen

_STYLES     = ("mind", "body", "stamina")
_STYLE_BTNS = (BUTTON_TYPES["UP"], BUTTON_TYPES["CONFIRM"], BUTTON_TYPES["DOWN"])
_CODE_BTNS  = {
    BUTTON_TYPES["UP"]:      "A",
    BUTTON_TYPES["RIGHT"]:   "B",
    BUTTON_TYPES["CONFIRM"]: "C",
    BUTTON_TYPES["DOWN"]:    "D",
    BUTTON_TYPES["LEFT"]:    "E",
}

# ── Colours ────────────────────────────────────────────────────────────────────
_GREEN  = (0,    1,   0.5)
_WHITE  = (1,    1,   1  )
_GREY   = (0.5,  0.5, 0.5)
_RED    = (1,    0.3, 0.3)
_YELLOW = (1,    0.8, 0  )
_BLUE   = (0.4,  0.6, 1  )
_ORANGE = (1,    0.4, 0  )


class EMFightApp(app.App):

    def __init__(self):
        super().__init__()
        self.button_states = Buttons(self)
        self.state         = ST_INIT
        self.server        = DEFAULT_SERVER

        # Credentials
        self.device_id = None
        self.token     = None

        # Fight state
        self.fight_id          = None
        self.fight_token       = None
        self.display_code      = None
        self.fight_style       = None
        self.fight_result      = None   # {outcome, points, opponent}
        self.opponent_username = None
        self.qr_matrix         = None

        # Code-entry state (joiner)
        self.entered_code  = ""

        # Pairing
        self.pairing_code         = None
        self.pairing_qr           = None
        self.pair_after_register  = False
        self.last_pair_poll       = 0  # ticks_ms of last /device/stats probe

        # Home screen stats (loaded from /device/stats)
        self.home_username  = ""
        self.home_wins      = 0
        self.home_rank      = 0    # 0 = unknown / not paired

        # Menu timeout
        self.menu_entered   = 0    # ticks_ms when ST_MENU was entered

        # Scoreboard
        self.sb_page        = 1    # 1-indexed current page
        self.sb_entries     = []   # entries for current page
        self.sb_pages       = 1    # total pages (capped at 5 = top 20)
        self.sb_my_rank     = 0
        self.sb_context     = None # list of context entries or None
        self.sb_show_ctx    = False # True when displaying the context page

        # Timers
        self.last_hb       = 0
        self.last_poll     = 0
        self.anim_start    = 0
        self.fight_started = 0   # ticks_ms when fight was created locally

        # LED management
        self._leds_paused = False

        self.error_msg  = ""

    # ── Main loop ──────────────────────────────────────────────────────────────

    async def run(self, render_update):
        self.device_id = settings.get(_S_DEVICE_ID)
        self.token     = settings.get(_S_TOKEN)
        srv            = settings.get(_S_SERVER)
        if srv:
            self.server = srv

        self.state = ST_LOADING_STATS if (self.device_id and self.token) else ST_REGISTERING

        last_t = utime.ticks_ms()

        while True:
            now   = utime.ticks_ms()
            delta = utime.ticks_diff(now, last_t)
            last_t = now

            _prev_state = self.state
            self.update(delta)
            await render_update()

            if self.state == ST_REGISTERING:
                await self._do_register()

            elif self.state == ST_INITIATING:
                await self._do_initiate()

            elif self.state == ST_FIGHTING:
                elapsed = utime.ticks_diff(utime.ticks_ms(), self.fight_started)
                if self.fight_started and elapsed >= FIGHT_EXPIRY_SECONDS * 1000:
                    self.error_msg = "Fight expired"
                    self.state = ST_ERROR
                else:
                    if utime.ticks_diff(utime.ticks_ms(), self.last_hb) >= HEARTBEAT_MS:
                        await self._do_heartbeat()
                        self.last_hb = utime.ticks_ms()
                    if utime.ticks_diff(utime.ticks_ms(), self.last_poll) >= POLL_MS:
                        await self._do_poll()
                        self.last_poll = utime.ticks_ms()

            elif self.state == ST_JOINING:
                await self._do_join()

            elif self.state == ST_SUBMITTING:
                await self._do_submit_style()

            elif self.state == ST_WAITING_RESULT:
                if utime.ticks_diff(utime.ticks_ms(), self.last_poll) >= POLL_MS:
                    await self._do_poll_result()
                    self.last_poll = utime.ticks_ms()

            elif self.state == ST_PAIRING_FETCH:
                await self._do_pairing_fetch()

            elif self.state == ST_PAIRING:
                if utime.ticks_diff(utime.ticks_ms(), self.last_pair_poll) >= PAIR_POLL_MS:
                    await self._do_pair_check()
                    self.last_pair_poll = utime.ticks_ms()

            elif self.state == ST_LOADING_STATS:
                await self._do_load_stats()

            elif self.state == ST_LOADING_SCOREBOARD:
                await self._do_load_scoreboard()

            elif self.state == ST_MENU:
                if utime.ticks_diff(utime.ticks_ms(), self.menu_entered) >= MENU_TIMEOUT_MS:
                    self.state = ST_HOME

            elif self.state == ST_ANIMATING:
                if utime.ticks_diff(utime.ticks_ms(), self.anim_start) >= ANIM_MS:
                    self.state = ST_RESULT

            # Re-enable LEDs when leaving the ST_FIGHTING QR screen
            if _prev_state == ST_FIGHTING and self.state != ST_FIGHTING and self._leds_paused:
                self._leds_on()

            await asyncio.sleep(0.05)

    # ── Button handling ────────────────────────────────────────────────────────

    def update(self, delta):
        if self.button_states.get(BUTTON_TYPES["CANCEL"]):
            self.button_states.clear()
            self._on_cancel()
            return

        if self.state == ST_HOME:
            self._update_home()
        elif self.state == ST_MENU:
            self._update_menu()
        elif self.state == ST_SCOREBOARD:
            self._update_scoreboard()
        elif self.state == ST_MATCHED:
            self._update_style()
        elif self.state == ST_ENTER_CODE:
            self._update_code()

    def _on_cancel(self):
        if self.state in (ST_HOME, ST_REGISTERING):
            self.minimise()
        elif self.state == ST_MENU:
            self.state = ST_HOME
        elif self.state == ST_SCOREBOARD:
            self.state = ST_HOME
        elif self.state == ST_ENTER_CODE:
            if self.entered_code:
                self.entered_code = self.entered_code[:-1]   # backspace
            else:
                self.state = ST_MENU
        elif self.state == ST_FIGHTING:
            self._cancel_fight()
        else:
            self._go_home()

    def _reset_to_menu(self):
        self.fight_id = self.fight_token = self.display_code = None
        self.qr_matrix = self.opponent_username = self.fight_style = None
        self.fight_result = None
        self.entered_code = ""
        self.fight_started = 0
        self.state = ST_MENU
        self.menu_entered = utime.ticks_ms()

    def _go_home(self):
        """Clear fight state and return to home screen with refreshed stats."""
        self.fight_id = self.fight_token = self.display_code = None
        self.qr_matrix = self.opponent_username = self.fight_style = None
        self.fight_result = None
        self.entered_code = ""
        self.fight_started = 0
        self.state = ST_LOADING_STATS

    def _update_home(self):
        if self.button_states.get(BUTTON_TYPES["RIGHT"]):    # B → menu
            self.button_states.clear()
            self.menu_entered = utime.ticks_ms()
            self.state = ST_MENU
        elif self.button_states.get(BUTTON_TYPES["LEFT"]):   # E → scoreboard
            self.button_states.clear()
            self.sb_page     = 1
            self.sb_show_ctx = False
            self.state = ST_LOADING_SCOREBOARD

    def _update_menu(self):
        if self.button_states.get(BUTTON_TYPES["UP"]):
            self.button_states.clear()
            if not self.token:
                self.error_msg = "Pair your badge\nfirst"
                self.state = ST_ERROR
            else:
                self._reset_to_menu()
                self.state = ST_INITIATING
        elif self.button_states.get(BUTTON_TYPES["CONFIRM"]):
            self.button_states.clear()
            if not self.token:
                self.error_msg = "Pair your badge\nfirst"
                self.state = ST_ERROR
            else:
                self.entered_code = ""
                self.state = ST_ENTER_CODE
        elif self.button_states.get(BUTTON_TYPES["DOWN"]):
            self.button_states.clear()
            self.state = ST_PAIRING_FETCH

    def _update_scoreboard(self):
        if self.button_states.get(BUTTON_TYPES["UP"]):        # A → prev page
            self.button_states.clear()
            if self.sb_show_ctx:
                # Go back to last normal page
                self.sb_show_ctx = False
                self.sb_page = min(5, self.sb_pages)
                self.state = ST_LOADING_SCOREBOARD
            elif self.sb_page > 1:
                self.sb_page -= 1
                self.state = ST_LOADING_SCOREBOARD
        elif self.button_states.get(BUTTON_TYPES["DOWN"]):    # D → next page
            self.button_states.clear()
            if not self.sb_show_ctx and self.sb_page < min(5, self.sb_pages):
                self.sb_page += 1
                self.state = ST_LOADING_SCOREBOARD
            elif not self.sb_show_ctx and self.sb_context and self.sb_my_rank > 20:
                # Advance to the context page
                self.sb_show_ctx = True
                self.state = ST_SCOREBOARD  # entries already loaded

    def _update_style(self):
        for i, btn in enumerate(_STYLE_BTNS):
            if self.button_states.get(btn):
                self.button_states.clear()
                self.fight_style = _STYLES[i]
                self.state = ST_SUBMITTING
                return

    def _update_code(self):
        for btn, char in _CODE_BTNS.items():
            if self.button_states.get(btn):
                self.button_states.clear()
                if len(self.entered_code) < 5:
                    self.entered_code += char
                    if len(self.entered_code) == 5:
                        self.state = ST_JOINING
                return

    # ── API calls ──────────────────────────────────────────────────────────────

    def _hdrs(self):
        return {"Authorization": f"Bearer {self.token}"}

    def _get_mac(self):
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        return ubinascii.hexlify(wlan.config("mac")).decode()

    def _nonce(self):
        if _HAS_URANDOM:
            return ubinascii.hexlify(_os.urandom(16)).decode()
        return "{:032x}".format(utime.ticks_ms())

    async def _do_register(self):
        try:
            r = requests.post(
                f"{self.server}/api/v1/device/register",
                json={"mac": self._get_mac(), "nonce": self._nonce()},
            )
            if r.status_code == 201:
                d = r.json(); r.close()
                self.device_id = d["device_id"]
                self.token     = d["token"]
                settings.set(_S_DEVICE_ID, self.device_id)
                settings.set(_S_TOKEN,     self.token)
                settings.save()
                if self.pair_after_register:
                    self.pair_after_register = False
                    self.state = ST_PAIRING_FETCH
                else:
                    self.state = ST_LOADING_STATS
            else:
                r.close()
                self.error_msg = f"Reg failed ({r.status_code})"
                self.state     = ST_ERROR
        except Exception:
            self.error_msg = "Network error"
            self.state     = ST_ERROR

    async def _do_initiate(self):
        """POST /fight/initiate — no style, just get a fight_id and display code."""
        try:
            r = requests.post(
                f"{self.server}/api/v1/fight/initiate",
                json={},
                headers=self._hdrs(),
            )
            if r.status_code == 201:
                d = r.json(); r.close()
                self.fight_id     = d["fight_id"]
                self.fight_token  = d["fight_token"]
                self.display_code = d["display_code"]
                self.qr_matrix = None
                if _HAS_QR:
                    try:
                        self.qr_matrix = qr_encode(
                            f"{self.server}/f/{self.fight_token}"
                        )
                    except Exception:
                        self.qr_matrix = None
                now = utime.ticks_ms()
                self.last_hb = self.last_poll = now
                self.fight_started = now
                self.state   = ST_FIGHTING
                await self._leds_off()
            else:
                try:
                    d = r.json()
                except Exception:
                    d = {}
                r.close()
                self.error_msg = d.get("error", f"Error {r.status_code}")
                self.state     = ST_ERROR
        except Exception:
            self.error_msg = "Network error"
            self.state     = ST_ERROR

    async def _do_heartbeat(self):
        if not self.fight_id:
            return
        try:
            requests.post(
                f"{self.server}/api/v1/fight/heartbeat",
                json={"fight_id": self.fight_id},
                headers=self._hdrs(),
            ).close()
        except Exception:
            pass

    async def _do_poll(self):
        """Poll during ST_FIGHTING — wait for opponent to join (MATCHED status)."""
        if not self.fight_id:
            return
        try:
            r = requests.get(
                f"{self.server}/api/v1/fight/{self.fight_id}/poll",
                headers=self._hdrs(),
            )
            if r.status_code == 200:
                d = r.json(); r.close()
                status = d.get("status")
                if status == "matched":
                    self.opponent_username = d.get("opponent_username", "Opponent")
                    self.state = ST_MATCHED
                elif status == "complete":
                    # Resolved without style selection (shouldn't happen, but handle it)
                    self._store_result(d)
                    self.anim_start = utime.ticks_ms()
                    self.state = ST_ANIMATING
                elif status in ("cancelled", "expired"):
                    self.error_msg = f"Fight {status}"
                    self.state     = ST_ERROR
            else:
                r.close()
        except Exception:
            pass

    async def _do_join(self):
        """POST /fight/join with display code — no style yet, get opponent name."""
        try:
            r = requests.post(
                f"{self.server}/api/v1/fight/join",
                json={"code": self.entered_code},
                headers=self._hdrs(),
            )
            sc = r.status_code
            try:
                d = r.json()
            except Exception:
                d = {}
            r.close()

            if sc == 200:
                self.fight_id          = d.get("fight_id")
                self.opponent_username = d.get("opponent_username", "Opponent")
                self.state = ST_MATCHED
            elif sc == 403 and d.get("error") == "no_points_available":
                next_at = d.get("next_fight_at", "")[:10]
                self.error_msg = f"No pts until\n{next_at}"
                self.state = ST_ERROR
            else:
                self.error_msg = d.get("error", f"Error {sc}")
                self.state = ST_ERROR
        except Exception:
            self.error_msg = "Network error"
            self.state = ST_ERROR

    async def _do_submit_style(self):
        """POST /fight/<id>/select-style — called once after style picked in ST_MATCHED."""
        if not self.fight_id or not self.fight_style:
            return
        try:
            r = requests.post(
                f"{self.server}/api/v1/fight/{self.fight_id}/select-style",
                json={"style": self.fight_style},
                headers=self._hdrs(),
            )
            sc = r.status_code
            try:
                d = r.json()
            except Exception:
                d = {}
            r.close()

            if sc == 200:
                if d.get("status") == "complete":
                    self._store_result(d)
                    self.anim_start = utime.ticks_ms()
                    self.state = ST_ANIMATING
                else:
                    # Other player hasn't chosen yet
                    self.last_poll = utime.ticks_ms()
                    self.state = ST_WAITING_RESULT
            else:
                self.error_msg = d.get("error", f"Error {sc}")
                self.state = ST_ERROR
        except Exception:
            self.error_msg = "Network error"
            self.state = ST_ERROR

    async def _do_poll_result(self):
        """Poll during ST_WAITING_RESULT — wait for opponent to choose style."""
        if not self.fight_id:
            return
        try:
            r = requests.get(
                f"{self.server}/api/v1/fight/{self.fight_id}/poll",
                headers=self._hdrs(),
            )
            if r.status_code == 200:
                d = r.json(); r.close()
                status = d.get("status")
                if status == "complete":
                    self._store_result(d)
                    self.anim_start = utime.ticks_ms()
                    self.state = ST_ANIMATING
                elif status in ("cancelled", "expired"):
                    self.error_msg = f"Fight {status}"
                    self.state     = ST_ERROR
            else:
                r.close()
        except Exception:
            pass

    def _store_result(self, d):
        self.fight_result = {
            "outcome":  d.get("outcome", "?"),
            "points":   d.get("points_awarded", 0),
            "opponent": d.get("opponent_username") or self.opponent_username or "",
        }

    async def _do_load_stats(self):
        """GET /device/stats — populate home screen username/wins/rank."""
        if not self.token:
            self.home_username = ""
            self.home_wins     = 0
            self.home_rank     = 0
            self.state = ST_HOME
            return
        try:
            r = requests.get(
                f"{self.server}/api/v1/device/stats",
                headers=self._hdrs(),
            )
            if r.status_code == 200:
                d = r.json(); r.close()
                self.home_username = d.get("username_display", "")
                self.home_wins     = d.get("wins", 0)
                self.home_rank     = d.get("rank") or 0
            else:
                r.close()
                # 403 = not paired; 404 = unexpected — go home with blanks
                self.home_username = ""
                self.home_wins     = 0
                self.home_rank     = 0
        except Exception:
            pass  # keep existing values, still go to home
        self.state = ST_HOME

    async def _do_load_scoreboard(self):
        """GET /scoreboard?per_page=4&page=N — populate badge scoreboard page."""
        try:
            hdrs = self._hdrs() if self.token else {}
            r = requests.get(
                f"{self.server}/api/v1/scoreboard?per_page=4&page={self.sb_page}",
                headers=hdrs,
            )
            if r.status_code == 200:
                d = r.json(); r.close()
                self.sb_entries  = d.get("entries", [])
                # Cap displayed pages at 5 (= top 20)
                self.sb_pages    = min(d.get("pages", 1), 5)
                self.sb_my_rank  = d.get("my_rank") or 0
                ctx = d.get("context")
                if ctx and self.sb_my_rank > 20:
                    self.sb_context = ctx
                else:
                    self.sb_context = None
            else:
                r.close()
            self.state = ST_SCOREBOARD
        except Exception:
            self.state = ST_HOME

    async def _do_pair_check(self):
        """While the pairing QR is on screen, poll /device/stats to see if the
        user has completed pairing on the web. A 200 means the device is now
        attached to a user — go back to the home screen with their stats.
        """
        try:
            r = requests.get(
                f"{self.server}/api/v1/device/stats",
                headers=self._hdrs(),
            )
            status = r.status_code
            if status == 200:
                d = r.json(); r.close()
                self.home_username = d.get("username_display", "")
                self.home_wins     = d.get("wins", 0)
                self.home_rank     = d.get("rank") or 0
                # Clear pairing scratch state so the screen doesn't flash on
                # the next entry.
                self.pairing_code = None
                self.pairing_qr   = None
                self.state = ST_HOME
            else:
                r.close()
                # Anything else (typically 401/403 = still unpaired) — keep
                # showing the QR and try again next tick.
        except Exception:
            # Network blip — try again on the next tick.
            pass

    async def _do_pairing_fetch(self):
        try:
            r = requests.post(
                f"{self.server}/api/v1/device/pairing-code",
                headers=self._hdrs(),
            )
            if r.status_code == 200:
                d = r.json(); r.close()
                self.pairing_code = d["code"]
                url = d.get("url",
                            f"{self.server}/pair?code={self.pairing_code}")
                self.pairing_qr = None
                if _HAS_QR:
                    try:
                        self.pairing_qr = qr_encode(url)
                    except Exception:
                        # Encoder failure (e.g. URL too long) — fall through
                        # to text-only pairing screen rather than erroring out.
                        self.pairing_qr = None
                self.last_pair_poll = utime.ticks_ms()
                self.state = ST_PAIRING
            elif r.status_code in (401, 403):
                r.close()
                # Token invalid — clear credentials and re-register first
                self.device_id = None
                self.token     = None
                settings.set(_S_DEVICE_ID, "")
                settings.set(_S_TOKEN,     "")
                settings.save()
                self.pair_after_register = True
                self.state = ST_REGISTERING
            else:
                r.close()
                self.error_msg = "Pairing failed"
                self.state = ST_ERROR
        except Exception:
            self.error_msg = "Network error"
            self.state = ST_ERROR

    async def _leds_off(self):
        """Pause LED pattern and blank all pattern LEDs (indices 1–12)."""
        try:
            from system.patterndisplay.events import PatternDisable
            from system.eventbus import eventbus
            eventbus.emit(PatternDisable())
            # Yield so the async PatternDisable handler runs and sets enabled=False
            # before we write zeros — otherwise the pattern task overwrites us.
            await asyncio.sleep(0)
            from tildagonos import tildagonos
            for i in range(12):
                tildagonos.leds[i + 1] = (0, 0, 0)
            tildagonos.leds.write()
            self._leds_paused = True
        except Exception:
            pass

    def _leds_on(self):
        """Resume LED pattern."""
        try:
            from system.patterndisplay.events import PatternEnable
            from system.eventbus import eventbus
            eventbus.emit(PatternEnable())
        except Exception:
            pass
        self._leds_paused = False

    def _cancel_fight(self):
        if self.fight_id:
            try:
                requests.post(
                    f"{self.server}/api/v1/fight/cancel",
                    json={"fight_id": self.fight_id},
                    headers=self._hdrs(),
                ).close()
            except Exception:
                pass
        self._reset_to_menu()

    # ── Drawing ────────────────────────────────────────────────────────────────

    def draw(self, ctx):
        ctx.save()
        ctx.rgb(0, 0, 0).rectangle(-120, -120, 240, 240).fill()

        draw_fn = {
            ST_INIT:                self._draw_loading,
            ST_REGISTERING:         self._draw_registering,
            ST_HOME:                self._draw_home,
            ST_LOADING_STATS:       lambda c: self._draw_status(c, "Loading..."),
            ST_MENU:                self._draw_menu,
            ST_INITIATING:          lambda c: self._draw_status(c, "Starting..."),
            ST_FIGHTING:            self._draw_fighting,
            ST_MATCHED:             self._draw_matched,
            ST_SUBMITTING:          lambda c: self._draw_status(c, "Submitting..."),
            ST_WAITING_RESULT:      self._draw_waiting_result,
            ST_ENTER_CODE:          self._draw_enter_code,
            ST_JOINING:             lambda c: self._draw_status(c, "Joining..."),
            ST_ANIMATING:           self._draw_animation,
            ST_RESULT:              self._draw_result,
            ST_PAIRING_FETCH:       lambda c: self._draw_status(c, "Getting code..."),
            ST_PAIRING:             self._draw_pairing,
            ST_ERROR:               self._draw_error,
            ST_SCOREBOARD:          self._draw_scoreboard,
            ST_LOADING_SCOREBOARD:  lambda c: self._draw_status(c, "Loading..."),
        }.get(self.state)

        if draw_fn:
            draw_fn(ctx)

        ctx.restore()

    @staticmethod
    def _t(ctx, size, color, text, x, y):
        ctx.rgb(*color)
        ctx.font = "Arimo Bold"
        ctx.font_size = size
        ctx.text_align = ctx.CENTER
        ctx.text_baseline = ctx.MIDDLE
        ctx.move_to(x, y).text(text)

    def _draw_loading(self, ctx):
        self._t(ctx, 36, _GREEN, "EMFight", 0, 0)

    def _draw_registering(self, ctx):
        self._t(ctx, 36, _GREEN, "EMFight",        0, -25)
        self._t(ctx, 20, _GREY,  "Registering...", 0,  20)

    def _draw_menu(self, ctx):
        self._t(ctx, 30, _GREEN, "EMFight",              0, -70)
        self._t(ctx, 20, _WHITE, "[A]  Start the fight", 0, -28)
        self._t(ctx, 20, _WHITE, "[C]  Join the fight",  0,   4)
        self._t(ctx, 16, _WHITE, "[D]  Re-pair badge",   0,  36)
        self._t(ctx, 14, _GREY,  "[F]  Back",            0,  82)

    def _draw_status(self, ctx, msg):
        self._t(ctx, 22, _GREEN, msg, 0, 0)

    def _draw_fighting(self, ctx):
        if _HAS_QR and self.qr_matrix:
            self._draw_qr(ctx, self.qr_matrix, 0, -28, 130)
        else:
            self._t(ctx, 20, _GREEN, "Waiting for",  0, -20)
            self._t(ctx, 20, _GREEN, "opponent...",  0,   8)

        code = self.display_code or "-----"
        self._t(ctx, 30, _WHITE, code,         0,  62)
        self._t(ctx, 16, _GREY,  "[F] Cancel", 0,  90)

    def _draw_matched(self, ctx):
        opp = self.opponent_username or "Opponent"
        self._t(ctx, 20, _GREY,   f"vs {opp}",    0, -70)
        self._t(ctx, 22, _GREEN,  "Pick style:",  0, -38)
        self._t(ctx, 22, _YELLOW, "[A]  Mind",    0,  -2)
        self._t(ctx, 22, _ORANGE, "[C]  Body",    0,  32)
        self._t(ctx, 22, _BLUE,   "[D]  Stamina", 0,  66)
        self._t(ctx, 16, _GREY,   "[F]  Back",    0,  90)

    def _draw_waiting_result(self, ctx):
        opp = self.opponent_username or "opponent"
        self._t(ctx, 20, _GREEN, "Waiting for",   0, -20)
        self._t(ctx, 20, _GREEN, f"{opp}...",      0,  10)
        self._t(ctx, 16, _GREY,  "[F] Back",       0,  88)

    def _draw_enter_code(self, ctx):
        self._t(ctx, 24, _GREEN, "Enter code",         0, -68)
        display = self.entered_code + "_" * (5 - len(self.entered_code))
        self._t(ctx, 38, _WHITE, display,              0, -10)
        self._t(ctx, 16, _GREY,  "A B C D E",          0,  32)
        self._t(ctx, 16, _GREY,  "[F] Back / delete",  0,  88)

    def _draw_animation(self, ctx):
        elapsed = utime.ticks_diff(utime.ticks_ms(), self.anim_start)
        color   = _GREEN if (elapsed // 200) % 2 else _YELLOW
        opp     = self.opponent_username or ""
        self._t(ctx, 56, color,  "FIGHT!",       0, -20)
        if opp:
            self._t(ctx, 18, _GREY, f"vs {opp}", 0,  30)

    def _draw_result(self, ctx):
        if not self.fight_result:
            return
        outcome  = self.fight_result.get("outcome", "?")
        points   = self.fight_result.get("points",   0)
        opponent = self.fight_result.get("opponent", "")

        color, label = {
            "win":  (_GREEN,  "WIN!"),
            "loss": (_RED,    "LOSS"),
        }.get(outcome, (_YELLOW, "DRAW"))

        self._t(ctx, 52, color,  label,            0, -50)
        self._t(ctx, 26, _WHITE, f"+{points} pts", 0,   8)
        if opponent:
            self._t(ctx, 18, _GREY, f"vs {opponent}", 0, 40)
        self._t(ctx, 16, _GREY, "[F] Menu", 0, 88)

    def _draw_error(self, ctx):
        self._t(ctx, 24, _RED, "Error", 0, -55)
        lines = str(self.error_msg).split("\n")
        for i, line in enumerate(lines[:3]):
            self._t(ctx, 18, _WHITE, line, 0, -5 + i * 26)
        self._t(ctx, 16, _GREY, "[F] Back", 0, 88)

    def _draw_pairing(self, ctx):
        if _HAS_QR and self.pairing_qr:
            self._draw_qr(ctx, self.pairing_qr, 0, -25, 130)
        self._t(ctx, 22, _WHITE, self.pairing_code or "", 0, 62)
        self._t(ctx, 16, _GREY,  "[F] Back",              0, 88)

    def _draw_home(self, ctx):
        self._t(ctx, 36, _GREEN, "EMFight", 0, -68)
        if self.home_username:
            name = self.home_username[:14]
            self._t(ctx, 20, _WHITE, name,                                 0, -28)
            self._t(ctx, 18, _GREY,  f"Wins: {self.home_wins}",            0,   8)
            rank_str = f"Rank: #{self.home_rank}" if self.home_rank else "Rank: --"
            self._t(ctx, 18, _GREY,  rank_str,                             0,  32)
        else:
            self._t(ctx, 16, _YELLOW, "Pair badge to play",                0,  -8)
        self._t(ctx, 14, _GREY, "[B] Menu  [E] Scores",                    0,  64)
        self._t(ctx, 14, _GREY, "[F] Exit",                                0,  82)

    def _draw_scoreboard(self, ctx):
        self._t(ctx, 18, _GREEN, "Scoreboard", 0, -82)

        entries = self.sb_context if self.sb_show_ctx else self.sb_entries

        for i, e in enumerate(entries[:4]):
            y     = -52 + i * 30
            rank  = e.get("rank", "?")
            name  = e.get("username_display", "?")[:11]
            pts   = e.get("points", 0)
            is_me = e.get("is_me", False)
            color = _YELLOW if is_me else _WHITE

            ctx.rgb(*color)
            ctx.font = "Arimo Bold"
            ctx.font_size = 15
            ctx.text_baseline = ctx.MIDDLE

            # Rank left-aligned
            ctx.text_align = ctx.LEFT
            ctx.move_to(-105, y).text(f"#{rank}")

            # Name centered-ish
            ctx.move_to(-55, y).text(name)

            # Points right-aligned
            ctx.text_align = ctx.RIGHT
            ctx.move_to(105, y).text(f"{pts}pts")

        # Page indicator / context label
        if self.sb_show_ctx:
            label = f"Your pos: #{self.sb_my_rank}"
        else:
            has_ctx = bool(self.sb_context) and self.sb_my_rank > 20
            max_p   = min(5, self.sb_pages)
            suffix  = "+" if has_ctx and self.sb_page >= max_p else ""
            label   = f"pg {self.sb_page}/{max_p}{suffix}"
        self._t(ctx, 13, _GREY, label,              0, 70)
        self._t(ctx, 13, _GREY, "[A]◄  [D]►  [F]Back", 0, 86)

    @staticmethod
    def _draw_qr(ctx, matrix, cx, cy, max_size):
        n     = len(matrix)
        cell  = min(max_size // n, 5)
        total = n * cell
        ox    = cx - total // 2
        oy    = cy - total // 2
        qz = max(cell * 2, 8)   # quiet zone: at least 2 modules or 8px
        ctx.rgb(1, 1, 1).rectangle(ox - qz, oy - qz, total + qz * 2, total + qz * 2).fill()
        ctx.rgb(0, 0, 0)
        for r in range(n):
            for c in range(n):
                if matrix[r][c]:
                    ctx.rectangle(ox + c * cell, oy + r * cell, cell, cell).fill()


__app_export__ = EMFightApp
