# PyInstaller entry point. __main__.py uses a relative import, which fails when
# PyInstaller runs it as a top-level script.
from cleam.cli import main

raise SystemExit(main())
