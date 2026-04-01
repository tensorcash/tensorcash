"""
Confidential Mode Crypto Service for W2 Worker.

Handles:
- X25519 key pair management
- Public key self-registration with auth-service
- CEK fetch from auth-service
- Payload encryption/decryption using AES-GCM
"""

import os
import base64
import json
import logging
from typing import Optional, Dict, Any, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Try to import cryptography library
try:
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False
    logger.warning("cryptography library not available - confidential mode disabled")


@dataclass
class ConfidentialConfig:
    """Configuration for confidential mode"""
    enabled: bool
    agent_id: str
    auth_service_url: str
    private_key: Optional[bytes]  # 32 bytes X25519 private key
    public_key: Optional[bytes]   # 32 bytes X25519 public key


class ConfidentialCryptoService:
    """
    Handles cryptographic operations for confidential mode.
    """

    def __init__(self, config: ConfidentialConfig):
        self.config = config
        self._private_key: Optional[X25519PrivateKey] = None
        self._public_key: Optional[X25519PublicKey] = None
        self._cek_cache: Dict[str, bytes] = {}  # room_id -> CEK

        if config.enabled and CRYPTO_AVAILABLE:
            self._load_keys()

    @staticmethod
    def _decode_b64_any(value: Any, field_name: str = "value") -> bytes:
        """
        Decode base64url/base64 string for strict confidential contract fields.
        """
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Missing or invalid {field_name}: expected non-empty string")

        normalized = value.strip().replace("-", "+").replace("_", "/")
        pad = (-len(normalized)) % 4
        if pad:
            normalized += "=" * pad

        try:
            return base64.b64decode(normalized, validate=True)
        except Exception as exc:
            raise ValueError(f"Invalid base64 payload for {field_name}: {exc}") from exc

    @staticmethod
    def _as_dict(value: Any) -> Dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _truthy(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "enabled", "active"}
        return bool(value)

    @staticmethod
    def _canonical_attestation_type(value: Any) -> Optional[str]:
        if not isinstance(value, str) or not value.strip():
            return None
        raw = value.strip()
        upper = raw.upper().replace("-", "_")
        aliases = {
            "SEV": "SEV",
            "SEV_SNP": "SEV-SNP",
            "SEV_ES": "SEV-ES",
            "TDX": "TDX",
            "INTEL_TDX": "INTEL_TDX",
            "GCP_INTEL_TDX": "GCP_INTEL_TDX",
            "PHALA_INTEL_TDX": "PHALA_INTEL_TDX",
            "PHALA_TDX": "PHALA_INTEL_TDX",
            "DSTACK_TDX": "PHALA_INTEL_TDX",
            "NVIDIA_CC": "NVIDIA_CC",
            "NVIDIA_CC_TEE": "NVIDIA_CC",
            "SGX": "SGX",
            "MOCK": "MOCK",
        }
        return aliases.get(upper, raw)

    @staticmethod
    def _attestation_marker_text(bundle: Dict[str, Any]) -> str:
        platform = ConfidentialCryptoService._as_dict(bundle.get("platform_attestation"))
        marker_values = [
            platform.get("platform"),
            platform.get("platform_attestation_source"),
            platform.get("provider"),
            platform.get("source"),
            bundle.get("provider"),
            bundle.get("cloud_provider"),
            bundle.get("attestation_service"),
            bundle.get("vm_metadata"),
            bundle.get("system_config"),
            bundle.get("dstack"),
            bundle.get("phala"),
        ]
        text_parts = []
        for value in marker_values:
            if value in (None, "", [], {}):
                continue
            try:
                text_parts.append(json.dumps(value, default=str, sort_keys=True))
            except Exception:
                text_parts.append(str(value))
        return " ".join(text_parts).lower()

    @staticmethod
    def _select_attestation_quote(bundle: Dict[str, Any]) -> Optional[str]:
        platform = ConfidentialCryptoService._as_dict(bundle.get("platform_attestation"))
        tdx_info = ConfidentialCryptoService._as_dict(platform.get("tdx"))
        vtpm_info = ConfidentialCryptoService._as_dict(platform.get("vtpm"))
        candidates = (
            bundle.get("quote"),
            tdx_info.get("td_quote_base64"),
            tdx_info.get("quote_base64"),
            tdx_info.get("quote"),
            tdx_info.get("td_quote"),
            vtpm_info.get("quote_base64"),
            vtpm_info.get("pcr_values_base64"),
            bundle.get("pcr_values_b64"),
        )
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None

    @staticmethod
    def _normalize_attestation_bundle(
        bundle: Dict[str, Any],
        model_hash: str = "",
        model_name: str = "",
    ) -> Dict[str, Any]:
        """
        Convert provider-specific attestation bundles into the auth-service
        registration shape. Keep operator-qualified TDX labels for logs/DB,
        while auth-service projects them to the public INTEL_TDX enum.
        """
        platform = ConfidentialCryptoService._as_dict(bundle.get("platform_attestation"))
        sev_info = ConfidentialCryptoService._as_dict(platform.get("sev"))
        tdx_info = ConfidentialCryptoService._as_dict(platform.get("tdx"))
        marker_text = ConfidentialCryptoService._attestation_marker_text(bundle)

        tee_type = ConfidentialCryptoService._canonical_attestation_type(bundle.get("type"))
        if ConfidentialCryptoService._truthy(sev_info.get("active")):
            tee_type = ConfidentialCryptoService._canonical_attestation_type(
                sev_info.get("type") or tee_type or "SEV"
            )
        else:
            has_tdx_quote = any(
                isinstance(tdx_info.get(field), str) and tdx_info.get(field).strip()
                for field in ("td_quote_base64", "quote_base64", "quote", "td_quote")
            )
            tdx_active = (
                ConfidentialCryptoService._truthy(tdx_info.get("active"))
                or has_tdx_quote
                or "intel_tdx" in marker_text
                or "tdx" in marker_text
            )
            if tdx_active:
                if "phala" in marker_text or "dstack" in marker_text:
                    tee_type = "PHALA_INTEL_TDX"
                elif "gcp" in marker_text or "google" in marker_text:
                    tee_type = "GCP_INTEL_TDX"
                elif tee_type not in {"TDX", "INTEL_TDX", "GCP_INTEL_TDX", "PHALA_INTEL_TDX"}:
                    tee_type = "INTEL_TDX"

        measurements = (
            bundle.get("measurements")
            or tdx_info.get("rtmrs")
            or bundle.get("pcr_values")
            or {}
        )

        return {
            "type": tee_type,
            "quote": ConfidentialCryptoService._select_attestation_quote(bundle),
            "measurements": measurements if isinstance(measurements, dict) else {},
            "model_hash": model_hash,
            "model_name": model_name,
            "bundle": bundle,
        }

    def _load_keys(self) -> None:
        """Load or generate X25519 key pair"""
        if self.config.private_key:
            try:
                self._private_key = X25519PrivateKey.from_private_bytes(self.config.private_key)
                self._public_key = self._private_key.public_key()
                logger.info("Loaded X25519 key pair from config")
            except Exception as e:
                logger.error(f"Failed to load private key: {e}")
                self._generate_keys()
        else:
            self._generate_keys()

    def _generate_keys(self) -> None:
        """Generate new X25519 key pair"""
        if not CRYPTO_AVAILABLE:
            return

        self._private_key = X25519PrivateKey.generate()
        self._public_key = self._private_key.public_key()
        logger.info("Generated new X25519 key pair")

    def get_public_key_b64(self) -> Optional[str]:
        """Get public key as base64 string"""
        if not self._public_key:
            return None

        raw_bytes = self._public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw
        )
        return base64.b64encode(raw_bytes).decode('ascii')

    def get_agent_id(self) -> str:
        """Get current agent_id (may be empty if not discovered yet)."""
        return (self.config.agent_id or "").strip()

    def set_agent_id(self, agent_id: str) -> None:
        """Set/override agent_id discovered from authoritative broker ACK."""
        self.config.agent_id = (agent_id or "").strip()

    async def _fetch_attestation_bundle(self, http_session) -> Optional[Dict[str, Any]]:
        """
        Fetch TEE attestation bundle from the local attestation service.

        The attestation service runs on the same Azure Confidential VM (port 9443)
        and returns SEV-SNP attestation bundles.

        Returns attestation dict on success, None on failure (graceful degradation
        for non-TEE environments).
        """
        from components import constants
        attestation_url = constants.ATTESTATION_SERVICE_URL

        try:
            nonce = base64.b64encode(os.urandom(32)).decode('ascii')
            url = f"{attestation_url}/attestation?nonce={nonce}"

            async with http_session.get(url, ssl=False, timeout=10) as resp:
                if resp.status == 200:
                    bundle = await resp.json()
                    # Include model hash — the VM measurements prove the image,
                    # the model hash proves which weights are loaded
                    model_hash = constants.MODEL_HASH or ""
                    model_name = constants.LOCAL_MODEL_NAME or ""
                    bundle["model_hash"] = model_hash
                    bundle["model_name"] = model_name
                    logger.info(
                        "Fetched attestation bundle from local service, model_hash=%s",
                        model_hash[:16] if model_hash else "(none)"
                    )
                    return self._normalize_attestation_bundle(bundle, model_hash, model_name)
                else:
                    text = await resp.text()
                    logger.warning(f"Attestation service returned HTTP {resp.status}: {text}")
                    return None

        except Exception as e:
            logger.info(f"Attestation service not available (non-TEE env?): {e}")
            return None

    async def register_public_key(self, http_session) -> bool:
        """
        Register public key with auth-service.

        Calls POST /auth/agents/{agent_id}/keys
        """
        if not self.config.enabled or not self._public_key:
            return False

        agent_id = self.get_agent_id()
        if not agent_id:
            logger.warning("Skipping key registration: agent_id not set yet")
            return False

        public_key_b64 = self.get_public_key_b64()
        if not public_key_b64:
            return False

        url = f"{self.config.auth_service_url}/auth/agents/{agent_id}/keys"

        try:
            # Get API key from environment (should have agent:register scope)
            from components import constants
            api_key = constants.PROVIDER_JWT_TOKEN

            if not api_key:
                logger.error("No PROVIDER_JWT_TOKEN configured for key registration")
                return False

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }

            payload = {
                # Auth-service expects wrap_pubkey (X25519 key used to wrap CEKs).
                "wrap_pubkey": public_key_b64,
                "algorithm": "X25519"
            }

            # Include TEE attestation if available
            attestation = await self._fetch_attestation_bundle(http_session)
            if attestation:
                payload["attestation"] = attestation

            async with http_session.post(url, json=payload, headers=headers) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    logger.info(f"Registered public key with auth-service: version={result.get('version')}")
                    return True
                elif resp.status == 403:
                    error = await resp.text()
                    logger.error(f"Key registration forbidden: {error}")
                    return False
                else:
                    error = await resp.text()
                    logger.error(f"Key registration failed: HTTP {resp.status} - {error}")
                    return False

        except Exception as e:
            logger.error(f"Key registration error: {e}")
            return False

    async def fetch_cek(self, http_session, room_id: str, epoch: Optional[int] = None) -> Optional[bytes]:
        """
        Fetch and unwrap CEK for a room.

        Calls GET /auth/keys/{room_id}/agent/{agent_id}
        Returns unwrapped CEK bytes or None.

        Cache strategy:
        - When epoch is specified: cache forever (CEK for a specific epoch won't change)
        - When epoch is None (latest): always fetch fresh (epoch may have rotated)
        """
        agent_id = self.get_agent_id()
        if not self.config.enabled or not self._private_key:
            return None
        if not agent_id:
            logger.error("Cannot fetch CEK: agent_id not set")
            return None

        # Only check cache when epoch is explicitly specified
        # "Latest" should always fetch fresh in case epoch rotated
        if epoch is not None:
            cache_key = f"{room_id}:{epoch}"
            if cache_key in self._cek_cache:
                return self._cek_cache[cache_key]

        url = f"{self.config.auth_service_url}/auth/keys/{room_id}/agent/{agent_id}"
        if epoch is not None:
            url += f"?epoch={epoch}"

        try:
            from components import constants
            api_key = constants.PROVIDER_JWT_TOKEN

            if not api_key:
                logger.error("No PROVIDER_JWT_TOKEN configured for CEK fetch")
                return None

            headers = {"Authorization": f"Bearer {api_key}"}

            async with http_session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    if not result:
                        logger.warning(f"No key package found for room {room_id}")
                        return None

                    wrapped_cek_b64 = result.get("wrapped_cek")
                    wrapper_info = result.get("wrapper_info") or {}

                    if not wrapped_cek_b64:
                        logger.error("Key package missing wrapped_cek")
                        return None

                    # Unwrap the CEK
                    cek = self._unwrap_cek(
                        wrapped_cek_b64,
                        wrapper_info,
                        room_id=room_id,
                        epoch=epoch,
                    )
                    # Only cache when epoch is specified (specific epoch CEKs don't change)
                    if cek and epoch is not None:
                        cache_key = f"{room_id}:{epoch}"
                        self._cek_cache[cache_key] = cek
                    return cek

                elif resp.status == 404:
                    logger.warning(f"No key package for room {room_id}, agent {self.config.agent_id}")
                    return None
                else:
                    error = await resp.text()
                    logger.error(f"CEK fetch failed: HTTP {resp.status} - {error}")
                    return None

        except Exception as e:
            logger.error(f"CEK fetch error: {e}")
            return None

    def _unwrap_cek(
        self,
        wrapped_cek_b64: str,
        wrapper_info: Dict[str, Any],
        room_id: Optional[str] = None,
        epoch: Optional[int] = None,
    ) -> Optional[bytes]:
        """
        Unwrap CEK using X25519 ECDH + HKDF.

        Expected contract:
        - wrapper_info.sender_pub (base64url X25519 public key)
        - wrapper_info.hkdf_salt (base64url 16-byte salt)
        - wrapper_info.iv (base64url 12-byte AES-GCM IV)
        - wrapped_cek (base64url ciphertext+tag)
        """
        if not CRYPTO_AVAILABLE or not self._private_key:
            return None

        try:
            if not isinstance(wrapper_info, dict):
                raise ValueError("Invalid wrapper_info contract: expected object")

            sender_pub = wrapper_info.get("sender_pub")
            hkdf_salt = wrapper_info.get("hkdf_salt")
            iv = wrapper_info.get("iv")
            if not sender_pub or not hkdf_salt or not iv:
                raise ValueError(
                    "Invalid wrapper_info contract: sender_pub, hkdf_salt, and iv are required"
                )
            if room_id is None:
                raise ValueError("Missing room_id for CEK unwrap contract")
            if epoch is None:
                raise ValueError("Missing epoch for CEK unwrap contract")

            sender_pub_b = self._decode_b64_any(sender_pub, "wrapper_info.sender_pub")
            hkdf_salt_b = self._decode_b64_any(hkdf_salt, "wrapper_info.hkdf_salt")
            iv_b = self._decode_b64_any(iv, "wrapper_info.iv")
            wrapped_ciphertext = self._decode_b64_any(wrapped_cek_b64, "wrapped_cek")

            sender_public = X25519PublicKey.from_public_bytes(sender_pub_b)
            shared_secret = self._private_key.exchange(sender_public)

            info_value = wrapper_info.get("info")
            if isinstance(info_value, str) and info_value:
                hkdf_info = info_value.encode("utf-8")
            else:
                hkdf_info = f"cek-wrap|{room_id}|{int(epoch)}".encode("utf-8")

            hkdf = HKDF(
                algorithm=hashes.SHA256(),
                length=32,
                salt=hkdf_salt_b,
                info=hkdf_info,
            )
            kek = hkdf.derive(shared_secret)

            aesgcm = AESGCM(kek)
            cek = aesgcm.decrypt(iv_b, wrapped_ciphertext, None)
            logger.debug(f"Successfully unwrapped CEK ({len(cek)} bytes)")
            return cek

        except Exception as e:
            wrapper_keys = list(wrapper_info.keys()) if isinstance(wrapper_info, dict) else []
            logger.error(
                "CEK unwrap failed: %s (wrapped_cek_len=%s, wrapper_keys=%s, room_id=%s, epoch=%s)",
                e,
                len(wrapped_cek_b64 or ""),
                wrapper_keys,
                room_id,
                epoch,
            )
            return None

    def decrypt_payload(self, encrypted_payload_b64: str, cek: bytes) -> Optional[Dict[str, Any]]:
        """
        Decrypt an encrypted payload using the CEK.

        Expected format:
        - nonce (12 bytes) + ciphertext (with 16-byte auth tag)
        """
        if not CRYPTO_AVAILABLE:
            return None

        try:
            encrypted_data = self._decode_b64_any(encrypted_payload_b64, "encrypted_payload")

            if len(encrypted_data) < 12:
                logger.error("Encrypted payload too short")
                return None

            nonce = encrypted_data[:12]
            ciphertext = encrypted_data[12:]

            aesgcm = AESGCM(cek)
            plaintext = aesgcm.decrypt(nonce, ciphertext, None)

            return json.loads(plaintext.decode('utf-8'))

        except Exception as e:
            logger.error(f"Payload decryption failed: {e}")
            return None

    def encrypt_response(self, response: Dict[str, Any], cek: bytes) -> Optional[str]:
        """
        Encrypt a response payload using the CEK.

        Returns base64-encoded nonce + ciphertext.
        """
        if not CRYPTO_AVAILABLE:
            return None

        try:
            plaintext = json.dumps(response).encode('utf-8')

            # Generate random nonce
            nonce = os.urandom(12)

            aesgcm = AESGCM(cek)
            ciphertext = aesgcm.encrypt(nonce, plaintext, None)

            # Combine nonce + ciphertext
            encrypted_data = nonce + ciphertext

            return base64.b64encode(encrypted_data).decode('ascii')

        except Exception as e:
            logger.error(f"Response encryption failed: {e}")
            return None

    def clear_cek_cache(self, room_id: Optional[str] = None) -> None:
        """Clear CEK cache for a room or all rooms"""
        if room_id:
            keys_to_remove = [k for k in self._cek_cache if k.startswith(f"{room_id}:")]
            for k in keys_to_remove:
                del self._cek_cache[k]
        else:
            self._cek_cache.clear()


