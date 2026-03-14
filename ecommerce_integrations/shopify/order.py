"""
order.py - WITH COMPREHENSIVE STORE 2 LOGGING
This handles the background job for orders/create webhook.
"""

import json
import traceback
from typing import Literal, Optional

import frappe
from frappe import _
from frappe.utils import cint, cstr, flt, get_datetime, getdate, nowdate
from shopify.collection import PaginatedIterator
from shopify.resources import Order

from ecommerce_integrations.shopify.connection import temp_shopify_session
from ecommerce_integrations.shopify.constants import (
    CUSTOMER_ID_FIELD,
    EVENT_MAPPER,
    ORDER_ID_FIELD,
    ORDER_ITEM_DISCOUNT_FIELD,
    ORDER_NUMBER_FIELD,
    ORDER_STATUS_FIELD,
    SETTING_DOCTYPE,
)
from ecommerce_integrations.shopify.customer import ShopifyCustomer
from ecommerce_integrations.shopify.product import create_items_if_not_exist, get_item_code
from ecommerce_integrations.shopify.utils import create_shopify_log
from ecommerce_integrations.utils.price_list import get_dummy_price_list
from ecommerce_integrations.utils.taxation import get_dummy_tax_category

DEFAULT_TAX_FIELDS = {
    "sales_tax": "default_sales_tax_account",
    "shipping": "default_shipping_charges_account",
}


def _is_debug_logging_enabled() -> bool:
    """Check Shopify Setting for debug logging flag.

    This mirrors the check in connection.py but is kept local to avoid
    circular imports.
    """
    try:
        setting = frappe.get_cached_doc(SETTING_DOCTYPE)
        return bool(getattr(setting, "enable_debug_logging", 0))
    except Exception:
        return False


def log_store2(step, message, store_name=None):
    """Helper function to log only for Store 2 when debug logging is enabled."""
    if not _is_debug_logging_enabled():
        return

    if store_name and store_name != "Store 1":
        frappe.log_error(
            title=f"[STORE2 ORDER] Step {step}",
            message=f"Store: {store_name}\n\n{message}"
        )


def sync_sales_order(payload, request_id=None, store_name=None):
    """Sync Shopify order to ERPNext Sales Order.
    
    This is called as a BACKGROUND JOB by the RQ worker.
    """
    order = payload
    
    # =========================================================================
    # STEP BG-1: Background job started
    # =========================================================================
    log_store2("BG-1", f"""
========================================
BACKGROUND JOB STARTED: sync_sales_order
========================================
request_id: {request_id}
store_name: {store_name}
Order ID: {order.get('id')}
Order Number: {order.get('name')}
frappe.local exists: {hasattr(frappe, 'local')}
""", store_name)
    
    frappe.set_user("Administrator")
    frappe.flags.request_id = request_id
    
    # =========================================================================
    # STEP BG-2: Set store context (CRITICAL!)
    # =========================================================================
    log_store2("BG-2", f"""
Setting store context in background worker...
Before: frappe.local.shopify_store_name = {getattr(frappe.local, 'shopify_store_name', 'NOT SET')}
""", store_name)
    
    if store_name:
        frappe.local.shopify_store_name = store_name
        log_store2("BG-2-OK", f"""
Store context set!
After: frappe.local.shopify_store_name = {frappe.local.shopify_store_name}
""", store_name)
    else:
        log_store2("BG-2-WARN", "store_name is None! Will default to Store 1 credentials!", store_name)
    
    # =========================================================================
    # STEP BG-3: Check if order already exists
    # =========================================================================
    log_store2("BG-3", f"Checking if Sales Order already exists for Shopify Order ID: {order['id']}", store_name)
    
    existing_so = frappe.db.get_value("Sales Order", filters={ORDER_ID_FIELD: cstr(order["id"])})
    
    if existing_so:
        log_store2("BG-3-SKIP", f"""
Sales Order already exists!
Existing SO: {existing_so}
Shopify Order ID: {order['id']}
Skipping creation.
""", store_name)
        create_shopify_log(status="Invalid", message="Sales order already exists, not synced")
        return
    
    log_store2("BG-3-OK", "No existing Sales Order found, proceeding with creation.", store_name)
    
    # =========================================================================
    # STEP BG-4: Process customer
    # =========================================================================
    try:
        log_store2("BG-4", "Processing customer...", store_name)
        
        shopify_customer = order.get("customer") if order.get("customer") is not None else {}
        shopify_customer["billing_address"] = order.get("billing_address", "")
        shopify_customer["shipping_address"] = order.get("shipping_address", "")
        customer_id = shopify_customer.get("id")
        
        log_store2("BG-4a", f"""
Customer data:
customer_id: {customer_id}
email: {shopify_customer.get('email')}
has billing_address: {bool(order.get('billing_address'))}
has shipping_address: {bool(order.get('shipping_address'))}
""", store_name)
        
        if customer_id:
            customer = ShopifyCustomer(customer_id=customer_id)
            if not customer.is_synced():
                log_store2("BG-4b", f"Customer {customer_id} not synced, creating...", store_name)
                customer.sync_customer(customer=shopify_customer)
                log_store2("BG-4c", f"Customer {customer_id} created.", store_name)
            else:
                log_store2("BG-4b", f"Customer {customer_id} already exists, updating addresses...", store_name)
                customer.update_existing_addresses(shopify_customer)
                log_store2("BG-4c", f"Customer {customer_id} addresses updated.", store_name)
        else:
            log_store2("BG-4-WARN", "No customer_id in order, will use default customer.", store_name)
        
        log_store2("BG-4-OK", "Customer processing complete.", store_name)
        
    except Exception as e:
        log_store2("BG-4-EXCEPTION", f"""
Exception processing customer!
Error: {str(e)}
Type: {type(e).__name__}

Traceback:
{traceback.format_exc()}
""", store_name)
        create_shopify_log(status="Error", exception=e, rollback=True)
        return
    
    # =========================================================================
    # STEP BG-5: Sync items/products
    # =========================================================================
    try:
        log_store2("BG-5", f"""
Syncing items from order...
Line items count: {len(order.get('line_items', []))}
Line items: {[item.get('title') for item in order.get('line_items', [])]}
""", store_name)
        
        create_items_if_not_exist(order)
        
        log_store2("BG-5-OK", "Items synced successfully.", store_name)
        
    except Exception as e:
        log_store2("BG-5-EXCEPTION", f"""
Exception syncing items!
Error: {str(e)}
Type: {type(e).__name__}

Traceback:
{traceback.format_exc()}
""", store_name)
        create_shopify_log(status="Error", exception=e, rollback=True)
        return
    
    # =========================================================================
    # STEP BG-6: Create Sales Order
    # =========================================================================
    try:
        log_store2("BG-6", "Creating Sales Order...", store_name)
        
        setting = frappe.get_doc(SETTING_DOCTYPE)
        
        log_store2("BG-6a", f"""
Settings loaded:
Company: {setting.company}
Warehouse: {setting.warehouse}
Sales Order Series: {setting.sales_order_series}
Default Customer: {setting.default_customer}
""", store_name)
        
        create_order(order, setting, store_name=store_name)
        
        log_store2("BG-6-OK", f"""
========================================
SALES ORDER CREATED SUCCESSFULLY!
========================================
Shopify Order ID: {order.get('id')}
Shopify Order Number: {order.get('name')}
Store: {store_name}
""", store_name)
        
    except Exception as e:
        log_store2("BG-6-EXCEPTION", f"""
Exception creating Sales Order!
Error: {str(e)}
Type: {type(e).__name__}

Traceback:
{traceback.format_exc()}
""", store_name)
        create_shopify_log(status="Error", exception=e, rollback=True)
        return
    
    # =========================================================================
    # STEP BG-7: Success!
    # =========================================================================
    log_store2("BG-7", "Creating success log entry...", store_name)
    create_shopify_log(status="Success")
    log_store2("BG-7-OK", f"""
========================================
BACKGROUND JOB COMPLETED SUCCESSFULLY!
========================================
Shopify Order: {order.get('name')}
Store: {store_name}
""", store_name)


