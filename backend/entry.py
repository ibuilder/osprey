"""Frozen-binary entrypoint (PyInstaller needs a real script, not `-m`)."""

from osprey.local import main

if __name__ == "__main__":
    main()
