# ac.product.stock_available  (requirements: requirement.checkout; operations: operation.order.place)
Feature: Stock must cover the quantity (negative case)

  Scenario: Stock must cover the quantity (negative case)
    Given the user is signed in
    When the customer orders more units than are in stock
    Then One or more products are out of stock (error OUT_OF_STOCK)
    And no data is changed
