# OpenSea SeaDrop Sniper - Setup Guide

This guide gets you from zero to a fully working FCFS sniper:

```
[Browser + Tampermonkey] → [Bot in sniper mode] → [Multi-RPC broadcast] → [Mint]
```

After setup, your per-drop effort is roughly **2 minutes** (open browser, run
bot). The userscript handles the click-Mint-and-grab-signature race
automatically; the bot fires the on-chain transaction the moment it receives
the signature.

---

## 1. Prerequisites

- Bot installed and working (`python main.py 0xCONTRACT --check` succeeds)
- `wallets.txt` populated with at least one private key
- Chrome / Edge / Firefox / Brave browser
- MetaMask installed and logged in
- Optional: a Singapore VPS (or any VPS) if you want to run the bot off-host

---

## 2. Install Tampermonkey

1. Go to your browser's extension store and install **Tampermonkey**:
    - Chrome / Edge / Brave: <https://www.tampermonkey.net/>
    - Firefox: same link, or Mozilla Add-ons store
2. Pin the extension to your toolbar (the Tampermonkey icon should be
   visible next to the address bar).

Verify Tampermonkey is installed by clicking the icon — you should see
"Dashboard" / "Create new script..." options.

---

## 3. Install the Sniper Userscript

1. Click the Tampermonkey icon → **Create a new script...**
2. A code editor opens. **Delete** all the placeholder content.
3. Open `userscript/opensea-sniper.user.js` from this repository in any text
   editor (or open the file in your browser via `file://`-style URL).
4. **Copy** the entire contents.
5. **Paste** into the Tampermonkey editor.
6. Save with `Ctrl+S` (or File → Save). The tab title should change from
   "*New Userscript" to "OpenSea SeaDrop Sniper".

### Verify Installation

1. Click the Tampermonkey icon → **Dashboard**.
2. You should see "OpenSea SeaDrop Sniper" in the list with a green ON
   toggle next to it.
3. Open any OpenSea drop page (e.g.
   `https://opensea.io/collection/some-drop`).
4. A floating green panel appears in the **top-right corner** showing:
    ```
    🟢 SeaDrop Sniper                armed
    contract: (detect on Mint click)
    bot: http://127.0.0.1:8888/signature
    ```
5. Open browser DevTools (F12) → Console. You should see:
    ```
    [Sniper] initialized {botUrl: ..., autoClick: true, ...}
    ```

If you don't see the panel: refresh the page, check Tampermonkey toggle is
ON, and confirm the URL matches `https://opensea.io/*`.

---

## 4. Configure the Userscript

By default the userscript posts to `http://127.0.0.1:8888/signature`. If your
bot runs locally, this is correct.

**To change the configuration**:

1. Click the Tampermonkey icon (on any OpenSea page).
2. Click **🎯 Configure Sniper** in the dropdown.
3. Enter your bot URL when prompted, e.g.:
    - Local bot:   `http://127.0.0.1:8888/signature`
    - VPS Singapore: `https://your-vps-domain.com/signature`
    - VPS by IP:   `http://1.2.3.4:8888/signature`
4. Enter shared secret (optional, but **strongly recommended for VPS**).
5. Confirm "auto-click Mint button" → **OK** (yes).
6. Confirm "auto-reject MetaMask popup" → **OK** (yes).

The settings persist via Tampermonkey storage; you only configure once.

---

## 5. Run the Bot in Sniper Mode

### Option A: Run on your local machine

```powershell
# Windows PowerShell
python main.py 0xNFT_CONTRACT_ADDRESS -c config.json --sniper
```

You should see:

```
╔══════════════════════════════════════════════════════════════════╗
║              OpenSea NFT Mint Bot v1.0  |  SeaDrop               ║
╚══════════════════════════════════════════════════════════════════╝

Collection: ...
  contract : 0x...
  chain    : Ethereum
  ...

Sniper mode active.
  Listening on http://127.0.0.1:8888/signature
  Configure userscript -> Bot URL: http://127.0.0.1:8888/signature
  Target phase  : FCFS (signed)
  Wallets       : 2 loaded
  Press Ctrl+C to stop.
```

### Option B: Run on a VPS (Singapore or elsewhere)

```bash
# On the VPS
python main.py 0xNFT_CONTRACT_ADDRESS -c config.json \
    --sniper \
    --sniper-host 0.0.0.0 \
    --sniper-port 8888 \
    --sniper-secret YOUR_SHARED_SECRET
```

