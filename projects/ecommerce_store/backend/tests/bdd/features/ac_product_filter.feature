# ac.product.filter  (requirements: requirement.product_filter; operations: operation.product.filter)
Feature: Filter products succeeds

  Scenario: Filter products succeeds
    Given the user is not signed in
    When Choose category and price range
    And Filter products
    Then Only matching products are listed