def create_order(order, setting, company=None, store_name=None):
    """Create order with related documents."""
    # local import to avoid circular dependencies
    from ecommerce_integrations.shopify.fulfillment import create_delivery_note
    from ecommerce_integrations.shopify.invoice import create_sales_invoice

    log_store2("CREATE-1", "Inside create_order()", store_name)
    
    so = create_sales_order(order, setting, company, store_name=store_name)
    
    if so:
        log_store2("CREATE-2", f"Sales Order created: {so.name}", store_name)
        
        if order.get("financial_status") == "paid":
            log_store2("CREATE-3", "Order is paid, creating Sales Invoice...", store_name)
            create_sales_invoice(order, setting, so)
            log_store2("CREATE-3-OK", "Sales Invoice created.", store_name)

        if order.get("fulfillments"):
            log_store2("CREATE-4", "Order has fulfillments, creating Delivery Note...", store_name)
            create_delivery_note(order, setting, so)
            log_store2("CREATE-4-OK", "Delivery Note created.", store_name)
    else:
        log_store2("CREATE-WARN", "create_sales_order returned None!", store_name)


def create_sales_order(shopify_order, setting, company=None, store_name=None):
    """Create the actual Sales Order document."""
    
    log_store2("SO-1", f"""
Creating Sales Order...
Shopify Order ID: {shopify_order.get('id')}
Shopify Order Number: {shopify_order.get('name')}
""", store_name)
    
    # Determine customer
    customer = setting.default_customer
    if shopify_order.get("customer", {}):
        if customer_id := shopify_order.get("customer", {}).get("id"):
            customer = frappe.db.get_value("Customer", {CUSTOMER_ID_FIELD: customer_id}, "name") or customer
    
    log_store2("SO-2", f"Customer determined: {customer}", store_name)
    
    # Check if SO already exists
    so = frappe.db.get_value("Sales Order", {ORDER_ID_FIELD: shopify_order.get("id")}, "name")

    if not so:
        log_store2("SO-3", "Getting order items...", store_name)
        
        items = get_order_items(
            shopify_order.get("line_items"),
            setting,
            getdate(shopify_order.get("created_at")),
            taxes_inclusive=shopify_order.get("taxes_included"),
            store_name=store_name,
        )
        
        log_store2("SO-3a", f"Items count: {len(items)}", store_name)

        if not items:
            log_store2("SO-3-FAIL", "No items returned! Cannot create Sales Order.", store_name)
            message = (
                "Following items exists in the shopify order but relevant records were"
                " not found in the shopify Product master"
            )
            create_shopify_log(status="Error", exception=message, rollback=True)
            return ""

        log_store2("SO-4", "Getting order taxes...", store_name)
        taxes = get_order_taxes(shopify_order, setting, items)
        log_store2("SO-4a", f"Taxes count: {len(taxes)}", store_name)
        
        log_store2("SO-5", "Creating Sales Order document...", store_name)
        
        so = frappe.get_doc(
            {
                "doctype": "Sales Order",
                "naming_series": setting.sales_order_series or "SO-Shopify-",
                ORDER_ID_FIELD: str(shopify_order.get("id")),
                ORDER_NUMBER_FIELD: shopify_order.get("name"),
                "customer": customer,
                "transaction_date": getdate(shopify_order.get("created_at")) or nowdate(),
                "delivery_date": getdate(shopify_order.get("created_at")) or nowdate(),
                "company": setting.company,
                "selling_price_list": get_dummy_price_list(),
                "ignore_pricing_rule": 1,
                "items": items,
                "taxes": taxes,
                "tax_category": get_dummy_tax_category(),
            }
        )

        if company:
            so.update({"company": company, "status": "Draft"})
        
        so.flags.ignore_mandatory = True
        so.flags.shopiy_order_json = json.dumps(shopify_order)
        
        log_store2("SO-6", "Saving Sales Order...", store_name)
        so.save(ignore_permissions=True)
        log_store2("SO-6a", f"Sales Order saved: {so.name}", store_name)
        
        log_store2("SO-7", "Submitting Sales Order...", store_name)
        so.submit()
        log_store2("SO-7a", f"Sales Order submitted: {so.name}", store_name)

        if shopify_order.get("note"):
            so.add_comment(text=f"Order Note: {shopify_order.get('note')}")
            log_store2("SO-8", "Added order note as comment.", store_name)

    else:
        log_store2("SO-EXISTS", f"Sales Order already exists: {so}", store_name)
        so = frappe.get_doc("Sales Order", so)

    return so


