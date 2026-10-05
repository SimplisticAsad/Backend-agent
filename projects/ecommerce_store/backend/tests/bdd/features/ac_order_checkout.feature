# ac.order.checkout  (requirements: requirement.checkout; operations: operation.order.place)
Feature: Check out succeeds

  Scenario: Check out succeeds
    Given the user is signed in as Customer
    When Enter shipping address and payment method
    And Place order
    Then The order is placed and its details are shown
