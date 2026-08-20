---
name: order-status
description: Use when an authenticated customer asks for the status of a specific order.
---

# Order Status

## Do not trigger when

- No order ID is available.
- The user asks to modify, cancel, refund, or create an order.
- The request requires data outside the authorized tenant scope.

## Procedure

1. Confirm the runtime supplied identity and tenant context.
2. Call only `erp.get_order_status`.
3. Verify the returned external reference matches the requested order ID.
4. Include status, observed time, and source reference.
5. Report connector failures as failures, not successful answers.

## Output

Return a concise customer-safe status message with evidence metadata.
