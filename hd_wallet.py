"""
HD Wallet address generation for payment processing.
Uses BIP44 derivation paths to generate unique addresses per payment.

Derivation paths:
  BTC:  m/44'/0'/0'/0/{index}
  ETH:  m/44'/60'/0'/0/{index}
  BSC:  m/44'/60'/0'/0/{index}   (same as ETH — EVM compatible)
  TRON: m/44'/195'/0'/0/{index}

Requires: hdwallet>=3.x library + mnemonic in config.
"""
import os
import logging
from config import HD_WALLET_MNEMONIC

logger = logging.getLogger(__name__)


def generate_address(chain: str, index: int) -> dict | None:
    """
    Generate a receiving address for a specific chain and derivation index.
    Returns {'address': str, 'path': str, 'chain': str, 'index': int} or None on error.
    """
    if not HD_WALLET_MNEMONIC:
        logger.error("HD_WALLET_MNEMONIC not configured")
        return None

    # BIP44 coin types
    COIN_TYPES = {
        "BTC": 0,
        "ETH": 60,
        "BSC": 60,    # EVM — same key, different chain
        "TRON": 195,
    }

    coin_type = COIN_TYPES.get(chain)
    if coin_type is None:
        logger.error(f"Unsupported chain for HD wallet: {chain}")
        return None

    try:
        from hdwallet import HDWallet
        from hdwallet.cryptocurrencies import Bitcoin, Ethereum, Tron
        from hdwallet.derivations import BIP44Derivation
        from hdwallet.mnemonics.bip39 import BIP39Mnemonic
        from hdwallet.hds import BIP44HD

        crypto_map = {
            "BTC": (Bitcoin, Bitcoin.NETWORKS.MAINNET),
            "ETH": (Ethereum, Ethereum.NETWORKS.MAINNET),
            "BSC": (Ethereum, Ethereum.NETWORKS.MAINNET),  # EVM compatible
            "TRON": (Tron, Tron.NETWORKS.MAINNET),
        }

        crypto_cls, network = crypto_map[chain]

        hdw = HDWallet(
            cryptocurrency=crypto_cls,
            hd=BIP44HD,
            network=network,
        ).from_mnemonic(
            mnemonic=BIP39Mnemonic(mnemonic=HD_WALLET_MNEMONIC)
        ).from_derivation(
            derivation=BIP44Derivation(
                coin_type=coin_type,
                account=0,
                change="external-chain",
                address=index,
            )
        )

        address = hdw.address()
        path = f"m/44'/{coin_type}'/0'/0/{index}"

        return {
            "address": address,
            "path": path,
            "chain": chain,
            "index": index,
        }

    except ImportError:
        logger.error("hdwallet library not installed. Run: pip install hdwallet")
        return None
    except Exception as e:
        logger.error(f"HD wallet generation error: {e}")
        return None


def generate_mnemonic_phrase() -> str:
    """Generate a new BIP39 mnemonic phrase (24 words)."""
    from hdwallet.mnemonics.bip39 import BIP39Mnemonic, BIP39_MNEMONIC_LANGUAGES
    from hdwallet.entropies.bip39 import BIP39Entropy

    entropy_hex = os.urandom(32).hex()
    entropy = BIP39Entropy(entropy=entropy_hex)
    mnemonic = BIP39Mnemonic.from_entropy(
        entropy=entropy, language=BIP39_MNEMONIC_LANGUAGES.ENGLISH
    )
    return mnemonic


def validate_mnemonic(mnemonic: str) -> bool:
    """Check if a mnemonic phrase is valid."""
    try:
        from hdwallet.mnemonics.bip39 import BIP39Mnemonic
        BIP39Mnemonic(mnemonic=mnemonic)
        return True
    except Exception:
        return False
