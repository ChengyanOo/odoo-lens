from odoo.http import route

from odoo.addons.x_sale_ext import const
from odoo.addons.x_sale_ext.const import SHOP_PATH
from odoo.addons.x_sale_ext.controllers.main import XShop as BaseShop


def x_route(*args, **kwargs):
    return route(*args, **kwargs)


class XPortal(BaseShop):

    @route(const.WEBHOOK_ROUTE, type='http', auth='public', csrf=False)
    def webhook(self, **kw):
        return ''

    @x_route(f"{SHOP_PATH}/<int:product_id>", type='http', auth='public')
    def shop_product(self, product_id, **kw):
        return ''

    @route()
    def orders(self, page=1, **kw):
        return super().orders(page=page, **kw)


class XPortalMore(XPortal):

    @route()
    def cart(self, **kw):
        return super().cart(**kw)
