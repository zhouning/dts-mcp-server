"""Generate a local CA and a server certificate bound to this host's IP.

Two constants here are load-bearing and must not be "tidied":

* The leaf is valid for **825 days**. That is the CA/Browser Forum maximum that
  Apple platforms enforce; a longer leaf is rejected outright by macOS, so the
  client would fail to connect no matter how the CA is trusted.
* The SAN is an ``IPAddress``, not a DNS name, because clients dial a bare IP.
  A certificate with only a CN and no matching SAN is rejected by every modern
  client.

``path_length=0`` on the CA forbids intermediates, so this CA can only ever sign
leaves — importing it into a macOS keychain grants it a correspondingly narrow
power.
"""
import argparse
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from ipaddress import IPv4Address, IPv6Address, ip_address
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


@dataclass(frozen=True)
class CertificatePaths:
    ca_certificate: Path
    ca_private_key: Path
    server_certificate: Path
    server_private_key: Path


def _paths(output_dir: Path) -> CertificatePaths:
    return CertificatePaths(
        ca_certificate=output_dir / "ca.crt",
        ca_private_key=output_dir / "ca.key",
        server_certificate=output_dir / "server.crt",
        server_private_key=output_dir / "server.key",
    )


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _write_private_key(path: Path, key: rsa.RSAPrivateKey) -> None:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    os.chmod(path, 0o600)


def generate_certificates(
    output_dir: Path,
    server_ip: IPv4Address | IPv6Address,
    *,
    repository_root: Path | None = None,
    rotate: bool = False,
) -> CertificatePaths:
    output_dir = output_dir.resolve()
    if repository_root is not None and _is_within(output_dir, repository_root):
        raise ValueError("private keys must be generated outside the repository")

    paths = _paths(output_dir)
    existing = [path for path in paths.__dict__.values() if path.exists()]
    if len(existing) == len(paths.__dict__) and not rotate:
        return paths
    if existing and not rotate:
        raise FileExistsError(
            "certificate set is incomplete; use --rotate to replace it"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    ca_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "DTS MCP Local CA")]
    )
    ca_certificate = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, str(server_ip))]
    )
    server_certificate = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=825))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(server_ip)]),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    _write_private_key(paths.ca_private_key, ca_key)
    _write_private_key(paths.server_private_key, server_key)
    paths.ca_certificate.write_bytes(
        ca_certificate.public_bytes(serialization.Encoding.PEM)
    )
    paths.server_certificate.write_bytes(
        server_certificate.public_bytes(serialization.Encoding.PEM)
    )
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a local CA and fixed-IP DTS MCP TLS certificate."
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--ip", required=True, type=ip_address)
    parser.add_argument("--repository-root", type=Path)
    parser.add_argument("--rotate", action="store_true")
    arguments = parser.parse_args()
    paths = generate_certificates(
        arguments.output_dir,
        arguments.ip,
        repository_root=arguments.repository_root,
        rotate=arguments.rotate,
    )
    print(paths.ca_certificate)


if __name__ == "__main__":
    main()
