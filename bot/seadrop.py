"""SeaDrop contract interaction layer.

Wraps reads from the SeaDrop contract (`getPublicDrop`, `getMintStats`, etc.)
into a clean `MintPhase` data model and exposes high-level helpers used by
the eligibility checker and the minter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from web3 import Web3
from web3.contract import Contract
from web3.exceptions import ContractLogicError, Web3Exception

from .abi import ERC721_GENERIC_ABI, ERC721_SEADROP_ABI, SEADROP_ABI
from .chains import OPENSEA_FEE_RECIPIENT
from .logger import get_logger
from .utils import fmt_eth, fmt_timestamp, safe_get


log = get_logger("seadrop")


PHASE_PUBLIC = "public"
PHASE_GUARANTEED = "guaranteed"
PHASE_FCFS = "fcfs"
PHASE_ALLOWLIST = "allowlist"
PHASE_SIGNED = "signed"  # OpenSea Studio Guaranteed/FCFS via mintSigned


@dataclass
class MintPhase:
    """Normalised representation of a single mint stage / phase."""

    name: str
    phase_type: str  # "public" / "guaranteed" / "fcfs" / "allowlist" / "signed"
    start_time: int
    end_time: int
    mint_price_wei: int
    max_per_wallet: int
    fee_bps: int = 0
    fee_recipient: str = OPENSEA_FEE_RECIPIENT
    restrict_fee_recipients: bool = True
    drop_stage_index: int = 0
    max_token_supply_for_stage: int = 0
    # mintAllowList (merkle) data
    merkle_root: str = ""
    proofs: Dict[str, List[str]] = field(default_factory=dict)
    # mintSigned data (per-wallet signature payloads)
    # Each entry: {"salt": "0x..." or int, "signature": "0x..."}
    signed_mints: Dict[str, Dict[str, str]] = field(default_factory=dict)
    source: str = "onchain"  # "onchain" | "opensea_api" | "manual"

    # ------------------------------------------------------------------
    @property
    def is_public(self) -> bool:
        return self.phase_type == PHASE_PUBLIC

    @property
    def is_signed(self) -> bool:
        return self.phase_type == PHASE_SIGNED

    @property
    def requires_allowlist(self) -> bool:
        return self.phase_type in (PHASE_GUARANTEED, PHASE_FCFS, PHASE_ALLOWLIST)

    @property
    def requires_signature(self) -> bool:
        return self.phase_type == PHASE_SIGNED

    def has_proof_for(self, address: str) -> bool:
        return address.lower() in self.proofs

    def proof_for(self, address: str) -> List[str]:
        return self.proofs.get(address.lower(), [])

    def has_signature_for(self, address: str) -> bool:
        return address.lower() in self.signed_mints

    def signature_for(self, address: str) -> Dict[str, str]:
        return self.signed_mints.get(address.lower(), {})

    @property
    def is_stub(self) -> bool:
        """True when phase was inferred from on-chain hints but lacks proof/signature data."""
        return self.source == "onchain_partial"

    def status(self, now_ts: int) -> str:
        if self.is_stub:
            return "data needed"
        if self.start_time <= 0:
            return "not configured"
        if now_ts < self.start_time:
            return "upcoming"
        if self.end_time and self.end_time != 0 and now_ts >= self.end_time:
            return "ended"
        return "live"

    def describe(self, native_symbol: str = "ETH") -> str:
        price = fmt_eth(self.mint_price_wei, native_symbol)
        return (
            f"{self.name} ({self.phase_type}) | price={price} | "
            f"max/wallet={self.max_per_wallet} | "
            f"start={fmt_timestamp(self.start_time)} | "
            f"end={fmt_timestamp(self.end_time)}"
        )


# ---------------------------------------------------------------------------
# Contract object factory
# ---------------------------------------------------------------------------

def get_seadrop_contract(w3: Web3, address: str) -> Contract:
    return w3.eth.contract(address=Web3.to_checksum_address(address), abi=SEADROP_ABI)


def get_nft_contract(w3: Web3, address: str, *, seadrop_extensions: bool = True) -> Contract:
    abi = ERC721_SEADROP_ABI if seadrop_extensions else ERC721_GENERIC_ABI
    return w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi)


# ---------------------------------------------------------------------------
# Reading on-chain data
# ---------------------------------------------------------------------------

def fetch_collection_meta(w3: Web3, nft_address: str) -> Dict[str, str]:
    """Return name/symbol/maxSupply for a contract (best-effort)."""
    nft = get_nft_contract(w3, nft_address, seadrop_extensions=False)
    meta: Dict[str, str] = {"address": Web3.to_checksum_address(nft_address)}
    for fn in ("name", "symbol"):
        try:
            meta[fn] = nft.functions[fn]().call()
        except Exception:
            meta[fn] = ""
    for fn in ("totalSupply", "maxSupply"):
        try:
            meta[fn] = str(nft.functions[fn]().call())
        except Exception:
            meta[fn] = "-"
    return meta


def fetch_public_drop(w3: Web3, seadrop_address: str, nft_address: str) -> Optional[MintPhase]:
    """Return the SeaDrop public phase for an NFT (or None when missing)."""
    seadrop = get_seadrop_contract(w3, seadrop_address)
    try:
        result = seadrop.functions.getPublicDrop(Web3.to_checksum_address(nft_address)).call()
    except (ContractLogicError, Web3Exception, ValueError) as exc:
        log.debug("getPublicDrop failed: %s", exc)
        return None

    # SeaDrop returns a tuple matching PublicDrop struct
    try:
        (
            mint_price,
            start_time,
            end_time,
            max_per_wallet,
            fee_bps,
            restrict_fee_recipients,
        ) = result
    except (TypeError, ValueError):
        log.debug("Unexpected getPublicDrop layout: %s", result)
        return None

    if int(start_time) == 0 and int(end_time) == 0 and int(mint_price) == 0 and int(max_per_wallet) == 0:
        return None  # Phase not configured

    fee_recipient = OPENSEA_FEE_RECIPIENT
    try:
        recipients = seadrop.functions.getAllowedFeeRecipients(
            Web3.to_checksum_address(nft_address)
        ).call()
        if recipients:
            fee_recipient = Web3.to_checksum_address(recipients[0])
    except Exception:
        pass

    return MintPhase(
        name="Public",
        phase_type=PHASE_PUBLIC,
        start_time=int(start_time),
        end_time=int(end_time),
        mint_price_wei=int(mint_price),
        max_per_wallet=int(max_per_wallet),
        fee_bps=int(fee_bps),
        fee_recipient=fee_recipient,
        restrict_fee_recipients=bool(restrict_fee_recipients),
    )


def fetch_signers(w3: Web3, seadrop_address: str, nft_address: str) -> List[str]:
    """Return the list of allowed signers configured on SeaDrop, or [] when none.

    A non-empty list indicates the drop supports `mintSigned` (Guaranteed/FCFS
    via signatures from OpenSea's backend signer key).
    """
    seadrop = get_seadrop_contract(w3, seadrop_address)
    try:
        signers = seadrop.functions.getSigners(
            Web3.to_checksum_address(nft_address)
        ).call()
    except (ContractLogicError, Web3Exception, ValueError) as exc:
        log.debug("getSigners failed: %s", exc)
        return []
    if not signers:
        return []
    return [Web3.to_checksum_address(s) for s in signers]


def fetch_allowlist_root(w3: Web3, seadrop_address: str, nft_address: str) -> str:
    """Return the merkle root configured on SeaDrop, or '' when none."""
    seadrop = get_seadrop_contract(w3, seadrop_address)
    try:
        root = seadrop.functions.getAllowListMerkleRoot(
            Web3.to_checksum_address(nft_address)
        ).call()
    except (ContractLogicError, Web3Exception, ValueError) as exc:
        log.debug("getAllowListMerkleRoot failed: %s", exc)
        return ""
    if not root:
        return ""
    if isinstance(root, (bytes, bytearray)):
        if int.from_bytes(root, "big") == 0:
            return ""
        return "0x" + root.hex()
    return str(root)


def fetch_mint_stats(
    w3: Web3, seadrop_address: str, nft_address: str, minter: str
) -> Tuple[int, int, int]:
    """Return (minterNumMinted, currentTotalSupply, maxSupply) – defaults 0/0/0."""
    seadrop = get_seadrop_contract(w3, seadrop_address)
    try:
        minted, supply, max_supply = seadrop.functions.getMintStats(
            Web3.to_checksum_address(nft_address),
            Web3.to_checksum_address(minter),
        ).call()
        return int(minted), int(supply), int(max_supply)
    except Exception:
        # Fall back to NFT contract's own getMintStats if exposed
        try:
            nft = get_nft_contract(w3, nft_address, seadrop_extensions=True)
            minted, supply, max_supply = nft.functions.getMintStats(
                Web3.to_checksum_address(minter)
            ).call()
            return int(minted), int(supply), int(max_supply)
        except Exception:
            return 0, 0, 0


# ---------------------------------------------------------------------------
# Allowlist file ingestion
# ---------------------------------------------------------------------------

def phase_from_manual_dict(entry: Dict[str, object]) -> Optional[MintPhase]:
    """Build a MintPhase from a manual allowlist / signed-mint config entry.

    Common fields (all optional; sane defaults applied):
      name, type, start_time, end_time, mint_price_wei, max_per_wallet,
      fee_bps, fee_recipient, drop_stage_index, max_token_supply_for_stage,
      restrict_fee_recipients

    Merkle allowlist (type = guaranteed/fcfs/allowlist):
      merkle_root: "0x..."
      proofs: { "0xWALLET": ["0xproof1", "0xproof2"] }

    Signed mint (type = signed) — used by OpenSea Studio drops:
      signed_mints: {
        "0xWALLET": {
          "salt": "0x..." or "12345" (uint256),
          "signature": "0x...rsv65bytes"
        }
      }
    """
    if not isinstance(entry, dict):
        return None

    phase_type = str(entry.get("type") or PHASE_ALLOWLIST).lower()
    valid = (PHASE_PUBLIC, PHASE_GUARANTEED, PHASE_FCFS, PHASE_ALLOWLIST, PHASE_SIGNED)
    if phase_type not in valid:
        log.warning("Unknown phase type '%s' - defaulting to allowlist", phase_type)
        phase_type = PHASE_ALLOWLIST

    name = str(entry.get("name") or phase_type.title())

    raw_price = entry.get("mint_price_wei", entry.get("price_wei", 0))
    try:
        mint_price_wei = int(raw_price)
    except (TypeError, ValueError):
        mint_price_wei = 0

    # Merkle proofs
    proofs_raw = entry.get("proofs") or {}
    proofs: Dict[str, List[str]] = {}
    if isinstance(proofs_raw, dict):
        for addr, p in proofs_raw.items():
            if not isinstance(addr, str) or not isinstance(p, list):
                continue
            proofs[addr.lower()] = [str(x) for x in p]

    # Signed mints (per-wallet salt+signature)
    signed_raw = entry.get("signed_mints") or entry.get("signatures") or {}
    signed_mints: Dict[str, Dict[str, str]] = {}
    if isinstance(signed_raw, dict):
        for addr, payload in signed_raw.items():
            if not isinstance(addr, str) or not isinstance(payload, dict):
                continue
            salt = payload.get("salt")
            sig = payload.get("signature") or payload.get("sig")
            if salt is None or not sig:
                log.warning("Signed mint entry for %s missing salt/signature", addr)
                continue
            signed_mints[addr.lower()] = {
                "salt": str(salt),
                "signature": str(sig),
            }

    return MintPhase(
        name=name,
        phase_type=phase_type,
        start_time=int(safe_get(entry, "start_time", default=0) or 0),
        end_time=int(safe_get(entry, "end_time", default=0) or 0),
        mint_price_wei=mint_price_wei,
        max_per_wallet=int(safe_get(entry, "max_per_wallet", default=1) or 1),
        fee_bps=int(safe_get(entry, "fee_bps", default=0) or 0),
        fee_recipient=str(entry.get("fee_recipient") or OPENSEA_FEE_RECIPIENT),
        restrict_fee_recipients=bool(entry.get("restrict_fee_recipients", True)),
        drop_stage_index=int(safe_get(entry, "drop_stage_index", default=0) or 0),
        max_token_supply_for_stage=int(safe_get(entry, "max_token_supply_for_stage", default=0) or 0),
        merkle_root=str(entry.get("merkle_root") or ""),
        proofs=proofs,
        signed_mints=signed_mints,
        source="manual",
    )


# ---------------------------------------------------------------------------
# Mint params encoding (for mintAllowList)
# ---------------------------------------------------------------------------

def encode_mint_params(phase: MintPhase) -> Tuple[int, int, int, int, int, int, int, bool]:
    """Return tuple matching SeaDrop's MintParams struct."""
    return (
        int(phase.mint_price_wei),
        int(phase.max_per_wallet),
        int(phase.start_time),
        int(phase.end_time),
        int(phase.drop_stage_index),
        int(phase.max_token_supply_for_stage),
        int(phase.fee_bps),
        bool(phase.restrict_fee_recipients),
    )


def encode_proof(proof: List[str]) -> List[bytes]:
    """Convert a list of hex strings to bytes32 for the mintAllowList call."""
    out: List[bytes] = []
    for p in proof or []:
        s = p.strip()
        if s.lower().startswith("0x"):
            s = s[2:]
        b = bytes.fromhex(s)
        if len(b) != 32:
            # left-pad just in case (should already be 32-byte hashes)
            b = b.rjust(32, b"\x00")
        out.append(b)
    return out


def encode_salt(salt: str) -> int:
    """Convert a salt value (hex string or decimal string) to uint256."""
    if isinstance(salt, int):
        return salt
    s = str(salt).strip()
    if s.lower().startswith("0x"):
        return int(s, 16)
    # Plain decimal string
    return int(s)


def encode_signature(signature: str) -> bytes:
    """Convert a hex signature string to bytes (typically 65 bytes for ECDSA r||s||v)."""
    s = str(signature).strip()
    if s.lower().startswith("0x"):
        s = s[2:]
    if len(s) % 2 != 0:
        raise ValueError(f"signature has odd hex length: {len(s)} chars")
    return bytes.fromhex(s)
