// ==UserScript==
// @name         OpenSea SeaDrop Sniper
// @namespace    https://github.com/local/opensea-mint-bot
// @version      1.0.0
// @description  Auto-click Mint button, intercept signed-mint signature, forward to local bot for instant on-chain fire.
// @author       local
// @match        https://opensea.io/*
// @match        https://*.opensea.io/*
// @match        https://testnets.opensea.io/*
// @run-at       document-start
// @grant        GM_xmlhttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_registerMenuCommand
// @connect      *
// ==/UserScript==

/*
 * OpenSea SeaDrop Sniper - Userscript companion to opensea-mint-bot
 * 
 * What it does:
 *  1. Watches for the "Mint" button on SeaDrop drop pages.
 *  2. When the button enables (phase opens), auto-clicks it instantly.
 *  3. Intercepts the network response containing { salt, signature } from
 *     OpenSea's backend signing endpoint.
 *  4. Auto-rejects the MetaMask popup so no tx is sent from the browser.
 *  5. Forwards the signature payload to the local mint bot via HTTP POST.
 *  6. Bot fires the on-chain tx using its multi-RPC pipeline.
 * 
 * Configuration: click the Tampermonkey menu -> "Configure Sniper".
 */

(function () {
    'use strict';

    // --------------------------------------------------------------------
    // Configuration
    // --------------------------------------------------------------------
    const DEFAULTS = {
        botUrl: 'http://127.0.0.1:8888/signature',  // sniper bot endpoint
        sharedSecret: '',                            // if bot requires auth
        autoClick: true,                             // auto-click Mint button
        autoRejectMetamask: true,                    // auto-reject popup
        pollIntervalMs: 50,                          // button enable poll rate
        targetContract: '',                          // optional filter (lowercase 0x..)
        targetPhase: '',                             // 'public' | 'allowlist' | 'signed' | ''
        clickAfterUtcMs: 0,                          // 0 = no time gate. Set epoch ms to delay click until that time.
        clickBeforeUtcMs: 0,                         // 0 = no upper bound. Set to skip click after phase ends.
        autoResetAfterMs: 90000,                     // reset state if no signature captured N ms after click (0 = disabled)
        phaseName: '',                               // free-form label for panel UI (e.g. 'FCFS WL')
        verboseLogs: false,
    };

    const cfg = {
        botUrl: GM_getValue('botUrl', DEFAULTS.botUrl),
        sharedSecret: GM_getValue('sharedSecret', DEFAULTS.sharedSecret),
        autoClick: GM_getValue('autoClick', DEFAULTS.autoClick),
        autoRejectMetamask: GM_getValue('autoRejectMetamask', DEFAULTS.autoRejectMetamask),
        pollIntervalMs: GM_getValue('pollIntervalMs', DEFAULTS.pollIntervalMs),
        targetContract: GM_getValue('targetContract', DEFAULTS.targetContract),
        targetPhase: GM_getValue('targetPhase', DEFAULTS.targetPhase),
        clickAfterUtcMs: GM_getValue('clickAfterUtcMs', DEFAULTS.clickAfterUtcMs),
        clickBeforeUtcMs: GM_getValue('clickBeforeUtcMs', DEFAULTS.clickBeforeUtcMs),
        autoResetAfterMs: GM_getValue('autoResetAfterMs', DEFAULTS.autoResetAfterMs),
        phaseName: GM_getValue('phaseName', DEFAULTS.phaseName),
        verboseLogs: GM_getValue('verboseLogs', DEFAULTS.verboseLogs),
    };

    function persist(key, val) {
        GM_setValue(key, val);
        cfg[key] = val;
    }

    function log(...args) {
        // Always print critical messages; verbose only when enabled.
        console.log('%c[Sniper]', 'color:#22c55e;font-weight:bold', ...args);
    }
    function vlog(...args) {
        if (cfg.verboseLogs) {
            console.log('%c[Sniper:v]', 'color:#94a3b8', ...args);
        }
    }

    // --------------------------------------------------------------------
    // State
    // --------------------------------------------------------------------
    const state = {
        captured: false,        // signature already captured this session
        firedAt: null,          // timestamp of auto-click
        contract: '',           // detected from URL/page
        wallet: '',             // detected from intercepted request
    };

    // Extract NFT contract address from current URL.
    // OpenSea drop URLs look like:
    //   https://opensea.io/collection/<slug>
    //   https://opensea.io/assets/ethereum/0xCONTRACT/<id>
    //   https://opensea.io/seadrop/<slug>
    function detectContract() {
        const url = window.location.href;
        const match = url.match(/0x[a-fA-F0-9]{40}/);
        if (match) {
            state.contract = match[0].toLowerCase();
        }
        return state.contract;
    }

    // --------------------------------------------------------------------
    // Floating status panel
    // --------------------------------------------------------------------
    let panel = null;
    function ensurePanel() {
        if (panel || !document.body) return;
        panel = document.createElement('div');
        panel.id = 'sniper-panel';
        panel.style.cssText = [
            'position:fixed', 'bottom:12px', 'right:12px', 'z-index:2147483647',
            'background:rgba(15,23,42,0.95)', 'color:#f1f5f9',
            'border:1px solid #22c55e', 'border-radius:8px',
            'padding:10px 14px', 'font:12px/1.45 -apple-system,Segoe UI,Roboto,sans-serif',
            'box-shadow:0 8px 24px rgba(0,0,0,.4)', 'min-width:240px',
            'pointer-events:auto',
        ].join(';');
        panel.innerHTML = `
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
                <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#22c55e"></span>
                <strong style="font-size:13px">SeaDrop Sniper</strong>
                <span id="sniper-status" style="margin-left:auto;font-size:11px;color:#22c55e">armed</span>
            </div>
            <div id="sniper-phase" style="font-size:11px;color:#fbbf24;margin-bottom:4px;display:none">
                phase: <span id="sniper-phase-name">-</span>
                <span id="sniper-countdown" style="float:right;color:#fbbf24;font-variant-numeric:tabular-nums"></span>
            </div>
            <div id="sniper-info" style="font-size:11px;color:#cbd5e1;margin-bottom:6px">
                contract: <span id="sniper-contract">-</span><br>
                bot: <span id="sniper-bot">${escapeHtml(cfg.botUrl)}</span>
            </div>
            <div id="sniper-log" style="font-size:11px;color:#94a3b8;max-height:120px;overflow:auto;border-top:1px solid #1e293b;padding-top:6px"></div>
        `;
        document.body.appendChild(panel);
        renderContract();
        renderPhase();
    }

    function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, c => ({
            '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
        }[c]));
    }

    function setStatus(text, color) {
        const el = document.getElementById('sniper-status');
        if (el) {
            el.textContent = text;
            if (color) el.style.color = color;
        }
    }

    function renderContract() {
        const el = document.getElementById('sniper-contract');
        if (el) el.textContent = state.contract || '(detect on Mint click)';
    }

    function renderPhase() {
        const wrap = document.getElementById('sniper-phase');
        const nameEl = document.getElementById('sniper-phase-name');
        if (!wrap || !nameEl) return;
        const hasTarget = cfg.clickAfterUtcMs > 0 || cfg.phaseName;
        wrap.style.display = hasTarget ? '' : 'none';
        nameEl.textContent = cfg.phaseName || '(target time set)';
        updateCountdown();
    }

    function formatCountdown(ms) {
        if (ms <= 0) return 'OPEN';
        const totalSec = Math.floor(ms / 1000);
        const h = Math.floor(totalSec / 3600);
        const m = Math.floor((totalSec % 3600) / 60);
        const s = totalSec % 60;
        const pad = (n) => String(n).padStart(2, '0');
        if (h > 0) return `T-${h}h ${pad(m)}m ${pad(s)}s`;
        return `T-${pad(m)}m ${pad(s)}s`;
    }

    function updateCountdown() {
        const el = document.getElementById('sniper-countdown');
        if (!el) return;
        if (!cfg.clickAfterUtcMs) {
            el.textContent = '';
            return;
        }
        const remain = cfg.clickAfterUtcMs - Date.now();
        el.textContent = formatCountdown(remain);
        // Yellow before open, green at open, red after click window ended
        if (cfg.clickBeforeUtcMs && Date.now() > cfg.clickBeforeUtcMs) {
            el.style.color = '#ef4444';
        } else if (remain <= 0) {
            el.style.color = '#22c55e';
        } else {
            el.style.color = '#fbbf24';
        }
    }

    function uiLog(msg) {
        const el = document.getElementById('sniper-log');
        if (!el) return;
        const ts = new Date().toLocaleTimeString();
        const line = document.createElement('div');
        line.textContent = `[${ts}] ${msg}`;
        el.appendChild(line);
        el.scrollTop = el.scrollHeight;
        log(msg);
    }

    // --------------------------------------------------------------------
    // Network interception (fetch + XHR)
    //
    // OpenSea's backend signing endpoint returns JSON containing fields like
    // `signature`, `salt`, plus mint params. Path patterns vary; we match
    // the response body shape rather than URL to be resilient to path changes.
    // --------------------------------------------------------------------
    function looksLikeSigPayload(obj) {
        if (!obj || typeof obj !== 'object') return false;
        // Accept any object that has a hex signature plus a salt/nonce field.
        const sig = obj.signature || obj.sig || obj.signed?.signature;
        const salt = obj.salt || obj.nonce || obj.signed?.salt;
        if (typeof sig !== 'string' || typeof salt !== 'string') return false;
        if (!/^0x[a-fA-F0-9]{2,}$/.test(sig)) return false;
        if (!/^0x[a-fA-F0-9]{2,}$/.test(salt)) return false;
        return true;
    }

    function extractSig(obj) {
        const sig = obj.signature || obj.sig || obj.signed?.signature;
        const salt = obj.salt || obj.nonce || obj.signed?.salt;
        const minter = obj.minter || obj.wallet || obj.recipient || obj.signed?.minter || '';
        const phase = obj.phase || obj.dropStage || obj.stage || obj.dropStageIndex || '';
        return {
            signature: sig,
            salt,
            minter: typeof minter === 'string' ? minter.toLowerCase() : '',
            phase: typeof phase === 'string' ? phase : (typeof phase === 'number' ? `stage_${phase}` : ''),
            contract: state.contract,
            captured_at: Date.now(),
            raw: obj,
        };
    }

    // Wrap fetch
    const _fetch = window.fetch;
    window.fetch = async function patchedFetch(...args) {
        const res = await _fetch.apply(this, args);
        try {
            const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
            if (/opensea/i.test(url)) {
                const ct = res.headers.get('content-type') || '';
                if (ct.includes('json')) {
                    const clone = res.clone();
                    clone.text().then(txt => {
                        let data;
                        try { data = JSON.parse(txt); } catch { return; }
                        scanForSignature(data, url);
                    }).catch(() => {});
                }
            }
        } catch (e) {
            vlog('fetch hook error', e);
        }
        return res;
    };

    // Wrap XHR
    const _XHROpen = XMLHttpRequest.prototype.open;
    const _XHRSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (method, url, ...rest) {
        this.__sniperUrl = url;
        return _XHROpen.call(this, method, url, ...rest);
    };
    XMLHttpRequest.prototype.send = function (...args) {
        this.addEventListener('load', () => {
            try {
                const url = this.__sniperUrl || '';
                if (!/opensea/i.test(url)) return;
                if (!this.responseText) return;
                let data;
                try { data = JSON.parse(this.responseText); } catch { return; }
                scanForSignature(data, url);
            } catch (e) {
                vlog('xhr hook error', e);
            }
        });
        return _XHRSend.apply(this, args);
    };

    function scanForSignature(data, url) {
        if (state.captured) return;
        // Walk arbitrary structure depth 4
        const found = findSig(data, 4);
        if (!found) return;
        state.captured = true;
        const payload = extractSig(found);
        if (!state.contract) detectContract();
        if (!payload.contract) payload.contract = state.contract;
        uiLog(`✓ Signature captured (sig=${payload.signature.slice(0, 12)}…)`);
        setStatus('captured', '#22c55e');
        forwardToBot(payload);
    }

    function findSig(node, depth) {
        if (!node || depth < 0) return null;
        if (looksLikeSigPayload(node)) return node;
        if (Array.isArray(node)) {
            for (const item of node) {
                const r = findSig(item, depth - 1);
                if (r) return r;
            }
            return null;
        }
        if (typeof node === 'object') {
            for (const k of Object.keys(node)) {
                const r = findSig(node[k], depth - 1);
                if (r) return r;
            }
        }
        return null;
    }

    // --------------------------------------------------------------------
    // Forward signature to local bot
    // --------------------------------------------------------------------
    function forwardToBot(payload) {
        if (!cfg.botUrl) {
            uiLog('⚠ No bot URL configured. Click menu -> Configure.');
            return;
        }
        const body = JSON.stringify({
            ...payload,
            shared_secret: cfg.sharedSecret || undefined,
            user_agent: navigator.userAgent,
            page: location.href,
        });
        GM_xmlhttpRequest({
            method: 'POST',
            url: cfg.botUrl,
            headers: {
                'Content-Type': 'application/json',
                'X-Sniper-Source': 'tampermonkey',
            },
            data: body,
            timeout: 5000,
            onload: (res) => {
                if (res.status >= 200 && res.status < 300) {
                    uiLog(`✓ Bot accepted (${res.status})`);
                    setStatus('fired → bot', '#22c55e');
                } else {
                    uiLog(`✗ Bot rejected (${res.status}): ${res.responseText.slice(0, 80)}`);
                    setStatus('bot error', '#ef4444');
                }
            },
            onerror: (err) => {
                uiLog(`✗ Bot unreachable: ${err.error || 'connection failed'}`);
                setStatus('bot offline', '#ef4444');
            },
            ontimeout: () => {
                uiLog('✗ Bot timeout (>5s)');
                setStatus('timeout', '#f59e0b');
            },
        });
    }

    // --------------------------------------------------------------------
    // Mint button auto-click
    //
    // Strategy: query buttons whose text matches /mint/i and check if
    // they are NOT disabled. Re-evaluate every pollIntervalMs ms because
    // OpenSea uses React and the button toggles enabled state at phase open.
    // --------------------------------------------------------------------
    let pollTimer = null;
    function startButtonPoller() {
        if (pollTimer) return;
        pollTimer = setInterval(tryClickMint, cfg.pollIntervalMs);
        vlog(`button poller started (every ${cfg.pollIntervalMs}ms)`);
    }
    function stopButtonPoller() {
        if (pollTimer) {
            clearInterval(pollTimer);
            pollTimer = null;
        }
    }

    function findMintButton() {
        const buttons = Array.from(document.querySelectorAll('button, [role="button"]'));
        for (const b of buttons) {
            const txt = (b.textContent || '').trim().toLowerCase();
            if (!txt) continue;
            // Match common labels: "Mint", "Mint now", "Mint X", "Buy"
            if (/^mint\b/.test(txt) || txt === 'mint' || /^mint\s/.test(txt)) {
                return b;
            }
        }
        return null;
    }

    function isClickable(btn) {
        if (!btn) return false;
        if (btn.disabled) return false;
        const ariaDisabled = btn.getAttribute('aria-disabled');
        if (ariaDisabled === 'true') return false;
        const style = window.getComputedStyle(btn);
        if (style.pointerEvents === 'none') return false;
        if (style.display === 'none' || style.visibility === 'hidden') return false;
        // Some UI keep button visible but with low opacity when disabled
        const op = parseFloat(style.opacity || '1');
        if (op < 0.5) return false;
        return true;
    }

    function tryClickMint() {
        if (state.firedAt) return;     // already clicked once this cycle
        if (!cfg.autoClick) return;
        const now = Date.now();
        // Time guards — prevent firing during wrong phase
        if (cfg.clickAfterUtcMs && now < cfg.clickAfterUtcMs) {
            // Not yet target phase open
            return;
        }
        if (cfg.clickBeforeUtcMs && now >= cfg.clickBeforeUtcMs) {
            // Target phase window ended
            vlog('click window ended — disarming');
            stopButtonPoller();
            setStatus('window ended', '#ef4444');
            return;
        }
        const btn = findMintButton();
        if (!btn) return;
        if (!isClickable(btn)) {
            vlog('Mint button found but not clickable yet');
            return;
        }
        // Click!
        state.firedAt = now;
        const phaseLabel = cfg.phaseName ? ` [${cfg.phaseName}]` : '';
        uiLog(`▶ Mint button enabled${phaseLabel} — auto-clicking`);
        setStatus('clicked', '#22c55e');
        try {
            btn.click();
        } catch (e) {
            uiLog(`✗ Click failed: ${e.message}`);
        }
        // Stop polling after a short grace period — keep just in case the
        // first click was eaten by a modal animation.
        setTimeout(stopButtonPoller, 2000);
        // Auto-reset: if no signature is captured within autoResetAfterMs,
        // assume the click was for a phase this wallet wasn't eligible for
        // (e.g. KOL/GTD window for a FCFS-only wallet). Reset state so the
        // next phase open can trigger another click.
        if (cfg.autoResetAfterMs > 0) {
            const clickedAt = state.firedAt;
            setTimeout(() => {
                if (state.captured) return;            // success — keep state
                if (state.firedAt !== clickedAt) return; // already reset by user
                uiLog(`⟲ No signature in ${Math.round(cfg.autoResetAfterMs/1000)}s — auto-reset for next phase`);
                state.firedAt = null;
                state.captured = false;
                setStatus('armed', '#22c55e');
                startButtonPoller();
            }, cfg.autoResetAfterMs);
        }
    }

    // --------------------------------------------------------------------
    // MetaMask auto-reject
    //
    // We DO NOT want the tx to be sent from the browser — the bot will
    // build and broadcast its own tx with the captured signature. Auto-
    // rejecting the popup avoids accidental double-spend / fee waste.
    //
    // We patch window.ethereum.request to intercept eth_sendTransaction.
    // --------------------------------------------------------------------
    function patchEthereum() {
        const eth = window.ethereum;
        if (!eth || eth.__sniperPatched) return;
        const origRequest = eth.request?.bind(eth);
        if (!origRequest) return;
        eth.request = async function patchedRequest(args) {
            try {
                if (cfg.autoRejectMetamask && args && args.method === 'eth_sendTransaction') {
                    uiLog('⤺ Auto-rejecting MetaMask sendTransaction (bot will fire)');
                    return Promise.reject(new Error('Sniper: rejected (handled by bot)'));
                }
            } catch (e) {
                vlog('eth patch error', e);
            }
            return origRequest(args);
        };
        eth.__sniperPatched = true;
        vlog('window.ethereum patched');
    }

    // --------------------------------------------------------------------
    // Tampermonkey menu commands
    // --------------------------------------------------------------------
    function registerMenu() {
        try {
            GM_registerMenuCommand('🎯 Configure Sniper', () => {
                const url = prompt('Bot URL (e.g. http://127.0.0.1:8888/signature):', cfg.botUrl);
                if (url !== null) persist('botUrl', url.trim() || DEFAULTS.botUrl);
                const sec = prompt('Shared secret (optional, blank to skip):', cfg.sharedSecret);
                if (sec !== null) persist('sharedSecret', sec.trim());
                const auto = confirm('Enable auto-click Mint button? (OK=yes, Cancel=no)');
                persist('autoClick', auto);
                const reject = confirm('Auto-reject MetaMask popup? (OK=yes, Cancel=no)');
                persist('autoRejectMetamask', reject);
                alert(`Sniper configured.\n\nbotUrl: ${cfg.botUrl}\nautoClick: ${cfg.autoClick}\nautoRejectMetamask: ${cfg.autoRejectMetamask}`);
                renderContract();
                const elBot = document.getElementById('sniper-bot');
                if (elBot) elBot.textContent = cfg.botUrl;
            });
            GM_registerMenuCommand('⏰ Set Target Phase Time', () => {
                const help = (
                    'Enter target phase opening time.\n\n' +
                    'Accepted formats:\n' +
                    '  • ISO UTC:       2026-05-11T15:45:00Z\n' +
                    '  • ISO local:     2026-05-11T22:45:00+07:00\n' +
                    '  • Local time:    2026-05-11 22:45  (uses your browser timezone)\n' +
                    '  • Blank/0:       clear time guard\n\n' +
                    'Tampermonkey will NOT auto-click before this time.'
                );
                const current = cfg.clickAfterUtcMs ? new Date(cfg.clickAfterUtcMs).toISOString() : '';
                const input = prompt(help, current);
                if (input === null) return;
                const trimmed = input.trim();
                let ms = 0;
                if (trimmed && trimmed !== '0') {
                    let parsed = Date.parse(trimmed);
                    if (isNaN(parsed) && /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}/.test(trimmed)) {
                        // "YYYY-MM-DD HH:MM" without timezone → assume local
                        parsed = Date.parse(trimmed.replace(' ', 'T'));
                    }
                    if (isNaN(parsed)) {
                        alert(`Could not parse: ${trimmed}\nKept previous value.`);
                        return;
                    }
                    ms = parsed;
                }
                persist('clickAfterUtcMs', ms);
                const phase = prompt('Phase name label (optional, e.g. "FCFS WL"):', cfg.phaseName);
                if (phase !== null) persist('phaseName', phase.trim());
                const endInput = prompt(
                    'Optional: phase END time (skip clicks after this). Blank/0 = no upper bound.',
                    cfg.clickBeforeUtcMs ? new Date(cfg.clickBeforeUtcMs).toISOString() : ''
                );
                if (endInput !== null) {
                    const et = endInput.trim();
                    let endMs = 0;
                    if (et && et !== '0') {
                        let p = Date.parse(et);
                        if (isNaN(p) && /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}/.test(et)) {
                            p = Date.parse(et.replace(' ', 'T'));
                        }
                        if (!isNaN(p)) endMs = p;
                    }
                    persist('clickBeforeUtcMs', endMs);
                }
                const summary = (
                    `Target time:  ${ms ? new Date(ms).toString() : '(none)'}\n` +
                    `Phase label:  ${cfg.phaseName || '(none)'}\n` +
                    `End time:     ${cfg.clickBeforeUtcMs ? new Date(cfg.clickBeforeUtcMs).toString() : '(none)'}`
                );
                alert('Time guard updated.\n\n' + summary);
                renderPhase();
            });
            GM_registerMenuCommand('🔄 Reset Capture State', () => {
                state.captured = false;
                state.firedAt = null;
                setStatus('armed', '#22c55e');
                uiLog('State reset — ready for next drop');
                startButtonPoller();
            });
            GM_registerMenuCommand('🧹 Clear Time Guard', () => {
                persist('clickAfterUtcMs', 0);
                persist('clickBeforeUtcMs', 0);
                persist('phaseName', '');
                renderPhase();
                alert('Time guard cleared. Sniper will click as soon as Mint button is enabled.');
            });
            GM_registerMenuCommand('📊 Toggle Verbose Logs', () => {
                persist('verboseLogs', !cfg.verboseLogs);
                alert(`Verbose logs: ${cfg.verboseLogs ? 'ON' : 'OFF'}`);
            });
        } catch (e) {
            // GM functions may be unavailable in some adapters; non-fatal.
            log('menu registration failed:', e);
        }
    }

    // --------------------------------------------------------------------
    // Bootstrap
    // --------------------------------------------------------------------
    function init() {
        detectContract();
        ensurePanel();
        renderContract();
        renderPhase();
        registerMenu();
        patchEthereum();
        startButtonPoller();
        // Countdown ticker — refresh phase countdown every second
        setInterval(updateCountdown, 1000);
        // Re-patch ethereum on every event in case MM injects late
        window.addEventListener('ethereum#initialized', patchEthereum, { once: true });
        // Re-detect contract on SPA navigations
        let lastUrl = location.href;
        new MutationObserver(() => {
            if (location.href !== lastUrl) {
                lastUrl = location.href;
                detectContract();
                renderContract();
                // Reset state on navigation
                if (!state.captured) startButtonPoller();
            }
        }).observe(document, { subtree: true, childList: true });
        log('initialized', cfg);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init, { once: true });
    } else {
        init();
    }
})();