def get_order_items(order_items, setting, delivery_date, taxes_inclusive, store_name=None):
    """Get order items for Sales Order."""
    items = []
    all_product_exists = True
    product_not_exists = []

    log_store2("ITEMS-1", f"Processing {len(order_items)} line items...", store_name)

    for idx, shopify_item in enumerate(order_items):
        product_id = shopify_item.get("product_id")
        
        log_store2(f"ITEMS-2-{idx}", f"""
Processing item {idx + 1}:
  title: {shopify_item.get('title')}
  product_id: {product_id}
  variant_id: {shopify_item.get('variant_id')}
  sku: {shopify_item.get('sku')}
  quantity: {shopify_item.get('quantity')}
  price: {shopify_item.get('price')}
  product_exists: {shopify_item.get('product_exists')}
""", store_name)
        
        # Handle items without product_id (tips, samples, fees)
        if not product_id:
            item_code = get_item_code(shopify_item)
            log_store2(f"ITEMS-2-{idx}-NOID", f"No product_id, mapped to: {item_code}", store_name)
            if item_code:
                items.append(
                    {
                        "item_code": item_code,
                        "item_name": shopify_item.get("name") or shopify_item.get("title"),
                        "rate": _get_item_price(shopify_item, taxes_inclusive),
                        "delivery_date": delivery_date,
                        "qty": shopify_item.get("quantity"),
                        "stock_uom": "Nos",
                        "warehouse": setting.warehouse,
                        ORDER_ITEM_DISCOUNT_FIELD: (
                            _get_total_discount(shopify_item) / cint(shopify_item.get("quantity"))
                        ),
                    }
                )
            continue
        
        # Original logic for items with product_id
        if not shopify_item.get("product_exists"):
            all_product_exists = False
            product_not_exists.append(
                {"title": shopify_item.get("title"), ORDER_ID_FIELD: shopify_item.get("id")}
            )
            log_store2(f"ITEMS-2-{idx}-NOTEXIST", f"Product does not exist in Shopify!", store_name)
            continue

        if all_product_exists:
            item_code = get_item_code(shopify_item)
            log_store2(f"ITEMS-2-{idx}-CODE", f"Item code: {item_code}", store_name)
            
            if not item_code:
                log_store2(f"ITEMS-2-{idx}-NOCODE", f"Could not get item_code!", store_name)
                continue
                
            items.append(
                {
                    "item_code": item_code,
                    "item_name": shopify_item.get("name"),
                    "rate": _get_item_price(shopify_item, taxes_inclusive),
                    "delivery_date": delivery_date,
                    "qty": shopify_item.get("quantity"),
                    "stock_uom": shopify_item.get("uom") or "Nos",
                    "warehouse": setting.warehouse,
                    ORDER_ITEM_DISCOUNT_FIELD: (
                        _get_total_discount(shopify_item) / cint(shopify_item.get("quantity"))
                    ),
                }
            )
        else:
            items = []

    log_store2("ITEMS-3", f"Returning {len(items)} items", store_name)
    return items


# Keep the rest of the functions unchanged but add store_name parameter where needed
def _get_item_price(line_item, taxes_inclusive: bool) -> float:
    price = flt(line_item.get("price"))
    qty = cint(line_item.get("quantity"))
    total_discount = _get_total_discount(line_item)

    if not taxes_inclusive:
        return price - (total_discount / qty)

    total_taxes = 0.0
    for tax in line_item.get("tax_lines"):
        total_taxes += flt(tax.get("price"))

    return price - (total_taxes + total_discount) / qty


def _get_total_discount(line_item) -> float:
    discount_allocations = line_item.get("discount_allocations") or []
    return sum(flt(discount.get("amount")) for discount in discount_allocations)


