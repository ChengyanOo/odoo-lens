{
    'name': 'X Sale Extension',
    'version': '18.0.1.0.0',
    'summary': 'Customer customizations of sales',
    'depends': ['sale', 'mail'],
    'data': [
        'security/ir.model.access.csv',
        'views/sale_order_views.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'x_sale_ext/static/src/js/order_widget.js',
            'x_sale_ext/static/src/xml/order_widget.xml',
        ],
    },
    'installable': True,
    'license': 'LGPL-3',
}
