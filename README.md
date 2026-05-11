Bot Python untuk mint NFT di **OpenSea (SeaDrop)** dengan dukungan
multi-akun, multi-chain (Ethereum, Base, Arbitrum, Optimism, Polygon),
pengaturan gas yang fleksibel, deteksi otomatis fase mint
(Public / FCFS / Guaranteed) beserta jadwal & eligibility per wallet,
dan eksekusi paralel.

> Bot ini menggunakan **OpenSea SeaDrop** (`0x00005EA00Ac477B1030CE78506496e8C2dE24bf5`)
> sebagai mekanisme mint resmi. Karena alamat SeaDrop sama di setiap chain
> (deterministic CREATE2), bot otomatis mendeteksi fase yang tersedia.

---

## Fitur

- **Multi akun**: load private key dari `wallets.txt` (satu per baris) atau JSON.
- **Multi chain**: Ethereum, Base, Arbitrum, Optimism, Polygon - default RPC publik
  bawaan + override RPC kustom (termasuk RPC private/Alchemy/Infura).
- **3 jenis mint SeaDrop**:
  - `mintPublic` — public phase, full auto.
  - `mintAllowList` — merkle-based Guaranteed/FCFS lama, butuh `allowlist.json`.
  - `mintSigned` — signed mint Guaranteed/FCFS modern (OpenSea Studio 2024+),
    butuh `signed_mints.json` dengan signature scrape dari DevTools.
- **Deteksi fase otomatis**: membaca `getPublicDrop`, `getAllowListMerkleRoot`,
  `getSigners`, `getMintStats` dari kontrak SeaDrop on-chain.
- **Eligibility per wallet**: tahu wallet kamu eligible di Public / Guaranteed
  / FCFS / Signed sebelum mint mulai.
- **Harga otomatis**: `mint_price` dibaca langsung dari kontrak (tidak perlu
  input harga manual - sudah sesuai dengan tampilan OpenSea).
- **Setting jumlah mint per wallet** + auto-cap ke `maxTotalMintableByWallet`.
- **Setting gas**: EIP-1559 (`maxFeePerGas`, `priorityFee`) atau legacy
  (`gasPrice`), serta multiplier dan gas limit override.
- **Scheduler**: bot menunggu sampai detik `startTime` (dengan `lead_time_ms`)
  lalu menembak transaksi.
- **Eksekusi paralel** lintas wallet (thread pool).
- **Logging berwarna** ke konsol + rotasi file (`logs/mint_bot.log`).
- **Error handling**: nonce safe-lock, retry otomatis, balance pre-check,
  receipt timeout dengan link explorer.

---

## Persiapan

### 1. Install dependency

