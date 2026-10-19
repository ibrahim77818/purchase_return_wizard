# -*- coding: utf-8 -*-
from collections import defaultdict

from markupsafe import Markup
from psycopg2 import errors as pg_errors

from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools import float_compare, float_is_zero, float_round

from ..models.purchase_return_log import SCENARIOS


# ---------------------------------------------------------------------------
# Pure routing rules (module level so they are trivially unit-testable)
# ---------------------------------------------------------------------------
def classify_scenario(received, billed, rounding):
    if float_compare(received, 0.0, precision_rounding=rounding) <= 0:
        return "not_received"
    if float_compare(billed, 0.0, precision_rounding=rounding) <= 0:
        return "received_not_billed"
    if float_compare(billed, received, precision_rounding=rounding) >= 0:
        return "billed"
    return "partially_billed"


def compute_credit_qty(return_qty, received, billed, rounding):
    """Quantity to credit = the part of the return that would otherwise leave
    the vendor over-billed:  min(Q, max(0, B - (R - Q)))

    R=10 B=10 Q=4 -> 4 (fully billed: credit everything returned)
    R=10 B=6  Q=4 -> 0 (the 4 returned were never billed)
    R=10 B=8  Q=4 -> 2 (only 2 of the returned units had been billed)
    """
    if float_compare(return_qty, 0.0, precision_rounding=rounding) <= 0:
        return 0.0
    over_billed = billed - (received - return_qty)
    return float_round(max(0.0, min(return_qty, over_billed)), precision_rounding=rounding)


