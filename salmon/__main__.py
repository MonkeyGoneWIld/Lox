"""Entry point for ``python -m salmon``.

The console script installed by pyproject points at ``run:main``. This module
exists so the web UI can shell out to the CLI without depending on the console
script being on PATH.
"""

from run import main

if __name__ == "__main__":
    main()
