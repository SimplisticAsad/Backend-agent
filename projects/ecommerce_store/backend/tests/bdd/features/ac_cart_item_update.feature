# ac.cart_item.update  (requirements: requirement.shopping_cart; operations: operation.cart_item.update)
Feature: Update Cart Item succeeds

  Scenario: Update Cart Item succeeds
    Given the user is signed in as Customer
    And the record being worked on exists
    When Change the Cart Item details
    And Update Cart Item
    Then The changes to the Cart Item are saved