class PurchaseReturnWizard(models.TransientModel):
    _name = "purchase.return.wizard"
    _description = "Purchase Return & Vendor Credit Wizard"

    purchase_id = fields.Many2one(
        "purchase.order", required=True, readonly=True, ondelete="cascade")
    # Two-step flow handled server-side. The previous `confirm="..."` attribute
    # on the form button opened a client ConfirmationDialog stacked on top of
    # the wizard dialog; once the server action replaced the wizard, that
    # confirmation dialog's owner was destroyed and it stayed on screen even
    # though the return/refund had been processed. A review state avoids any
    # client-side dialog stacking and also shows the user what will happen.
    state = fields.Selection(
        [("select", "Select Lines"), ("review", "Review")],
        default="select", required=True, readonly=True)
    picking_id = fields.Many2one(
        "stock.picking", string="Receipt", readonly=True, ondelete="cascade",
        help="Set when launched from a receipt: only that receipt is returned.")
    unlinked_move_count = fields.Integer(
        readonly=True,
        help="Products on the receipt with no PO line; they cannot be credited "
             "through the PO and are not listed.")
    partner_id = fields.Many2one(related="purchase_id.partner_id")
    company_id = fields.Many2one(related="purchase_id.company_id")
    currency_id = fields.Many2one(related="purchase_id.currency_id")
    has_draft_bills = fields.Boolean(readonly=True)
    note = fields.Text("Return Reason")

    line_ids = fields.One2many("purchase.return.wizard.line", "wizard_id", string="All Lines")
    # Same lines, restricted by the persisted filter flag. The o2m domain is
    # applied server-side when the field is read, so the popup only renders
    # (and only sends back) the rows the user is looking at -> stays fast on
    # POs with thousands of lines.
    visible_line_ids = fields.One2many(
        "purchase.return.wizard.line", "wizard_id", string="Lines",
        domain=[("is_visible", "=", True)])

    # ---- filter bar -------------------------------------------------------
    filter_text = fields.Char("Product / SKU / Barcode")
    filter_qty_field = fields.Selection(
        [("qty_ordered", "Ordered"), ("qty_received", "Received"),
         ("qty_returnable", "Returnable"), ("qty_billed", "Billed")],
        string="Quantity Filter On")
    filter_qty_min = fields.Float("Min Qty", digits="Product Unit")
    filter_qty_max = fields.Float(
        "Max Qty", digits="Product Unit", help="0 = no upper bound")
    filter_scenario = fields.Selection(SCENARIOS, string="Status")
    filter_selected_only = fields.Boolean("Only Lines Marked for Return")

    line_count = fields.Integer(compute="_compute_counts")
    visible_count = fields.Integer(compute="_compute_counts", string="Shown")
    selected_count = fields.Integer(compute="_compute_counts", string="Marked for Return")

    # ---- review step ------------------------------------------------------
    selected_line_ids = fields.One2many(
        "purchase.return.wizard.line", "wizard_id", string="Lines to Return",
        domain=[("action", "=", "return")], readonly=True)
    review_routing = fields.Selection(
        [("return_only", "Return Transfer Only"),
         ("return_credit", "Return Transfer + Draft Credit Note")],
        compute="_compute_review", string="Documents to Create")
    review_return_qty = fields.Float(
        compute="_compute_review", string="Total Qty to Return",
        digits="Product Unit")
    review_credit_amount = fields.Monetary(
        compute="_compute_review", string="Credit Note Amount (untaxed)",
        currency_field="currency_id")

    @api.depends("line_ids.is_visible", "line_ids.action", "visible_line_ids.action")
    def _compute_counts(self):
        """`visible_line_ids` is the o2m the user edits in the popup, so the
        live "Marked for Return" counter reads the shown rows from it and the
        hidden rows (not editable while filtered out) from `line_ids`."""
        for wiz in self:
            hidden = wiz.line_ids.filtered(lambda l: not l.is_visible)
            wiz.line_count = len(wiz.line_ids)
            wiz.visible_count = len(wiz.line_ids) - len(hidden)
            wiz.selected_count = (
                len(wiz.visible_line_ids.filtered(lambda l: l.action == "return"))
                + len(hidden.filtered(lambda l: l.action == "return")))

    @api.depends("line_ids.action", "line_ids.return_qty", "line_ids.credit_qty",
                 "line_ids.credit_amount")
    def _compute_review(self):
        for wiz in self:
            selected = wiz.line_ids.filtered(lambda l: l.action == "return")
            credited = selected.filtered(
                lambda l: not float_is_zero(l.credit_qty, precision_rounding=l._prw_rounding()))
            wiz.review_routing = "return_credit" if credited else "return_only"
            wiz.review_return_qty = sum(selected.mapped("return_qty"))
            wiz.review_credit_amount = sum(selected.mapped("credit_amount"))

    # ---------------------------------------------------------------------
    # Creation / navigation
    # ---------------------------------------------------------------------
    @api.model
    def create_from_order(self, order, picking=None):
        """Build the wizard for a PO, optionally scoped to one of its receipts.

        PO scope:      all stockable/consumable PO lines; returns are
                       allocated across every receipt (newest first).
        Receipt scope: only the PO lines received in ``picking``; returns are
                       allocated against ``picking`` only, so the return
                       transfer always points at the receipt the user opened.
        """
        order.ensure_one()
        if order.state not in ("purchase", "done"):
            raise UserError(_("Only confirmed purchase orders can be returned."))
        unlinked_count = 0
        if picking:
            picking.ensure_one()
            scope_moves = picking._prw_get_po_receipt_moves().filtered(
                lambda m: m.purchase_line_id.order_id == order)
            po_lines = scope_moves.purchase_line_id.sorted(lambda l: (l.sequence, l.id))
            # products received in this picking without a PO line (added by hand)
            unlinked_count = len(picking.move_ids.filtered(
                lambda m: not m.purchase_line_id and m._prw_is_vendor_receipt()))
        else:
            po_lines = order.order_line
        po_lines = po_lines.filtered(
            # v18: "storable" is no longer a type; goods are type "consu"
            # (with is_storable for tracked stock), services stay excluded
            lambda l: not l.display_type and l.product_id.type == "consu")
        if not po_lines:
            if picking:
                raise UserError(_("Receipt %s has no purchase order products to return.",
                                  picking.name))
            raise UserError(_("This purchase order has no stockable or consumable lines."))

        metrics = po_lines._prw_get_return_metrics(picking=picking)
        if picking and not any(m["qty_returnable"] > 0 for m in metrics.values()):
            raise UserError(_(
                "Everything received in %s has already been returned or is on a "
                "pending return transfer.", picking.name))

        line_cmds = []
        for pol in po_lines:
            m = metrics[pol.id]
            line_cmds.append(Command.create({
                "sequence": pol.sequence,
                "purchase_line_id": pol.id,
                "name": pol.name,
                "product_uom_id": pol.product_uom_id.id,
                "qty_ordered": m["qty_ordered"],
                "qty_received": m["qty_received"],
                "qty_in_receipt": m["qty_in_receipt"],
                "qty_billed": m["qty_billed"],
                "qty_returnable": m["qty_returnable"],
                "billed_price_unit": m["price_unit"],
            }))
        return self.create({
            "purchase_id": order.id,
            "picking_id": picking.id if picking else False,
            "unlinked_move_count": unlinked_count,
            "has_draft_bills": any(m["draft_bill_qty"] > 0 for m in metrics.values()),
            "line_ids": line_cmds,
        })

    def _action_reopen(self):
        self.ensure_one()
        title = self.picking_id.name if self.picking_id else self.purchase_id.name
        return {
            "type": "ir.actions.act_window",
            "name": _("Return / Refund: %s", title),
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
            # wide dialog: the line table has up to 13 columns
            "context": {"dialog_size": "extra-large"},
        }

    # ---------------------------------------------------------------------
    # Filter & bulk buttons (form buttons save pending row edits first)
    # ---------------------------------------------------------------------
    def _prw_filter_domain(self):
        self.ensure_one()
        domain = [("wizard_id", "=", self.id)]
        text = (self.filter_text or "").strip()
        if text:
            domain += ["|", "|", "|",
                       ("product_id.name", "ilike", text),
                       ("product_id.default_code", "ilike", text),
                       ("product_id.barcode", "ilike", text),
                       ("name", "ilike", text)]
        if self.filter_qty_field:
            domain.append((self.filter_qty_field, ">=", self.filter_qty_min))
            if self.filter_qty_max:
                domain.append((self.filter_qty_field, "<=", self.filter_qty_max))
        if self.filter_scenario:
            domain.append(("scenario", "=", self.filter_scenario))
        if self.filter_selected_only:
            domain.append(("action", "=", "return"))
        return domain

    def action_apply_filter(self):
        self.ensure_one()
        visible = self.env["purchase.return.wizard.line"].search(self._prw_filter_domain())
        (self.line_ids - visible).write({"is_visible": False})
        visible.write({"is_visible": True})
        self.invalidate_recordset(["visible_line_ids"])
        return self._action_reopen()

    def action_reset_filter(self):
        self.ensure_one()
        self.write({
            "filter_text": False, "filter_qty_field": False, "filter_qty_min": 0.0,
            "filter_qty_max": 0.0, "filter_scenario": False, "filter_selected_only": False,
        })
        self.line_ids.write({"is_visible": True})
        self.invalidate_recordset(["visible_line_ids"])
        return self._action_reopen()

    def action_return_all_visible(self):
        self.ensure_one()
        for line in self.visible_line_ids:
            if float_compare(line.qty_returnable, 0.0,
                             precision_rounding=line._prw_rounding()) > 0:
                line.write({"action": "return", "return_qty": line.qty_returnable})
        return self._action_reopen()

    def action_keep_all_visible(self):
        self.ensure_one()
        self.visible_line_ids.write({"action": "keep", "return_qty": 0.0})
        return self._action_reopen()

    # ---------------------------------------------------------------------
    # Execution
    # ---------------------------------------------------------------------
    def _prw_get_lines_to_return(self):
        self.ensure_one()
        lines = self.line_ids.filtered(
            lambda l: l.action == "return" and float_compare(
                l.return_qty, 0.0, precision_rounding=l._prw_rounding()) > 0)
        if not lines:
            raise UserError(_("Mark at least one line as 'Return' with a quantity above zero."))
        return lines

    def action_review(self):
        """Step 1 -> 2: freeze the selection and show a summary."""
        self.ensure_one()
        self._prw_get_lines_to_return()
        self.state = "review"
        return self._action_reopen()

    def action_back(self):
        """Step 2 -> 1: go back and change the selection."""
        self.ensure_one()
        self.state = "select"
        return self._action_reopen()

    def action_confirm(self):
        self.ensure_one()
        if self.state != "review":
            raise UserError(_("Please review the return before confirming it."))
        lines = self._prw_get_lines_to_return()

        order = self.purchase_id
        self._prw_lock_order_lines(lines.purchase_line_id)
        # Snapshot values may be stale (another user returned/billed meanwhile):
        # re-read everything from the database under the lock.
        self.env.invalidate_all()
        picking = self.picking_id
        if picking:
            # the receipt may have been touched meanwhile (e.g. a manual return)
            picking._prw_check_returnable()
        metrics = lines.purchase_line_id._prw_get_return_metrics(picking=picking)

        plan, errors = [], []
        for line in lines:
            pol = line.purchase_line_id
            m = metrics[pol.id]
            rounding = line._prw_rounding()
            if float_compare(line.return_qty, m["qty_returnable"], precision_rounding=rounding) > 0:
                errors.append(_(
                    "%(product)s: requested %(req)s %(uom)s but only %(avail)s can be "
                    "returned now (ordered %(ord)s, received %(rec)s).",
                    product=pol.product_id.display_name, req=line.return_qty,
                    uom=pol.product_uom_id.name, avail=m["qty_returnable"],
                    ord=m["qty_ordered"], rec=m["qty_received"]))
                continue
            plan.append({
                "purchase_line": pol,
                "rounding": rounding,
                "return_qty": line.return_qty,
                "credit_qty": compute_credit_qty(
                    line.return_qty, m["qty_received"], m["qty_billed"], rounding),
                "scenario": classify_scenario(m["qty_received"], m["qty_billed"], rounding),
                "price_unit": m["price_unit"],
                "tax_ids": m["tax_ids"],
            })
        if errors:
            raise ValidationError(
                _("The order changed since this wizard was opened:\n") + "\n".join(errors))

        log_name = self.env["ir.sequence"].sudo().with_company(order.company_id).next_by_code(
            "purchase.return.log") or "/"
        pickings = self._prw_create_return_pickings(plan)
        refund = self._prw_create_credit_note(plan, log_name)
        log = self._prw_create_log(log_name, plan, pickings, refund)
        self._prw_post_order_message(log, pickings, refund)
        return self._prw_done_action(log, pickings, refund)

    def _prw_done_action(self, log, pickings, refund):
        """Close the wizard dialog, then show a notification.

        `act_window_close` is the standard way to end a dialog wizard: the
        dialog is removed and the Purchase Order form underneath reloads, so
        the "Returns" smart button and the chatter update at once. No new
        window is opened from inside the dialog, which is what left the
        old confirmation screen on the page.
        """
        def url(record):
            # v19 native route (legacy /web# links are only kept for retro-compatibility)
            return "/odoo/%s/%s" % (record._name, record.id)

        # display_notification fills each "%s" of `message` with one link, in order
        links = [{"label": log.name, "url": url(log)}]
        links += [{"label": p.name, "url": url(p)} for p in pickings]
        message = _("Return %s processed.") + " " + _("Validated transfer(s):") + " " \
            + ", ".join(["%s"] * len(pickings)) + "."
        if refund:
            label = refund.name if refund.name and refund.name != "/" else _("Draft credit note")
            links.append({"label": label, "url": url(refund)})
            message += " " + _("Credit note to review: %s.")
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Purchase return completed"),
                "message": message,
                "links": links,
                "type": "success",
                "sticky": False,
                "next": {"type": "ir.actions.act_window_close"},
            },
        }

    def _prw_lock_order_lines(self, po_lines):
        """Row-lock the PO lines so two users can't return the same goods."""
        try:
            with self.env.cr.savepoint():
                self.env.cr.execute(
                    "SELECT id FROM purchase_order_line WHERE id IN %s FOR UPDATE NOWAIT",
                    [tuple(po_lines.ids)])
        except pg_errors.LockNotAvailable:
            raise UserError(_(
                "Another user is processing a return or bill on this purchase order. "
                "Please try again in a moment.")) from None

    # ---- Stock -----------------------------------------------------------
    def _prw_create_return_pickings(self, plan):
        """Allocate each line's quantity over its receipts (LIFO), then create
        and validate one return transfer per original receipt."""
        by_receipt = defaultdict(list)
        for item in plan:
            pol = item["purchase_line"]
            product_uom = pol.product_id.uom_id
            prec = product_uom.rounding
            remaining = pol.product_uom_id._compute_quantity(
                item["return_qty"], product_uom, rounding_method="HALF-UP")
            for move in pol._prw_get_receipt_moves(picking=self.picking_id):
                if float_compare(remaining, 0.0, precision_rounding=prec) <= 0:
                    break
                take = min(move._prw_get_returnable_qty(), remaining)
                if float_compare(take, 0.0, precision_rounding=prec) <= 0:
                    continue
                by_receipt[move.picking_id].append((move, take))
                remaining -= take
            if float_compare(remaining, 0.0, precision_rounding=prec) > 0:
                raise UserError(_(
                    "%s: not enough received quantity left on the receipts to return.",
                    pol.product_id.display_name))

        returns = self.env["stock.picking"]
        for receipt, allocations in by_receipt.items():
            return_wiz = self.env["stock.return.picking"].with_context(
                active_model="stock.picking", active_id=receipt.id, active_ids=receipt.ids,
            ).create({
                "picking_id": receipt.id,
                "product_return_moves": [Command.create({
                    "product_id": move.product_id.id,
                    "move_id": move.id,
                    "quantity": qty,          # product UoM, as the std wizard expects
                    "to_refund": True,        # keeps PO qty_received in sync
                }) for move, qty in allocations],
            })
            # v18: _create_returns() -> (id, type) became _create_return() -> picking
            picking = return_wiz._create_return()
            self._prw_validate_return_picking(picking)
            returns |= picking
        return returns

    def _prw_validate_return_picking(self, picking):
        if picking.state != "assigned":
            picking.action_assign()
        short = picking.move_ids.filtered(lambda m: m.state != "assigned")
        if short:
            raise UserError(_(
                "Cannot validate the return: the following products are no longer "
                "available in the receipt location:\n%s",
                "\n".join(short.product_id.mapped("display_name"))))
        # v17: reserved quantity lives in move.quantity; marking the moves as
        # picked is what tells button_validate to process exactly that amount
        # (replaces v16's move_line.qty_done = reserved_uom_qty).
        picking.move_ids.picked = True
        res = picking.with_context(
            skip_backorder=True, picking_ids_not_to_backorder=picking.ids,
            skip_sms=True,
        ).button_validate()
        if isinstance(res, dict) or picking.state != "done":
            raise UserError(_(
                "Return %s needs manual confirmation (lots, expiry or quality checks). "
                "Nothing has been saved.", picking.name))

    # ---- Accounting ------------------------------------------------------
    def _prw_create_credit_note(self, plan, log_name):
        items = [i for i in plan
                 if not float_is_zero(i["credit_qty"], precision_rounding=i["rounding"])]
        if not items:
            return self.env["account.move"]
        order = self.purchase_id.with_company(self.purchase_id.company_id)
        vals = order.with_context(default_move_type="in_refund")._prepare_invoice()
        ref = _("%(log)s - Return of %(po)s", log=log_name, po=order.name)
        vals.update({
            "move_type": "in_refund",
            "ref": ref,
            "payment_reference": ref,
            "narration": self.note or vals.get("narration"),
            "invoice_line_ids": [],
        })
        for item in items:
            line_vals = item["purchase_line"]._prepare_account_move_line()
            line_vals.update({
                "quantity": item["credit_qty"],
                "price_unit": item["price_unit"],
                "tax_ids": [Command.set(item["tax_ids"].ids)],
            })
            vals["invoice_line_ids"].append(Command.create(line_vals))
        return self.env["account.move"].with_company(order.company_id).with_context(
            default_move_type="in_refund").create(vals)

    # ---- Traceability ----------------------------------------------------
    def _prw_create_log(self, log_name, plan, pickings, refund):
        currency = self.purchase_id.currency_id
        return self.env["purchase.return.log"].create({
            "name": log_name,
            "purchase_id": self.purchase_id.id,
            "origin_picking_id": self.picking_id.id or False,
            "routing": "return_credit" if refund else "return_only",
            "note": self.note,
            "return_picking_ids": [Command.set(pickings.ids)],
            "refund_id": refund.id or False,
            "line_ids": [Command.create({
                "purchase_line_id": item["purchase_line"].id,
                "product_id": item["purchase_line"].product_id.id,
                "product_uom_id": item["purchase_line"].product_uom_id.id,
                "scenario": item["scenario"],
                "qty_returned": item["return_qty"],
                "qty_credited": item["credit_qty"],
                "price_unit": item["price_unit"],
                "amount_credited": currency.round(item["credit_qty"] * item["price_unit"]),
            }) for item in plan],
        })

    def _prw_post_order_message(self, log, pickings, refund):
        links = [p._get_html_link() for p in pickings]
        if refund:
            links.append(refund._get_html_link())
        body = Markup("%s<br/>%s") % (
            _("Purchase return %s processed.", log.name), Markup(", ").join(links))
        self.purchase_id.message_post(body=body)
        if self.picking_id:
            self.picking_id.message_post(body=body)