```powershell
# (opsional) buat virtual env
python -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

### 2. Siapkan wallet

Buat file `wallets.txt` (lihat format di bawah) dan isi private key satu per baris:

```
0xPRIVATEKEY1,wallet_utama
0xPRIVATEKEY2,alt1
0xPRIVATEKEY3,alt2
```

Format: `PRIVATE_KEY[,LABEL]`. Label opsional, dipakai untuk log.

> **PENTING**: Jangan pernah commit `wallets.txt`. Folder sudah memuat
> `.gitignore` yang mengecualikan file ini.

### 3. (Opsional) Siapkan config

Buat file `config.json` di folder project. Bot juga bisa dijalankan tanpa
file config (menu interaktif). Untuk struktur lengkap, lihat section
[Config](#config) di bawah.

---

## Menjalankan

### Mode interaktif (rekomendasi pemula)

```powershell
python main.py
```

Lalu pilih menu:

1. **Configure new mint** - input alamat NFT, chain, jumlah mint, gas, dll.
2. **Load configuration from file** - kalau sudah punya `config.json`.
3. **Save current configuration to file** - simpan setting saat ini.
4. **Check eligibility (no mint)** - lihat fase + status wallet, tanpa mint.
5. **Run mint with current configuration** - jalankan mint sesungguhnya.

### Mode otomatis dari config

```powershell
python main.py --config config.json
```

Tambah `--non-interactive` (atau `-y`) untuk skip konfirmasi (cocok untuk
schedule otomatis):

```powershell
python main.py -c config.json -y
```

Cek eligibility tanpa mint:

```powershell
python main.py -c config.json --check
```

---

## Pengaturan jumlah mint

`config.json -> mint.amount_per_wallet` → jumlah token yang ingin di-mint per
wallet (akan otomatis dipotong jika melewati `maxTotalMintableByWallet`).

Contoh:

```json
"mint": {
  "phase": "auto",
  "amount_per_wallet": 2,
  "fee_recipient": ""
}
```

Atau, di mode interaktif, isi pertanyaan **"Amount to mint per wallet"**.

---

## Pengaturan gas fee

```json
"gas": {
  "mode": "eip1559",        // "eip1559" | "legacy" | "auto"
  "max_fee_gwei": 0,         // 0 = auto (pakai baseFee*2 + priority)
  "priority_fee_gwei": 0,    // 0 = auto (pakai max_priority_fee dari node)
  "gas_price_gwei": 0,       // hanya untuk mode legacy
  "multiplier": 1.25,        // multiplier untuk mode auto
  "gas_limit": 0             // 0 = estimate + 20% buffer
}
```

- **`mode = "auto"`** → bot pilih EIP-1559 jika chain mendukung; multiplier
  diterapkan ke base fee + priority.
- **`mode = "eip1559"`** → paksa EIP-1559. Isi `max_fee_gwei` dan
  `priority_fee_gwei` untuk override total.
- **`mode = "legacy"`** → kirim transaksi legacy `gasPrice`.

Bot akan log gas yang dipakai sebelum kirim, contoh:

```
2025-05-08 02:00:01 | INFO    | minter         | Gas (EIP-1559): baseFee=12.500 gwei priority=1.500 gwei maxFee=33.250 gwei
```

---

## Deteksi jam buka & eligibility

Saat menjalankan **Check eligibility** atau **Run mint**, bot akan
menampilkan tabel seperti ini:

```
Mint phases:
  [0] Guaranteed     type=guaranteed  price=0.010000 ETH max/wallet=1   starts=2025-05-08 12:00:00 UTC (in 1h 30m 0s)
  [1] FCFS           type=fcfs        price=0.015000 ETH max/wallet=2   starts=2025-05-08 13:00:00 UTC (in 2h 30m 0s)
  [2] Public         type=public      price=0.020000 ETH max/wallet=5   starts=2025-05-08 14:00:00 UTC (in 3h 30m 0s)

Wallet eligibility:
  0xAaaa...1111
    - Guaranteed   (guaranteed)  YES  remaining=1  reason=on 'Guaranteed' allowlist
    - FCFS         (fcfs)         no   remaining=0  reason=not on 'FCFS' allowlist
    - Public       (public)      YES  remaining=5  reason=public phase – open to all
```

Sumber data:

| Fase                                | Otomatis                                                          | Manual                                            |
| ----------------------------------- | ----------------------------------------------------------------- | ------------------------------------------------- |
| **Public**                          | `getPublicDrop` dari kontrak SeaDrop                              | tidak perlu                                       |
| **Allowlist merkle** (lama)         | OpenSea API (jika `opensea_api_key` diisi)                        | file `allowlist.json` (proofs per wallet)         |
| **Signed mint** (Guaranteed/FCFS modern) | tidak ada — signature harus discrape dari DevTools (per wallet)  | file `signed_mints.json` (salt+signature per wallet) |

> Drop OpenSea Studio yang baru (2024+) **mayoritas pakai signed mint**, bukan
> merkle proof. Lihat section [Signed Mint](#signed-mint-guaranteed--fcfs-modern)
> di bawah untuk cara dapat signature-nya.

### Stub phases ("data needed")

Saat bot mendeteksi **merkle root** atau **signers** terdaftar on-chain
namun kamu **belum supply** `allowlist.json` / `signed_mints.json`, bot akan
menampilkan **stub phase** dengan status `data needed` di tabel eligibility.
Contoh:

```
Mint phases:
  [0] Allowlist (data missing)  type=allowlist  price=0.0000 ETH  max/wallet=0  status=data needed
  [1] Signed Mint (data missing) type=signed     price=0.0000 ETH  max/wallet=0  status=data needed
  [2] Public                     type=public     price=0.0015 ETH  max/wallet=10 status=live
