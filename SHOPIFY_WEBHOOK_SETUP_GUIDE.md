# Shopify Webhook Setup Guide - orders/edited (Both Stores)

## Overview

This guide covers setting up `orders/edited` webhook for **both Store 1 and Store 2**. The same webhook URL is used for both stores, and the system automatically routes webhooks to the correct store based on the shop domain.

## Automatic Webhook Registration (Recommended)

The easiest way is to let ERPNext automatically register webhooks:

### For Store 1:
1. Go to: **Shopify Setting** doctype in ERPNext
2. Enable **"Enable Shopify"**
3. Enter Store 1 credentials (Shop URL, Password, Shared Secret)
4. Save the document
5. ERPNext will automatically register `orders/edited` webhook with Shopify Store 1

### For Store 2:
1. In the same **Shopify Setting** document
2. Enable **"Enable Store 2"**
3. Enter Store 2 credentials (Shop URL, Password, Shared Secret)
4. Enter Store 2 Name (e.g., "ZipCovers")
5. Save the document
6. ERPNext will automatically register `orders/edited` webhook with Shopify Store 2

**Note**: Both stores use the same webhook URL, and the system automatically identifies which store the webhook came from based on the `X-Shopify-Shop-Domain` header.

## Manual Webhook Configuration (Alternative)

If you prefer to configure webhooks manually in Shopify Admin:

### Step 1: Configure Webhook in Shopify Admin (Store 1)

1. **Login to Shopify Admin**
   - Go to: https://admin.shopify.com
   - Navigate to your store

2. **Go to Webhooks Settings**
   - Click: **Settings** (bottom left)
   - Click: **Notifications**
   - Scroll down to **Webhooks** section
   - Click: **Create webhook** button

3. **Configure the Webhook**
   - **Event**: Select **"Order edit"** from the dropdown
     - Note: In Shopify UI it shows as "Order edit" but API event name is `orders/edited`
   - **Format**: Select **"JSON"**
   - **URL**: Paste this exact URL:
     ```
     https://dev.cozycornerpatios.com/api/method/ecommerce_integrations.shopify.connection.store_request_data
     ```
     - ⚠️ **IMPORTANT**: Make sure the URL is complete with `_request_data` at the end
   - **Webhook API version**: Select **"2024-01"** (or your API version, NOT "unstable")
   - Click: **"Save webhook"**

4. **Verify Webhook Created**
   - You should see the webhook listed under "Webhooks" section
   - Status should show as "Active" or similar

### Step 2: Configure Webhook in Shopify Admin (Store 2)

If you have a second Shopify store (Store 2):

1. **Login to Store 2 Shopify Admin**
   - Go to: https://admin.shopify.com
   - Switch to your Store 2 (if you have multiple stores)

2. **Go to Webhooks Settings**
   - Click: **Settings** (bottom left)
   - Click: **Notifications**
   - Scroll down to **Webhooks** section
   - Click: **Create webhook** button

3. **Configure the Webhook (Same URL as Store 1)**
   - **Event**: Select **"Order edit"** from the dropdown
   - **Format**: Select **"JSON"**
   - **URL**: Paste the **SAME URL** as Store 1:
     ```
     https://dev.cozycornerpatios.com/api/method/ecommerce_integrations.shopify.connection.store_request_data
     ```
     - ⚠️ **IMPORTANT**: Both stores use the same URL - the system routes based on shop domain
   - **Webhook API version**: Select **"2024-01"** (or your API version, NOT "unstable")
   - Click: **"Save webhook"**

4. **Verify Webhook Created**
   - You should see the webhook listed under "Webhooks" section
   - Status should show as "Active" or similar

### Step 3: Remove Old Webhooks (if any)

For **both Store 1 and Store 2**, if you have `orders/updated` webhook configured:
1. Find it in the webhooks list
2. Click on it
3. Click **"Delete webhook"** or **"Remove"**
4. Confirm deletion

### Step 4: Test the Webhooks

**Test Store 1:**
1. **Create a test order** in Store 1 Shopify (or use existing order)
2. **Edit the order** in Shopify Admin:
   - Add an item
   - OR change item quantity
   - OR change shipping address
   - OR add a note
