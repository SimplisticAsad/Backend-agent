# ac.order.cart_not_empty  (requirements: requirement.checkout; operations: operation.order.place)
Feature: Cart must not be empty (negative case)

  Scenario: Cart must not be empty (negative case)
    Given the user is signed in
    When the customer checks out with an empty cart
    Then The cart is empty (error CART_EMPTY)
    And no data is changed
