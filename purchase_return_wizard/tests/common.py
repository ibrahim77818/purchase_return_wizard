# -*- coding: utf-8 -*-
"""Shared fixtures and helpers for the purchase_return_wizard tests.

AccountTestInvoicingCommon is a TransactionCase that also installs a chart of
accounts on a fresh company, which vendor bills / credit notes need.
"""
from odoo import Command, fields

from odoo.addons.account.tests.common import AccountTestInvoicingCommon


class PurchaseReturnCommon(AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls, chart_template_ref=None):
        super().setUpClass(chart_template_ref=chart_template_ref)
        cls.env.user.write({"groups_id": [
            Command.link(cls.env.ref("purchase_return_wizard.group_purchase_return").id),
            Command.link(cls.env.ref("purchase.group_purchase_manager").id),
            Command.link(cls.env.ref("stock.group_stock_manager").id),
        ]})
        cls.vendor = cls.partner_a
        cls.purchase_tax = cls.company_data["default_tax_purchase"]

        def product(name, code, taxes=None):
            return cls.env["product.product"].create({
                "name": name,
                "default_code": code,
                "detailed_type": "product",
                "purchase_method": "receive",
                "supplier_taxes_id": [Command.set(taxes.ids if taxes else [])],
            })

        cls.beam = product("Steel Beam", "PRW-BEAM", cls.purchase_tax)
        cls.bolt = product("Anchor Bolt", "PRW-BOLT")
        cls.panel = product("Glass Panel", "PRW-PANEL")

    # ------------------------------------------------------------------ helpers
    def _create_po(self, lines):
        po = self.env["purchase.order"].create({
            "partner_id": self.vendor.id,
            "order_line": [Command.create({
                "product_id": p.id,
                "product_qty": qty,
                "product_uom": p.uom_po_id.id,
                "price_unit": price,
            }) for p, qty, price in lines],
        })
        po.button_confirm()
        return po

    def _pol(self, po, product):
        return po.order_line.filtered(lambda l: l.product_id == product)

    def _receive(self, po, qty_by_product):
        picking = po.picking_ids.filtered(lambda p: p.state not in ("done", "cancel"))
        self.assertEqual(len(picking), 1)
        for move in picking.move_ids:
            # v17: quantity + picked replace quantity_done
            move.quantity = qty_by_product.get(move.product_id, 0.0)
            move.picked = True
        picking._action_done()  # creates a backorder for anything not received
        return picking

    def _bill(self, po, qty_by_product=None, delete_products=None):
        po.action_create_invoice()
        bill = po.invoice_ids.filtered(lambda m: m.state == "draft" and m.move_type == "in_invoice")
        cmds = []
        for aml in bill.invoice_line_ids:
            product = aml.product_id
            if delete_products and product in delete_products:
                cmds.append(Command.delete(aml.id))
            elif qty_by_product and product in qty_by_product:
                cmds.append(Command.update(aml.id, {"quantity": qty_by_product[product]}))
        if cmds:
            bill.write({"invoice_line_ids": cmds})
        bill.invoice_date = fields.Date.today()
        bill.action_post()
        return bill

    def _open_wizard(self, po):
        action = po.action_open_return_wizard()
        self.assertEqual(action["res_model"], "purchase.return.wizard")
        return self.env["purchase.return.wizard"].browse(action["res_id"])

    def _wline(self, wizard, product):
        return wizard.line_ids.filtered(lambda l: l.product_id == product)

    def _confirm(self, wizard):
        """Review then confirm, as the user does; return the created log."""
        wizard.action_review()
        self.assertEqual(wizard.state, "review")
        before = wizard.purchase_id.return_log_ids
        action = wizard.action_confirm()
        # the wizard must close itself (regression: confirmation stuck on screen)
        self.assertEqual(action["tag"], "display_notification")
        self.assertEqual(action["params"]["next"], {"type": "ir.actions.act_window_close"})
        wizard.purchase_id.invalidate_recordset(["return_log_ids"])
        log = wizard.purchase_id.return_log_ids - before
        self.assertEqual(len(log), 1)
        return log

    def _returned_qty(self, pickings, product):
        return sum(pickings.move_ids.filtered(
            lambda m: m.product_id == product).mapped("quantity"))
