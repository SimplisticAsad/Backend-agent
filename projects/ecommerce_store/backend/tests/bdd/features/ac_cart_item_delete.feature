# ac.cart_item.delete  (requirements: requirement.shopping_cart; operations: operation.cart_item.delete)
Feature: Delete Cart Item succeeds

  Scenario: Delete Cart Item succeeds
    Given the user is signed in as Customer
    And the record being worked on exists
    When Confirm deleting the Cart Item
    And Delete Cart Item
    Then The Cart Item is removed from the Cart Item list