Then from your local browser, configure the userscript with:
- Bot URL: `http://VPS_PUBLIC_IP:8888/signature`
- Shared secret: `YOUR_SHARED_SECRET`

**Security note**: with `--sniper-host 0.0.0.0` the port is exposed to the
public internet. ALWAYS set `--sniper-secret` to a random 32+ character
string, and ideally put the bot behind a reverse proxy with TLS (Caddy
recommended — see Section 8).

---

## 6. Per-Drop Workflow

Once Tampermonkey + bot are configured, every drop follows this flow:

```
T-30 min : Drop time approaches
T-15 min : Run bot in sniper mode (Section 5)
            $ python main.py 0xCONTRACT -c config.json --sniper
            ↓
            Bot pre-fetches drop info, parses phases, evaluates eligibility
            Bot starts HTTP server, prints "Sniper mode active"

T-10 min : Open browser, navigate to drop page on OpenSea
            ↓
            Tampermonkey auto-loads sniper script
            Floating panel appears: "🟢 armed"
            
T-5 min  : Verify wallet connected to OpenSea (MetaMask)
            Walk away (script handles the rest)

T = 0   : Phase opens
            ↓
            [50ms]   Userscript poll detects Mint button enabled
            [100ms]  Userscript auto-clicks Mint
            [400ms]  OpenSea backend signs & returns signature
            [450ms]  Userscript intercepts signature, POSTs to bot
            [500ms]  Bot receives, builds tx, multi-RPC broadcasts
            [600ms]  Tx in mempool
            [12s]    Tx mined ✓

[Bot console output]
            sniper: signature received wallet=0xabc... contract=0x... sig=0x...
            sniper: firing wallet=0xabc... salt=0x... sig=0x...
            [main] tx submitted: 0xfedcba...
            [main] mint succeeded in block 19234567
```

The `🟢 armed` indicator in the panel changes through `clicked` →
`captured` → `fired → bot` as the pipeline progresses.

---

## 7. Multi-Wallet Support

The sniper accepts signatures from multiple wallets. To use:

1. Add all wallets to `wallets.txt`.
2. For each wallet, log into OpenSea with that wallet (use multiple browser
   profiles or sign out/in between).
3. Open the drop page and trigger the userscript for each wallet **before**
   T=0, OR open multiple browser tabs each logged into a different wallet.
4. At T=0, each tab fires its own signature → bot receives N signatures →
   bot fires N transactions in parallel.

**Tip**: Chrome profiles are the cleanest way to manage multi-wallet:

```
Chrome menu → Manage profiles → Add profile
  Profile A: MetaMask account A → tab 1: drop page A
  Profile B: MetaMask account B → tab 2: drop page B
```

Each profile is independent (separate cookies, separate MetaMask session).

---

## 8. Optional: Reverse Proxy with TLS (VPS Production)

If running on a VPS, expose the bot behind a TLS proxy:

### Caddy (recommended, automatic Let's Encrypt)

Install Caddy on your VPS, then create `/etc/caddy/Caddyfile`:

```caddy
sniper.yourdomain.com {
    reverse_proxy 127.0.0.1:8888
    
    # Optional: limit to your IP only
    @blocked not remote_ip 1.2.3.4   # your home IP
    respond @blocked 403
}
```

Then:

```bash
sudo systemctl reload caddy
```

Caddy auto-fetches a TLS cert. Now use `https://sniper.yourdomain.com/signature`
in the userscript Bot URL.

### nginx alternative

```nginx
server {
    listen 443 ssl;
    server_name sniper.yourdomain.com;
    ssl_certificate /etc/letsencrypt/live/sniper.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/sniper.yourdomain.com/privkey.pem;
    
    location / {
        proxy_pass http://127.0.0.1:8888;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

---

## 9. Troubleshooting

### Userscript panel not appearing

- Verify Tampermonkey toggle is ON for "OpenSea SeaDrop Sniper".
- Check the Tampermonkey icon — it should show a number badge when scripts
  are running on the current page.
- Open DevTools Console (F12) — look for `[Sniper] initialized` log.

### Bot logs "wallet ... not in wallets.txt"

The userscript captured a signature for a wallet that's not in your
`wallets.txt`. Either:
- Switch to a wallet that IS in your `wallets.txt` (via MetaMask)
- OR add the wallet's private key to `wallets.txt` (then restart bot)

### Bot logs "Bot rejected (401): unauthorized"

You've configured `--sniper-secret` on the bot but the userscript isn't
sending the matching value. Open Tampermonkey → Configure Sniper, paste the
same secret string.

### "Bot unreachable"

- Confirm bot is running and you see "Listening on ..." in its console.
- If localhost: try `curl http://127.0.0.1:8888/health` from a terminal.
- If VPS: try `curl http://VPS_IP:8888/health` from your local machine.
- Firewall: ensure port 8888 (or whatever you chose) is open on the VPS.
  ```
  sudo ufw allow 8888/tcp   # Ubuntu
  ```

