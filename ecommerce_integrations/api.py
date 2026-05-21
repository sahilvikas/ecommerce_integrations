import frappe
import requests
import base64

@frappe.whitelist()
def proxy_image(url):
    try:
        response = requests.get(url, timeout=10)
        b64 = base64.b64encode(response.content).decode("utf-8")
        return b64
    except Exception as e:
        frappe.log_error(str(e), "proxy_image error")
        return None