def get_order_taxes(shopify_order, setting, items):
    taxes = []
    line_items = shopify_order.get("line_items")

    for line_item in line_items:
        item_code = get_item_code(line_item)
        for tax in line_item.get("tax_lines"):
            taxes.append(
                {
                    "charge_type": "Actual",
                    "account_head": get_tax_account_head(tax, charge_type="sales_tax"),
                    "description": (
                        get_tax_account_description(tax)
                        or f"{tax.get('title')} - {tax.get('rate') * 100.0:.2f}%"
                    ),
                    "tax_amount": tax.get("price"),
                    "included_in_print_rate": 0,
                    "cost_center": setting.cost_center,
                    "item_wise_tax_detail": {item_code: [flt(tax.get("rate")) * 100, flt(tax.get("price"))]},
                    "dont_recompute_tax": 1,
                }
            )

    update_taxes_with_shipping_lines(
        taxes,
        shopify_order.get("shipping_lines"),
        setting,
        items,
        taxes_inclusive=shopify_order.get("taxes_included"),
    )

    if cint(setting.consolidate_taxes):
        taxes = consolidate_order_taxes(taxes)

    for row in taxes:
        tax_detail = row.get("item_wise_tax_detail")
        if isinstance(tax_detail, dict):
            row["item_wise_tax_detail"] = json.dumps(tax_detail)

    return taxes


def consolidate_order_taxes(taxes):
    tax_account_wise_data = {}
    for tax in taxes:
        account_head = tax["account_head"]
        tax_account_wise_data.setdefault(
            account_head,
            {
                "charge_type": "Actual",
                "account_head": account_head,
                "description": tax.get("description"),
                "cost_center": tax.get("cost_center"),
                "included_in_print_rate": 0,
                "dont_recompute_tax": 1,
                "tax_amount": 0,
                "item_wise_tax_detail": {},
            },
        )
        tax_account_wise_data[account_head]["tax_amount"] += flt(tax.get("tax_amount"))
        if tax.get("item_wise_tax_detail"):
            tax_account_wise_data[account_head]["item_wise_tax_detail"].update(tax["item_wise_tax_detail"])

    return tax_account_wise_data.values()


def get_tax_account_head(tax, charge_type: Literal["shipping", "sales_tax"] | None = None):
    tax_title = str(tax.get("title"))

    tax_account = frappe.db.get_value(
        "Shopify Tax Account",
        {"parent": SETTING_DOCTYPE, "shopify_tax": tax_title},
        "tax_account",
    )

    if not tax_account and charge_type:
        tax_account = frappe.db.get_single_value(SETTING_DOCTYPE, DEFAULT_TAX_FIELDS[charge_type])

    if not tax_account:
        frappe.throw(_("Tax Account not specified for Shopify Tax {0}").format(tax.get("title")))

    return tax_account


def get_tax_account_description(tax):
    tax_title = tax.get("title")

    tax_description = frappe.db.get_value(
        "Shopify Tax Account",
        {"parent": SETTING_DOCTYPE, "shopify_tax": tax_title},
        "tax_description",
    )

    return tax_description


def update_taxes_with_shipping_lines(taxes, shipping_lines, setting, items, taxes_inclusive=False):
    shipping_as_item = cint(setting.add_shipping_as_item) and setting.shipping_item
    for shipping_charge in shipping_lines:
        if shipping_charge.get("price"):
            shipping_discounts = shipping_charge.get("discount_allocations") or []
            total_discount = sum(flt(discount.get("amount")) for discount in shipping_discounts)

            shipping_taxes = shipping_charge.get("tax_lines") or []
            total_tax = sum(flt(discount.get("price")) for discount in shipping_taxes)

            shipping_charge_amount = flt(shipping_charge["price"]) - flt(total_discount)
            if bool(taxes_inclusive):
                shipping_charge_amount -= total_tax

            if shipping_as_item:
                items.append(
                    {
                        "item_code": setting.shipping_item,
                        "rate": shipping_charge_amount,
                        "delivery_date": items[-1]["delivery_date"] if items else nowdate(),
                        "qty": 1,
                        "stock_uom": "Nos",
                        "warehouse": setting.warehouse,
                    }
                )
            else:
                taxes.append(
                    {
                        "charge_type": "Actual",
                        "account_head": get_tax_account_head(shipping_charge, charge_type="shipping"),
                        "description": get_tax_account_description(shipping_charge)
                        or shipping_charge["title"],
                        "tax_amount": shipping_charge_amount,
                        "cost_center": setting.cost_center,
                    }
                )

        for tax in shipping_charge.get("tax_lines"):
            taxes.append(
                {
                    "charge_type": "Actual",
                    "account_head": get_tax_account_head(tax, charge_type="sales_tax"),
                    "description": (
                        get_tax_account_description(tax)
                        or f"{tax.get('title')} - {tax.get('rate') * 100.0:.2f}%"
                    ),
                    "tax_amount": tax["price"],
                    "cost_center": setting.cost_center,
                    "item_wise_tax_detail": {
                        setting.shipping_item: [flt(tax.get("rate")) * 100, flt(tax.get("price"))]
                    }
                    if shipping_as_item
                    else {},
                    "dont_recompute_tax": 1,
                }
            )


def get_sales_order(order_id):
    """Get ERPNext sales order using shopify order id."""
    sales_order = frappe.db.get_value("Sales Order", filters={ORDER_ID_FIELD: order_id})
    if sales_order:
        return frappe.get_doc("Sales Order", sales_order)