```

Stub ini memberi petunjuk **dimana eligibility tidak bisa diverifikasi** dan
apa yang perlu kamu lakukan (scrape proof / signature dari DevTools, atau
pakai sniper mode). Saat fire mode dijalankan, stub di-skip otomatis dengan
warning agar tidak mengirim transaksi yang pasti revert.

### Per-wallet phase eligibility (sniper mode)

Drop SeaDrop modern (KOL WL / GTD WL / FCFS WL / Public) hanya dibedakan
di backend OpenSea — kontrak cuma melihat `mintSigned`. Karena tiap wallet
bisa eligible di phase berbeda, bot dapat ditugaskan jadwal phase + map
eligibility manual lewat `config.json`:

```json
{
  "phase_schedule": {
    "KOL WL":  { "start": "2026-05-11T15:00:00Z", "end": "2026-05-11T15:15:00Z" },
    "GTD WL":  { "start": "2026-05-11T15:15:00Z", "end": "2026-05-11T15:45:00Z" },
    "FCFS WL": { "start": "2026-05-11T15:45:00Z", "end": "2026-05-11T16:15:00Z" },
    "Public":  { "start": "2026-05-11T16:15:00Z", "end": "2026-05-11T16:45:00Z" }
  },
  "wallet_eligibility": {
    "0xMAIN_ADDR": ["FCFS WL", "Public"],
    "0xDENI_ADDR": ["KOL WL",  "Public"]
  }
}
```

Saat sniper mode aktif, bot:

1. Print eligibility matrix pas startup (per wallet × phase, `OK` / `--`).
2. Untuk tiap signature masuk: tentukan phase aktif sekarang (UTC ± 60s
   tolerance), cek apakah wallet ada di list eligibility phase tsb.
3. Kalau tidak eligible — **tolak tx** dengan log `REJECT wallet=... reason=...`
   sehingga gas tidak terbuang untuk transaksi yang pasti revert on-chain.

Kalau key `phase_schedule` / `wallet_eligibility` kosong/absent, fitur ini
off dan bot fire signature apapun dari wallet yang terdaftar di
`wallets.txt` (perilaku lama). Cocok untuk drop sederhana 1-phase atau
kalau sudah trust Tampermonkey time guard.

OpenSea sering merotasi endpoint allowlist publiknya, jadi cara paling
andal untuk Guaranteed/FCFS adalah menyediakan file allowlist manual
(lihat `allowlist.json` untuk merkle, `signed_mints.json`
untuk signed). Format minimal merkle:

```json
{
  "phases": [
    {
      "name": "Guaranteed",
      "type": "guaranteed",
      "start_time": 1735689600,
      "end_time":   1735693200,
      "mint_price_wei": "10000000000000000",
      "max_per_wallet": 1,
      "merkle_root": "0xabc...",
      "proofs": {
        "0xWalletA": ["0x...", "0x..."],
        "0xWalletB": ["0x...", "0x..."]
      }
    }
  ]
}
```

Lalu di `config.json`:

```json
"allowlists": ["allowlist.json"]
```

> Tip: kamu bisa scrape proofs dari halaman OpenSea (Network tab → cari
> request berisi `proof`) atau dari Discord/Notion drop tersebut.

---

## Signed Mint (Guaranteed / FCFS modern)

Drop modern di OpenSea Studio (mayoritas drop 2024+) tidak pakai merkle
proof. Mereka pakai **signed mint**: OpenSea backend menanda-tangani tiap
request mint dengan EIP-712, dan signature itu di-verify on-chain saat
panggil `mintSigned()`.

Bot **tidak bisa generate signature sendiri** (signer key dirahasiakan
OpenSea). Tapi bot bisa kirim signature yang sudah di-scrape ke kontrak —
termasuk di detik mint mulai persis, multi-wallet paralel, gas tinggi.

### Cara dapat salt + signature (DevTools, ~5 menit)

1. **Buka halaman drop di OpenSea** + connect wallet kamu yang eligible.
2. **Open DevTools** (`F12` / `Ctrl+Shift+I`) → tab **Network** → filter
   ketik `mint` atau `sign`.
3. **Klik tombol "Mint"** di OpenSea — MetaMask popup muncul. **JANGAN
   confirm tx** (cukup biarkan popup terbuka).
4. **Cari di Network tab** request ke OpenSea backend yang return JSON
   berisi `salt` dan `signature` (atau `signedMintRequest`). Bentuk
   response biasanya:

   ```json
   {
     "mintParams": {
       "mintPrice": "10000000000000000",
       "maxTotalMintableByWallet": "1",
       "startTime": "1735689600",
       "endTime": "1735693200",
       "dropStageIndex": "1",
       "maxTokenSupplyForStage": "5000",
       "feeBps": "250",
       "restrictFeeRecipients": true
     },
     "salt": "0x1234567890abcdef...",
     "signature": "0xabcdef...rsv65bytes...",
     "feeRecipient": "0x0000a26b00c1F0DF003000390027140000fAa719"
   }
   ```

5. **Cancel MetaMask popup** (tidak perlu mint via UI).
6. **Copy** ke `signed_mints.json` (file template tersedia di repo):

   ```json
   {
     "phases": [
       {
         "name": "Guaranteed",
         "type": "signed",
         "start_time": 1735689600,
         "end_time": 1735693200,
         "mint_price_wei": "10000000000000000",
         "max_per_wallet": 1,
         "fee_bps": 250,
         "drop_stage_index": 1,
         "max_token_supply_for_stage": 5000,
         "signed_mints": {
           "0xwallet_kamu_lowercase": {
             "salt": "0x1234567890abcdef...",
             "signature": "0xabcdef..."
           }
         }
       }
     ]
   }
   ```

7. Ulangi step 1–6 untuk **tiap wallet** yang mau ikut mint (signature
   per-wallet, tidak bisa share antar wallet).
8. Daftarkan di `config.json`:

   ```json
   {
     "allowlists": ["signed_mints.json"]
   }
   ```

9. Test: `python main.py -c config.json --check`. Bot harus tampilkan:

   ```
   Wallet eligibility:
     0xWALLET...
       - Guaranteed (signed)  YES  reason=have signed-mint payload for 'Guaranteed'
   ```

### Tips DevTools

- Filter Network tab pakai `XHR` lalu sort by **Time** (DESC) — request
  signature biasanya yang paling baru saat klik Mint.
- Beberapa drop pakai GraphQL, search `query` atau `mutation` di Network
  filter, body request mengandung kata `mint` atau `sign`.
- Pastikan `salt` dan `signature` di-copy **persis** termasuk `0x` prefix.
- Signature biasanya 130-character hex (65 bytes ECDSA r||s||v).
- `salt` bisa berupa hex (`0x...`) atau decimal (`"123456789"`); bot
  handle dua-duanya.

### Cara cek drop pakai signed mint atau merkle

Saat jalankan `python main.py -c config.json --check`, bot akan log:

```
2026-05-08 02:00:01 | INFO | eligibility | SeaDrop signed-mint signer(s) registered: 0xfCe4...F8C3 -> drop supports mintSigned
```

Kalau muncul log seperti itu = drop pakai **signed mint**, butuh
`signed_mints.json`. Kalau muncul:

```
2026-05-08 02:00:01 | INFO | eligibility | SeaDrop allowlist merkle root present: 0xabc...
```

= drop pakai **merkle allowlist**, butuh `allowlist.json` dengan
`proofs`. Kalau dua-duanya tidak muncul (cuma public phase) = tinggal
`mintPublic`, no extra files needed.

---

## Multi-chain & RPC

Default supported chains:

| Key        | Name           | Chain ID |
| ---------- | -------------- | -------- |
| `ethereum` | Ethereum       | 1        |
| `base`     | Base           | 8453     |
| `arbitrum` | Arbitrum One   | 42161    |
| `optimism` | Optimism       | 10       |
| `polygon`  | Polygon        | 137      |

Bot mencoba beberapa RPC publik secara berurutan. Untuk hasil terbaik
saat mint kompetitif, gunakan RPC private (Alchemy / Infura / QuickNode):

```json
{
  "chain": "base",
  "rpc_url": "https://base-mainnet.g.alchemy.com/v2/<APIKEY>"
}
```

Atau di mode interaktif, isi pertanyaan **"Custom RPC URL"**.

---

## Behaviour saat mint

1. Bot urutkan fase berdasarkan `startTime`.
2. Untuk tiap fase: hitung wallet yang eligible & belum hit cap.
3. Sleep sampai `startTime - lead_time_ms`, lalu broadcast paralel
   per wallet.
4. Jika `stop_on_first_success_per_wallet` = `true`, wallet yang sudah
   sukses di fase awal (mis. Guaranteed) tidak ikut fase berikutnya
   (mis. Public) → hemat gas.
5. Setiap kegagalan akan retry sebanyak `max_retries` dengan delay
   `retry_delay_ms`.

---

## Logging

- Konsol berwarna (level INFO ke atas).
- File rotasi `logs/mint_bot.log` (5 MB × 5 backup).
- Atur level via `config.json -> logging.level`
  (`DEBUG`, `INFO`, `WARNING`, `ERROR`).

Contoh output ringkas:

```
2025-05-08 02:00:01 | INFO    | wallet         | Connected to Base via https://mainnet.base.org (chainId=8453)
2025-05-08 02:00:02 | INFO    | eligibility    | Public phase detected: Public (public) | price=0.005000 ETH | ...
2025-05-08 02:00:02 | INFO    | main           | Loaded 5 wallet(s) from wallets.txt
2025-05-08 02:00:30 | INFO    | minter         | [main_wallet] Sending Public mint (qty=2, value=0.010000 ETH) attempt 1/3
2025-05-08 02:00:31 | INFO    | minter         | [main_wallet] tx submitted: 0xabc...
2025-05-08 02:00:42 | INFO    | minter         | [main_wallet] mint succeeded in block 12345678
```

---

## Struktur project

```
opensea-mint-bot/
├── main.py                     # CLI entry point
├── allowlist.json              # template merkle allowlist (edit per drop)
├── signed_mints.json           # template signed mint (edit per drop)
├── requirements.txt
├── .gitignore
└── bot/
    ├── __init__.py
    ├── chains.py               # konfigurasi chain & RPC
    ├── abi.py                  # ABI SeaDrop (mintPublic/mintAllowList/mintSigned) + ERC721
    ├── logger.py               # logging berwarna + file
    ├── utils.py                # helper (gwei/eth conv, format, sleep)
    ├── wallet.py               # loader wallet & koneksi web3
    ├── opensea_api.py          # client API OpenSea (best-effort)
    ├── seadrop.py              # interaksi kontrak SeaDrop + parser allowlist/signed
    ├── eligibility.py          # discovery + check eligibility per wallet
    └── minter.py               # build/sign/send tx (mintPublic/mintAllowList/mintSigned)
