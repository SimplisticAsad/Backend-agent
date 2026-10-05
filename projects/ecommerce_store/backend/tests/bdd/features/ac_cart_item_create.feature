# ac.cart_item.create  (requirements: requirement.shopping_cart; operations: operation.cart_item.create)
Feature: Create Cart Item succeeds

  Scenario: Create Cart Item succeeds
    Given the user is signed in as Customer
    When Fill in the Cart Item form
    And Create Cart Item
    Then The Cart Item is created and appears in the Cart Item list
