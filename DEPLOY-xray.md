# Turning on xray-core

The panel ships with two data planes. The built-in Python relay is what runs
by default; xray-core is faster and speaks more transports, but needs a
different container.

**Nothing changes until you opt in.** `railway.json` builds with Nixpacks and
runs `python main.py`, exactly as before. The xray code is present but inert:
with no binary installed, `xray_manager.available()` is false and every
request goes through the Python relay.

## What xray buys you

| | Python relay | xray-core |
|---|---|---|
| Throughput | one core, interpreted | Go, multi-core |
| Transports | VLESS over WebSocket | VLESS/VMess over WS, **XHTTP** |
| CDN survival | WebSocket is increasingly fingerprinted | XHTTP looks like ordinary HTTP |

## Switching over

1. Edit `railway.json` and replace the `build` block with:

   ```json
   "build": { "builder": "DOCKERFILE", "dockerfilePath": "deploy/xray/Dockerfile" }
   ```

   and delete the `startCommand` line — the image has its own entrypoint.

2. Redeploy. The build compiles Pillow and psutil and downloads xray, so the
   first one takes several minutes.

3. Check **Settings → xray** in the panel. It reports the detected version, or
   says it fell back to the Python relay.

## Why these files live in `deploy/xray/`

Railway detects a `Dockerfile` in the repository root and builds with it
**even when `railway.json` asks for Nixpacks**. Keeping them in a
subdirectory means the default deployment cannot be hijacked by their mere
presence; you opt in by naming the path explicitly, as above.

## Before you switch

- **The build is heavier.** nginx, xray and the panel share one container.
  On a memory-capped free plan, watch the first deploy rather than assuming.
- **The build can fail where Nixpacks did not** — it fetches xray from GitHub
  at build time, so a bad moment there is a failed deploy.
- **Do it when you are not mid-sale.** Reverting means putting the Nixpacks
  block back and redeploying, which is quick, but it is still a redeploy.
- **Your data is safe either way.** Users and settings live in `data/db.json`
  and the Telegram backup, neither of which the builder touches.

## Rolling back

Restore the original `build` block and `startCommand`, then redeploy:

```json
"build": { "builder": "NIXPACKS" },
"deploy": { "startCommand": "python main.py", ... }
```