def _addresses_differ(order, sales_order, store_name=None) -> bool:
    """Check if Shopify shipping/billing address differs from ERP addresses (category B)."""
    changed = False

    shipping = order.get("shipping_address") or {}
    billing = order.get("billing_address") or {}

    # Compare shipping address with Sales Order's shipping_address_name (if any)
    try:
        if shipping and getattr(sales_order, "shipping_address_name", None):
            so_shipping = frappe.get_doc("Address", sales_order.shipping_address_name)
            if any(
                [
                    (so_shipping.address_line1 or "").strip() != (shipping.get("address1") or "").strip(),
                    (so_shipping.address_line2 or "").strip() != (shipping.get("address2") or "").strip(),
                    (so_shipping.city or "").strip() != (shipping.get("city") or "").strip(),
                    (so_shipping.pincode or "").strip() != (shipping.get("zip") or "").strip(),
                    (so_shipping.country or "").strip() != (shipping.get("country") or "").strip(),
                    (so_shipping.state or "").strip() != (shipping.get("province") or "").strip(),
                ]
            ):
                changed = True
    except Exception:
        # If we cannot resolve/compare address safely, don't treat it as a guaranteed change
        pass

    # Compare billing address with Sales Order's customer_address (if any)
    try:
        if billing and getattr(sales_order, "customer_address", None):
            so_billing = frappe.get_doc("Address", sales_order.customer_address)
            if any(
                [
                    (so_billing.address_line1 or "").strip() != (billing.get("address1") or "").strip(),
                    (so_billing.address_line2 or "").strip() != (billing.get("address2") or "").strip(),
                    (so_billing.city or "").strip() != (billing.get("city") or "").strip(),
                    (so_billing.pincode or "").strip() != (billing.get("zip") or "").strip(),
                    (so_billing.country or "").strip() != (billing.get("country") or "").strip(),
                    (so_billing.state or "").strip() != (billing.get("province") or "").strip(),
                ]
            ):
                changed = True
    except Exception:
        pass

    if changed:
        log_store2("UPDATED-B", "Detected address/contact changes", store_name)

    return changed


def _line_items_differ(order, sales_order, setting, store_name=None) -> bool:
    """Check if Shopify line items differ from ERP items (category C)."""
    shopify_items = order.get("line_items") or []

    # Quick length check (number of non-shipping, non-fee items)
    so_items = [row for row in sales_order.items if row.item_code]
    if len(shopify_items) != len(so_items):
        log_store2("UPDATED-C", "Line item count differs", store_name)
        return True

    # Build index of Sales Order items by item_code for comparison
    so_index = {row.item_code: row for row in so_items}

    taxes_inclusive = order.get("taxes_included")
    for idx, shopify_item in enumerate(shopify_items):
        item_code = get_item_code(shopify_item)
        if not item_code:
            # If we cannot resolve item code, skip strict comparison for this item
            continue

        so_row = so_index.get(item_code)
        if not so_row:
            log_store2("UPDATED-C-MISS", f"No matching Sales Order row for item_code={item_code}", store_name)
            return True

        expected_qty = cint(shopify_item.get("quantity"))
        expected_rate = _get_item_price(shopify_item, taxes_inclusive)

        if so_row.qty != expected_qty or flt(so_row.rate) != flt(expected_rate):
            log_store2(
                "UPDATED-C-DIFF",
                f"Item mismatch for {item_code}: "
                f"SO qty={so_row.qty}, Shopify qty={expected_qty}; "
                f"SO rate={so_row.rate}, Shopify rate={expected_rate}",
                store_name,
            )
            return True

    return False


def _shipping_differ(order, sales_order, setting, store_name=None) -> bool:
    """Check if total shipping price differs (category D)."""
    shipping_lines = order.get("shipping_lines") or []
    if not shipping_lines:
        return False

    # Shopify shipping total from payload
    shopify_shipping_total = sum(flt(sl.get("price") or 0) for sl in shipping_lines)

    # ERP shipping via shipping item (if configured)
    shipping_total_erp = 0.0
    if getattr(setting, "shipping_item", None):
        for row in sales_order.items:
            if row.item_code == setting.shipping_item:
                shipping_total_erp += flt(row.base_net_amount or row.net_amount or row.amount)

    if shipping_total_erp and flt(shopify_shipping_total) != flt(shipping_total_erp):
        log_store2(
            "UPDATED-D",
            f"Shipping total differs: SO={shipping_total_erp}, Shopify={shopify_shipping_total}",
            store_name,
        )
        return True

    return False


def _discounts_differ(order, sales_order, store_name=None) -> bool:
    """Check if total discounts differ (category E)."""
    shopify_total_discounts = flt(order.get("total_discounts") or 0)

    # Approximate ERP discount as sum of per-unit discount field * qty
    erp_total_discounts = 0.0
    for row in sales_order.items:
        per_unit_disc = flt(getattr(row, ORDER_ITEM_DISCOUNT_FIELD, 0))
        erp_total_discounts += per_unit_disc * flt(row.qty or 0)

    if shopify_total_discounts and flt(shopify_total_discounts) != flt(erp_total_discounts):
        log_store2(
            "UPDATED-E",
            f"Discount total differs: SO={erp_total_discounts}, Shopify={shopify_total_discounts}",
            store_name,
        )
        return True

    return False


def _note_changed(order, sales_order, store_name=None) -> bool:
    """Detect meaningful changes in the Shopify order note.

    We compare the Shopify note against the latest ERPNext comment that starts
    with "Order Note:" (same convention used when creating Sales Orders from
    Shopify orders). This avoids treating every webhook for orders that simply
    *have* a note as a change.
    """
    shopify_note = (order.get("note") or "").strip()

    # Fetch latest relevant "Order Note:" comment on the Sales Order, if any
    erp_note = ""
    try:
        comments = frappe.get_all(
            "Comment",
            filters={
                "reference_doctype": "Sales Order",
                "reference_name": sales_order.name,
                "comment_type": "Comment",
            },
            fields=["content"],
            order_by="creation desc",
            limit=5,
        )
        for c in comments:
            content = (c.get("content") or "").strip()
            if "Order Note:" in content:
                erp_note = content.replace("Order Note:", "").strip()
                break
    except Exception:
        # If we can't load comments, fall back to simple presence check but
        # don't treat it as a change unless Shopify actually has a note.
        pass

    if shopify_note != erp_note:
        log_store2(
            "UPDATED-NOTE",
            f"Order note changed: ERP='{erp_note}' -> Shopify='{shopify_note}'",
            store_name,
        )
        # Only treat as interesting if there is actually some content on either side
        return bool(shopify_note or erp_note)

    return False


