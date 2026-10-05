# ac.order.browse  (requirements: requirement.checkout; operations: operation.order.list, operation.order.read)
Feature: Browse Orders succeeds

  Scenario: Browse Orders succeeds
    Given the user is signed in as Customer
    And the record being worked on exists
    When List Orders
    And View Order
    Then The Order information is displayed
