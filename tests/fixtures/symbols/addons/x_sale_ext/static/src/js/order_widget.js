/** @odoo-module **/

import { Component } from "@odoo/owl";
import { registry } from "@web/core/registry";

export class XOrderWidget extends Component {
    static template = "x_sale_ext.XOrderWidget";
    static props = {};
}

export function formatXAmount(value) {
    return value.toFixed(2);
}

export const xOrderWidget = {
    component: XOrderWidget,
};

registry.category("fields").add("x_order_widget", xOrderWidget);