def _tags_or_properties_present(order, store_name=None) -> bool:
    """Treat tags or line item properties as interesting metadata changes.

    We don't have a reliable previous snapshot in ERP for comparison,
    so we consider the current presence of these fields as a signal
    that this order carries important metadata for the banner.
    """
    tags = (order.get("tags") or "").strip()
    has_tags = bool(tags)

    has_properties = False
    for li in order.get("line_items") or []:
        props = li.get("properties") or []
        # ignore empty placeholder properties
        if any((p.get("name") or "").strip() or (p.get("value") or "").strip() for p in props):
            has_properties = True
            break

    if has_tags or has_properties:
        log_store2(
            "UPDATED-META",
            f"Order has metadata - tags_present={has_tags}, properties_present={has_properties}",
            store_name,
        )
        return True

    return False


def _get_detailed_changes(order, sales_order, setting, store_name=None) -> dict:
    """Get detailed breakdown of what changed: added, removed, modified items, properties, addresses.
    
    Returns a dictionary with:
    - items_added: List of items that were added to the order
    - items_removed: List of items that were removed from the order
    - items_modified: List of items with quantity/price/property changes
    - address_changed: Dictionary with shipping/billing address field changes
    - note_changed: Boolean and note details
    - discount_changed: Boolean and discount details
    - tags_changed: Boolean and tag details
    - properties_changed: List of line items with property changes
    """
    changes = {
        "items_added": [],
        "items_removed": [],
        "items_modified": [],
        "properties_changed": [],
        "address_changed": {},
        "note_changed": False,
        "note_details": {},
        "discount_changed": False,
        "discount_details": {},
        "tags_changed": False,
        "tags_details": {},
    }
    
    shopify_items = order.get("line_items") or []
    so_items = [row for row in sales_order.items if row.item_code]
    
    # Build maps for comparison
    shopify_item_map = {}
    for item in shopify_items:
        item_code = get_item_code(item)
        if item_code:
            shopify_item_map[item_code] = item
    
    so_item_map = {row.item_code: row for row in so_items}
    
    taxes_inclusive = order.get("taxes_included")
    
    # Find added items (in Shopify but not in ERP)
    for item_code, shopify_item in shopify_item_map.items():
        if item_code not in so_item_map:
            changes["items_added"].append({
                "item_code": item_code,
                "item_name": shopify_item.get("name") or shopify_item.get("title"),
                "quantity": cint(shopify_item.get("quantity")),
                "price": _get_item_price(shopify_item, taxes_inclusive),
                "properties": shopify_item.get("properties", []),
                "sku": shopify_item.get("sku"),
            })
    
    # Find removed items (in ERP but not in Shopify)
    for item_code, so_row in so_item_map.items():
        if item_code not in shopify_item_map:
            changes["items_removed"].append({
                "item_code": item_code,
                "item_name": so_row.item_name,
                "quantity": so_row.qty,
                "price": so_row.rate,
            })
    
    # Find modified items (quantity or price changed)
    for item_code, shopify_item in shopify_item_map.items():
        if item_code in so_item_map:
            so_row = so_item_map[item_code]
            expected_qty = cint(shopify_item.get("quantity"))
            expected_rate = _get_item_price(shopify_item, taxes_inclusive)
            
            item_changes = {}
            if so_row.qty != expected_qty:
                item_changes["quantity"] = {
                    "old": so_row.qty,
                    "new": expected_qty
                }
            if flt(so_row.rate) != flt(expected_rate):
                item_changes["price"] = {
                    "old": flt(so_row.rate),
                    "new": flt(expected_rate)
                }
            
            # Check for property changes
            shopify_props = shopify_item.get("properties", [])
            if shopify_props:
                # Store properties if they exist (we can't easily compare old vs new without storing previous state)
                item_changes["properties"] = shopify_props
                changes["properties_changed"].append({
                    "item_code": item_code,
                    "item_name": shopify_item.get("name") or shopify_item.get("title"),
                    "properties": shopify_props,
                })
            
            if item_changes:
                changes["items_modified"].append({
                    "item_code": item_code,
                    "item_name": shopify_item.get("name") or shopify_item.get("title"),
                    "changes": item_changes,
                })
    
    # Address changes - detailed
    shipping = order.get("shipping_address") or {}
    billing = order.get("billing_address") or {}
    
    if _addresses_differ(order, sales_order, store_name):
        address_changes = {}
        
        # Shipping address changes
        if shipping and getattr(sales_order, "shipping_address_name", None):
            try:
                so_shipping = frappe.get_doc("Address", sales_order.shipping_address_name)
                shipping_changes = {}
                
                if (so_shipping.address_line1 or "").strip() != (shipping.get("address1") or "").strip():
                    shipping_changes["address_line1"] = {
                        "old": so_shipping.address_line1,
                        "new": shipping.get("address1")
                    }
                if (so_shipping.address_line2 or "").strip() != (shipping.get("address2") or "").strip():
                    shipping_changes["address_line2"] = {
                        "old": so_shipping.address_line2,
                        "new": shipping.get("address2")
                    }
                if (so_shipping.city or "").strip() != (shipping.get("city") or "").strip():
                    shipping_changes["city"] = {
                        "old": so_shipping.city,
                        "new": shipping.get("city")
                    }
                if (so_shipping.pincode or "").strip() != (shipping.get("zip") or "").strip():
                    shipping_changes["pincode"] = {
                        "old": so_shipping.pincode,
                        "new": shipping.get("zip")
                    }
                if (so_shipping.country or "").strip() != (shipping.get("country") or "").strip():
                    shipping_changes["country"] = {
                        "old": so_shipping.country,
                        "new": shipping.get("country")
                    }
                if (so_shipping.state or "").strip() != (shipping.get("province") or "").strip():
                    shipping_changes["state"] = {
                        "old": so_shipping.state,
                        "new": shipping.get("province")
                    }
                
                if shipping_changes:
                    address_changes["shipping"] = shipping_changes
            except Exception:
                pass
        
        # Billing address changes
        if billing and getattr(sales_order, "customer_address", None):
            try:
                so_billing = frappe.get_doc("Address", sales_order.customer_address)
                billing_changes = {}
                
                if (so_billing.address_line1 or "").strip() != (billing.get("address1") or "").strip():
                    billing_changes["address_line1"] = {
                        "old": so_billing.address_line1,
                        "new": billing.get("address1")
                    }
                if (so_billing.address_line2 or "").strip() != (billing.get("address2") or "").strip():
                    billing_changes["address_line2"] = {
                        "old": so_billing.address_line2,
                        "new": billing.get("address2")
                    }
                if (so_billing.city or "").strip() != (billing.get("city") or "").strip():
                    billing_changes["city"] = {
                        "old": so_billing.city,
                        "new": billing.get("city")
                    }
                if (so_billing.pincode or "").strip() != (billing.get("zip") or "").strip():
                    billing_changes["pincode"] = {
                        "old": so_billing.pincode,
                        "new": billing.get("zip")
                    }
                if (so_billing.country or "").strip() != (billing.get("country") or "").strip():
                    billing_changes["country"] = {
                        "old": so_billing.country,
                        "new": billing.get("country")
                    }
                if (so_billing.state or "").strip() != (billing.get("province") or "").strip():
                    billing_changes["state"] = {
                        "old": so_billing.state,
                        "new": billing.get("province")
                    }
                
                if billing_changes:
                    address_changes["billing"] = billing_changes
            except Exception:
                pass
        
        if address_changes:
            changes["address_changed"] = address_changes
    
    # Note changes
    if _note_changed(order, sales_order, store_name):
        changes["note_changed"] = True
        shopify_note = (order.get("note") or "").strip()
        
        # Get old note from ERP comments
        erp_note = ""
        try:
            comments = frappe.get_all(
                "Comment",
                filters={
                    "reference_doctype": "Sales Order",
                    "reference_name": sales_order.name,
                    "comment_type": "Comment",
                },
                fields=["content"],
                order_by="creation desc",
                limit=5,
            )
            for c in comments:
                content = (c.get("content") or "").strip()
                if "Order Note:" in content:
                    erp_note = content.replace("Order Note:", "").strip()
                    break
        except Exception:
            pass
        
        changes["note_details"] = {
            "old": erp_note,
            "new": shopify_note,
        }
    
    # Discount changes
    if _discounts_differ(order, sales_order, store_name):
        changes["discount_changed"] = True
        shopify_total_discounts = flt(order.get("total_discounts") or 0)
        
        # Calculate ERP discount
        erp_total_discounts = 0.0
        for row in sales_order.items:
            per_unit_disc = flt(getattr(row, ORDER_ITEM_DISCOUNT_FIELD, 0))
            erp_total_discounts += per_unit_disc * flt(row.qty or 0)
        
        changes["discount_details"] = {
            "old": erp_total_discounts,
            "new": shopify_total_discounts,
        }
    
    # Tags changes
    shopify_tags = (order.get("tags") or "").strip()
    if shopify_tags:
        changes["tags_changed"] = True
        changes["tags_details"] = {
            "tags": shopify_tags,
            "tag_list": [tag.strip() for tag in shopify_tags.split(",") if tag.strip()],
        }
    
    return changes


