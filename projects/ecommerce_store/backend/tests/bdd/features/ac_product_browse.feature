# ac.product.browse  (requirements: requirement.product_catalog; operations: operation.product.list, operation.product.read)
Feature: Browse Products succeeds

  Scenario: Browse Products succeeds
    Given the user is not signed in
    And the record being worked on exists
    When List Products
    And View Product
    Then The Product information is displayed
