"""Keys for the apiag-cloud access tokens (mall.apiag_tokens).

    docker compose exec -T app python -m mall.apiag_keys          prints a new key, as the environment line to set
    docker compose exec -T app python -m mall.apiag_keys rotate   re-encrypts every saved token with the first key

To rotate: put the new key first and keep the old one after a comma (APIAG_TOKEN_KEYS=new,old), recreate the app service, run
`rotate`, then drop the old key. Tokens that no key reads are listed; an admin pastes them again in Cuentas."""
import os
import sys

from cryptography.fernet import Fernet


def main(argv=None, stdout=None):
    argv = sys.argv[1:] if argv is None else argv
    stdout = stdout or sys.stdout
    if argv[:1] != ['rotate']:
        print(f'APIAG_TOKEN_KEYS={Fernet.generate_key().decode()}', file=stdout)
        return 0
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    import django
    django.setup()
    from .apiag_tokens import rotate_all
    moved, unreadable = rotate_all()
    print(f'Tokens cifrados de nuevo con la primera clave: {moved}.', file=stdout)
    if unreadable:
        print('Ninguna clave lee el token de: ' + ', '.join(unreadable) + '. Vuelve a ingresarlo en Administración → Cuentas.', file=stdout)
    return 1 if unreadable else 0


if __name__ == '__main__':
    sys.exit(main())