def is_valid_updated_order(order, sales_order, setting, store_name=None) -> bool:
    """Return True only if orders/edited has changes we care about.

    Categories:
    - B: Address/contact change
    - C: Line items (qty/price) change
    - E: Discounts change
    - Notes: note added/changed (based on "Order Note:" comment vs Shopify note)
    - Tags/Properties: order tags or line item properties present
    """
    address_changed = _addresses_differ(order, sales_order, store_name)
    line_items_changed = _line_items_differ(order, sales_order, setting, store_name)
    discounts_changed = _discounts_differ(order, sales_order, store_name)
    note_changed = _note_changed(order, sales_order, store_name)
    metadata_changed = _tags_or_properties_present(order, store_name)

    any_change = any(
        [
            address_changed,
            line_items_changed,
            discounts_changed,
            note_changed,
            metadata_changed,
        ]
    )

    log_store2(
        "UPDATED-SUMMARY",
        f"""
orders/edited change summary:
  address_changed: {address_changed}
  line_items_changed: {line_items_changed}
  discounts_changed: {discounts_changed}
  note_changed: {note_changed}
  metadata_changed (tags/properties): {metadata_changed}
  any_change: {any_change}
""",
        store_name,
    )

    return any_change


def order_updated(payload, request_id=None, store_name=None):
    """Handler for orders/edited webhook.

    This is intentionally conservative:
    - It only processes orders that already exist in ERP (no backfill/creation).
    - It uses is_valid_updated_order to decide if there are meaningful changes
      (categories B, C, D, E and notes). If not, it exits quietly.
    - It does NOT mutate ERP documents; it just logs interesting updates that
      your custom ERP logic can react to via Ecommerce Integration Log.
    
    Note: orders/edited only fires on actual order edits (items added/removed/modified, 
    address changes, etc.) and not on irrelevant changes like tags, timeline posts, etc.
    """
    frappe.set_user("Administrator")
    frappe.flags.request_id = request_id

    order = payload

    if store_name:
        frappe.local.shopify_store_name = store_name
        log_store2(
            "UPDATED-1",
            f"orders/edited received for store {store_name}, order_id={order.get('id')}",
            store_name,
        )

    try:
        # Only proceed if Sales Order already exists; avoid creating historical orders.
        sales_order = get_sales_order(order.get("id"))
        if not sales_order:
            log_store2(
                "UPDATED-SKIP-NO-SO",
                f"Skipping order update/edit; Sales Order not found for Shopify Order ID {order.get('id')}",
                store_name,
            )
            # Don't create a log for non-existent orders - delete the one created in process_request
            if request_id:
                try:
                    frappe.delete_doc("Ecommerce Integration Log", request_id, ignore_permissions=True)
                except Exception:
                    pass
            return

        setting = frappe.get_doc(SETTING_DOCTYPE)

        if not is_valid_updated_order(order, sales_order, setting, store_name):
            log_store2(
                "UPDATED-SKIP-NOCHANGE",
                "Order update/edit has no relevant changes (B/C/D/E/notes); deleting log and skipping.",
                store_name,
            )
            # Delete the original Ecommerce Integration Log created in process_request
            if request_id:
                try:
                    frappe.delete_doc("Ecommerce Integration Log", request_id, ignore_permissions=True)
                except Exception as delete_err:
                    log_store2(
                        "UPDATED-DELETE-ERROR",
                        f"Failed to delete Ecommerce Integration Log {request_id}: {delete_err}",
                        store_name,
                    )
            return

        # At this point we have an existing Sales Order and meaningful changes.
        # Get detailed breakdown of what changed
        detailed_changes = _get_detailed_changes(order, sales_order, setting, store_name)
        
        # Store detailed changes in the log for your ERP banner/notification system
        create_shopify_log(
            status="Success", 
            message="Order update/edit with relevant changes detected",
            response_data=detailed_changes  # Store detailed changes here for comparison
        )
        log_store2(
            "UPDATED-OK",
            f"Order update/edit logged with relevant changes for Sales Order {sales_order.name}",
            store_name,
        )
        log_store2(
            "UPDATED-DETAILS",
            f"Detailed changes: {json.dumps(detailed_changes, indent=2, default=str)}",
            store_name,
        )

    except Exception as e:
        log_store2("UPDATED-ERROR", f"Error: {str(e)}\n{traceback.format_exc()}", store_name)
        create_shopify_log(status="Error", exception=e, rollback=True)


