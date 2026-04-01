"""
Tests for confidential_crypto.py — TEE attestation fetch and key registration.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import pytest
import json
import base64
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass


# Mock the cryptography imports before importing the module
@pytest.fixture(autouse=True)
def mock_crypto():
    """Ensure cryptography is available or mocked."""
    pass


class TestFetchAttestationBundle:
    """Tests for _fetch_attestation_bundle method.

    Verifies the worker→auth-service contract: ``attestation.type`` must
    resolve to a string the auth-service's ``_platform_from_attestation_type``
    projector recognises, otherwise the agent is silently dropped from
    /s2s/confidential/agent-keys/active (see
    the Auth Service and
    test_s2s_confidential_agents.test_excludes_unknown_platform_*).
    """

    @staticmethod
    def _make_mock_session(bundle_response: dict) -> MagicMock:
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value=bundle_response)

        mock_session = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session.get.return_value = mock_ctx
        return mock_session

    @staticmethod
    def _make_service():
        from components.confidential_crypto import ConfidentialCryptoService, ConfidentialConfig
        config = ConfidentialConfig(
            enabled=True,
            agent_id="test-agent",
            auth_service_url="http://localhost:8001",
            private_key=None,
            public_key=None,
        )
        return ConfidentialCryptoService(config)

    @pytest.mark.asyncio
    async def test_fetch_success_sev_snp(self):
        """Azure NCC (AMD SEV-SNP) shape — platform_attestation.sev.active=True."""
        service = self._make_service()
        bundle_response = {
            "platform_attestation": {
                "platform": "Azure_AMD_SEV_SNP",
                "sev": {"active": True, "type": "SEV-SNP"},
                "vtpm": {"active": True},
            },
            "quote": "base64quote...",
            "measurements": {"pcr0": "aaa", "pcr7": "bbb"},
        }
        mock_session = self._make_mock_session(bundle_response)

        with patch("components.constants.ATTESTATION_SERVICE_URL", "https://localhost:9443"), \
             patch("components.constants.MODEL_HASH", "abc123def456"), \
             patch("components.constants.LOCAL_MODEL_NAME", "Qwen/Qwen3-8B"):
            result = await service._fetch_attestation_bundle(mock_session)

        assert result is not None
        assert result["type"] == "SEV-SNP"
        assert result["model_hash"] == "abc123def456"
        assert result["model_name"] == "Qwen/Qwen3-8B"
        assert result["measurements"] == {"pcr0": "aaa", "pcr7": "bbb"}
        assert "bundle" in result

    @pytest.mark.asyncio
    async def test_fetch_success_gcp_intel_tdx(self):
        """GCP cGPU (Intel TDX, no SEV block) — must emit GCP_INTEL_TDX so
        auth-service projects to INTEL_TDX. Regression for the
        ``platform: 'GCP_Intel_TDX', tdx.active: true, no sev`` bundle
        that previously registered with type=None and was filtered out
        of /s2s/confidential/agent-keys/active."""
        service = self._make_service()
        bundle_response = {
            "platform_attestation": {
                "platform": "GCP_Intel_TDX",
                "tdx": {
                    "active": True,
                    "quote_base64": "dGR4LXF1b3RlLWdjcA==",
                },
            },
            "measurements": {"mrtd": "abc"},
        }
        mock_session = self._make_mock_session(bundle_response)

        with patch("components.constants.ATTESTATION_SERVICE_URL", "https://localhost:9443"), \
             patch("components.constants.MODEL_HASH", ""), \
             patch("components.constants.LOCAL_MODEL_NAME", ""):
            result = await service._fetch_attestation_bundle(mock_session)

        assert result is not None
        assert result["type"] == "GCP_INTEL_TDX"
        # Quote falls back to platform_attestation.tdx.quote_base64 when
        # no top-level quote / vTPM PCRs are present.
        assert result["quote"] == "dGR4LXF1b3RlLWdjcA=="

    @pytest.mark.asyncio
    async def test_fetch_success_phala_intel_tdx_via_source_marker(self):
        """Phala Cloud dstack: platform_attestation.platform stays the
        generic 'Intel_TDX' while platform_attestation_source carries the
        operator hint. Detector must catch Phala via the source marker."""
        service = self._make_service()
        bundle_response = {
            "platform_attestation": {
                "platform": "Intel_TDX",
                "platform_attestation_source": "phala-dstack",
                "tdx": {"active": True, "quote_base64": "cGhhbGEtcXVvdGU="},
            },
        }
        mock_session = self._make_mock_session(bundle_response)

        with patch("components.constants.ATTESTATION_SERVICE_URL", "https://localhost:9443"), \
             patch("components.constants.MODEL_HASH", ""), \
             patch("components.constants.LOCAL_MODEL_NAME", ""):
            result = await service._fetch_attestation_bundle(mock_session)

        assert result is not None
        assert result["type"] == "PHALA_INTEL_TDX"

    @pytest.mark.asyncio
    async def test_fetch_success_generic_intel_tdx_fallback(self):
        """Unbranded Intel TDX bundle (no GCP/Phala marker) — must fall
        back to plain 'INTEL_TDX', which the auth-service still
        recognises."""
        service = self._make_service()
        bundle_response = {
            "platform_attestation": {
                "platform": "Intel_TDX",
                "tdx": {"active": True, "quote_base64": "Z2VuZXJpYy10ZHg="},
            },
        }
        mock_session = self._make_mock_session(bundle_response)

        with patch("components.constants.ATTESTATION_SERVICE_URL", "https://localhost:9443"), \
             patch("components.constants.MODEL_HASH", ""), \
             patch("components.constants.LOCAL_MODEL_NAME", ""):
            result = await service._fetch_attestation_bundle(mock_session)

        assert result is not None
        assert result["type"] == "INTEL_TDX"

    @pytest.mark.asyncio
    async def test_fetch_no_active_tee_returns_type_none(self):
        """Non-TEE host (development laptop, etc.): both SEV and TDX
        blocks inactive → type stays None. Caller decides whether to
        register; the worker does NOT silently mint a fake type."""
        service = self._make_service()
        bundle_response = {
            "platform_attestation": {
                "platform": "Unknown",
                "sev": {"active": False},
                "tdx": {"active": False},
            },
        }
        mock_session = self._make_mock_session(bundle_response)

        with patch("components.constants.ATTESTATION_SERVICE_URL", "https://localhost:9443"), \
             patch("components.constants.MODEL_HASH", ""), \
             patch("components.constants.LOCAL_MODEL_NAME", ""):
            result = await service._fetch_attestation_bundle(mock_session)

        assert result is not None
        assert result["type"] is None

    @pytest.mark.asyncio
    async def test_fetch_non_tee_env(self):
        """Should return None when attestation service is not available."""
        from components.confidential_crypto import ConfidentialCryptoService, ConfidentialConfig

        config = ConfidentialConfig(
            enabled=True,
            agent_id="test-agent",
            auth_service_url="http://localhost:8001",
            private_key=None,
            public_key=None,
        )
        service = ConfidentialCryptoService(config)

        mock_session = MagicMock()
        mock_session.get.side_effect = ConnectionRefusedError("Connection refused")

        with patch("components.constants.ATTESTATION_SERVICE_URL", "https://localhost:9443"):
            result = await service._fetch_attestation_bundle(mock_session)

        assert result is None

    @pytest.mark.asyncio
    async def test_fetch_http_error(self):
        """Should return None on non-200 response."""
        from components.confidential_crypto import ConfidentialCryptoService, ConfidentialConfig

        config = ConfidentialConfig(
            enabled=True,
            agent_id="test-agent",
            auth_service_url="http://localhost:8001",
            private_key=None,
            public_key=None,
        )
        service = ConfidentialCryptoService(config)

        mock_resp = AsyncMock()
        mock_resp.status = 500
        mock_resp.text = AsyncMock(return_value="Internal Server Error")

        mock_session = MagicMock()
        mock_ctx = AsyncMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session.get.return_value = mock_ctx

        with patch("components.constants.ATTESTATION_SERVICE_URL", "https://localhost:9443"):
            result = await service._fetch_attestation_bundle(mock_session)

        assert result is None


class TestNormalizeAttestationBundle:
    """Provider-specific TEE bundles normalize to auth-service registration shape."""

    def test_legacy_top_level_attestation_is_preserved(self):
        from components.confidential_crypto import ConfidentialCryptoService

        result = ConfidentialCryptoService._normalize_attestation_bundle(
            {
                "type": "SEV-SNP",
                "quote": "legacy-quote",
                "measurements": {"pcr0": "aaa"},
            },
            "hash",
            "model",
        )

        assert result["type"] == "SEV-SNP"
        assert result["quote"] == "legacy-quote"
        assert result["measurements"] == {"pcr0": "aaa"}
        assert result["model_hash"] == "hash"
        assert result["model_name"] == "model"

    def test_gcp_tdx_uses_td_quote_base64(self):
        from components.confidential_crypto import ConfidentialCryptoService

        result = ConfidentialCryptoService._normalize_attestation_bundle({
            "platform_attestation": {
                "platform": "GCP_Intel_TDX",
                "tdx": {
                    "active": True,
                    "td_quote_base64": "gcp-td-quote",
                    "rtmrs": {"rtmr0": "00"},
                },
                "vtpm": {
                    "quote_base64": "vtpm-quote",
                    "pcr_values_base64": "vtpm-pcrs",
                },
            },
        })

        assert result["type"] == "GCP_INTEL_TDX"
        assert result["quote"] == "gcp-td-quote"
        assert result["measurements"] == {"rtmr0": "00"}

    def test_phala_dstack_tdx_uses_quote_base64(self):
        from components.confidential_crypto import ConfidentialCryptoService

        result = ConfidentialCryptoService._normalize_attestation_bundle({
            "platform_attestation": {
                "platform": "Intel_TDX",
                "platform_attestation_source": "phala-dstack-guest-agent",
                "tdx": {
                    "active": True,
                    "quote_base64": "phala-td-quote",
                    "rtmrs": {"rtmr3": "33"},
                },
                "vtpm": {"available": False, "pcr_values_base64": ""},
            },
            "vm_metadata": {"vmSize": "phala-tdx-h100"},
        })

        assert result["type"] == "PHALA_INTEL_TDX"
        assert result["quote"] == "phala-td-quote"
        assert result["measurements"] == {"rtmr3": "33"}


class TestRegisterPublicKeyWithAttestation:
    """Tests that register_public_key includes attestation when available."""

    @pytest.mark.asyncio
    async def test_includes_attestation_in_payload(self):
        from components.confidential_crypto import ConfidentialCryptoService, ConfidentialConfig

        try:
            from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
            from cryptography.hazmat.primitives import serialization
            private_key = X25519PrivateKey.generate()
            private_bytes = private_key.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption(),
            )
        except ImportError:
            pytest.skip("cryptography library not available")

        config = ConfidentialConfig(
            enabled=True,
            agent_id="test-agent",
            auth_service_url="http://localhost:8001",
            private_key=private_bytes,
            public_key=None,
        )
        service = ConfidentialCryptoService(config)

        attestation_data = {
            "type": "SEV",
            "quote": "q",
            "measurements": {},
            "model_hash": "abc",
            "model_name": "test",
            "bundle": {},
        }

        # Track what payload was POSTed
        captured_payload = {}

        mock_post_resp = AsyncMock()
        mock_post_resp.status = 200
        mock_post_resp.json = AsyncMock(return_value={"version": 1})

        mock_session = MagicMock()

        def fake_post(url, json=None, headers=None):
            captured_payload.update(json or {})
            ctx = MagicMock()
            ctx.__aenter__ = AsyncMock(return_value=mock_post_resp)
            ctx.__aexit__ = AsyncMock(return_value=None)
            return ctx

        mock_session.post = fake_post

        with patch.object(service, '_fetch_attestation_bundle', return_value=attestation_data), \
             patch("components.constants.PROVIDER_JWT_TOKEN", "fake_token"):
            result = await service.register_public_key(mock_session)

        assert result is True
        assert "attestation" in captured_payload
        assert captured_payload["attestation"]["type"] == "SEV"
        assert captured_payload["attestation"]["model_hash"] == "abc"

    @pytest.mark.asyncio
    async def test_no_attestation_when_unavailable(self):
        from components.confidential_crypto import ConfidentialCryptoService, ConfidentialConfig

        try:
            from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
            from cryptography.hazmat.primitives import serialization
            private_key = X25519PrivateKey.generate()
            private_bytes = private_key.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption(),
            )
        except ImportError:
            pytest.skip("cryptography library not available")

        config = ConfidentialConfig(
            enabled=True,
            agent_id="test-agent",
            auth_service_url="http://localhost:8001",
            private_key=private_bytes,
            public_key=None,
        )
        service = ConfidentialCryptoService(config)

        captured_payload = {}

        mock_post_resp = AsyncMock()
        mock_post_resp.status = 200
        mock_post_resp.json = AsyncMock(return_value={"version": 1})

        mock_session = MagicMock()

        def fake_post(url, json=None, headers=None):
            captured_payload.update(json or {})
            ctx = MagicMock()
            ctx.__aenter__ = AsyncMock(return_value=mock_post_resp)
            ctx.__aexit__ = AsyncMock(return_value=None)
            return ctx

        mock_session.post = fake_post

        # Attestation fetch returns None (non-TEE env)
        with patch.object(service, '_fetch_attestation_bundle', return_value=None), \
             patch("components.constants.PROVIDER_JWT_TOKEN", "fake_token"):
            result = await service.register_public_key(mock_session)

        assert result is True
        assert "attestation" not in captured_payload