### Mint button found but never clicks

- Some drops use custom button text ("Buy", "Claim", etc). Edit
  `findMintButton()` in the userscript to match additional patterns.
- The button might be inside a Shadow DOM. Tampermonkey scripts can't
  access shadow DOM by default — open an issue if this happens.

### Signature captured but tx fails on-chain

Check the bot logs for the revert reason:
- `IneligibleAccountForFeeRecipient`: the `fee_recipient` doesn't match
  the on-chain SeaDrop config. Update `mint.fee_recipient` in your config.
- `MintQuantityExceedsMaxMintedPerWallet`: lower `amount_per_wallet`.
- `IneligibleAccount`: the captured signature is for a different wallet,
  or the phase index doesn't match. Verify userscript captured the right
  one.

### Signature is captured TWICE (browser shows two captures)

The userscript intercepts both `fetch` and `XHR`. If OpenSea uses both
for the same request (rare), you might see a duplicate. Bot's
`fired_wallets` set deduplicates by wallet address, so only one tx will
fire. Safe to ignore.

---

## 10. Performance Tuning

### Lower poll interval (faster detection)

In Tampermonkey → Edit script → change `pollIntervalMs` default from `50`
to e.g. `20`. Trade-off: higher CPU usage on the browser tab.

### Pre-build cache window

In `config.json` under `"scheduler"`:
```json
{
    "prebuild_lead_seconds": 8,
    "broadcast_rpc_count": 4
}
```
Bot pre-builds the tx N seconds before the phase opens (cached) and
broadcasts to N RPCs in parallel. Higher = more chance the tx is ready;
lower = less stale (fresh nonce + gas).

### Co-locate browser + bot

Browser → Tampermonkey → bot has a network latency overhead of ~10-200ms
depending on hop. Best setups (in order of speed):

1. Bot on the same machine as browser: ~5ms latency (best)
2. Bot on Singapore VPS, browser at home (Indonesia): ~30ms
3. Bot on US-east VPS, browser at home: ~250ms (Singapore is closer)

For most FCFS drops, option 1 is fast enough. Use VPS only if you also
need 24/7 uptime or a US-east region for chain-specific reasons.

---

## 11. Security Best Practices

- **Never** put your private key in the userscript. The userscript only
  intercepts signatures from OpenSea; it never touches PKs.
- **Never** expose the bot HTTP port without `--sniper-secret`.
- Use a **dedicated wallet** for FCFS sniping with a small balance, not
  your main vault.
- Add `wallets.txt` and `.env` to `.gitignore` (already done).
- Regularly rotate the shared secret if you suspect VPS compromise.

---

## 12. Files Reference

```
opensea-mint-bot/
├── main.py                              # CLI + sniper dispatcher
├── bot/
│   └── sniper.py                        # HTTP server + signature handler
├── userscript/
│   └── opensea-sniper.user.js           # Tampermonkey script
├── config.json                          # bot config (gas, scheduler, RPCs)
├── wallets.txt                          # private keys (gitignored)
└── SNIPER_SETUP.md                      # this file
```

---

## 13. Quick Reference

| Task                                   | Command / action                                      |
| -------------------------------------- | ----------------------------------------------------- |
| Start sniper bot (local)               | `python main.py 0xCONTRACT -c config.json --sniper`  |
| Start sniper bot (VPS, public)         | add `--sniper-host 0.0.0.0 --sniper-secret SECRET`   |
| Test bot is running                    | `curl http://127.0.0.1:8888/health`                   |
| Configure userscript                   | Tampermonkey icon → 🎯 Configure Sniper               |
| Reset capture state                    | Tampermonkey icon → 🔄 Reset Capture State            |
| Stop sniper bot                        | Ctrl+C in bot terminal                                |
| Disable userscript temporarily         | Tampermonkey dashboard → toggle OFF                   |

That's everything. Happy sniping.