def cancel_order(payload, request_id=None, store_name=None):
    """Called by order/cancelled event."""
    frappe.set_user("Administrator")
    frappe.flags.request_id = request_id
    
    if store_name:
        frappe.local.shopify_store_name = store_name
        log_store2("CANCEL-1", f"Cancelling order {payload.get('id')}", store_name)

    order = payload

    try:
        order_id = order["id"]
        order_status = order["financial_status"]

        sales_order = get_sales_order(order_id)

        if not sales_order:
            log_store2("CANCEL-FAIL", f"Sales Order not found for {order_id}", store_name)
            create_shopify_log(status="Invalid", message="Sales Order does not exist")
            return

        sales_invoice = frappe.db.get_value("Sales Invoice", filters={ORDER_ID_FIELD: order_id})
        delivery_notes = frappe.db.get_list("Delivery Note", filters={ORDER_ID_FIELD: order_id})

        if sales_invoice:
            frappe.db.set_value("Sales Invoice", sales_invoice, ORDER_STATUS_FIELD, order_status)

        for dn in delivery_notes:
            frappe.db.set_value("Delivery Note", dn.name, ORDER_STATUS_FIELD, order_status)

        if not sales_invoice and not delivery_notes and sales_order.docstatus == 1:
            sales_order.cancel()
            log_store2("CANCEL-OK", f"Sales Order {sales_order.name} cancelled", store_name)
        else:
            frappe.db.set_value("Sales Order", sales_order.name, ORDER_STATUS_FIELD, order_status)
            log_store2("CANCEL-STATUS", f"Sales Order {sales_order.name} status updated", store_name)

    except Exception as e:
        log_store2("CANCEL-ERROR", f"Error: {str(e)}\n{traceback.format_exc()}", store_name)
        create_shopify_log(status="Error", exception=e)
    else:
        create_shopify_log(status="Success")


@temp_shopify_session
def sync_old_orders():
    shopify_setting = frappe.get_cached_doc(SETTING_DOCTYPE)
    if not cint(shopify_setting.sync_old_orders):
        return

    orders = _fetch_old_orders(shopify_setting.old_orders_from, shopify_setting.old_orders_to)

    for order in orders:
        log = create_shopify_log(
            method=EVENT_MAPPER["orders/create"], request_data=json.dumps(order), make_new=True
        )
        sync_sales_order(order, request_id=log.name)

    shopify_setting = frappe.get_doc(SETTING_DOCTYPE)
    shopify_setting.sync_old_orders = 0
    shopify_setting.save()


def _fetch_old_orders(from_time, to_time):
    from_time = get_datetime(from_time).astimezone().isoformat()
    to_time = get_datetime(to_time).astimezone().isoformat()
    orders_iterator = PaginatedIterator(
        Order.find(created_at_min=from_time, created_at_max=to_time, limit=250)
    )

    for orders in orders_iterator:
        for order in orders:
            yield order.to_dict()