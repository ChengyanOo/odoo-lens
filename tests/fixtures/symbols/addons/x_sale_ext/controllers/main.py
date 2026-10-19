from odoo import http
from odoo.http import request, route


class XShop(http.Controller):
    _export_route = '/x/export'

    @http.route('/shop/cart', type='http', auth='public', website=True)
    def cart(self, **kw):
        return request.render('x_sale_ext.cart')

    @route(['/x/orders', '/x/orders/page/<int:page>'], type='http', auth='user')
    def orders(self, page=1, **kw):
        return request.render('x_sale_ext.orders')

    @route(
        route='/x/orders/<int:order_id>',
        type='jsonrpc',
        auth='user',
    )
    def order(self, order_id, **kw):
        return {}

    @route(_export_route, type='http', auth='user')
    def export(self, **kw):
        return ''

    def helper(self):
        return 1


class XShopExtended(XShop):

    @http.route()
    def cart(self, **kw):
        return super().cart(**kw)
