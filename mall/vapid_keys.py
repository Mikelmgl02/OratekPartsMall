"""Prints a new VAPID key pair for browser push notifications (mall.push), as the environment lines to set:

    docker compose exec -T app python -m mall.vapid_keys

Generate it once per environment and keep it: browsers subscribed with one public key stop receiving pushes when it changes."""
import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def encoded(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def main():
    key = ec.generate_private_key(ec.SECP256R1())
    print(f'VAPID_PRIVATE_KEY={encoded(key.private_numbers().private_value.to_bytes(32, "big"))}')
    print(f'VAPID_PUBLIC_KEY={encoded(key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))}')
    print('VAPID_SUBJECT=mailto:<correo de contacto del equipo>')


if __name__ == '__main__':
    main()
