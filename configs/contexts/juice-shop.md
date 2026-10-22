## App summary
OWASP Juice Shop: an online juice store built as an Angular single-page application with hash
routing (`/#/...`). Customers browse and search products, manage a basket, check out, track orders
and manage their account. An administrator can see extra pages.

## Key workflows
1. Browse the product list, open product details and reviews, use the search box.
2. Add a product to the basket -> open the basket -> start checkout -> choose or add an address ->
   choose a delivery method -> reach the payment page (do not complete a real payment).
3. Open order history and order tracking.
4. Account menu: privacy & security, change password page, data export page, wallet, deluxe membership.
5. Customer feedback form, complaint page, recycling request, about us, photo wall, score board.
6. As administrator: the administration page and accounting page.

## Areas to prioritise
Pages that only appear after signing in (account menu, basket, orders, administration).

## Areas to avoid
Logging out, deleting your account, changing the password, and the language selector (it reloads the app).

## Test data
Use a test name such as "Test User" and the address "1 Test Street, Testville, 12345".
