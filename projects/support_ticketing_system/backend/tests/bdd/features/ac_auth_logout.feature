# ac.auth.logout  (requirements: requirement.authentication; operations: operation.user.logout)
Feature: Log out succeeds

  Scenario: Log out succeeds
    Given the user is signed in as Customer or Agent or Manager
    When Choose log out
    And Log out
    Then The session ends and the login screen is shown
