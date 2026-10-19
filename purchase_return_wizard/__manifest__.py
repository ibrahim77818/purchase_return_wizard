# -*- coding: utf-8 -*-
{
    "name": "Purchase Return & Vendor Credit Wizard",
    "version": "16.0.1.2.0",
    "category": "Inventory/Purchase",
    "summary": "Line-by-line PO returns with automatic return transfers "
               "and draft vendor credit notes for billed quantities",
    "description": """
Purchase Return & Vendor Credit Wizard
======================================
* "Return / Refund" button on confirmed Purchase Orders and on done PO receipts
* Filterable, editable popup of all PO lines (name / SKU / barcode /
  ordered / received / returnable thresholds)
* Keep / Return disposition per line with client + server validation
* Automatic routing:
    - received & not billed  -> validated return transfer
    - received & billed      -> validated return transfer + draft in_refund
    - partially billed       -> transfer for the full qty, credit note
                                only for the over-billed portion
* Immutable traceability log (PRET/xxxx) linking PO, transfers, credit notes
""",
    "author": "Ibrahim Yahia",
    "website": "https://www.linkedin.com/in/ibrahim-yahia",
    "support": "ibrahim.yahiah@gmail.com",
    "images": ["static/description/banner.png"],
    "license": "LGPL-3",
    "depends": ["purchase_stock", "account"],
    "data": [
        # groups must exist before the ACL csv references them
        "security/purchase_return_security.xml",
        "security/ir.model.access.csv",
        "data/ir_sequence_data.xml",
        "wizard/purchase_return_wizard_views.xml",
        "views/purchase_return_log_views.xml",
        "views/purchase_order_views.xml",
        "views/stock_picking_views.xml",
    ],
    "installable": True,
    "application": False,
}
