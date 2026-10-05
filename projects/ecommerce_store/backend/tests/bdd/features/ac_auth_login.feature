# ac.auth.login  (requirements: requirement.authentication; operations: operation.user.login)
Feature: Log in succeeds

  Scenario: Log in succeeds
    Given the user is not signed in
    When Enter email and password
    And Log in
    Then The user is authenticated and sees their home screen
