"""Create a short-lived TLS cert for LAN phone access (camera needs https)."""

from __future__ import annotations

import ipaddress
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

CERT_DIR = Path(__file__).resolve().parent / "certs"


def lan_ips() -> list[str]:
    found: set[str] = {"127.0.0.1"}
    try:
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                found.add(ip)
    except OSError:
        pass
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        found.add(sock.getsockname()[0])
        sock.close()
    except OSError:
        pass
    return sorted(found)


def main() -> None:
    CERT_DIR.mkdir(exist_ok=True)
    ips = lan_ips()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    names = [x509.DNSName("localhost")]
    for ip in ips:
        names.append(x509.IPAddress(ipaddress.ip_address(ip)))
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ABSENCE LESS LAN")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .sign(key, hashes.SHA256())
    )
    (CERT_DIR / "key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    (CERT_DIR / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    print("IPs:", ", ".join(ips))


if __name__ == "__main__":
    main()
