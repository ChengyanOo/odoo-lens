from __future__ import annotations

import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from odoo_lens.extract import extract_source  # noqa: E402
from odoo_lens.extract.common import SrcIndex  # noqa: E402
from odoo_lens.extract.pyfacts import _Src  # noqa: E402
from odoo_lens.facts import (  # noqa: E402
    CLASSES,
    F_COL,
    F_END_COL,
    F_LINE,
    F_NAME,
    K_FIELDS,
    R_COL,
    R_CTX,
    R_END_COL,
    R_KIND,
    R_LINE,
    R_VALUE,
    REF_METHOD,
    REFS,
)
from odoo_lens.registry import Registry, _magic_positions  # noqa: E402

FRAMEWORK = """\
class MetaModel(type):
    def _add_magic_fields(self):
        def add(name, field):
            pass

        def add_default(name, field):
            pass

        label = 'é𝄞'; add('id', fields.Id(automatic=True))
        add('display_name', fields.Char())
        add_default('create_uid', fields.Many2one('res.users'))
        add_default('create_date', fields.Datetime())
        add_default('write_uid', fields.Many2one('res.users'))
        add_default('write_date', fields.Datetime())
        add_default(self.CONCURRENCY_CHECK_FIELD, fields.Datetime())


class BaseModel(metaclass=MetaModel):
    _auto = False


class AbstractModel(BaseModel):
    _auto = False


class Model(AbstractModel):
    _auto = True
"""

MODELS = """\
from odoo import fields, models


class Thing(models.Model):
    _name = 'x.thing'

    count = fields.Integer(compute='_compute_count', compute_sql='_compute_sql_count')

    def _compute_count(self):
        pass

    def _compute_sql_count(self, alias, query):
        pass


class Mixin(models.AbstractModel):
    _name = 'x.mixin'


class NoLog(models.Model):
    _name = 'x.nolog'
    _log_access = False
"""


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class ComputeSqlRefTest(unittest.TestCase):
    def test_compute_sql_is_a_method_ref(self):
        src = MODELS.encode()
        facts = extract_source("/x/models.py", "py", src)
        refs = [r for r in facts[REFS] if r[R_KIND] == REF_METHOD and r[R_CTX][0] == "compute_sql"]
        self.assertEqual(len(refs), 1)
        ref = refs[0]
        self.assertEqual(ref[R_VALUE], "_compute_sql_count")
        self.assertEqual(ref[R_CTX], ("compute_sql", "x.thing"))
        line = MODELS.splitlines()[ref[R_LINE]]
        self.assertEqual(line[ref[R_COL] : ref[R_END_COL]], "_compute_sql_count")


class SrcReuseTest(unittest.TestCase):
    def test_pyfacts_src_is_srcindex(self):
        self.assertTrue(issubclass(_Src, SrcIndex))
        src = "a = 'é𝄞'; b\nc".encode()
        s = _Src(src)
        b = src.index(b"b")
        self.assertEqual(s.pos(b), (0, b))
        self.assertEqual(s.pos(src.index(b"c")), (1, 0))
        self.assertEqual(s.col(0, b), len("a = 'é𝄞'; ".encode("utf-16-le")) // 2)

    def test_field_columns_are_utf16(self):
        text = "from odoo import fields, models\n\n\nclass T(models.Model):\n    _name = 'x.t'\n    é = 1; name = fields.Char()\n"
        facts = extract_source("/x/t.py", "py", text.encode())
        field = facts[CLASSES][0][K_FIELDS][0]
        self.assertEqual(field[F_NAME], "name")
        line = text.splitlines()[field[F_LINE]]
        self.assertEqual(line[field[F_COL] : field[F_END_COL]], "name")


class MagicFieldsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="odoo-lens-fix-extract-")
        cls.fw = os.path.join(cls.tmp, "odoo")
        cls.models_py = os.path.join(cls.fw, "models.py")
        _write(cls.models_py, FRAMEWORK)
        mod = os.path.join(cls.tmp, "addons", "x_mod")
        _write(os.path.join(mod, "__manifest__.py"), "{'name': 'X', 'depends': []}\n")
        _write(os.path.join(mod, "__init__.py"), "from . import models\n")
        _write(os.path.join(mod, "models.py"), MODELS)
        cls.module_dirs = {"x_mod": mod}
        cls.files = {}
        for path, kind in (
            (cls.models_py, "py"),
            (os.path.join(mod, "__manifest__.py"), "manifest"),
            (os.path.join(mod, "__init__.py"), "py"),
            (os.path.join(mod, "models.py"), "py"),
        ):
            with open(path, "rb") as fh:
                cls.files[path] = extract_source(path, kind, fh.read())

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def registry(self, major: int | None) -> Registry:
        env = SimpleNamespace(version=SimpleNamespace(major=major) if major is not None else None, framework_dir=self.fw)
        return Registry(env, dict(self.files), self.module_dirs)

    def test_magic_positions_are_utf16(self):
        pos = _magic_positions(self.models_py)
        lines = FRAMEWORK.splitlines()
        for name in ("id", "display_name", "create_uid", "write_date"):
            line, col, end = pos[name]
            text16 = lines[line].encode("utf-16-le")
            self.assertEqual(text16[2 * col : 2 * end].decode("utf-16-le"), name)
        line, col, end = pos["__last_update"]
        self.assertEqual(lines[line].encode("utf-16-le")[2 * col : 2 * end].decode("utf-16-le"), "self.CONCURRENCY_CHECK_FIELD")

    def test_magic_field_defs_use_utf16_columns(self):
        reg = self.registry(18)
        d = reg.fields("x.thing")["id"][0]
        line16 = FRAMEWORK.splitlines()[d.line].encode("utf-16-le")
        self.assertEqual(line16[2 * d.col : 2 * d.end_col].decode("utf-16-le"), "id")

    def test_last_update_gate(self):
        self.assertIn("__last_update", self.registry(16).fields("x.thing"))
        self.assertIn("__last_update", self.registry(None).fields("x.thing"))
        self.assertNotIn("__last_update", self.registry(17).fields("x.thing"))
        self.assertNotIn("__last_update", self.registry(18).fields("x.thing"))

    def test_log_access_fields(self):
        reg = self.registry(18)
        self.assertIn("create_uid", reg.fields("x.thing"))
        self.assertIn("create_uid", reg.fields("x.mixin"))
        self.assertNotIn("create_uid", reg.fields("x.nolog"))
        self.assertIn("id", reg.fields("x.nolog"))


if __name__ == "__main__":
    unittest.main()
