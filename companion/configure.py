"""Interactive pairing: the raw token stays on this laptop, only its hash is shown."""
import hashlib
import secrets
from pathlib import Path

from companion import validate_backend_url


def main():
    print("Personal Options Trader — laptop companion setup")
    config = Path(__file__).with_name(".env")
    if config.exists():
        if input("A companion configuration already exists. Replace it? Type REPLACE: ").strip() != "REPLACE":
            print("Existing configuration kept.")
            return
    backend = validate_backend_url(input("Backend URL (for local use press Enter): ").strip() or "http://127.0.0.1:8000")
    default_folder = Path.home() / "Documents" / "PaperTrading" / "Trades"
    folder = Path(input(f"Save folder (press Enter for {default_folder}): ").strip() or default_folder).expanduser().resolve()
    if any(char in str(folder) for char in ('"', "\n", "\r")):
        raise ValueError("Folder cannot contain quotes or line breaks")
    folder.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(48)
    # Forward slashes keep Windows paths portable in a dotenv value.
    config.write_text(f'BACKEND_URL={backend}\nCOMPANION_TOKEN={token}\nTRADES_FOLDER="{folder.as_posix()}"\nPOLL_SECONDS=10\n', encoding="utf-8")
    print("\nCompanion configuration saved on this laptop. Keep companion/.env private.")
    print("Copy this hash into the backend environment setting COMPANION_TOKEN_SHA256:")
    print(hashlib.sha256(token.encode()).hexdigest())
    print("\nThe hash cannot be used as the companion token. Restart the backend after setting it.")


if __name__ == "__main__":
    main()
