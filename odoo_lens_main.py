# Indexing pool workers re-import this file as __mp_main__: keep module-level code minimal.
import os
import sys

# started with python -I: the repository root is not on sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if __name__ == "__main__":
    from odoo_lens.cli import main

    sys.exit(main())
