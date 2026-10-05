# ac.auth.password_reset  (requirements: requirement.authentication; operations: operation.user.request_password_reset, operation.user.reset_password)
Feature: Reset password succeeds

  Scenario: Reset password succeeds
    Given the user is not signed in
    When Enter the account email
    And Request password reset
    And Enter the new password
    And Reset password
    Then The password is changed and the user can log in again
