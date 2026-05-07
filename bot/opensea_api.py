"""Best-effort OpenSea public API client for drop / phase / proof discovery.

OpenSea does not document a stable public endpoint for fetching drop-stage
information including merkle proofs, so this module is intentionally
defensive: every method returns `None`/`{}` on failure and the bot will fall
back to on-chain data + user-supplied allowlist files.

If `OPENSEA_API_KEY` is provided in config, the bot will send it on
every request to unlock higher rate limits and additional data.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from .logger import get_logger
from .utils import safe_get


log = get_logger("opensea_api")

OPENSEA_BASE = "https://api.opensea.io/api/v2"
DEFAULT_TIMEOUT = 15


class OpenSeaClient:
    """Lightweight client around OpenSea's v2 REST API."""

    def __init__(self, api_key: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT) -> None:
        self.api_key = (api_key or "").strip()
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"accept": "application/json", "user-agent": "opensea-mint-bot/1.0"})
        if self.api_key:
            self._session.headers["x-api-key"] = self.api_key

    # -------------------- low-level ----------------------------------------

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        url = f"{OPENSEA_BASE}{path}"
        try:
            resp = self._session.get(url, params=params or {}, timeout=self.timeout)
        except requests.RequestException as exc:
            log.debug("GET %s failed: %s", url, exc)
            return None

        if resp.status_code == 404:
            log.debug("GET %s -> 404", url)
            return None
        if resp.status_code == 401:
            log.warning("OpenSea API returned 401 for %s; provide a valid api key", url)
            return None
        if resp.status_code == 429:
            log.warning("OpenSea API rate-limited (%s)", url)
            return None
        if resp.status_code >= 400:
            log.debug("GET %s -> %s", url, resp.status_code)
            return None

        try:
            return resp.json()
        except ValueError:
            log.debug("Bad JSON from %s", url)
            return None

    # -------------------- collection metadata -----------------------------

    def get_collection_by_contract(self, chain_slug: str, contract: str) -> Optional[Dict[str, Any]]:
        """Return the collection JSON for a given chain+contract, or None."""
        return self._get(f"/chain/{chain_slug}/contract/{contract}")

    def get_collection(self, slug: str) -> Optional[Dict[str, Any]]:
        return self._get(f"/collections/{slug}")

    # -------------------- drop / mint info --------------------------------

    def get_drop_info(self, chain_slug: str, contract: str) -> Optional[Dict[str, Any]]:
        """Try several known endpoints to retrieve drop-stage information.

        This endpoint surface keeps changing on OpenSea's side, so we attempt
        a few candidates and return the first non-empty one.
        """
        candidates = [
            f"/chain/{chain_slug}/contract/{contract}/drops",
            f"/chain/{chain_slug}/contract/{contract}/mint",
            f"/mint/{chain_slug}/{contract}",
        ]
        for path in candidates:
            data = self._get(path)
            if data:
                log.debug("OpenSea drop info via %s", path)
                return data
        return None

    def get_allowlist_proof(
        self, chain_slug: str, contract: str, address: str, stage_id: Optional[str] = None
    ) -> Optional[List[str]]:
        """Try to fetch a merkle proof for a wallet on an allowlist phase.

        Returns the proof (list of bytes32 hex strings) or None when not found.
        """
        addr = address.lower()
        params = {"wallet_address": addr}
        if stage_id:
            params["stage_id"] = stage_id

        candidates = [
            f"/chain/{chain_slug}/contract/{contract}/allowlist/proof",
            f"/mint/{chain_slug}/{contract}/proof",
        ]
        for path in candidates:
            data = self._get(path, params)
            if not data:
                continue
            proof = (
                safe_get(data, "proof")
                or safe_get(data, "merkle_proof")
                or safe_get(data, "data", "proof")
            )
            if isinstance(proof, list) and proof:
                return [str(p) for p in proof]
        return None