3. **Check ERPNext**:
   - Go to: **Ecommerce Integration Log** doctype
   - Look for new log entry with status "Success"
   - Check the `response_data` field - it should contain detailed changes
   - The log should show it came from Store 1

**Test Store 2:**
1. **Create a test order** in Store 2 Shopify (or use existing order)
2. **Edit the order** in Store 2 Shopify Admin
3. **Check ERPNext**:
   - Go to: **Ecommerce Integration Log** doctype
   - Look for new log entry with status "Success"
   - The log should show it came from Store 2 (check debug logs if enabled)

### Step 5: Verify in ERPNext

1. **Check Shopify Settings**
   - Go to: **Shopify Setting** doctype
   - **Store 1**: Check "Webhooks" table - should show `orders/edited` webhook
   - **Store 2**: Check "Webhooks (Store 2)" table - should show `orders/edited` webhook

2. **Monitor Logs**
   - Check **Ecommerce Integration Log** for webhook events
   - Status should be "Success" for relevant changes
   - Logs with no relevant changes should be automatically deleted
   - Both Store 1 and Store 2 webhooks will create logs in the same doctype

## What Changes Are Tracked

The webhook now tracks **detailed changes**:

### Items
- ✅ **Items Added**: Which items were added to the order
- ✅ **Items Removed**: Which items were removed from the order
- ✅ **Items Modified**: Which items had quantity/price changes
- ✅ **Properties Changed**: Line item properties added/changed

### Addresses
- ✅ **Shipping Address**: Field-by-field changes (address_line1, city, pincode, country, state, etc.)
- ✅ **Billing Address**: Field-by-field changes

### Other
- ✅ **Order Notes**: Old note vs new note
- ✅ **Discounts**: Old discount vs new discount
- ✅ **Tags**: Current tags on the order

## What Does NOT Trigger Webhook

The `orders/edited` webhook will **NOT** fire on:
- ❌ Email sent to customer
- ❌ Tags added/removed (unless part of order edit)
- ❌ Timeline posts/comments
- ❌ Order status changes
- ❌ Payment status changes
- ❌ Fulfillment status changes
- ❌ App updates

## Accessing Change Data in ERPNext

The detailed changes are stored in the `Ecommerce Integration Log` for **both stores**:

1. Go to: **Ecommerce Integration Log** doctype
2. Find log with method: `ecommerce_integrations.shopify.order.order_updated`
3. Check `request_data` field to see which store it came from (check `shop_domain` or use debug logs)
4. Check `response_data` field (JSON field) - contains detailed changes:
   ```json
   {
     "items_added": [...],
     "items_removed": [...],
     "items_modified": [...],
     "address_changed": {...},
     "note_changed": true,
     "note_details": {...},
     "discount_changed": true,
     "discount_details": {...}
   }
   ```

## Troubleshooting

### Webhook Not Firing
- Check webhook URL is correct and accessible
- Verify webhook is "Active" in Shopify (for both stores)
- Check ERPNext Error Log for webhook delivery errors
- Verify Store 2 is enabled in Shopify Settings if using Store 2

### Too Many Logs
- `orders/edited` should only fire on actual edits
- If you still see irrelevant logs, check if `orders/updated` webhook still exists in either store
- Make sure you removed `orders/updated` from both Store 1 and Store 2

### Logs Not Showing Changes
- Check `is_valid_updated_order()` function - it filters irrelevant changes
- Only logs with relevant changes (address, items, discounts, notes) are kept
- Works the same for both Store 1 and Store 2

### Store 2 Webhooks Not Working
- Verify Store 2 is enabled in Shopify Settings
- Check Store 2 credentials are correct
- Verify Store 2 webhook is registered (check webhooks_2 table in Shopify Settings)
- Enable "Enable Debug Logging (Store 2)" to see detailed logs

## Support

For issues, check:
- ERPNext Error Log
- Ecommerce Integration Log
- Shopify webhook delivery logs (in Shopify Admin)