def load_confidential_config() -> ConfidentialConfig:
    """Load confidential mode configuration from environment"""
    from components import constants

    enabled = constants.CONFIDENTIAL_MODE_ENABLED and CRYPTO_AVAILABLE

    # Try to load keys
    private_key = None
    public_key = None

    if enabled:
        # Try inline base64 first
        if constants.AGENT_PRIVATE_KEY_B64:
            try:
                private_key = base64.b64decode(constants.AGENT_PRIVATE_KEY_B64)
            except Exception as e:
                logger.error(f"Failed to decode AGENT_PRIVATE_KEY_B64: {e}")

        if constants.AGENT_PUBLIC_KEY_B64:
            try:
                public_key = base64.b64decode(constants.AGENT_PUBLIC_KEY_B64)
            except Exception as e:
                logger.error(f"Failed to decode AGENT_PUBLIC_KEY_B64: {e}")

        # Try file paths
        if not private_key and constants.AGENT_PRIVATE_KEY_PATH:
            try:
                with open(constants.AGENT_PRIVATE_KEY_PATH, 'rb') as f:
                    private_key = f.read()
                    # Handle PEM format
                    if private_key.startswith(b'-----'):
                        from cryptography.hazmat.primitives import serialization
                        key = serialization.load_pem_private_key(private_key, password=None)
                        private_key = key.private_bytes(
                            encoding=serialization.Encoding.Raw,
                            format=serialization.PrivateFormat.Raw,
                            encryption_algorithm=serialization.NoEncryption()
                        )
            except Exception as e:
                logger.error(f"Failed to load private key from file: {e}")

    return ConfidentialConfig(
        enabled=enabled,
        agent_id=constants.AGENT_ID,
        auth_service_url=constants.AUTH_SERVICE_URL,
        private_key=private_key,
        public_key=public_key
    )


# Singleton instance
_crypto_service: Optional[ConfidentialCryptoService] = None


def get_crypto_service() -> Optional[ConfidentialCryptoService]:
    """Get or create the confidential crypto service singleton"""
    global _crypto_service
    if _crypto_service is None:
        config = load_confidential_config()
        if config.enabled:
            _crypto_service = ConfidentialCryptoService(config)
        else:
            logger.info("Confidential mode not enabled")
    return _crypto_service