class PurchaseReturnWizardLine(models.TransientModel):
    _name = "purchase.return.wizard.line"
    _description = "Purchase Return Wizard Line"
    _order = "sequence, id"

    wizard_id = fields.Many2one(
        "purchase.return.wizard", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer()
    is_visible = fields.Boolean(default=True, index=True)

    purchase_line_id = fields.Many2one(
        "purchase.order.line", required=True, readonly=True, ondelete="cascade")
    product_id = fields.Many2one(related="purchase_line_id.product_id", store=True)
    default_code = fields.Char(related="product_id.default_code", string="SKU")
    name = fields.Char("Description", readonly=True)
    product_uom_id = fields.Many2one("uom.uom", string="UoM", readonly=True)
    currency_id = fields.Many2one(related="wizard_id.currency_id")
    picking_id = fields.Many2one(related="wizard_id.picking_id")

    # snapshot at opening (re-validated against live data on confirm)
    qty_ordered = fields.Float("Ordered", digits="Product Unit", readonly=True)
    qty_received = fields.Float(
        "Received", digits="Product Unit", readonly=True,
        help="Net of returns already processed.")
    qty_in_receipt = fields.Float(
        "In This Receipt", digits="Product Unit", readonly=True,
        help="Quantity done on the receipt the wizard was opened from.")
    qty_billed = fields.Float(
        "Billed", digits="Product Unit", readonly=True,
        help="Posted bills minus posted and draft credit notes.")
    qty_returnable = fields.Float("Returnable", digits="Product Unit", readonly=True)
    billed_price_unit = fields.Float("Billed Unit Price", digits="Product Price", readonly=True)
    scenario = fields.Selection(
        SCENARIOS, string="Status", compute="_compute_scenario", store=True)

    # user input
    action = fields.Selection(
        [("keep", "Keep"), ("return", "Return")], default="keep", required=True)
    return_qty = fields.Float("Return Qty", digits="Product Unit", default=0.0)

    # preview
    credit_qty = fields.Float(
        "Credit Qty", digits="Product Unit", compute="_compute_credit")
    credit_amount = fields.Monetary(
        "Credit Amount", compute="_compute_credit", currency_field="currency_id")

    def _prw_rounding(self):
        self.ensure_one()
        return self.product_uom_id.rounding or 0.01

    @api.depends("qty_received", "qty_billed", "product_uom_id")
    def _compute_scenario(self):
        for line in self:
            line.scenario = classify_scenario(
                line.qty_received, line.qty_billed, line._prw_rounding())

    @api.depends("action", "return_qty", "qty_received", "qty_billed", "billed_price_unit")
    def _compute_credit(self):
        for line in self:
            qty = line.return_qty if line.action == "return" else 0.0
            line.credit_qty = compute_credit_qty(
                qty, line.qty_received, line.qty_billed, line._prw_rounding())
            line.credit_amount = line.credit_qty * line.billed_price_unit

    # ---- client-side guidance (onchange) ---------------------------------
    @api.onchange("action")
    def _onchange_action(self):
        if self.action == "return":
            if float_is_zero(self.return_qty, precision_rounding=self._prw_rounding()):
                self.return_qty = self.qty_returnable
        else:
            self.return_qty = 0.0

    @api.onchange("return_qty")
    def _onchange_return_qty(self):
        rounding = self._prw_rounding()
        if float_compare(self.return_qty, 0.0, precision_rounding=rounding) < 0:
            self.return_qty = 0.0
            return {"warning": {
                "title": _("Invalid quantity"),
                "message": _("The return quantity cannot be negative.")}}
        if float_compare(self.return_qty, self.qty_returnable, precision_rounding=rounding) > 0:
            limit = self.qty_returnable
            self.return_qty = limit
            return {"warning": {
                "title": _("Quantity too high"),
                "message": _(
                    "%(product)s: at most %(limit)s can be returned "
                    "(ordered %(ord)s, received %(rec)s). The quantity was capped.",
                    product=self.product_id.display_name, limit=limit,
                    ord=self.qty_ordered, rec=self.qty_received)}}
        is_zero = float_is_zero(self.return_qty, precision_rounding=rounding)
        if not is_zero and self.action == "keep":
            self.action = "return"
        elif is_zero and self.action == "return":
            self.action = "keep"

    # ---- server-side validation ------------------------------------------
    @api.constrains("action", "return_qty")
    def _check_return_qty(self):
        for line in self:
            rounding = line._prw_rounding()
            product = line.product_id.display_name
            qty = line.return_qty
            if float_compare(qty, 0.0, precision_rounding=rounding) < 0:
                raise ValidationError(_("%s: the return quantity cannot be negative.", product))
            is_zero = float_is_zero(qty, precision_rounding=rounding)
            if line.action == "return" and is_zero:
                raise ValidationError(_("%s: set a quantity to return.", product))
            if line.action == "keep" and not is_zero:
                raise ValidationError(_(
                    "%s: a quantity is set but the line is marked 'Keep'.", product))
            if float_compare(qty, line.qty_ordered, precision_rounding=rounding) > 0:
                raise ValidationError(_(
                    "%(product)s: return quantity %(qty)s exceeds the ordered quantity %(ord)s.",
                    product=product, qty=qty, ord=line.qty_ordered))
            if float_compare(qty, line.qty_received, precision_rounding=rounding) > 0:
                raise ValidationError(_(
                    "%(product)s: return quantity %(qty)s exceeds the received quantity %(rec)s.",
                    product=product, qty=qty, rec=line.qty_received))
            if float_compare(qty, line.qty_returnable, precision_rounding=rounding) > 0:
                if line.picking_id:
                    raise ValidationError(_(
                        "%(product)s: only %(avail)s can be returned from receipt "
                        "%(picking)s. Use the purchase order to return goods from "
                        "other receipts.",
                        product=product, avail=line.qty_returnable,
                        picking=line.picking_id.name))
                raise ValidationError(_(
                    "%(product)s: only %(avail)s can still be returned "
                    "(other returns are pending on the receipts).",
                    product=product, avail=line.qty_returnable))