```

---

## FAQ

**T: Kontrak NFT-nya bukan SeaDrop, bisa?**
J: Bot ini fokus ke SeaDrop. Untuk kontrak custom, kamu bisa fork
`bot/minter.py` lalu ganti `build_public_mint_tx` ke fungsi `mint(...)`
spesifik kontrak (ABI generik `ERC721_GENERIC_ABI` sudah disertakan).

**T: Bagaimana cara dapat merkle proof untuk Guaranteed/FCFS?**
J: Tiga opsi:
1. Isi `opensea_api_key` di config – bot coba ambil dari OpenSea API.
2. Buka detail drop di OpenSea, buka DevTools → Network → cari request
   yang mengandung "proof" → copy ke `allowlist.json`.
3. Minta organizer drop untuk shared file allowlist.

**T: Drop yang aku target pakai signed mint, bukan merkle. Gimana?**
J: Lihat section [Signed Mint](#signed-mint-guaranteed--fcfs-modern).
Workflow-nya sama dengan merkle scrape, tapi yang di-copy adalah `salt`
+ `signature` per wallet (bukan proof array). Disimpan di
`signed_mints.json` dengan `"type": "signed"`.

**T: Bagaimana tahu drop pakai merkle atau signed mint?**
J: Jalankan `python main.py -c config.json --check`. Bot akan log baris
yang spesifik:
- `SeaDrop allowlist merkle root present: 0x...` → pakai merkle
  (`mintAllowList`).
- `SeaDrop signed-mint signer(s) registered: 0x...` → pakai signed
  (`mintSigned`).
Bisa juga keduanya (jarang) atau cuma public phase (no extra files).

**T: Bisa atur waktu mint sendiri (tidak menunggu fase mulai)?**
J: Bisa - di file allowlist manual, set `start_time` ke timestamp yang
kamu inginkan. Bot akan menunggu sesuai field itu.

**T: Aman buat private key?**
J: Private key tetap di mesin lokal, hanya digunakan untuk signing.
Bot tidak mengirim PK ke server eksternal. Tapi kamu tetap bertanggung
jawab menjaga `wallets.txt` (sudah di `.gitignore`).

---

## Disclaimer

Bot ini disediakan apa adanya untuk tujuan edukasi & riset. Mint NFT
adalah aktivitas yang melibatkan risiko finansial (gas burn, gagal mint,
volatilitas harga). Penggunaan sepenuhnya tanggung jawab kamu sendiri.
