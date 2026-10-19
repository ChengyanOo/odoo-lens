"""Entry point used by bin/odoo-lens (started with ``python -I``, so the repository
root must be put on sys.path explicitly). Keep module-level code minimal: worker
processes of the indexing pool re-import this file as ``__mp_main__``."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if __name__ == "__main__":
    from odoo_lens.cli import main

    sys.exit(main())
