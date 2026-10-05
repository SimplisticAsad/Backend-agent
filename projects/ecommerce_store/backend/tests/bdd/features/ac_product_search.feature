# ac.product.search  (requirements: requirement.product_search; operations: operation.product.search)
Feature: Search products succeeds

  Scenario: Search products succeeds
    Given the user is not signed in
    When Enter search words
    And Search products
    Then Matching products are listed
