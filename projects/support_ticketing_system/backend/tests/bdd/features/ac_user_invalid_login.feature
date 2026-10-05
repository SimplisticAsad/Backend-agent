# ac.user.invalid_login  (requirements: requirement.authentication; operations: operation.user.login)
Feature: Wrong credentials are rejected (negative case)

  Scenario: Wrong credentials are rejected (negative case)
    Given the user is not signed in
    When the user submits a wrong password
    Then Email or password is wrong (error INVALID_CREDENTIALS)
    And no data is changed
