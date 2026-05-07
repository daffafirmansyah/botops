"""Contract ABIs used by the mint bot.

Only the subset of functions actually called by the bot is included to keep
encoding deterministic and the file small.
"""

from __future__ import annotations

from typing import Any, List


# ---------------------------------------------------------------------------
# SeaDrop v1 (canonical OpenSea drop contract)
# Address: 0x00005EA00Ac477B1030CE78506496e8C2dE24bf5
# ---------------------------------------------------------------------------
SEADROP_ABI: List[Any] = [
    {
        "name": "mintPublic",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {"name": "nftContract", "type": "address"},
            {"name": "feeRecipient", "type": "address"},
            {"name": "minterIfNotPayer", "type": "address"},
            {"name": "quantity", "type": "uint256"},
        ],
        "outputs": [],
    },
    {
        "name": "mintAllowList",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {"name": "nftContract", "type": "address"},
            {"name": "feeRecipient", "type": "address"},
            {"name": "minterIfNotPayer", "type": "address"},
            {"name": "quantity", "type": "uint256"},
            {
                "name": "mintParams",
                "type": "tuple",
                "components": [
                    {"name": "mintPrice", "type": "uint256"},
                    {"name": "maxTotalMintableByWallet", "type": "uint256"},
                    {"name": "startTime", "type": "uint256"},
                    {"name": "endTime", "type": "uint256"},
                    {"name": "dropStageIndex", "type": "uint256"},
                    {"name": "maxTokenSupplyForStage", "type": "uint256"},
                    {"name": "feeBps", "type": "uint256"},
                    {"name": "restrictFeeRecipients", "type": "bool"},
                ],
            },
            {"name": "proof", "type": "bytes32[]"},
        ],
        "outputs": [],
    },
    {
        # Selector: 0x4b61cd6f (used by OpenSea Studio drops for Guaranteed/FCFS).
        # Note: salt is uint256 (NOT bytes32). signature is the EIP-712 ECDSA
        # signature over the mint params, signed by an address registered via
        # SeaDrop.updateSigners(nft, signers).
        "name": "mintSigned",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {"name": "nftContract", "type": "address"},
            {"name": "feeRecipient", "type": "address"},
            {"name": "minterIfNotPayer", "type": "address"},
            {"name": "quantity", "type": "uint256"},
            {
                "name": "mintParams",
                "type": "tuple",
                "components": [
                    {"name": "mintPrice", "type": "uint256"},
                    {"name": "maxTotalMintableByWallet", "type": "uint256"},
                    {"name": "startTime", "type": "uint256"},
                    {"name": "endTime", "type": "uint256"},
                    {"name": "dropStageIndex", "type": "uint256"},
                    {"name": "maxTokenSupplyForStage", "type": "uint256"},
                    {"name": "feeBps", "type": "uint256"},
                    {"name": "restrictFeeRecipients", "type": "bool"},
                ],
            },
            {"name": "salt", "type": "uint256"},
            {"name": "signature", "type": "bytes"},
        ],
        "outputs": [],
    },
    {
        "name": "getSigners",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "nftContract", "type": "address"}],
        "outputs": [{"name": "", "type": "address[]"}],
    },
    {
        "name": "getSignedMintValidationParams",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "nftContract", "type": "address"},
            {"name": "signer", "type": "address"},
        ],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {"name": "minMintPrice", "type": "uint80"},
                    {"name": "maxMaxTotalMintableByWallet", "type": "uint24"},
                    {"name": "minStartTime", "type": "uint40"},
                    {"name": "maxEndTime", "type": "uint40"},
                    {"name": "maxMaxTokenSupplyForStage", "type": "uint40"},
                    {"name": "minFeeBps", "type": "uint16"},
                    {"name": "maxFeeBps", "type": "uint16"},
                ],
            }
        ],
    },
    {
        "name": "getPublicDrop",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "nftContract", "type": "address"}],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {"name": "mintPrice", "type": "uint80"},
                    {"name": "startTime", "type": "uint48"},
                    {"name": "endTime", "type": "uint48"},
                    {"name": "maxTotalMintableByWallet", "type": "uint16"},
                    {"name": "feeBps", "type": "uint16"},
                    {"name": "restrictFeeRecipients", "type": "bool"},
                ],
            }
        ],
    },
    {
        "name": "getAllowListMerkleRoot",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "nftContract", "type": "address"}],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
    {
        "name": "getMintStats",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "nftContract", "type": "address"},
            {"name": "minter", "type": "address"},
        ],
        "outputs": [
            {"name": "minterNumMinted", "type": "uint256"},
            {"name": "currentTotalSupply", "type": "uint256"},
            {"name": "maxSupply", "type": "uint256"},
        ],
    },
    {
        "name": "getCreatorPayoutAddress",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "nftContract", "type": "address"}],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "name": "getAllowedFeeRecipients",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "nftContract", "type": "address"}],
        "outputs": [{"name": "", "type": "address[]"}],
    },
    {
        "name": "getPayers",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "nftContract", "type": "address"}],
        "outputs": [{"name": "", "type": "address[]"}],
    },
]


# ---------------------------------------------------------------------------
# Minimal NFT contract ABI (ERC721SeaDrop / generic ERC721 with mint).
# Used for fallback total-supply / max-supply queries and a generic
# `mint(uint256)` function for non-SeaDrop drop contracts.
# ---------------------------------------------------------------------------
ERC721_GENERIC_ABI: List[Any] = [
    {
        "name": "totalSupply",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "maxSupply",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "name",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "string"}],
    },
    {
        "name": "symbol",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "string"}],
    },
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "owner", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "mint",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [{"name": "quantity", "type": "uint256"}],
        "outputs": [],
    },
]


# ---------------------------------------------------------------------------
# Same as ERC721_GENERIC_ABI but with `getMintStats(minter)` exposed by some
# ERC721SeaDrop-style contracts that surface stats on the NFT itself.
# ---------------------------------------------------------------------------
ERC721_SEADROP_ABI: List[Any] = ERC721_GENERIC_ABI + [
    {
        "name": "getMintStats",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "minter", "type": "address"}],
        "outputs": [
            {"name": "minterNumMinted", "type": "uint256"},
            {"name": "currentTotalSupply", "type": "uint256"},
            {"name": "maxSupply", "type": "uint256"},
        ],
    },
]
