# ac.cart_item.browse  (requirements: requirement.shopping_cart; operations: operation.cart_item.list)
Feature: Browse Cart Items succeeds

  Scenario: Browse Cart Items succeeds
    Given the user is signed in as Customer
    When List Cart Items
    Then The Cart Item information is displayed
