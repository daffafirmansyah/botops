"""Chain configuration and RPC management.

Supports Ethereum, Base, Arbitrum and Optimism mainnets out of the box.
Custom RPC URLs can override the defaults via config or interactively.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


# SeaDrop deterministic deployment address (CREATE2). Same on every chain
# OpenSea has deployed it to.
SEADROP_ADDRESS = "0x00005EA00Ac477B1030CE78506496e8C2dE24bf5"

# OpenSea's canonical fee recipient (used as default for SeaDrop public mints).
# Can be overridden through config.
OPENSEA_FEE_RECIPIENT = "0x0000a26b00c1F0DF003000390027140000fAa719"


@dataclass
class ChainConfig:
    name: str
    chain_id: int
    slug: str  # OpenSea API slug
    rpc_urls: List[str] = field(default_factory=list)
    explorer: str = ""
    native_symbol: str = "ETH"
    seadrop: str = SEADROP_ADDRESS
    supports_eip1559: bool = True


CHAINS: Dict[str, ChainConfig] = {
    "ethereum": ChainConfig(
        name="Ethereum",
        chain_id=1,
        slug="ethereum",
        # Ordered by reliability/latency (fastest+most reliable first).
        # User's Alchemy URL (if configured) is prepended at runtime.
        rpc_urls=[
            "https://eth.llamarpc.com",
            "https://ethereum-rpc.publicnode.com",
            "https://eth.drpc.org",
            "https://rpc.ankr.com/eth",
            "https://eth.blockrazor.xyz",
            "https://cloudflare-eth.com",
            "https://eth.meowrpc.com",
            "https://1rpc.io/eth",
        ],
        explorer="https://etherscan.io",
        native_symbol="ETH",
    ),
    "base": ChainConfig(
        name="Base",
        chain_id=8453,
        slug="base",
        rpc_urls=[
            "https://mainnet.base.org",
            "https://base.llamarpc.com",
            "https://base-rpc.publicnode.com",
            "https://base.drpc.org",
            "https://base.meowrpc.com",
            "https://base.blockpi.network/v1/rpc/public",
            "https://1rpc.io/base",
        ],
        explorer="https://basescan.org",
        native_symbol="ETH",
    ),
    "arbitrum": ChainConfig(
        name="Arbitrum One",
        chain_id=42161,
        slug="arbitrum",
        rpc_urls=[
            "https://arb1.arbitrum.io/rpc",
            "https://arbitrum.llamarpc.com",
            "https://arbitrum-one-rpc.publicnode.com",
            "https://arbitrum.drpc.org",
            "https://arbitrum.meowrpc.com",
            "https://1rpc.io/arb",
        ],
        explorer="https://arbiscan.io",
        native_symbol="ETH",
    ),
    "optimism": ChainConfig(
        name="Optimism",
        chain_id=10,
        slug="optimism",
        rpc_urls=[
            "https://mainnet.optimism.io",
            "https://optimism.llamarpc.com",
            "https://optimism-rpc.publicnode.com",
            "https://optimism.drpc.org",
            "https://1rpc.io/op",
        ],
        explorer="https://optimistic.etherscan.io",
        native_symbol="ETH",
    ),
    "polygon": ChainConfig(
        name="Polygon",
        chain_id=137,
        slug="matic",
        rpc_urls=[
            "https://polygon-rpc.com",
            "https://polygon.llamarpc.com",
            "https://polygon-bor-rpc.publicnode.com",
            "https://polygon.drpc.org",
            "https://polygon.meowrpc.com",
            "https://1rpc.io/matic",
        ],
        explorer="https://polygonscan.com",
        native_symbol="MATIC",
    ),
}


def list_chains() -> List[str]:
    """Return list of supported chain keys."""
    return list(CHAINS.keys())


def get_chain(name: str) -> ChainConfig:
    """Lookup a chain by key (case-insensitive). Raises KeyError if missing."""
    key = (name or "").strip().lower()
    if key not in CHAINS:
        raise KeyError(
            f"Unknown chain '{name}'. Supported: {', '.join(CHAINS.keys())}"
        )
    return CHAINS[key]


def resolve_rpc(chain: ChainConfig, override: Optional[str] = None) -> List[str]:
    """Return ordered list of RPC URLs to try for a chain.

    Custom override (if non-empty) takes precedence and is tried first.
    """
    urls: List[str] = []
    if override and override.strip():
        urls.append(override.strip())
    for url in chain.rpc_urls:
        if url not in urls:
            urls.append(url)
    return urls


def resolve_broadcast_rpcs(
    chain: ChainConfig,
    primary: Optional[str] = None,
    extras: Optional[List[str]] = None,
    *,
    limit: int = 5,
) -> List[str]:
    """Return ordered list of RPC URLs to broadcast a raw transaction to.

    Order:
      1. primary (e.g. user's Alchemy URL) - first to ensure consistent hash
      2. user-provided extras (config.rpc_urls_broadcast)
      3. chain default public RPC list (used as backup propagation paths)

    Caller typically takes the first ~3-5 entries to broadcast in parallel.
    Tx hash is deterministic from the signed payload, so duplicates are
    deduplicated by the network mempool — broadcasting to many RPCs is safe.
    """
    urls: List[str] = []
    if primary and primary.strip():
        urls.append(primary.strip())
    if extras:
        for u in extras:
            if u and u.strip() and u.strip() not in urls:
                urls.append(u.strip())
    for url in chain.rpc_urls:
        if url not in urls:
            urls.append(url)
        if len(urls) >= limit:
            break
    return urls[:limit]
