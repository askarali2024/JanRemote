# Contributing to JanRemote

Thanks for your interest! JanRemote is a small, focused project: a local
web remote for Hisense VIDAA OS TVs. Keep that scope in mind when proposing
changes.

## Ways to help

- **Firmware compatibility reports** — the most valuable contribution.
  VIDAA firmware varies a lot between models and years, and several behaviors
  (does it answer `getvolume`? numeric vs string source IDs? does `sendtext`
  work?) differ per set. Open an issue with your TV model, firmware version
  (Settings → About), and what worked/didn't.
- **Bug reports** — include your TV model, what you clicked, what happened,
  and the `python app.py` terminal output at the time.
- **Code** — bug fixes and small features. UI is plain
  `templates/index.html` (Tailwind via CDN, no build step); backend is
  `app.py` (FastAPI + pyvidaa).

## Ground rules

1. **No secrets in the repo, ever.** `config.json` and `certs/` are
   gitignored — keep them that way. The VIDAA client certificate is
   Hisense's proprietary material: never commit it, and never open a PR
   that adds it or the keystore password-protected blobs. Point users at
   the README's extraction instructions instead.
2. **Test against real hardware when you can.** Changes to TV commands
   can't be verified by unit tests alone. If you don't own a VIDAA TV, say
   so in the PR and describe how you validated the change.
3. **Keep the stack as-is**: Python + FastAPI + pyvidaa, single-file
   backend, single-file frontend, no build tooling. This is deliberate —
   the app should stay a `git clone && pip install && python app.py`
   experience.
4. **Firmware quirks go in code comments.** If you had to work around a
   firmware-specific behavior, document what the TV actually did (see the
   comments in `app.py` about broadcast-only volume and string source IDs
   for the style).

## Development setup

```bash
git clone https://github.com/askarali2024/JanRemote.git
cd JanRemote
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py   # then open http://localhost:8000
```

You'll need a Hisense VIDAA TV on your LAN to test pairing and commands —
see the README for setup and the client-certificate situation on 2024+
firmware.

## Submitting

1. Fork, create a branch, make your change
2. Open a PR with **what you changed, why, and how you tested it**
   (including TV model/firmware if TV-facing)
3. For anything that changes on-wire behavior, include the observed
   MQTT topic + payload in the description

Issues and PRs in English, please. Be kind — this is a hobby project.
