# EMFight — Tildagon Badge App

A real-time badge-fighting game for [EMF Camp](https://www.emfcamp.org/), running on the [Tildagon](https://tildagon.badge.emfcamp.org/) ESP32 badge.

The server that powers this app lives in the companion repository: **[JonTheNiceGuy/emfight-server](https://github.com/JonTheNiceGuy/emfight-server)**

---

## What is EMFight?

Two badge holders challenge each other to a fight:

1. One player presses **[A] Start the fight** — the badge registers with the server and shows a 5-character code plus a QR code on screen.
2. The second player presses **[C] Join the fight** and types the 5-character code (buttons A–E), or scans the QR code with the web PWA on their phone.
3. Both players see their opponent's name and choose a **fighting style**: Mind (A), Body (C), or Stamina (D).
4. The server rolls dice for each player based on their hidden stat for the chosen style and declares the result.
5. Win/draw/loss and the points awarded are displayed on the badge.

Each player has a hidden stat profile — a random permutation of 1, 2, and 3 assigned to Mind, Body, and Stamina at first registration. Picking the style your badge is strongest in improves your odds, but you won't know your opponent's stats.

---

## Controls

| Button | Action |
|--------|--------|
| B (Top Right) | Open the menu from the home screen |
| E (Bottom Left) | View the scoreboard from the home screen |
| A (Up) | Start a fight (menu) / previous scoreboard page |
| C (Confirm) | Join a fight by code (menu) |
| D (Down) | Re-pair badge to web account (menu) / next scoreboard page |
| F (Cancel) | Back / cancel / exit |

When entering a join code, buttons A–E act as the characters A–E. Press F to delete the last character.

---

## Pairing your badge

Your badge auto-registers with the server anonymously on first launch. To appear on the scoreboard with a username, pair it to a web account:

1. Open the menu and press **[D] Re-pair badge** — the badge shows a QR code and a short code.
2. On your phone or laptop, open the server's web address, log in (GitHub, Pocket-ID, or email), and either scan the QR or type the pairing code.
3. Your badge will now show your username and rank on the home screen.

---

## Installation

### From the Tildagon App Store

Search for **EMFight** in the badge's built-in app store, or find it at the store URL listed in `tildagon.toml`.

### Manual / local deployment

From the root of the `tildagon` repo, run:

```bash
./deploy.sh emfight apps/EMFight/tildagon-emfight
```

This copies `app.py`, `qr.py`, `tildagon.toml`, and `metadata.json` to the badge via `mpremote`.

To point the app at a different server (e.g. a local dev instance), set the `emfight_server` key in the badge's settings storage before launching the app.

---

## App structure

`app.py` — the entire badge application, a single `EMFightApp` class.

The app is a state machine with the following states:

| State | Description |
|-------|-------------|
| `ST_INIT` | Startup splash |
| `ST_REGISTERING` | First-run device registration with the server |
| `ST_LOADING_STATS` | Fetching username / wins / rank from the server |
| `ST_HOME` | Home screen showing username, wins, and rank |
| `ST_MENU` | Fight menu (start / join / re-pair) |
| `ST_INITIATING` | Creating a new fight on the server |
| `ST_FIGHTING` | Waiting for an opponent — shows QR code and display code |
| `ST_MATCHED` | Opponent joined — choose your fighting style |
| `ST_SUBMITTING` | Sending chosen style to the server |
| `ST_WAITING_RESULT` | Waiting for the opponent to choose their style |
| `ST_ANIMATING` | Brief "FIGHT!" animation before the result |
| `ST_RESULT` | Win / draw / loss screen |
| `ST_ENTER_CODE` | Entering a 5-character join code |
| `ST_JOINING` | Sending the join code to the server |
| `ST_PAIRING_FETCH` | Fetching a pairing code from the server |
| `ST_PAIRING` | Showing the pairing QR code and short code |
| `ST_LOADING_SCOREBOARD` | Fetching a scoreboard page |
| `ST_SCOREBOARD` | Displaying the leaderboard (up to top 20 + personal position) |
| `ST_ERROR` | Error screen |

`qr.py` — a pure-MicroPython QR code encoder used to display fight tokens and pairing URLs on the badge display.

---

## Server API summary

The badge communicates exclusively over HTTPS using a Bearer token obtained at first registration. Key calls:

- `POST /api/v1/device/register` — register device, receive token
- `POST /api/v1/fight/initiate` — start a fight
- `POST /api/v1/fight/heartbeat` — keep fight alive every 25 s
- `GET  /api/v1/fight/<id>/poll` — poll for opponent join / result
- `POST /api/v1/fight/join` — join a fight by code
- `POST /api/v1/fight/<id>/select-style` — submit fighting style
- `POST /api/v1/fight/cancel` — cancel an open fight
- `GET  /api/v1/device/stats` — username + wins + rank for home screen
- `POST /api/v1/device/pairing-code` — get a pairing code + URL
- `GET  /api/v1/scoreboard` — paginated leaderboard

Full API documentation is in the server repository.
